import json
import sqlite3
import tempfile
import unittest
import urllib.request
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from ralph.usage import Ledger

from ralph_service import commands, jobs, monitor, status


class CommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = monitor.NotificationStore(self.root / "telegram.sqlite3")
        self.bot = {"id": 9, "username": "RalphBot"}
        self.admins = [
            {"status": "creator", "user": {"id": 1}},
            {
                "status": "administrator",
                "user": {"id": 9, "is_bot": True},
                "can_post_messages": True,
            },
        ]
        self.updates = []
        self.api = patch.object(commands.telegram, "api_request", create=True).start()
        self.addCleanup(patch.stopall)
        self.api.side_effect = self.request

    def request(self, token, method, data, **kwargs):
        if method == "getUpdates":
            return self.updates
        self.assertEqual(method, "getChatAdministrators")
        self.assertEqual(data, {"chat_id": -100123, "return_bots": True})
        return self.admins

    def update(self, update_id=10, text="/help", **fields):
        post = {
            "chat": {"type": "channel", "id": -100123, "username": "RalphChannel"},
            "text": text,
        }
        post.update(fields)
        return {"update_id": update_id, "channel_post": post}

    def poll(self, destination="-100123"):
        return commands.poll_commands(
            self.store,
            "token",
            destination,
            self.bot,
            self.root,
            self.root,
            "https://reports",
        )

    def messages(self):
        with closing(self.store._connect()) as db:
            return dict(db.execute("SELECT key, text FROM outbox"))

    def offset(self):
        with closing(self.store._connect()) as db:
            row = db.execute(
                "SELECT value FROM metadata WHERE key = 'update_offset'"
            ).fetchone()
            return row[0] if row else None

    def test_high_request_is_durable_and_idempotent(self):
        self.updates = [self.update(text="/high@RalphBot 42")]
        self.assertEqual(self.poll(), 1)
        self.assertEqual(self.poll(), 0)
        with closing(self.store._connect()) as db:
            self.assertEqual(
                db.execute("SELECT id, number FROM review_requests").fetchall(),
                [(10, 42)],
            )
        self.assertIn("high review", self.messages()["command:10"])

    def test_high_invalid_and_unauthorized_requests(self):
        for index, text in enumerate(
            ("/high", "/high -1", "/high https://elsewhere/42", "/high 42 extra")
        ):
            self.updates = [self.update(10 + index, text=text)]
            self.poll()
        self.updates = [self.update(20, text="/high 42", **{"from": {"id": 1234}})]
        self.poll()
        with closing(self.store._connect()) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM review_requests").fetchone()[0], 0
            )
        self.assertNotIn("command:20", self.messages())

    def test_ignored_chat_private_message_and_edited_post(self):
        stranger = self.update(10)
        stranger["channel_post"]["chat"]["id"] = -999
        private = self.update(11, chat={"type": "private", "id": -100123})
        edited = {"update_id": 12, "edited_channel_post": self.update()["channel_post"]}
        direct = {"update_id": 13, "message": self.update()["channel_post"]}
        self.updates = [stranger, private, edited, direct]
        self.poll()
        self.assertEqual(self.messages(), {})
        self.assertEqual(self.offset(), "14")
        self.assertEqual(self.api.call_count, 1)

    def test_identified_sender_must_be_an_administrator(self):
        self.updates = [self.update(10, **{"from": {"id": 2}})]
        self.poll()
        self.assertEqual(self.messages(), {})
        self.admins.append(
            {"status": "administrator", "can_post_messages": True, "user": {"id": 2}}
        )
        self.updates = [
            self.update(11, **{"from": {"id": 2}}),
            self.update(12, **{"from": {"id": 1}}),
            self.update(13, **{"from": {"id": 9, "is_bot": True}}),
        ]
        self.poll()
        self.assertEqual(len(self.messages()), 3)

    def test_authorization_api_failure_does_not_acknowledge_batch(self):
        self.updates = [self.update(10), self.update(11)]
        self.api.side_effect = [
            self.updates,
            self.admins,
            commands.telegram.TelegramError(),
        ]
        with self.assertRaises(commands.telegram.TelegramError):
            self.poll()
        self.assertEqual(self.messages(), {})
        self.assertIsNone(self.offset())

    def test_status_reads_recorded_usage_and_stale_quota_without_network(self):
        now = datetime.now(UTC)
        store = jobs.JobStore(self.root / "jobs.sqlite3")
        store.enqueue(
            42, "master", "a" * 40, "opened", engine_identity={"name": "current"}
        )
        job = store.claim()
        store.save_result(
            job,
            {
                "schema_version": 1,
                "run": {"status": "failed"},
                "output": {"schema_version": 2, "exports": {"findings": []}},
            },
        )
        store.complete(job)
        for filename, usage in (
            (
                "usage.sqlite3",
                {
                    "input_tokens": 1234,
                    "output_tokens": 345,
                    "cached_tokens": 1200,
                    "cache_write_tokens": 0,
                    "complete": True,
                },
            ),
            ("followup/usage.sqlite3", None),
        ):
            path = self.root / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            Ledger(path)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute(
                    "INSERT INTO requests (token, review_id, stage, model, month, status, usage_json, requested_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        filename,
                        "pr:42:1",
                        "verifier",
                        "claude-opus-5-5",
                        now.strftime("%Y-%m"),
                        "completed" if usage else "uncertain",
                        json.dumps(usage) if usage else None,
                        int(now.timestamp()) - 1,
                    ),
                )
        (self.root / "stats").mkdir()
        (self.root / "stats/stats.json").write_text(
            json.dumps(
                {
                    "subscriptions": {
                        "status": "ok",
                        "checked_at": now.isoformat(),
                        "accounts": [
                            {
                                "provider": "claude",
                                "status": "ok",
                                "observed_at": (now - timedelta(hours=1)).isoformat(),
                                "windows": [
                                    {
                                        "name": "Weekly",
                                        "used_percent": 37,
                                        "resets_at": None,
                                    }
                                ],
                            }
                        ],
                    }
                }
            )
        )
        self.updates = [self.update(text="/status")]
        with (
            patch.object(status, "_services", return_value=[]),
            patch.object(
                urllib.request, "urlopen", side_effect=AssertionError("network request")
            ),
        ):
            self.poll()
        message = self.messages()["command:10"]
        self.assertIn("All-time delivery jobs: 1 completed", message)
        self.assertIn("Saved reviews: 1 · Failed reviews: 1", message)
        self.assertIn("2 requests", message)
        self.assertIn("input 1,234", message)
        self.assertIn("Missing usage: 1", message)
        self.assertIn("63.0% remaining", message)
        self.assertIn("stale", message)
        self.assertNotIn("$", message)
        self.assertIn("Reports: https://reports", message)
        self.assertIn("Stats: https://reports/stats/", message)

    def test_status_missing_usage_is_unavailable(self):
        self.updates = [self.update(text="/status")]
        with (
            patch.object(status, "_services", return_value=[]),
            patch.object(
                urllib.request, "urlopen", side_effect=AssertionError("network request")
            ),
        ):
            self.poll()
        message = self.messages()["command:10"]
        self.assertIn("Usage unavailable", message)
        self.assertIn("Quota snapshot unavailable", message)
        self.assertNotIn("input 0", message)


if __name__ == "__main__":
    unittest.main()
