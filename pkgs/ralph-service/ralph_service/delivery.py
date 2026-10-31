"""Durable publication attempts; uncertain remote writes are never replayed blindly."""

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
            INSERT INTO deliveries (job_id, number, destination, state)
            SELECT id, number, '*', 'uncertain' FROM jobs
            WHERE review_result IS NOT NULL AND status != 'complete'
        """)


def held(jobs):
    with jobs._connection() as db:
        return [
            dict(row)
            for row in db.execute(
                "SELECT job_id, number, destination, operation, path FROM deliveries "
                "WHERE state = 'uncertain' ORDER BY number, job_id, destination"
            )
        ]


def release(jobs, number):
    """Operator asserts all outstanding writes for this PR can no longer arrive."""
    with jobs._connection(write=True) as db:
        count = db.execute(
            "UPDATE deliveries SET state = 'ready' WHERE number = ? AND state = 'uncertain'",
            (number,),
        ).rowcount
        db.execute(
            "DELETE FROM deliveries WHERE number = ? AND destination = '*'", (number,)
        )
        # Never restart analysis. Only revive the latest saved delivery.
        latest = db.execute(
            "SELECT * FROM jobs WHERE number = ? ORDER BY generation DESC LIMIT 1",
            (number,),
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
        self.jobs = jobs
        self.job = job
        self.destination = destination

    def _record(self):
        with self.jobs._connection() as db:
            row = db.execute(
                "SELECT * FROM deliveries WHERE job_id = ? AND destination = ?",
                (self.job["id"], self.destination),
            ).fetchone()
            return dict(row) if row else None

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
                    "SELECT * FROM deliveries WHERE number = ? AND state = 'uncertain'",
                    (self.job["number"],),
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
            "SELECT 1 FROM deliveries WHERE number = ? AND state = 'uncertain'",
            (self.job["number"],),
        ).fetchone():
            raise DeliveryHeld(
                "publication uncertain; reconcile or explicitly release this PR"
            )

    def execute(self, operation, path, body, comment, dispatch, is_current):
        if not is_current():
            return "stale"
        with self.jobs._connection(write=True) as db:
            self._check_hold(db)
            db.execute(
                "INSERT OR IGNORE INTO deliveries VALUES (?, ?, ?, ?, ?, ?, 'ready')",
                (
                    self.job["id"],
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
