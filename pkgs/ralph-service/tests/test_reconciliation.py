import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ralph_service import coordinator, config, service
from ralph_service.jobs import JobStore

IDENTITY = {"name": "current", "config": {"profile": {"name": "default"}}}


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.jobs = JobStore(
            Path(directory.name) / "jobs.sqlite3", retry_base_seconds=0
        )
        self.config = config.BotConfig(
            "https://forge/owner/repo.git",
            "owner/repo",
            "https://forge/api/v1/repos/owner/repo",
        )

    def hint(self, **kwargs):
        return self.jobs.reconcile_hint(
            42, "master", "opened", engine_identity=IDENTITY, **kwargs
        )

    def resolve(self, head, *, state="open", effect=None):
        pull = {
            "number": 42,
            "state": state,
            "base": {"ref": "master"},
            "head": {"sha": head},
        }
        with patch.object(
            service.forgejo, "forgejo_request", side_effect=effect, return_value=pull
        ):
            return service.reconcile_next(self.jobs, self.config, "token")

    def test_delayed_hint_preserves_current_work_but_real_force_push_supersedes(self):
        self.jobs.enqueue(42, "master", "b" * 40, "opened", engine_identity=IDENTITY)
        running = self.jobs.claim()
        self.hint()
        self.assertTrue(self.jobs.is_current(running))
        self.resolve("b" * 40)
        self.assertTrue(self.jobs.is_current(running))
        self.hint()
        self.resolve("a" * 40)
        self.assertFalse(self.jobs.is_current(running))
        self.jobs.supersede(running)
        self.assertEqual(self.jobs.claim()["head"], "a" * 40)

    def test_observation_invalidated_during_fetch_and_failed_fetch_survive_restart(
        self,
    ):
        self.hint()

        def change(*args):
            self.hint()
            return {
                "number": 42,
                "state": "open",
                "base": {"ref": "master"},
                "head": {"sha": "a" * 40},
            }

        self.resolve("a" * 40, effect=change)
        self.assertIsNone(self.jobs.claim())
        with self.assertRaises(TimeoutError):
            self.resolve("a" * 40, effect=TimeoutError())
        self.jobs = JobStore(self.jobs.path, retry_base_seconds=0)
        self.jobs.recover()
        self.resolve("b" * 40)
        self.assertEqual(self.jobs.claim()["head"], "b" * 40)

    def test_forced_high_intent_and_request_dedup_survive_ordinary_hints(self):
        high = {"name": "current", "config": {"profile": {"name": "high"}}}
        self.jobs.reconcile_hint(
            42,
            "sha:" + "c" * 40,
            "opened",
            True,
            engine_identity=high,
            request_id="telegram:1",
        )
        self.hint()
        self.resolve("b" * 40)
        job = self.jobs.claim()
        self.assertEqual(job["base_ref"], "sha:" + "c" * 40)
        self.assertEqual(job["engine_identity"], high)
        self.assertTrue(job["force"])
        self.jobs.complete(job)
        self.assertFalse(
            self.jobs.reconcile_hint(
                42,
                "master",
                "opened",
                True,
                engine_identity=high,
                request_id="telegram:1",
            )
        )
        self.assertFalse(self.resolve("b" * 40))

    def test_closed_pr_consumes_hint_without_enqueuing(self):
        self.hint()
        self.resolve("a" * 40, state="closed")
        self.assertIsNone(self.jobs.claim())
        self.assertFalse(self.resolve("a" * 40))

    def test_hints_are_fair_and_command_failure_does_not_block_reconciliation(self):
        with patch("ralph_service.jobs.time.time", side_effect=[1, 2, 3, 4]):
            self.hint()
            self.jobs.reconcile_hint(43, "master", "opened", engine_identity=IDENTITY)
            self.hint()
            self.assertEqual(self.jobs.claim_reconciliation()["number"], 43)
        stop = Mock()
        stop.is_set.side_effect = [False, False, True]
        with (
            patch.object(service, "consume_review_requests", side_effect=OSError()),
            patch.object(service, "reconcile_next") as reconcile,
            self.assertLogs(level="ERROR"),
        ):
            coordinator.command_worker(
                None,
                self.jobs,
                {
                    "core": {
                        "bot": self.config,
                        "forgejo_token": "token",
                        "high_engine": None,
                    }
                },
                stop,
            )
        reconcile.assert_called_once()

    def test_stale_forced_refresh_does_not_repeat_same_head_review(self):
        self.jobs.enqueue(
            42, "master", "a" * 40, "opened", True, engine_identity=IDENTITY
        )
        running = self.jobs.claim()
        service.reconcile_stale_review(self.jobs, 42, "master", "opened", IDENTITY)
        self.jobs.supersede(running)
        self.resolve("a" * 40)
        self.assertIsNone(self.jobs.claim())
