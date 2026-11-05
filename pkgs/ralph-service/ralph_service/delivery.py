"""Durable publication attempts; uncertain remote writes are never replayed blindly."""

from contextlib import closing
from pathlib import Path
import sqlite3
import urllib.error


class DeliveryHeld(urllib.error.URLError):
    """A previous write may still arrive, so this PR must not receive another."""


def initialize(db):
    existed = db.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'deliveries'"
    ).fetchone()
    db.execute("""
        CREATE TABLE IF NOT EXISTS deliveries (
            job_id INTEGER NOT NULL,
            repo_id TEXT NOT NULL,
            number INTEGER NOT NULL,
            destination TEXT NOT NULL,
            operation TEXT,
            path TEXT,
            body TEXT,
            state TEXT NOT NULL,
            PRIMARY KEY(job_id, destination)
        )
    """)
    if not existed:
        # A legacy saved result does not prove that its remote write finished.
        db.execute("""
            INSERT INTO deliveries (job_id, repo_id, number, destination, state)
            SELECT id, repo_id, number, '*', 'uncertain' FROM jobs
            WHERE review_result IS NOT NULL AND status != 'complete'
        """)


def held(jobs):
    with jobs._connection() as db:
        return [
            dict(row)
            for row in db.execute(
                "SELECT job_id, repo_id, number, destination, operation, path FROM deliveries "
                "WHERE state = 'uncertain' AND repo_id = ? ORDER BY number, job_id, destination",
                (jobs.repo_id,),
            )
        ]


def list_held(path):
    """Read every repository's holds without opening the store for writing."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as db:
        db.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in db.execute(
                "SELECT job_id, repo_id, number, destination, operation, path FROM deliveries "
                "WHERE state = 'uncertain' ORDER BY repo_id, number, job_id, destination"
            )
        ]


def release(jobs, number):
    """Operator asserts all outstanding writes for this PR can no longer arrive."""
    with jobs._connection(write=True) as db:
        count = db.execute(
            "UPDATE deliveries SET state = 'ready' WHERE repo_id = ? AND number = ? AND state = 'uncertain'",
            (jobs.repo_id, number),
        ).rowcount
        db.execute(
            "DELETE FROM deliveries WHERE repo_id = ? AND number = ? AND destination = '*'",
            (jobs.repo_id, number),
        )
        # Never restart analysis. Only revive the latest saved delivery.
        latest = db.execute(
            "SELECT * FROM jobs WHERE repo_id = ? AND number = ? ORDER BY generation DESC LIMIT 1",
            (jobs.repo_id, number),
        ).fetchone()
        if (
            latest
            and latest["review_result"] is not None
            and latest["status"] in {"failed", "retry"}
        ):
            db.execute(
                "UPDATE jobs SET status = 'retry', attempts = 0, next_attempt = 0, error = NULL WHERE id = ?",
                (latest["id"],),
            )
        return count


class Session:
    def __init__(self, jobs, job, destination):
        jobs._check_job(job)
        self.jobs = jobs
        self.job = job
        self.destination = destination

    def _record(self):
        with self.jobs._connection() as db:
            self._check_destination(db)
            row = db.execute(
                "SELECT * FROM deliveries WHERE job_id = ? AND destination = ?",
                (self.job["id"], self.destination),
            ).fetchone()
            return dict(row) if row else None

    def _check_destination(self, db):
        provider = self.destination.partition(":")[0]
        for row in db.execute(
            "SELECT destination FROM deliveries WHERE job_id = ?", (self.job["id"],)
        ):
            old = row["destination"]
            if old.partition(":")[0] == provider and old != self.destination:
                raise DeliveryHeld(
                    "saved publication belongs to a different destination"
                )

    def finished(self):
        row = self._record()
        return row is not None and row["state"] == "confirmed"

    def body(self, render):
        row = self._record()
        return row["body"] if row else render()

    def reconcile(self, comment, lookup):
        with self.jobs._connection() as db:
            unresolved = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM deliveries WHERE repo_id = ? AND number = ? AND state = 'uncertain'",
                    (self.job["repo_id"], self.job["number"]),
                )
            ]
        for row in unresolved:
            if row["destination"] != self.destination:
                continue
            observed = comment if row["operation"] == "POST" else lookup(row["path"])
            matched = (
                observed is None
                if row["operation"] == "DELETE"
                else observed is not None and observed.get("body") == row["body"]
            )
            if matched:
                with self.jobs._connection(write=True) as db:
                    db.execute(
                        "UPDATE deliveries SET state = 'confirmed' WHERE job_id = ? AND destination = ? AND state = 'uncertain'",
                        (row["job_id"], row["destination"]),
                    )

    def check_hold(self):
        self._check_hold()

    def has_intent(self):
        return self._record() is not None

    def _check_hold(self, db=None):
        if db is None:
            with self.jobs._connection() as connection:
                return self._check_hold(connection)
        if db.execute(
            "SELECT 1 FROM deliveries WHERE repo_id = ? AND number = ? AND state = 'uncertain'",
            (self.job["repo_id"], self.job["number"]),
        ).fetchone():
            raise DeliveryHeld(
                "publication uncertain; reconcile or explicitly release this PR"
            )

    def execute(self, operation, path, body, comment, dispatch, is_current):
        if not is_current():
            return "stale"
        with self.jobs._connection(write=True) as db:
            self._check_hold(db)
            self._check_destination(db)
            db.execute(
                "INSERT OR IGNORE INTO deliveries (job_id, repo_id, number, destination, operation, path, body, state) VALUES (?, ?, ?, ?, ?, ?, ?, 'ready')",
                (
                    self.job["id"],
                    self.job["repo_id"],
                    self.job["number"],
                    self.destination,
                    operation,
                    path,
                    body,
                ),
            )
            row = db.execute(
                "SELECT * FROM deliveries WHERE job_id = ? AND destination = ?",
                (self.job["id"], self.destination),
            ).fetchone()
            if row["state"] == "confirmed":
                return "unchanged"
            operation, path, body = row["operation"], row["path"], row["body"]
            if operation == "POST" and comment is not None:
                if comment.get("body") != body:
                    raise DeliveryHeld(
                        "saved create conflicts with an existing bot comment"
                    )
                db.execute(
                    "UPDATE deliveries SET state = 'confirmed' WHERE job_id = ? AND destination = ?",
                    (self.job["id"], self.destination),
                )
                return "unchanged"
            if (
                operation == "PATCH"
                and comment is not None
                and path.endswith("/" + str(comment["id"]))
                and comment.get("body") == body
            ):
                db.execute(
                    "UPDATE deliveries SET state = 'confirmed' WHERE job_id = ? AND destination = ?",
                    (self.job["id"], self.destination),
                )
                return "unchanged"
            db.execute(
                "UPDATE deliveries SET state = 'uncertain' WHERE job_id = ? AND destination = ?",
                (self.job["id"], self.destination),
            )
        if not is_current():
            self._state("ready")
            return "stale"
        try:
            dispatch(operation, path, body)
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500 and exc.code != 408:
                self._state("ready")
                raise
            raise DeliveryHeld("remote write may have succeeded") from exc
        except Exception as exc:
            raise DeliveryHeld("remote write may have succeeded") from exc
        self._state("confirmed")
        return {"POST": "created", "PATCH": "updated", "DELETE": "deleted"}[operation]

    def _state(self, state):
        with self.jobs._connection(write=True) as db:
            db.execute(
                "UPDATE deliveries SET state = ? WHERE job_id = ? AND destination = ?",
                (state, self.job["id"], self.destination),
            )
