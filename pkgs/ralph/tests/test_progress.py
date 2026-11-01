import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from ralph import progress


class ProgressTests(unittest.TestCase):
    def test_parallel_stages_keep_identity_and_reset_after_failure(self):
        barrier = threading.Barrier(2)

        def run(repo):
            with progress.stage({"repo": repo, "job": repo}, "gui", "model"):
                barrier.wait(timeout=5)
                progress.emit("request_retry", delay_seconds=1)
                if repo == "failed":
                    raise ValueError("private response text")

        with self.assertLogs("ralph.progress", level="DEBUG") as logs:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(run, repo) for repo in ("ok", "failed")]
                futures[0].result()
                with self.assertRaises(ValueError):
                    futures[1].result()
            progress.emit("outside")
        messages = [record.getMessage() for record in logs.records]
        for repo in ("ok", "failed"):
            events = [line for line in messages if f'repo="{repo}"' in line]
            self.assertEqual(len(events), 3)
            self.assertTrue(all(f'job="{repo}"' in line for line in events))
        self.assertTrue(any('status="failed"' in line for line in messages))
        self.assertTrue(any('status="returned"' in line for line in messages))
        self.assertNotIn("private response text", "\n".join(messages))
        self.assertNotIn("repo=", messages[-1])
