"""Experiment execution is isolated and cannot turn setup failures into evidence."""

import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ralph import experiments, repository
from ralph.model import StaleReview


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        repository.git(self.repo, "init", "-q")
        repository.git(self.repo, "config", "user.name", "Test")
        repository.git(self.repo, "config", "user.email", "test@example.org")
        (self.repo / "code.txt").write_text("base\n")
        (self.repo / ".gitignore").write_text("ignored-test.py\n")
        (self.repo / ".gitattributes").write_text("code.txt export-ignore\n")
        repository.git(self.repo, "add", ".")
        repository.git(self.repo, "commit", "-qm", "base")
        base = repository.git(self.repo, "rev-parse", "HEAD").strip()
        (self.repo / "code.txt").write_text("head\n")
        repository.git(self.repo, "commit", "-qam", "head")
        head = repository.git(self.repo, "rev-parse", "HEAD").strip()
        self.snapshot = SimpleNamespace(
            checkout=self.repo, head_sha=head, diff_base_sha=base
        )

    def test_unavailable_sessions_are_not_execution_evidence(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            experiments.ExperimentSession(self.snapshot, ["F1"]) as session,
        ):
            result = json.loads(
                session.dispatch(
                    "experiment_run",
                    {
                        "finding_id": "F1",
                        "side": "head",
                        "command": "exit 0",
                    },
                )
            )
            self.assertEqual(result["status"], "unavailable")
            self.assertIsNone(result["exit_code"])

    def test_cancellation_kills_a_running_process_without_remote_checks(self):
        start = time.monotonic()

        def cancel():
            if time.monotonic() - start >= 0.2:
                raise StaleReview("new local generation")

        local = Mock(side_effect=cancel)
        remote = Mock(side_effect=AssertionError("remote check inside polling loop"))
        session = experiments.ExperimentSession(
            self.snapshot, ["F1"], check_cancelled=local, check_current=remote
        )
        with self.assertRaises(StaleReview):
            session._run(
                [sys.executable, "-c", "import time; time.sleep(30)"], limit=1024
            )
        self.assertLess(time.monotonic() - start, 3)
        self.assertGreater(local.call_count, 1)
        remote.assert_not_called()
        session.check_cancelled = None
        session.deadline = time.monotonic() + 0.1
        result = session._run(
            [sys.executable, "-c", "import time; time.sleep(30)"], limit=1024
        )
        self.assertEqual(result[0], "timeout")
        remote.assert_not_called()

    def test_source_change_without_local_cancellation_aborts_completed_experiment(self):
        current = True

        def validate():
            if not current:
                raise StaleReview("remote head changed")

        remote = Mock(side_effect=validate)
        local = Mock()
        session = experiments.ExperimentSession(
            self.snapshot, ["F1"], check_cancelled=local, check_current=remote
        )
        session.prlimit = session.bwrap = session.python = session.launcher = (
            session.bash
        ) = "unused"

        def run_isolated(_argv, _entry):
            nonlocal current
            result = session._run(
                [sys.executable, "-c", "import time; time.sleep(0.25)"], limit=1024
            )
            self.assertEqual(result[0], "completed")
            self.assertEqual(remote.call_count, 1)
            self.assertGreater(local.call_count, 1)
            current = False
            return result

        with (
            patch.object(session, "_prepare"),
            patch.object(session, "_archive", return_value=self.root / "source.tar"),
            patch.object(session, "_run_isolated", side_effect=run_isolated),
            self.assertRaises(StaleReview),
        ):
            session.dispatch(
                "experiment_run",
                {"finding_id": "F1", "side": "head", "command": "true"},
            )
        self.assertEqual(remote.call_count, 2)

    def test_bounded_output_stops_unbounded_producer(self):
        session = experiments.ExperimentSession(self.snapshot, ["F1"])
        status, _, output, truncated = session._run(
            [sys.executable, "-c", "import os;\nwhile True: os.write(1,b'x'*65536)"],
            limit=1024,
        )
        self.assertEqual(status, "error")
        self.assertEqual(len(output), 1024)
        self.assertTrue(truncated)
        archive = io.BytesIO()
        status, code, _, _ = session._run(
            [
                sys.executable,
                "-c",
                "import os; os.write(1,b'archive'); os.write(2,b'warning')",
            ],
            limit=1024,
            sink=archive,
        )
        self.assertEqual(
            (status, code, archive.getvalue()), ("completed", 0, b"archive")
        )

    def test_real_sandbox_edits_are_disposable_and_capture_new_tests(self):
        if not os.environ.get("RALPH_WRITE_VERIFIER_CGROUP"):
            self.skipTest("a delegated cgroup is required for real sandbox execution")
        if not shutil.which("bwrap") or not str(
            Path(sys.executable).resolve()
        ).startswith("/nix/store/"):
            self.skipTest("Nix Python and bubblewrap required")
        # coreutils env provides a tiny prepared-shell substitute without fetching
        # Bix or invoking the Nix daemon. Commands below use only Bash builtins.
        env_path = Path(shutil.which("env")).resolve().parent / "env"
        if not str(env_path).startswith("/nix/store/"):
            self.skipTest("Nix coreutils required")
        secret = self.root / "service-secret"
        secret.write_text("do not expose")
        with (
            patch.dict(
                os.environ,
                {
                    "RALPH_WRITE_VERIFIER_SHELL": str(env_path),
                    "RALPH_OPENAI_API_KEY": "host-secret",
                },
            ),
            experiments.ExperimentSession(self.snapshot, ["F1"]) as session,
        ):

            def run(script, side="head"):
                return json.loads(
                    session.dispatch(
                        "experiment_run",
                        {
                            "finding_id": "F1",
                            "side": side,
                            "command": script,
                        },
                    )
                )

            probe = run("printf ready")
            if probe["status"] != "completed":
                # Nix build sandboxes often disallow nested user namespaces.
                error = probe.get("error", "") + probe.get("output", "")
                if "Operation not permitted" in error or "No permissions" in error:
                    self.skipTest("kernel/build sandbox disallows user namespaces")
                self.fail(str(probe))
            result = run(
                f"set -eu; test ! -e {secret}; test ! -e /run; "
                'test -z "${RALPH_OPENAI_API_KEY:-}"; '
                "if printf forged >/proc/1/fd/1; then exit 10; fi; "
                "if printf x >/dev/unbounded; then exit 11; fi; "
                'read value < code.txt; test "$value" = head || exit 9; '
                "printf 'fixed\\n' > code.txt; "
                f"{Path(shutil.which('chmod')).resolve().parent / 'chmod'} +x code.txt; "
                "printf 'assert True\\n' > ignored-test.py; "
                "printf 'test failed as expected\\n'; (while :; do :; done) & exit 7"
            )
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual(result["exit_code"], 7)
            self.assertIn("test failed as expected", result["output"])
            self.assertIn("+fixed", result["patch"])
            self.assertIn("old mode 100644\nnew mode 100755", result["patch"])
            self.assertIn("+assert True", result["patch"])
            self.assertTrue(result["capture_complete"])
            self.assertEqual((self.repo / "code.txt").read_text(), "head\n")
            fresh = run(
                'read value < code.txt; test "$value" = head; test ! -e ignored-test.py'
            )
            self.assertEqual(fresh["exit_code"], 0)
            base = run('read value < code.txt; test "$value" = base', side="base")
            self.assertEqual(base["exit_code"], 0)
            self.assertEqual(base["revision"], self.snapshot.diff_base_sha)
            # Aggregate OOM kills only this experiment, even when each child
            # stays below its individual address-space limit.
            child = (
                f"{Path(sys.executable).resolve()} -c "
                "'import time; data=bytearray(80*1024*1024); time.sleep(10)'"
            )
            with patch.object(experiments, "WORK_BYTES", 128 * 1024 * 1024):
                exhausted = run(f"{child} & {child} & wait")
            self.assertEqual(exhausted["status"], "resource_limit", exhausted)
            self.assertGreater(exhausted["resource_events"]["memory"]["oom_kill"], 0)
            self.assertNotIn("capture_complete", exhausted)
            self.assertEqual(run("printf survived")["status"], "completed")
            self.assertFalse(list(session.cgroup_root.glob("experiment-*")))


if __name__ == "__main__":
    unittest.main()
