"""Profile selection, durable commands and per-review spending."""

import tempfile
import threading
import unittest
import urllib.error
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from ralph import engine, spend, profiles
from ralph_service import config, monitor, service
from ralph_service.jobs import JobStore


class ProfileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.jobs = JobStore(self.root / "jobs.sqlite3", retry_base_seconds=0)
        self.default = engine.load("current")
        self.high = engine.load("current", {"profile": "high"})
        self.store = monitor.NotificationStore(self.root / "telegram.sqlite3")
        self.config = config.BotConfig(
            "https://git/repo", "o/r", "https://git/api/v1/repos/o/r"
        )

    def enqueue(self, selected, head="a" * 40, **kwargs):
        return self.jobs.enqueue(
            42, "master", head, "opened", engine_identity=selected.identity, **kwargs
        )

    def test_worker_preserves_high_and_binds_unconfigured_migrated_jobs(self):
        for identity in (self.high.identity, None, {"name": "unknown", "config": None}):
            with self.subTest(identity=identity):
                self.jobs.enqueue(
                    42,
                    "master",
                    "a" * 40,
                    "opened",
                    force=True,
                    engine_identity=identity,
                )
                self.jobs = JobStore(self.jobs.path)
                stop = threading.Event()
                expected = self.high if identity == self.high.identity else self.default

                observed = []

                def process(job, *args):
                    observed.append((job["engine_identity"], args[7].identity))
                    self.jobs.complete(job)
                    stop.set()

                with patch.object(service, "process_job", side_effect=process):
                    service.worker(
                        self.jobs,
                        self.root,
                        "key",
                        "token",
                        "bot",
                        self.config,
                        None,
                        self.default,
                        stop_event=stop,
                    )
                self.assertEqual(observed, [(expected.identity, expected.identity)])

    def test_custom_engine_profiles_do_not_inherit_high(self):
        for selected in ("own-profile", {"name": "high"}, None):
            with self.subTest(profile=selected):
                self.enqueue(self.high, force=True)
                identity = {"name": "custom", "config": {"profile": selected}}
                self.jobs.enqueue(
                    42, "master", "b" * 40, "opened", engine_identity=identity
                )
                job = self.jobs.claim()
                self.assertEqual(job["engine_identity"], identity)
                self.jobs.complete(job)
                self.jobs.enqueue(
                    42, "master", "c" * 40, "opened", engine_identity=None
                )
                self.jobs.complete(self.jobs.claim())

    def test_transient_request_failure_does_not_starve_later_commands(self):
        with closing(self.store._connect()) as db, db:
            db.executemany(
                "INSERT INTO review_requests (id, number) VALUES (?, ?)",
                [(1, 41), (2, 42)],
            )
        pr = {
            "number": 42,
            "state": "open",
            "base": {"ref": "master"},
            "head": {"sha": "a" * 40},
        }
        for error in (
            TimeoutError("temporary"),
            urllib.error.HTTPError("https://git", 503, "unavailable", {}, None),
        ):
            with self.subTest(error=type(error).__name__):
                with closing(self.store._connect()) as db, db:
                    db.execute("UPDATE review_requests SET processed = 0")
                with (
                    patch.object(
                        service.forgejo, "forgejo_request", side_effect=[error, pr]
                    ),
                    self.assertLogs(level="WARNING"),
                ):
                    service.consume_review_requests(
                        self.store, self.jobs, self.config, "token", self.high
                    )
                with closing(self.store._connect()) as db:
                    self.assertEqual(
                        db.execute(
                            "SELECT id, processed FROM review_requests ORDER BY id"
                        ).fetchall(),
                        [(1, 0), (2, 1)],
                    )
        self.assertEqual(self.jobs.claim()["number"], 42)

    def test_same_head_high_is_fresh_and_preserved_on_update(self):
        self.enqueue(self.default)
        self.jobs.complete(self.jobs.claim())
        self.assertTrue(self.enqueue(self.high, force=True))
        self.assertTrue(self.enqueue(self.default, head="b" * 40))
        job = self.jobs.claim()
        self.assertEqual(job["engine_identity"], self.high.identity)
        self.assertEqual(job["head"], "b" * 40)
        self.assertTrue(job["force"])
        self.jobs.retry(job, RuntimeError("transient"))
        self.assertEqual(self.jobs.claim()["engine_identity"], self.high.identity)

    def test_running_high_same_head_webhook_does_not_queue_again(self):
        self.enqueue(self.high, force=True)
        self.jobs.claim()
        self.assertFalse(self.enqueue(self.default))
        self.assertIsNone(self.jobs.claim())

    def test_running_high_new_head_keeps_profile(self):
        self.enqueue(self.high, force=True)
        job = self.jobs.claim()
        self.enqueue(self.default, head="b" * 40)
        self.jobs.supersede(job)
        self.assertEqual(self.jobs.claim()["engine_identity"], self.high.identity)

    def test_duplicate_request_after_completion_does_not_repeat(self):
        self.enqueue(self.high, force=True, request_id="telegram:1")
        self.jobs.complete(self.jobs.claim())
        self.assertFalse(self.enqueue(self.high, force=True, request_id="telegram:1"))
        self.assertIsNone(self.jobs.claim())

    def test_budget_increases_without_mutating_shared_ledger(self):
        ledger = spend.Ledger(
            self.root / "spend.sqlite3", review_limit_usd=1, monthly_limit_usd=20
        )
        selected = profiles.get_profile("high").apply_budget(ledger)
        self.assertEqual(selected.review_limit_micros, 5_000_000)
        self.assertEqual(ledger.review_limit_micros, 1_000_000)
        self.assertEqual(selected.monthly_limit_micros, 20_000_000)
        self.assertEqual(selected.path, ledger.path)
        self.assertEqual(
            profiles.get_profile("default").apply_budget(ledger).review_limit_micros,
            ledger.review_limit_micros,
        )

    def test_migrate_unfinished_profile_once(self):
        legacy = dict(self.default.identity)
        legacy["config"] = self.default.config
        legacy["config"].pop("profile")
        from ralph.config import PromptConfig

        legacy["config"]["prompts"]["models"] = PromptConfig.load().models
        self.jobs.enqueue(42, "master", "a" * 40, "opened", engine_identity=legacy)
        reopened = JobStore(self.jobs.path)
        job = reopened.claim()
        self.assertEqual(job["engine_identity"]["config"]["profile"]["name"], "default")
        self.assertNotIn("models", job["engine_identity"]["config"]["prompts"])

    def test_request_handoff_is_idempotent(self):
        with closing(self.store._connect()) as db, db:
            db.execute("INSERT INTO review_requests (id, number) VALUES (1, 42)")
        pr = {
            "number": 42,
            "state": "open",
            "base": {"ref": "master"},
            "head": {"sha": "a" * 40},
        }
        with patch.object(service.forgejo, "forgejo_request", return_value=pr):
            service.consume_review_requests(
                self.store, self.jobs, self.config, "token", self.high
            )
            job = self.jobs.claim()
            self.assertEqual(job["engine_identity"], self.high.identity)
            self.assertTrue(job["force"])
            self.jobs.complete(job)
            # Simulate a crash after jobs commit but before acknowledging the command.
            with closing(self.store._connect()) as db, db:
                db.execute("UPDATE review_requests SET processed = 0")
            service.consume_review_requests(
                self.store, self.jobs, self.config, "token", self.high
            )
            self.assertIsNone(self.jobs.claim())

    def test_closed_request_is_rejected(self):
        with closing(self.store._connect()) as db, db:
            db.execute("INSERT INTO review_requests (id, number) VALUES (1, 42)")
        with patch.object(
            service.forgejo,
            "forgejo_request",
            return_value={"number": 42, "state": "closed"},
        ):
            service.consume_review_requests(
                self.store, self.jobs, self.config, "token", self.high
            )
        self.assertIsNone(self.jobs.claim())
        with closing(self.store._connect()) as db:
            self.assertEqual(
                db.execute("SELECT processed FROM review_requests").fetchone()[0], 1
            )
            self.assertIn(
                "not open", db.execute("SELECT text FROM outbox").fetchone()[0]
            )
