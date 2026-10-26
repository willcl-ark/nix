"""High reviews survive queue updates, retries and command replay."""

import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from ralph import engine
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

    def test_duplicate_request_after_completion_does_not_repeat(self):
        self.enqueue(self.high, force=True, request_id="telegram:1")
        self.jobs.complete(self.jobs.claim())
        self.assertFalse(self.enqueue(self.high, force=True, request_id="telegram:1"))
        self.assertIsNone(self.jobs.claim())

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
