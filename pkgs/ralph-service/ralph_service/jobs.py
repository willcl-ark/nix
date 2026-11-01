"""Durable work queue for pull request reviews."""

import copy
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager

from ralph import progress, stats_changes
from . import delivery


def _profile_name(identity):
    if identity and identity.get("name") == "current":
        profile = (identity.get("config") or {}).get("profile")
        if isinstance(profile, dict):
            return profile.get("name")
    return None


SCHEMA_VERSION = 1


def _repository_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", value):
        raise ValueError("repository ID must be a lowercase configuration key")
    return value


class JobStore:
    def __init__(
        self,
        path,
        *,
        repo_id="core",
        global_limit=None,
        repository_limits=None,
        max_attempts=3,
        retry_base_seconds=30,
        retry_max_seconds=300,
    ):
        if max_attempts < 1 or retry_base_seconds < 0 or retry_max_seconds < 0:
            raise ValueError("retry limits must be non-negative")
        self.path = path
        self.repo_id = _repository_id(repo_id)
        self._scoped = False
        self.global_limit = global_limit
        self.repository_limits = dict(repository_limits or {})
        if global_limit is not None and global_limit < 1:
            raise ValueError("global concurrency must be positive")
        for key, limit in self.repository_limits.items():
            _repository_id(key)
            if limit < 1:
                raise ValueError("repository concurrency must be positive")
        self.max_attempts = max_attempts
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        with self._connection(write=True) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            existing = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            if version not in {0, SCHEMA_VERSION}:
                raise RuntimeError("unsupported job database schema version")
            if existing and "repo_id" not in existing:
                raise RuntimeError(
                    "job database requires explicit repository migration"
                )
            db.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY,
                    repo_id TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    base_ref TEXT NOT NULL,
                    head TEXT NOT NULL,
                    action TEXT NOT NULL,
                    force INTEGER NOT NULL,
                    engine_identity TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0,
                    review_result TEXT,
                    token TEXT,
                    error TEXT,
                    UNIQUE(repo_id, number, generation)
                )
            """)
            delivery.initialize(db)
            columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            if "engine_identity" not in columns:
                raise RuntimeError(
                    "job database needs explicit engine migration; "
                    "run ralph-service --state-dir DIR --migrate-state"
                )
            db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_pending_per_pr
                    ON jobs(repo_id, number) WHERE status = 'pending'
            """)
            stats_changes.install(db, "jobs", "id")
            # Migrate unfinished reviews before loading their saved configuration.
            for row in db.execute(
                "SELECT id, engine_identity FROM jobs WHERE review_result IS NULL AND status IN ('pending', 'running', 'retry')"
            ).fetchall():
                if row["engine_identity"] is None:
                    continue
                identity = json.loads(row["engine_identity"])
                if identity is None:
                    continue
                if identity["name"] != "current" or identity["config"] is None:
                    continue
                from ralph import engine, profiles

                configured = identity["config"]
                profile = configured.get("profile")
                if profile is None and "models" in configured.get("prompts", {}):
                    models = configured["prompts"].pop("models")
                    profile = profiles.get_profile("default").with_models(models)
                    configured["profile"] = profile.to_dict()
                elif (
                    isinstance(profile, dict)
                    and "adversarial_deepseek" in profile["stages"]
                ):
                    del profile["stages"]["adversarial_deepseek"]
                else:
                    continue
                migrated = engine.load("current", configured).identity
                db.execute(
                    "UPDATE jobs SET engine_identity = ? WHERE id = ?",
                    (json.dumps(migrated, sort_keys=True), row["id"]),
                )
            db.execute(
                "CREATE TABLE IF NOT EXISTS review_requests (repo_id TEXT NOT NULL, id TEXT NOT NULL, PRIMARY KEY(repo_id, id))"
            )
            db.execute("""
                CREATE TABLE IF NOT EXISTS reconciliation (
                    repo_id TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    epoch INTEGER NOT NULL,
                    base_override TEXT,
                    action TEXT NOT NULL,
                    force INTEGER NOT NULL,
                    engine_identity TEXT NOT NULL,
                    token TEXT,
                    next_attempt REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(repo_id, number)
                )
            """)
            db.execute("DROP INDEX IF EXISTS jobs_one_running")
            db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_running_per_pr
                    ON jobs(repo_id, number) WHERE status = 'running'
            """)

            db.execute(
                "CREATE TABLE IF NOT EXISTS scheduler (id INTEGER PRIMARY KEY CHECK(id = 1), last_repo TEXT)"
            )
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def for_repository(self, repo_id):
        """Return a PR-scoped view without opening or migrating the shared store."""
        view = copy.copy(self)
        view.repo_id = _repository_id(repo_id)
        view._scoped = True
        return view

    def _check_job(self, job):
        if self._scoped and job["repo_id"] != self.repo_id:
            raise ValueError("job belongs to another repository")

    @contextmanager
    def _connection(self, *, write=False):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _job(row):
        if row is None:
            return None
        job = dict(row)
        job["force"] = bool(job["force"])
        job["review_result"] = (
            json.loads(job["review_result"])
            if job["review_result"] is not None
            else None
        )
        job["engine_identity"] = (
            json.loads(job["engine_identity"])
            if job["engine_identity"] is not None
            else None
        )
        return job

    def enqueue(
        self,
        number,
        base_ref,
        head,
        action,
        force=False,
        *,
        engine_identity,
        request_id=None,
    ):
        """Enqueue already resolved input, coalescing equivalent desired work."""
        with self._connection(write=True) as db:
            return self._enqueue(
                db, number, base_ref, head, action, force, engine_identity, request_id
            )

    def _enqueue(
        self,
        db,
        number,
        base_ref,
        head,
        action,
        force,
        engine_identity,
        request_id=None,
    ):
        if request_id is not None:
            if not db.execute(
                "INSERT OR IGNORE INTO review_requests VALUES (?, ?)",
                (self.repo_id, request_id),
            ).rowcount:
                return False
        identity = json.dumps(engine_identity, sort_keys=True)
        pending = db.execute(
            "SELECT * FROM jobs WHERE repo_id = ? AND number = ? AND status = 'pending'",
            (self.repo_id, number),
        ).fetchone()
        latest = db.execute(
            "SELECT * FROM jobs WHERE repo_id = ? AND number = ? ORDER BY generation DESC LIMIT 1",
            (self.repo_id, number),
        ).fetchone()
        # An ordinary update must retain an explicitly requested high review.
        active = pending or latest
        if active and active["status"] in {"pending", "running", "retry"}:
            previous = (
                (json.loads(active["engine_identity"]) or {})
                if active["engine_identity"]
                else {}
            )
            if (
                not force
                and _profile_name(previous) == "high"
                and _profile_name(engine_identity) == "default"
            ):
                identity = active["engine_identity"]
                if pending or active["head"] != head or active["base_ref"] != base_ref:
                    force = force or bool(active["force"])
        if pending:
            if (
                pending["head"] == head
                and pending["base_ref"] == base_ref
                and pending["engine_identity"] == identity
            ):
                if force and not pending["force"]:
                    db.execute(
                        "UPDATE jobs SET force = 1 WHERE id = ?", (pending["id"],)
                    )
                    return True
                return False
            generation = latest["generation"] + 1
            db.execute(
                """
                UPDATE jobs SET generation = ?, base_ref = ?, head = ?,
                    action = ?, force = ?, engine_identity = ?,
                    attempts = 0, error = NULL
                WHERE id = ?
            """,
                (
                    generation,
                    base_ref,
                    head,
                    action,
                    int(force),
                    identity,
                    pending["id"],
                ),
            )
            return True

        if (
            latest
            and latest["head"] == head
            and latest["base_ref"] == base_ref
            and latest["engine_identity"] == identity
            and not force
        ):
            return False
        if latest and latest["status"] == "retry":
            db.execute(
                """
                UPDATE jobs SET status = 'superseded', next_attempt = 0
                WHERE id = ?
            """,
                (latest["id"],),
            )
        generation = latest["generation"] + 1 if latest else 1
        db.execute(
            """
            INSERT INTO jobs (repo_id, number, generation, base_ref, head, action,
                              force, engine_identity, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        """,
            (
                self.repo_id,
                number,
                generation,
                base_ref,
                head,
                action,
                int(force),
                identity,
            ),
        )
        return True

    def reconcile_hint(
        self, number, base_ref, action, force=False, *, engine_identity, request_id=None
    ):
        """Durably request an authoritative read without invalidating running work."""
        with self._connection(write=True) as db:
            if (
                request_id is not None
                and not db.execute(
                    "INSERT OR IGNORE INTO review_requests VALUES (?, ?)",
                    (self.repo_id, request_id),
                ).rowcount
            ):
                return False
            previous = db.execute(
                "SELECT * FROM reconciliation WHERE repo_id = ? AND number = ?",
                (self.repo_id, number),
            ).fetchone()
            override = base_ref if base_ref.startswith("sha:") else None
            if previous:
                old_identity = json.loads(previous["engine_identity"])
                if (
                    not force
                    and _profile_name(old_identity) == "high"
                    and _profile_name(engine_identity) == "default"
                ):
                    engine_identity = old_identity
                if not force:
                    override = previous["base_override"]
                force = force or bool(previous["force"])
            identity = json.dumps(engine_identity, sort_keys=True)
            db.execute(
                """
                INSERT INTO reconciliation (repo_id, number, epoch, base_override, action, force, engine_identity, next_attempt)
                VALUES (?, ?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(repo_id, number) DO UPDATE SET epoch = epoch + 1,
                    base_override = excluded.base_override, action = excluded.action,
                    force = excluded.force, engine_identity = excluded.engine_identity,
                    next_attempt = excluded.next_attempt
            """,
                (
                    self.repo_id,
                    number,
                    override,
                    action,
                    int(force),
                    identity,
                    time.time(),
                ),
            )
            return True

    def claim_reconciliation(self):
        with self._connection(write=True) as db:
            row = db.execute(
                "SELECT * FROM reconciliation WHERE repo_id = ? AND token IS NULL AND next_attempt <= ? ORDER BY next_attempt, number LIMIT 1",
                (self.repo_id, time.time()),
            ).fetchone()
            if row is None:
                return None
            hint = dict(row)
            hint["token"] = uuid.uuid4().hex
            hint["engine_identity"] = json.loads(hint["engine_identity"])
            db.execute(
                "UPDATE reconciliation SET token = ? WHERE repo_id = ? AND number = ?",
                (hint["token"], self.repo_id, hint["number"]),
            )
            return hint

    def finish_reconciliation(self, hint, base_ref=None, head=None):
        if hint["repo_id"] != self.repo_id:
            raise ValueError("reconciliation belongs to another repository")
        with self._connection(write=True) as db:
            row = db.execute(
                "SELECT * FROM reconciliation WHERE repo_id = ? AND number = ? AND token = ?",
                (self.repo_id, hint["number"], hint["token"]),
            ).fetchone()
            if row is None:
                return False
            if row["epoch"] != hint["epoch"]:
                db.execute(
                    "UPDATE reconciliation SET token = NULL WHERE repo_id = ? AND number = ?",
                    (self.repo_id, hint["number"]),
                )
                return False
            if head is not None:
                self._enqueue(
                    db,
                    hint["number"],
                    hint["base_override"] or base_ref,
                    head,
                    hint["action"],
                    bool(hint["force"]),
                    hint["engine_identity"],
                )
            db.execute(
                "DELETE FROM reconciliation WHERE repo_id = ? AND number = ?",
                (self.repo_id, hint["number"]),
            )
            return True

    def retry_reconciliation(self, hint):
        if hint["repo_id"] != self.repo_id:
            raise ValueError("reconciliation belongs to another repository")
        with self._connection(write=True) as db:
            db.execute(
                """
                UPDATE reconciliation SET token = NULL,
                    next_attempt = CASE WHEN epoch = ? THEN ? ELSE next_attempt END
                WHERE repo_id = ? AND number = ? AND token = ?
            """,
                (
                    hint["epoch"],
                    time.time() + self.retry_base_seconds,
                    self.repo_id,
                    hint["number"],
                    hint["token"],
                ),
            )

    def recover(self):
        """Release claims left by a previous process. Call once at startup."""
        with self._connection(write=True) as db:
            db.execute(
                "UPDATE reconciliation SET token = NULL WHERE ? OR repo_id = ?",
                (not self._scoped, self.repo_id),
            )
            running = db.execute(
                "SELECT * FROM jobs WHERE status = 'running' AND (? OR repo_id = ?)",
                (not self._scoped, self.repo_id),
            ).fetchall()
            for job in running:
                newer = db.execute(
                    """
                    SELECT 1 FROM jobs WHERE repo_id = ? AND number = ? AND generation > ?
                        AND status = 'pending'
                """,
                    (job["repo_id"], job["number"], job["generation"]),
                ).fetchone()
                status = (
                    "superseded"
                    if newer
                    else "retry"
                    if job["review_result"] is not None
                    else "pending"
                )
                db.execute(
                    """
                    UPDATE jobs SET status = ?, token = NULL, next_attempt = 0
                    WHERE id = ?
                """,
                    (status, job["id"]),
                )
            return len(running)

    def claim(self):
        """Admit runnable repositories fairly under transactional concurrency caps."""
        with self._connection(write=True) as db:
            running = dict(
                db.execute(
                    "SELECT repo_id, count(*) FROM jobs WHERE status = 'running' GROUP BY repo_id"
                ).fetchall()
            )
            if (
                self.global_limit is not None
                and sum(running.values()) >= self.global_limit
            ):
                return None
            eligible = (
                [self.repo_id]
                if self._scoped or not self.repository_limits
                else sorted(self.repository_limits)
            )
            last = db.execute("SELECT last_repo FROM scheduler WHERE id = 1").fetchone()
            if last and last[0] in eligible:
                offset = eligible.index(last[0]) + 1
                eligible = eligible[offset:] + eligible[:offset]
            row = None
            for repo_id in eligible:
                limit = self.repository_limits.get(repo_id)
                if limit is not None and running.get(repo_id, 0) >= limit:
                    continue
                row = db.execute(
                    """
                    SELECT * FROM jobs AS candidate
                    WHERE candidate.repo_id = ?
                        AND (candidate.status = 'pending'
                            OR (candidate.status = 'retry' AND candidate.next_attempt <= ?))
                        AND NOT EXISTS (
                            SELECT 1 FROM jobs AS running
                            WHERE running.repo_id = candidate.repo_id
                                AND running.number = candidate.number
                                AND running.status = 'running'
                        )
                    ORDER BY candidate.id LIMIT 1
                    """,
                    (repo_id, time.time()),
                ).fetchone()
                if row is not None:
                    break
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute(
                "UPDATE jobs SET status = 'running', token = ? WHERE id = ?",
                (token, row["id"]),
            )
            db.execute(
                "INSERT INTO scheduler VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET last_repo = excluded.last_repo",
                (row["repo_id"],),
            )
            return self._job(
                db.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
            )

    def is_current(self, job):
        self._check_job(job)
        with self._connection() as db:
            return bool(
                db.execute(
                    """
                SELECT 1 FROM jobs AS current
                WHERE current.id = ? AND current.token = ?
                    AND current.status = 'running'
                    AND NOT EXISTS (
                        SELECT 1 FROM jobs AS newer
                        WHERE newer.repo_id = current.repo_id
                            AND newer.number = current.number
                            AND newer.generation > current.generation
                            AND newer.status = 'pending'
                    )
            """,
                    (job["id"], job["token"]),
                ).fetchone()
            )

    def save_result(self, job, result):
        """Commit the review payload before attempting to publish it."""
        self._check_job(job)
        payload = json.dumps(result)
        with self._connection(write=True) as db:
            cursor = db.execute(
                """
                UPDATE jobs SET review_result = ?
                WHERE id = ? AND token = ? AND status = 'running'
            """,
                (payload, job["id"], job["token"]),
            )
            return cursor.rowcount == 1

    def bind_engine(self, job, identity):
        """Bind an unfinished claimed review to the engine that will run it."""
        self._check_job(job)
        encoded = json.dumps(identity, sort_keys=True)
        with self._connection(write=True) as db:
            cursor = db.execute(
                """
                UPDATE jobs SET engine_identity = ?
                WHERE id = ? AND token = ? AND status = 'running'
                    AND review_result IS NULL
            """,
                (encoded, job["id"], job["token"]),
            )
            if cursor.rowcount != 1:
                return False
            job["engine_identity"] = identity
            return True

    def complete(self, job):
        return self._finish(job, "complete")

    def latest_completed_result(self, number):
        """Return the latest generation's result only after publication."""
        with self._connection() as db:
            row = db.execute(
                """
                SELECT status, review_result FROM jobs WHERE repo_id = ? AND number = ?
                ORDER BY generation DESC LIMIT 1
            """,
                (self.repo_id, number),
            ).fetchone()
            if (
                row is None
                or row["status"] != "complete"
                or row["review_result"] is None
            ):
                return None
            return json.loads(row["review_result"])

    def supersede(self, job):
        return self._finish(job, "superseded")

    def fail(self, job, error):
        return self._finish(job, "failed", error)

    def _finish(self, job, status, error=None):
        self._check_job(job)
        with self._connection(write=True) as db:
            cursor = db.execute(
                """
                UPDATE jobs SET status = ?, token = NULL, error = ?,
                    next_attempt = 0
                WHERE id = ? AND token = ? AND status = 'running'
            """,
                (
                    status,
                    str(error) if error is not None else None,
                    job["id"],
                    job["token"],
                ),
            )
            return cursor.rowcount == 1

    def retry(self, job, error):
        """Schedule a retry, or record a terminal failure at the attempt cap."""
        self._check_job(job)
        with self._connection(write=True) as db:
            row = db.execute(
                """
                SELECT * FROM jobs WHERE id = ? AND token = ?
                    AND status = 'running'
            """,
                (job["id"], job["token"]),
            ).fetchone()
            if row is None:
                return None
            newer = db.execute(
                """
                SELECT 1 FROM jobs WHERE repo_id = ? AND number = ? AND generation > ?
                    AND status = 'pending'
            """,
                (row["repo_id"], row["number"], row["generation"]),
            ).fetchone()
            attempts = row["attempts"] + 1
            if newer:
                status, next_attempt = "superseded", 0
            elif attempts >= self.max_attempts:
                status, next_attempt = "failed", 0
            else:
                status = "retry"
                delay = min(
                    self.retry_base_seconds * 2 ** (attempts - 1),
                    self.retry_max_seconds,
                )
                next_attempt = time.time() + delay
            db.execute(
                """
                UPDATE jobs SET status = ?, attempts = ?, next_attempt = ?,
                    error = ?, token = NULL WHERE id = ?
            """,
                (status, attempts, next_attempt, str(error), row["id"]),
            )
        progress.emit(
            "job_retry",
            repo_id=job["repo_id"],
            pr=job["number"],
            job=job["id"],
            generation=job["generation"],
            attempt=attempts,
            status=status,
            delay_seconds=delay if status == "retry" else 0,
            error_type=type(error).__name__,
        )
        return status


def _pre_engine_exports(debug):
    """Recover exports from saved results written before the engine interface."""
    from ralph.trace import published_concept_concern

    decisions = debug.get("decisions", [])
    attribution = {
        item.get("finding_id"): item for item in debug.get("finding_attribution", [])
    }
    findings = []
    index = 0
    for decision in decisions:
        if decision.get("disposition") != "publish":
            continue
        finding = decision.get("finding")
        if not isinstance(finding, dict):
            continue
        index += 1
        finding_id = f"finding:{index}"
        item = attribution.get(finding_id, {})
        findings.append(
            {
                "id": finding_id,
                **{
                    key: finding.get(key)
                    for key in (
                        "kind",
                        "severity",
                        "security",
                        "path",
                        "line",
                        "side",
                        "source_evidence",
                    )
                },
                "title": finding.get("title"),
                "body": finding.get("body"),
                "presented_title": item.get("title"),
                "presented_body": item.get("presented_body"),
                **{
                    key: item.get(key)
                    for key in (
                        "raised_by",
                        "raised_by_models",
                        "verified_by",
                        "verified_by_model",
                        "judged_by",
                        "judged_by_model",
                        "edited_by",
                        "edited_by_model",
                    )
                },
            }
        )
    verified_objection = published_concept_concern(debug)
    return {
        "findings": findings,
        "actionable": [
            *[item["id"] for item in findings],
            *(["concept_objection"] if verified_objection else []),
        ],
    }


def migrate_state(path):
    """Back up and convert pre-engine saved results once, before service startup."""
    path = str(path)
    backup_path = f"{path}.pre-engine.bak"
    with sqlite3.connect(path, timeout=30) as source:
        columns = {row[1] for row in source.execute("PRAGMA table_info(jobs)")}
        if "engine_identity" in columns:
            return False
        if not columns:
            raise ValueError("job database does not contain a jobs table")
        if os.path.exists(backup_path):
            raise FileExistsError(f"Migration backup already exists: {backup_path}")
        fd = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        try:
            with sqlite3.connect(backup_path) as backup:
                source.backup(backup)
        except BaseException:
            os.unlink(backup_path)
            raise

    from ralph.trace import review_metrics

    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("ALTER TABLE jobs ADD COLUMN engine_identity TEXT")
        rows = db.execute(
            "SELECT id, number, generation, status, attempts, review_result FROM jobs"
        ).fetchall()
        for job_id, number, generation, status, attempts, raw_result in rows:
            identity = (
                {
                    "name": "current",
                    "code_sha256": None,
                    "config_sha256": None,
                    "config": None,
                }
                if raw_result is not None
                else {
                    "name": "unknown",
                    "code_sha256": None,
                    "config_sha256": None,
                    "config": None,
                }
                if status in {"running", "retry"} or attempts
                else None
            )
            result = None
            if raw_result is not None:
                old = json.loads(raw_result)
                debug = old.get("debug", {})
                result = {
                    "schema_version": 1,
                    "engine": identity,
                    "base_sha": old["base_sha"],
                    "head_sha": old["head_sha"],
                    "merge_base": None,
                    "content": old["content"],
                    "output": {
                        "schema_version": 1,
                        "data": {"schema_version": 1, **debug},
                        "exports": _pre_engine_exports(debug),
                    },
                    "run": {
                        "review_id": f"pr:{number}:{job_id}:{generation}",
                        "status": "completed",
                        "skipped": bool(debug.get("skip")),
                        "metrics": review_metrics(debug),
                        "usage": debug.get("usage"),
                        "attempt": debug,
                    },
                }
            db.execute(
                """
                UPDATE jobs SET engine_identity = ?, review_result = ? WHERE id = ?
                """,
                (
                    json.dumps(identity, sort_keys=True)
                    if identity is not None
                    else None,
                    json.dumps(result) if result is not None else None,
                    job_id,
                ),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
    return True
