import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

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
                "number INTEGER, status TEXT, review_result TEXT, head TEXT)"
            )
        self.store = monitor.NotificationStore(self.root / "telegram.sqlite3")
        self.day = timestamp("2026-10-26T12:00:00")

    def job(self, job_id, status, *, result=None):
        with closing(sqlite3.connect(self.jobs_path)) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO jobs VALUES (?, 1, ?, ?, ?, 'abc')",
                (
                    job_id,
                    job_id + 100,
                    status,
                    json.dumps(result) if result else None,
                ),
            )

    def observe(self, now=None):
        self.store.observe(
            self.root,
            "https://reports.example",
            "https://forge.example/repo",
            report_dir=self.root / "reports",
            now=self.day if now is None else now,
        )

    def messages(self):
        with closing(sqlite3.connect(self.store.path)) as db:
            return dict(db.execute("SELECT key, text FROM outbox ORDER BY rowid"))

    def test_initial_baseline_ignores_historical_failures_and_reviews(self):
        self.job(1, "failed")
        self.job(2, "complete", result={"run": {"report_url": "https://report"}})
        self.observe()
        self.assertEqual(self.messages(), {})
        self.store = monitor.NotificationStore(self.store.path)
        self.observe()
        self.assertEqual(self.messages(), {})

    def test_groups_terminal_failures_ignores_retries_and_survives_restart(self):
        self.observe()
        self.job(1, "retry")
        self.observe()
        self.assertEqual(self.messages(), {})
        self.job(1, "failed")
        self.job(2, "failed")
        self.observe()
        messages = list(self.messages().values())
        self.assertEqual(len(messages), 1)
        self.assertIn("2 terminal review failure", messages[0])
        self.assertIn("https://forge.example/repo/pulls/101", messages[0])
        self.store = monitor.NotificationStore(self.store.path)
        self.observe()
        self.assertEqual(len(self.messages()), 1)

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
        render.assert_called_once_with("https://forge.example/repo", 101, result)
        self.assertEqual(self.messages(), {"review:1:1": "Review post"})

    def test_failed_report_retries_without_losing_completion(self):
        self.observe()
        self.job(1, "complete", result={"content": "review", "run": {}})
        with patch.object(monitor.report, "save_report", side_effect=OSError):
            self.observe()
        self.assertEqual(self.messages(), {})
        with (
            patch.object(monitor.report, "save_report", return_value="report.html"),
            patch.object(monitor.telegram, "review_message", return_value="review"),
        ):
            self.observe()
        self.assertEqual(self.messages(), {"review:1:1": "review"})
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 1)

    def test_2100_utc_rollover_digest_and_restart_do_not_replay(self):
        self.observe()
        self.job(
            1,
            "complete",
            result={
                "content": "Review body",
                "run": {"report_url": "https://report.example/1.html"},
            },
        )
        self.job(5, "complete")
        self.job(2, "failed")
        self.job(3, "pending")
        self.job(4, "running")
        self.observe()
        self.assertNotIn("digest:2026-10-25", self.messages())
        self.observe(timestamp("2026-10-26T20:59:59"))
        self.assertFalse(any(key.startswith("digest:") for key in self.messages()))
        boundary = timestamp("2026-10-26T21:00:00")
        self.observe(boundary)
        digest = self.messages()["digest:2026-10-25"]
        self.assertIn("2026-10-25 21:00 to 2026-10-26 21:00 UTC", digest)
        self.assertIn("Reviews completed: 1", digest)
        self.assertIn("Terminal failures: 1", digest)
        self.assertIn("Queue now: 1 pending, 1 running", digest)
        self.store = monitor.NotificationStore(self.store.path)
        self.observe(timestamp("2026-10-27T00:00:00"))
        self.assertEqual(sum(key.startswith("digest:") for key in self.messages()), 1)

    def test_spend_uses_2100_period_boundaries_all_ledgers_and_marks_unknowns(self):
        for filename in (
            "spend.sqlite3",
            "ppq-spend.sqlite3",
            "followup/spend.sqlite3",
        ):
            path = self.root / filename
            path.parent.mkdir(exist_ok=True)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute(
                    "CREATE TABLE requests (cost_micros INTEGER, reserved_micros INTEGER, requested_at REAL, status TEXT)"
                )
                db.executemany(
                    "INSERT INTO requests VALUES (?, ?, ?, ?)",
                    [
                        (1_000_000, 0, self.day, "settled"),
                        (None, 500_000, self.day, "reserved"),
                        (None, None, self.day, "unknown"),
                        (None, 0, self.day, "released"),
                        (None, 0, self.day, "duplicate"),
                        (9_000_000, 0, timestamp("2026-10-25T20:59:59"), "settled"),
                        (250_000, 0, timestamp("2026-10-25T21:00:00"), "settled"),
                        (750_000, 0, timestamp("2026-10-26T20:59:59"), "settled"),
                        (9_000_000, 0, timestamp("2026-10-26T21:00:00"), "settled"),
                    ],
                )
        day = datetime.fromtimestamp(timestamp("2026-10-25T21:00:00"), UTC).date()
        text = monitor._daily_spend(self.root, day)
        self.assertEqual(text.count("$2.000000 known"), 3)
        self.assertEqual(text.count("2 unknown cost(s)"), 3)
        self.assertEqual(text.count("$0.500000 reserved"), 3)
        self.assertEqual(text.count("1 unknown reservation(s)"), 3)
        self.assertNotIn("9.000000", text)

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

    def test_command_replies_bypass_background_backlog_but_respect_global_backoff(self):
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.executemany(
                "INSERT INTO outbox (key, text, next_attempt) VALUES (?, ?, ?)",
                [("review:1", "review", 0), ("command:1", "status", 0)],
            )
        with patch.object(monitor.telegram, "send_message") as send:
            self.store.deliver("secret", "channel", now=100, commands_only=True)
            send.assert_called_once_with("secret", "channel", "status")
            self.store.deliver("secret", "channel", now=101, commands_only=True)
            self.assertEqual(send.call_count, 1)
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("INSERT INTO outbox (key, text) VALUES ('command:2', 'help')")
        error = monitor.telegram.TelegramError(retry_after=120)
        with patch.object(
            monitor.telegram, "send_message", side_effect=[error, None, None]
        ) as send:
            with self.assertLogs(monitor.LOGGER, level="WARNING"):
                self.store.deliver("secret", "channel", now=102, commands_only=True)
            self.store.deliver("secret", "channel", now=200)
            self.assertEqual(send.call_count, 1)
            self.store.deliver("secret", "channel", now=222)
            send.assert_called_with("secret", "channel", "help")
            self.store.deliver("secret", "channel", now=223)
            send.assert_called_with("secret", "channel", "review")

    def test_monitor_recovers_initialization_and_delivers_despite_observe_error(self):
        from unittest.mock import Mock

        stop = Mock()
        stop.is_set.side_effect = [False, False, False, True]
        store = Mock()
        store.observe.side_effect = [sqlite3.OperationalError(), None]
        with (
            patch.object(
                monitor,
                "NotificationStore",
                side_effect=[sqlite3.OperationalError(), store],
            ) as constructor,
            patch.object(monitor.time, "monotonic", side_effect=[0, 0, 0, 31, 31, 31]),
            patch.object(
                monitor.telegram,
                "api_request",
                return_value={"id": 1, "username": "bot"},
            ),
            patch.object(monitor.commands, "poll_commands", return_value=0) as poll,
            self.assertLogs(monitor.LOGGER, level="WARNING"),
        ):
            monitor.run_monitor(
                self.root,
                self.root / "reports",
                "https://reports.example",
                "https://forge.example/repo",
                "secret",
                "channel",
                stop,
            )
        self.assertEqual(constructor.call_count, 2)
        self.assertEqual(store.observe.call_count, 2)
        self.assertEqual(store.deliver.call_count, 2)
        self.assertEqual(stop.wait.call_count, 1)
        self.assertEqual(poll.call_count, 2)

    def test_command_api_failure_does_not_stop_review_observation(self):
        from unittest.mock import Mock

        stop = Mock()
        stop.is_set.side_effect = [False, False, True]
        store = Mock()
        with (
            patch.object(monitor, "NotificationStore", return_value=store),
            patch.object(monitor.time, "monotonic", side_effect=[0, 0, 61, 61, 61]),
            patch.object(
                monitor.telegram,
                "api_request",
                side_effect=[monitor.telegram.TelegramError(), {"id": 1}],
            ),
            patch.object(monitor.commands, "poll_commands", return_value=0),
            self.assertLogs(monitor.LOGGER, level="WARNING"),
        ):
            monitor.run_monitor(
                self.root,
                self.root / "reports",
                "https://reports.example",
                "https://forge.example/repo",
                "secret",
                "channel",
                stop,
            )
        self.assertEqual(store.observe.call_count, 2)
        self.assertEqual(store.deliver.call_count, 2)
        stop.wait.assert_called_once_with(60)

    def test_commands_are_delivered_before_next_background_scan(self):
        from unittest.mock import Mock

        stop = Mock()
        stop.is_set.side_effect = [False, False, True]
        store = Mock()
        events = []
        store.deliver.side_effect = lambda *args, **kwargs: events.append(
            "reply" if kwargs.get("commands_only") else "background"
        )
        replies = iter((1, 0))

        def poll(*args, **kwargs):
            events.append("poll")
            return next(replies)

        with (
            patch.object(monitor, "NotificationStore", return_value=store),
            patch.object(monitor.time, "monotonic", side_effect=[0, 0, 0, 1, 1]),
            patch.object(monitor.telegram, "api_request", return_value={"id": 1}),
            patch.object(monitor.commands, "poll_commands", side_effect=poll),
        ):
            monitor.run_monitor(
                self.root,
                self.root / "reports",
                "https://reports.example",
                "https://forge.example/repo",
                "secret",
                "channel",
                stop,
            )
        self.assertEqual(store.observe.call_count, 1)
        self.assertEqual(events, ["background", "poll", "reply", "poll"])

    def test_standalone_monitor_cli(self):
        token = self.root / "token"
        token.write_text("secret\n")
        args = [
            "ralph-telegram",
            "--state-dir",
            str(self.root),
            "--report-dir",
            str(self.root / "reports"),
            "--report-base-url",
            "https://reports.example/",
            "--repository-url",
            "https://forge.example/repo/",
            "--telegram-token-file",
            str(token),
            "--telegram-chat-id",
            "-100123",
        ]
        with patch("sys.argv", args), patch.object(monitor, "run_monitor") as run:
            monitor.main()
        run.assert_called_once_with(
            self.root,
            self.root / "reports",
            "https://reports.example",
            "https://forge.example/repo",
            "secret",
            "-100123",
        )
        token.write_text("\n")
        with (
            patch("sys.argv", args),
            patch("sys.stderr", new_callable=io.StringIO) as stderr,
            self.assertRaises(SystemExit),
        ):
            monitor.main()
        self.assertIn("must not be empty", stderr.getvalue())

    def test_grouped_failure_overflow_includes_remaining_count(self):
        failures = [(i, 1, i) for i in range(300)]
        text = monitor._failure_message(failures, "https://forge.example/repo")
        self.assertLessEqual(monitor.telegram.message_length(text), 4096)
        self.assertIn("more.", text)


if __name__ == "__main__":
    unittest.main()
