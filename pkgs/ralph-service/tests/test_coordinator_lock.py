import subprocess
import sys
import select
import tempfile
import unittest
from pathlib import Path

from ralph_service.jobs import JobStore


class CoordinatorLockTests(unittest.TestCase):
    def test_checkout_lock_serializes_separate_processes(self):
        script = """
import sys
from pathlib import Path
from ralph_service.service import checkout_lock
with checkout_lock(Path(sys.argv[1])):
    print("locked", flush=True)
    sys.stdin.read()
"""
        with tempfile.TemporaryDirectory() as directory:
            processes = []
            try:
                for _ in range(2):
                    processes.append(
                        subprocess.Popen(
                            [sys.executable, "-c", script, directory],
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                        )
                    )
                    if len(processes) == 1:
                        self.assertEqual(
                            processes[0].stdout.readline().strip(), "locked"
                        )
                self.assertEqual(
                    select.select([processes[1].stdout], [], [], 0.2)[0], []
                )
                processes[0].communicate(timeout=5)
                self.assertEqual(processes[1].stdout.readline().strip(), "locked")
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=5)

    def test_coordinator_restart_preserves_independent_worker_claim(self):
        script = """
import sys
import select
from pathlib import Path
from ralph_service.service import coordinator_lock
from ralph_service.jobs import JobStore
with coordinator_lock(Path(sys.argv[1])):
    jobs = JobStore(Path(sys.argv[1]) / "jobs.sqlite3")
    jobs.recover_reconciliation()
    print("locked", flush=True)
    sys.stdin.read()
"""
        with tempfile.TemporaryDirectory() as directory:
            owner = subprocess.Popen(
                [sys.executable, "-c", script, directory],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.addCleanup(owner.kill)
            try:
                self.assertEqual(owner.stdout.readline().strip(), "locked")
                store = JobStore(Path(directory) / "jobs.sqlite3")
                store.enqueue(
                    42,
                    "master",
                    "a" * 40,
                    "opened",
                    engine_identity={"name": "test", "config": {}},
                )
                claim = store.claim()
                contender = subprocess.run(
                    [sys.executable, "-c", script, directory],
                    input="",
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertNotEqual(contender.returncode, 0)
                self.assertIn("already owns", contender.stderr)
                self.assertTrue(store.is_current(claim))
                owner.kill()
                owner.wait(timeout=10)
                successor = subprocess.run(
                    [sys.executable, "-c", script, directory],
                    input="",
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(successor.returncode, 0, successor.stderr)
                self.assertEqual(successor.stdout.strip(), "locked")
                self.assertTrue(store.is_current(claim))
            finally:
                if owner.poll() is None:
                    owner.kill()
                owner.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
