import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from ralph.usage import Ledger

from ralph_service import monitor


def timestamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp()


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.jobs_path = self.root / "jobs.sqlite3"
        with closing(sqlite3.connect(self.jobs_path)) as db, db:
            db.execute(
                "CREATE TABLE jobs (id INTEGER PRIMARY KEY, generation INTEGER, "
                "number INTEGER, status TEXT, review_result TEXT, head TEXT, repo_id TEXT)"
            )
        self.store = monitor.NotificationStore(self.root / "telegram.sqlite3")
        self.day = timestamp("2026-10-26T12:00:00")

    def job(self, job_id, status, *, result=None, repo_id="core"):
        with closing(sqlite3.connect(self.jobs_path)) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO jobs VALUES (?, 1, ?, ?, ?, 'abc', ?)",
                (
                    job_id,
                    job_id + 100,
                    status,
                    json.dumps(result) if result else None,
                    repo_id,
                ),
            )

    def observe(self, now=None):
        self.store.observe(
            self.root,
            "https://reports.example",
            {
                "core": {
                    "repository": "owner/core",
                    "repository_url": "https://forge.example/repo",
                },
                "gui": {
                    "repository": "owner/gui",
                    "repository_url": "https://forge.example/gui",
                },
            },
            report_dir=self.root / "reports",
            now=self.day if now is None else now,
        )

    def messages(self):
        with closing(sqlite3.connect(self.store.path)) as db:
            return dict(db.execute("SELECT key, text FROM outbox ORDER BY rowid"))

    def test_complete_review_enqueued_once_after_baseline(self):
        self.observe()
        result = {
            "content": "Review body",
            "run": {
                "pr_title": "Example PR",
                "report_url": "https://report.example/1.html",
            },
        }
        self.job(1, "complete", result=result)
        with patch.object(
            monitor.telegram, "review_message", return_value="Review post"
        ) as render:
            self.observe()
            self.observe()
        render.assert_called_once_with(
            "https://forge.example/repo", 101, result, repository_id="core"
        )
        self.assertEqual(self.messages(), {"review:1:1": "Review post"})

    def test_same_number_notifications_use_their_repository(self):
        self.observe()
        result = {
            "run": {"pr_title": "Change", "report_url": "https://reports/review.html"}
        }
        self.job(1, "complete", result=result)
        self.job(2, "complete", result=result, repo_id="gui")
        with closing(sqlite3.connect(self.jobs_path)) as db, db:
            db.execute("UPDATE jobs SET number = 42")
        self.observe()
        messages = self.messages()
        self.assertIn("core#42", messages["review:1:1"])
        self.assertIn("https://forge.example/repo/pulls/42", messages["review:1:1"])
        self.assertIn("gui#42", messages["review:2:1"])
        self.assertIn("https://forge.example/gui/pulls/42", messages["review:2:1"])

    def test_legacy_requests_require_explicit_migration_and_keep_offsets(self):
        path = self.root / "legacy.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TABLE review_requests (id INTEGER PRIMARY KEY, number INTEGER NOT NULL, processed INTEGER NOT NULL DEFAULT 0)"
            )
            db.execute("INSERT INTO review_requests VALUES (7, 42, 0)")
            db.execute(
                "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            db.execute("INSERT INTO metadata VALUES ('update_offset', '8')")
        with self.assertRaisesRegex(RuntimeError, "repository migration"):
            monitor.NotificationStore(path)
        self.assertTrue(monitor.migrate_notification_repositories(path))
        self.assertFalse(monitor.migrate_notification_repositories(path))
        store = monitor.NotificationStore(path)
        with closing(store._connect()) as db:
            self.assertEqual(
                db.execute(
                    "SELECT id, repo_id, number, processed FROM review_requests"
                ).fetchall(),
                [(7, "core", 42, 0)],
            )
            self.assertEqual(
                db.execute(
                    "SELECT value FROM metadata WHERE key = 'update_offset'"
                ).fetchone(),
                ("8",),
            )

    def test_outbox_is_idempotent_and_retries_with_rate_limit(self):
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.executemany(
                "INSERT OR IGNORE INTO outbox (key, text) VALUES (?, ?)",
                [("one", "message"), ("one", "duplicate"), ("two", "second message")],
            )
        error = monitor.telegram.TelegramError()
        error.retry_after = 120
        with patch.object(
            monitor.telegram, "send_message", side_effect=[error, None, None]
        ) as send:
            with self.assertLogs(monitor.LOGGER, level="WARNING") as logs:
                self.store.deliver("secret", "channel", now=100)
            self.assertNotIn("sensitive", " ".join(logs.output))
            self.store = monitor.NotificationStore(self.store.path)
            self.store.deliver("secret", "channel", now=219)
            self.assertEqual(send.call_count, 1)
            self.store.deliver("secret", "channel", now=220)
            self.assertEqual(send.call_count, 2)
            send.assert_called_with("secret", "channel", "message")
            self.store.deliver("secret", "channel", now=250)
            self.assertEqual(send.call_count, 3)
            send.assert_called_with("secret", "channel", "second message")
            self.store.deliver("secret", "channel", now=300)
            self.assertEqual(send.call_count, 3)

    def test_digest_counts_recorded_usage_between_utc_21_hour_boundaries(self):
        path = self.root / "followup/usage.sqlite3"
        path.parent.mkdir()
        Ledger(path)
        with closing(sqlite3.connect(path)) as db, db:
            for started in (
                "2026-10-26T20:59:59",
                "2026-10-26T21:00:00",
                "2026-10-27T20:59:59",
                "2026-10-27T21:00:00",
            ):
                db.execute(
                    "INSERT INTO requests (token, review_id, stage, model, month, status, usage_json, requested_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        started,
                        "pr:1:1",
                        "followup",
                        "gpt-6-luna",
                        "2026-10",
                        "completed",
                        json.dumps(
                            {
                                "input_tokens": 1,
                                "output_tokens": 2,
                                "cached_tokens": 0,
                                "cache_write_tokens": 0,
                                "complete": True,
                            }
                        ),
                        timestamp(started),
                    ),
                )
        self.observe(timestamp("2026-10-26T22:00:00"))
        self.observe(timestamp("2026-10-27T22:00:00"))
        message = self.messages()["digest:2026-10-26"]
        self.assertIn("2 requests", message)
        self.assertIn("input 2", message)
        self.assertIn("output 4", message)
        self.assertNotIn("$", message)


if __name__ == "__main__":
    unittest.main()
