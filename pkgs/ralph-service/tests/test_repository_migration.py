import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ralph_service.jobs import JobStore
from ralph_service.repository_migration import migrate_repositories


class RepositoryMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "jobs.sqlite3"
        self.payload = (
            '{ "content": "historical review", "run": {"review_id": "pr:42:17:1"} }'
        )
        with sqlite3.connect(self.path) as db:
            db.executescript("""
                CREATE TABLE jobs (
                    id INTEGER PRIMARY KEY, number INTEGER NOT NULL,
                    generation INTEGER NOT NULL, base_ref TEXT NOT NULL,
                    head TEXT NOT NULL, action TEXT NOT NULL, force INTEGER NOT NULL,
                    engine_identity TEXT NOT NULL, status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                    review_result TEXT, token TEXT, error TEXT, UNIQUE(number, generation));
                CREATE UNIQUE INDEX jobs_one_pending_per_pr ON jobs(number) WHERE status = 'pending';
                CREATE TABLE reconciliation (number INTEGER PRIMARY KEY, epoch INTEGER NOT NULL,
                    base_override TEXT, action TEXT NOT NULL, force INTEGER NOT NULL,
                    engine_identity TEXT NOT NULL, token TEXT, next_attempt REAL NOT NULL DEFAULT 0);
                CREATE TABLE review_requests (id TEXT PRIMARY KEY);
                CREATE TABLE deliveries (job_id INTEGER NOT NULL, number INTEGER NOT NULL,
                    destination TEXT NOT NULL, operation TEXT, path TEXT, body TEXT, state TEXT NOT NULL,
                    PRIMARY KEY(job_id, destination));
                INSERT INTO review_requests VALUES ('telegram:10');
                INSERT INTO reconciliation VALUES (42, 3, NULL, 'opened', 0, 'null', NULL, 0);
            """)
            db.execute(
                "INSERT INTO jobs VALUES (17, 42, 1, 'master', 'abc', 'opened', 0, 'null', 'retry', 2, 0, ?, NULL, 'timeout')",
                (self.payload,),
            )
            db.execute(
                "INSERT INTO deliveries VALUES (17, 42, 'github:bitcoin/bitcoin', 'POST', '/comments', ?, 'uncertain')",
                ("exact\nbody ",),
            )

    def test_explicit_migration_preserves_history_and_uncertain_intent(self):
        with self.assertRaisesRegex(RuntimeError, "explicit repository migration"):
            JobStore(self.path)
        self.assertTrue(migrate_repositories(self.path, legacy_repo_id="core"))
        self.assertFalse(migrate_repositories(self.path, legacy_repo_id="core"))
        store = JobStore(self.path)
        with store._connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE id = 17").fetchone()
            self.assertEqual(
                (row["repo_id"], row["review_result"], row["attempts"]),
                ("core", self.payload, 2),
            )
            intent = db.execute("SELECT * FROM deliveries WHERE job_id = 17").fetchone()
            self.assertEqual(
                (intent["repo_id"], intent["body"], intent["state"]),
                ("core", "exact\nbody ", "uncertain"),
            )
        gui = store.for_repository("gui")
        self.assertTrue(
            gui.enqueue(42, "master", "def", "opened", engine_identity=None)
        )
        self.assertFalse(
            store.reconcile_hint(
                42, "master", "opened", engine_identity=None, request_id="telegram:10"
            )
        )
        self.assertTrue(
            gui.reconcile_hint(
                42, "master", "opened", engine_identity=None, request_id="telegram:10"
            )
        )
        with sqlite3.connect(str(self.path) + ".pre-repositories.bak") as backup:
            self.assertEqual(
                backup.execute(
                    "SELECT review_result FROM jobs WHERE id = 17"
                ).fetchone()[0],
                self.payload,
            )
            self.assertNotIn(
                "repo_id", {row[1] for row in backup.execute("PRAGMA table_info(jobs)")}
            )

    def test_failure_rolls_back_entire_queue_cutover(self):
        with patch(
            "ralph_service.repository_migration.stats_changes.install",
            side_effect=RuntimeError("interrupted"),
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                migrate_repositories(self.path, legacy_repo_id="core")
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertNotIn(
                "repo_id", {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            )
            self.assertEqual(
                db.execute("SELECT review_result FROM jobs WHERE id = 17").fetchone()[
                    0
                ],
                self.payload,
            )
            self.assertEqual(
                db.execute("SELECT state FROM deliveries").fetchone()[0], "uncertain"
            )

    def test_future_schema_is_rejected(self):
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA user_version = 999")
        with self.assertRaisesRegex(RuntimeError, "unsupported"):
            JobStore(self.path)
