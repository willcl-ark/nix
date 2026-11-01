import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ralph_service import jobs

ENGINE = {
    "name": "current",
    "code_sha256": "a" * 64,
    "config_sha256": "b" * 64,
    "config": {},
}


class JobStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "jobs.sqlite"
        self.store = jobs.JobStore(self.path)

    def enqueue(self, head="a" * 40, *, force=False):
        return self.store.enqueue(
            42, "master", head, "synchronize", force, engine_identity=ENGINE
        )

    def test_queued_work_survives_restart_and_duplicate_event_coalesces(self):
        self.assertTrue(self.enqueue())
        self.assertFalse(self.enqueue())
        reopened = jobs.JobStore(self.path)
        self.assertEqual(reopened.recover(), 0)
        job = reopened.claim()
        self.assertEqual(
            (job["number"], job["head"], job["generation"]), (42, "a" * 40, 1)
        )
        self.assertTrue(reopened.complete(job))
        self.assertFalse(
            reopened.enqueue(
                42, "master", "a" * 40, "synchronize", engine_identity=ENGINE
            )
        )
        alternate = {**ENGINE, "name": "narrative", "config_sha256": "c" * 64}
        self.assertFalse(
            reopened.enqueue(
                42, "master", "a" * 40, "synchronize", engine_identity=alternate
            )
        )
        self.assertFalse(
            reopened.enqueue(42, "other", "a" * 40, "edited", engine_identity=alternate)
        )
        self.assertIsNone(reopened.claim())
        self.assertTrue(
            reopened.enqueue(
                42,
                "master",
                "a" * 40,
                "synchronize",
                True,
                engine_identity=alternate,
            )
        )
        alternate_job = reopened.claim()
        self.assertEqual(
            (alternate_job["generation"], alternate_job["engine_identity"]),
            (2, alternate),
        )

    def test_same_head_hints_preserve_pending_running_and_retry_work(self):
        self.enqueue()
        alternate = {**ENGINE, "config_sha256": "c" * 64}

        def reconcile():
            self.store.reconcile_hint(
                42, "other", "synchronized", engine_identity=alternate
            )
            hint = self.store.claim_reconciliation()
            self.assertTrue(self.store.finish_reconciliation(hint, "other", "a" * 40))

        reconcile()
        job = self.store.claim()
        self.assertEqual(job["generation"], 1)
        self.assertEqual(job["base_ref"], "master")
        self.assertEqual(job["engine_identity"], ENGINE)
        reconcile()
        self.assertTrue(self.store.is_current(job))
        self.assertEqual(self.store.retry(job, TimeoutError()), "retry")
        with self.store._connection() as db:
            before = dict(
                db.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone()
            )
        reconcile()
        with self.store._connection() as db:
            after = dict(
                db.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone()
            )
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
        self.assertEqual(before, after)

    def test_newer_head_is_not_lost_when_older_claim_completes(self):
        self.enqueue()
        old = self.store.claim()
        self.assertTrue(self.enqueue("b" * 40))
        self.assertFalse(self.store.is_current(old))
        self.assertIsNone(self.store.claim())
        self.assertTrue(self.store.complete(old))
        new = self.store.claim()
        self.assertEqual((new["head"], new["generation"]), ("b" * 40, 2))

    def test_recover_releases_claim_and_rejects_stale_token(self):
        self.enqueue()
        old = self.store.claim()
        restarted = jobs.JobStore(self.path)
        self.assertEqual(restarted.recover(), 1)
        new = restarted.claim()
        self.assertEqual(new["id"], old["id"])
        self.assertNotEqual(new["token"], old["token"])
        self.assertFalse(restarted.save_result(old, {"content": "old"}))
        self.assertFalse(restarted.complete(old))
        self.assertIsNone(restarted.retry(old, "stale"))
        self.assertTrue(restarted.complete(new))

    def test_simultaneous_claims_get_distinct_jobs_once(self):
        self.enqueue()
        self.store.enqueue(43, "master", "b" * 40, "opened", engine_identity=ENGINE)
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda _: self.store.claim(), range(8)))
        self.assertEqual({claim["number"] for claim in claims if claim}, {42, 43})
        self.assertEqual(sum(claim is not None for claim in claims), 2)


class MultiRepositoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "jobs.sqlite3"
        self.queue = jobs.JobStore(
            self.path, global_limit=2, repository_limits={"core": 2, "gui": 1}
        )
        self.core = self.queue.for_repository("core")
        self.gui = self.queue.for_repository("gui")

    def enqueue(self, store, number=42, head="a"):
        return store.enqueue(
            number, "master", head * 40, "opened", engine_identity=None
        )

    def test_same_number_and_request_are_independent_across_restart(self):
        for store in (self.core, self.gui):
            self.assertTrue(
                store.reconcile_hint(
                    42, "master", "opened", engine_identity=None, request_id="event:1"
                )
            )
            hint = store.claim_reconciliation()
            self.assertTrue(store.finish_reconciliation(hint, "master", "a" * 40))
        core = self.queue.claim()
        gui = self.queue.claim()
        self.assertEqual({core["repo_id"], gui["repo_id"]}, {"core", "gui"})
        self.assertEqual(core["generation"], gui["generation"])
        self.enqueue(self.gui, head="b")
        self.assertTrue(self.core.is_current(core))
        self.assertFalse(self.gui.is_current(gui))
        with self.assertRaises(ValueError):
            self.gui.complete(core)
        restarted = jobs.JobStore(self.path, repository_limits={"core": 2, "gui": 1})
        self.assertEqual(restarted.recover(), 2)
        claimed = [restarted.claim(), restarted.claim()]
        self.assertEqual(
            {(j["repo_id"], j["head"]) for j in claimed},
            {("core", "a" * 40), ("gui", "b" * 40)},
        )
        for store in (
            restarted.for_repository("core"),
            restarted.for_repository("gui"),
        ):
            self.assertFalse(
                store.reconcile_hint(
                    42, "master", "opened", engine_identity=None, request_id="event:1"
                )
            )

    def test_global_admission_is_fair_and_caps_are_atomic(self):
        for number in range(5):
            self.enqueue(self.core, number)
            self.enqueue(self.gui, number)
        with ThreadPoolExecutor(max_workers=8) as pool:
            claimed = [
                job for job in pool.map(lambda _: self.queue.claim(), range(8)) if job
            ]
        self.assertEqual(len(claimed), 2)
        self.assertEqual({job["repo_id"] for job in claimed}, {"core", "gui"})
        core = next(job for job in claimed if job["repo_id"] == "core")
        self.queue.complete(core)
        # GUI is already at its cap, so another Core review can use spare capacity.
        self.assertEqual(self.queue.claim()["repo_id"], "core")
        self.assertIsNone(self.queue.claim())

    def test_reconciliation_epoch_is_scoped_to_repository(self):
        for store in (self.core, self.gui):
            store.reconcile_hint(42, "master", "opened", engine_identity=None)
        core = self.core.claim_reconciliation()
        gui = self.gui.claim_reconciliation()
        self.gui.reconcile_hint(42, "master", "synchronize", engine_identity=None)
        self.assertTrue(self.core.finish_reconciliation(core, "master", "a" * 40))
        self.assertFalse(self.gui.finish_reconciliation(gui, "master", "a" * 40))
        self.assertEqual(self.core.claim()["repo_id"], "core")
        self.assertIsNone(self.gui.claim())


if __name__ == "__main__":
    unittest.main()
