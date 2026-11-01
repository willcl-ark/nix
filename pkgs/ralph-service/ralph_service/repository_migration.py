"""Offline, transactional cutover of a single-repository queue."""

import os
import re
import sqlite3

from ralph import stats_changes

from .jobs import SCHEMA_VERSION, _repository_id


def migrate_repositories(path, *, legacy_repo_id):
    """Assign existing records explicitly, preserving payloads and delivery holds.

    All service writers must be stopped. A failed transaction leaves the old
    schema intact; the backup is never overwritten. Remove or relocate that
    backup deliberately before retrying a failed migration.
    """
    _repository_id(legacy_repo_id)
    path = os.fspath(path)
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
        if version == SCHEMA_VERSION and "repo_id" in columns:
            return False
        if version != 0 or "repo_id" in columns:
            raise RuntimeError("unsupported job database schema version")
        if "engine_identity" not in columns:
            raise RuntimeError(
                "complete the engine migration before the repository migration"
            )
        backup_path = path + ".pre-repositories.bak"
        fd = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            with sqlite3.connect(backup_path) as backup:
                db.backup(backup)
                if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError(
                        "repository migration backup failed integrity check"
                    )
        except BaseException:
            os.unlink(backup_path)
            raise

        db.execute("BEGIN IMMEDIATE")
        for table in ("jobs", "reconciliation", "review_requests", "deliveries"):
            row = db.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            if row is None:
                continue
            old_columns = [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
            old_count = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            statement = re.sub(
                r"CREATE TABLE\s+(?:IF NOT EXISTS\s+)?[\"`\[]?"
                + table
                + r"[\"`\]]?\s*\(",
                f"CREATE TABLE {table}_repositories (repo_id TEXT NOT NULL, ",
                row[0],
                count=1,
                flags=re.IGNORECASE,
            )
            if table == "jobs":
                statement, count = re.subn(
                    r"UNIQUE\s*\(\s*number\s*,\s*generation\s*\)",
                    "UNIQUE(repo_id, number, generation)",
                    statement,
                    flags=re.IGNORECASE,
                )
                if count != 1:
                    raise RuntimeError("unrecognized legacy job uniqueness constraint")
            elif table in {"reconciliation", "review_requests"}:
                key = "number" if table == "reconciliation" else "id"
                statement, count = re.subn(
                    r"\b" + key + r"\s+(INTEGER|TEXT)\s+PRIMARY KEY",
                    key + r" \1 NOT NULL",
                    statement,
                    flags=re.IGNORECASE,
                )
                if count != 1:
                    raise RuntimeError(f"unrecognized legacy {table} primary key")
                statement = (
                    statement.rstrip().removesuffix(")")
                    + f", PRIMARY KEY(repo_id, {key}))"
                )
            db.execute(statement)
            names = ", ".join(old_columns)
            db.execute(
                f"INSERT INTO {table}_repositories (repo_id, {names}) SELECT ?, {names} FROM {table}",
                (legacy_repo_id,),
            )
            if (
                db.execute(f"SELECT count(*) FROM {table}_repositories").fetchone()[0]
                != old_count
            ):
                raise RuntimeError(f"row count changed while migrating {table}")
            db.execute(f"DROP TABLE {table}")
            db.execute(f"ALTER TABLE {table}_repositories RENAME TO {table}")

        db.execute(
            "CREATE UNIQUE INDEX jobs_one_pending_per_pr ON jobs(repo_id, number) WHERE status = 'pending'"
        )
        db.execute(
            "CREATE UNIQUE INDEX jobs_one_running_per_pr ON jobs(repo_id, number) WHERE status = 'running'"
        )
        # Consumers must refresh derived statistics after the schema cutover.
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'stats_sources'"
        ).fetchone():
            db.execute("DELETE FROM stats_sources WHERE table_name = 'jobs'")
            db.execute("DELETE FROM stats_changes WHERE table_name = 'jobs'")
        stats_changes.install(db, "jobs", "id")
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'deliveries'"
        ).fetchone():
            if db.execute(
                "SELECT 1 FROM deliveries d LEFT JOIN jobs j ON j.id = d.job_id WHERE j.id IS NULL OR j.repo_id != d.repo_id OR j.number != d.number LIMIT 1"
            ).fetchone():
                raise RuntimeError("delivery records do not match their jobs")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("migrated job database failed integrity check")
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        db.commit()
        return True
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
