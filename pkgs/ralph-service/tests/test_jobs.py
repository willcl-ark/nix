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
        self.assertTrue(
            reopened.enqueue(
                42, "master", "a" * 40, "synchronize", engine_identity=alternate
            )
        )
        alternate_job = reopened.claim()
        self.assertEqual(
            (alternate_job["generation"], alternate_job["engine_identity"]),
            (2, alternate),
        )

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


if __name__ == "__main__":
    unittest.main()
