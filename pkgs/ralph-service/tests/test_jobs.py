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
        self.assertEqual(reopened.active_attempts(), [])
        job = reopened.claim()
        self.assertEqual(
            (job["number"], job["head"], job["generation"]), (42, "a" * 40, 1)
        )
        self.assertTrue(reopened.complete(job))
        self.assertTrue(reopened.release_attempt(job["id"], job["token"]))
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
        self.assertIsNone(self.store.claim())
        self.store.release_attempt(old["id"], old["token"])
        new = self.store.claim()
        self.assertEqual((new["head"], new["generation"]), ("b" * 40, 2))

    def test_recover_releases_claim_and_rejects_stale_token(self):
        self.enqueue()
        old = self.store.claim()
        restarted = jobs.JobStore(self.path, retry_base_seconds=0)
        self.assertEqual(len(restarted.active_attempts()), 1)
        self.assertIsNone(restarted.claim())
        self.assertTrue(restarted.release_attempt(old["id"], old["token"]))
        new = restarted.claim()
        self.assertEqual(new["id"], old["id"])
        self.assertNotEqual(new["token"], old["token"])
        self.assertFalse(restarted.save_result(old, {"content": "old"}))
        self.assertFalse(restarted.complete(old))
        self.assertIsNone(restarted.retry(old, "stale"))
        self.assertTrue(restarted.complete(new))

    def test_completed_and_retrying_workers_hold_capacity_until_unit_exit(self):
        store = jobs.JobStore(self.path, global_limit=1, retry_base_seconds=0)
        self.enqueue()
        old = store.claim()
        self.assertEqual(store.retry(old, TimeoutError()), "retry")
        self.assertIsNone(store.claim())
        self.assertEqual(store.active_attempts()[0]["attempt_token"], old["token"])
        self.assertFalse(store.release_attempt(old["id"], "wrong-token"))
        store.release_attempt(old["id"], old["token"])
        new = store.claim()
        self.assertNotEqual(new["token"], old["token"])
        self.assertFalse(store.release_attempt(old["id"], old["token"]))
        store.complete(new)
        self.enqueue("b" * 40)
        self.assertIsNone(store.claim())
        store.release_attempt(new["id"], new["token"])
        self.assertEqual(store.claim()["head"], "b" * 40)

    def test_dead_worker_with_saved_result_retries_publication(self):
        self.store.retry_base_seconds = 0
        self.enqueue()
        job = self.store.claim()
        result = {"content": "saved before publication"}
        self.store.save_result(job, result)
        self.store.release_attempt(job["id"], job["token"])
        retried = self.store.claim()
        self.assertEqual(retried["review_result"], result)
        self.assertNotEqual(retried["token"], job["token"])

    def test_crashed_workers_obey_backoff_and_attempt_limit(self):
        self.enqueue()
        first = self.store.claim()
        self.store.release_attempt(first["id"], first["token"])
        self.assertIsNone(self.store.claim())
        with self.store._connection(write=True) as db:
            row = db.execute(
                "SELECT status, attempts, next_attempt FROM jobs"
            ).fetchone()
            self.assertEqual((row["status"], row["attempts"]), ("retry", 1))
            self.assertGreater(row["next_attempt"], 0)
            db.execute("UPDATE jobs SET next_attempt = 0")
        self.store.retry_base_seconds = 0
        for _ in range(2):
            job = self.store.claim()
            self.store.release_attempt(job["id"], job["token"])
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.active_attempts(), [])
        with self.store._connection() as db:
            row = db.execute("SELECT status, attempts FROM jobs").fetchone()
            self.assertEqual(tuple(row), ("failed", 3))

    def test_worker_open_preserves_live_claim_and_does_not_migrate(self):
        self.enqueue()
        job = self.store.claim()
        worker = jobs.JobStore(self.path, initialize=False)
        self.assertEqual(worker.get_claim(job["id"], job["token"]), job)
        self.assertIsNone(worker.get_claim(job["id"], "wrong-token"))
        self.assertIsNone(
            worker.for_repository("gui").get_claim(job["id"], job["token"])
        )
        with self.store._connection(write=True) as db:
            db.execute("PRAGMA user_version = 1")
        with self.assertRaisesRegex(RuntimeError, "unsupported"):
            jobs.JobStore(self.path, initialize=False)
        with self.store._connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
        with self.assertRaisesRegex(RuntimeError, "drain legacy"):
            jobs.JobStore(self.path)
        with self.assertRaises(FileNotFoundError):
            jobs.JobStore(self.path.parent / "missing.sqlite", initialize=False)

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
        restarted = jobs.JobStore(
            self.path, repository_limits={"core": 2, "gui": 1}, retry_base_seconds=0
        )
        self.assertEqual(len(restarted.active_attempts()), 2)
        for job in (core, gui):
            self.assertTrue(restarted.release_attempt(job["id"], job["token"]))
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
        self.assertIsNone(self.queue.claim())
        self.queue.release_attempt(core["id"], core["token"])
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
