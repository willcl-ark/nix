"""Subprocess coverage for the local CLI without providers or forge access."""

import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
FAKE_ENGINE = """
import os
import signal
from ralph.engine import Output

def configure(value):
    return value

def run(case, runtime, configured):
    runtime.attempt['fake'] = {'snapshot': case.snapshot.head_sha}
    if configured.get('fail'):
        raise RuntimeError('deterministic failure')
    if configured.get('cancel'):
        os.kill(os.getpid(), signal.SIGTERM)
        runtime.check_current()
    data = {'native': 'arbitrary engine schema', 'source': case.source,
            'backport': case.backport_evidence}
    return Output('# Test review', data, {'custom': ['native']}, schema_version=17)
"""


class CLITest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.repo / "file").write_text("before\n")
        self.git("add", "file")
        self.git("commit", "-qm", "base")
        (self.repo / "file").write_text("after\n")
        self.git("commit", "-qam", "head")
        self.key = self.root / "key"
        self.key.write_text("test credential")
        self.output = self.root / "result.json"
        self.settings = self.root / "engine.json"
        (self.root / "fake_engine.py").write_text(FAKE_ENGINE)
        self.configure()

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def configure(self, **kwargs):
        self.settings.write_text(json.dumps(kwargs))

    def cli(self):
        env = {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join((str(PACKAGE), str(self.root))),
        }
        command = [
            sys.executable,
            str(PACKAGE / "review.py"),
            "review",
            "HEAD^..HEAD",
            str(self.repo),
            "--openai-key-file",
            str(self.key),
            "--engine",
            "fake_engine",
            "--engine-config",
            str(self.settings),
            "--output",
            str(self.output),
        ]
        return subprocess.run(
            command, check=False, capture_output=True, text=True, env=env, timeout=20
        )

    def result(self):
        return json.loads(self.output.read_text())

    def test_local_review_has_no_pr_or_ppq_requirement(self):
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "# Test review\n")
        data = self.result()
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["output"]["schema_version"], 17)
        self.assertEqual(data["output"]["exports"], {"custom": ["native"]})
        self.assertIsNone(data["output"]["data"]["source"])
        self.assertIsNone(data["run"]["ppq_budget"])
        self.assertEqual(
            data["run"]["allowances"], {"openai_usd": 1.0, "ppq_usd": None}
        )
        self.assertEqual(data["run"]["budget"]["estimated_total_usd"], 0)
        self.assertEqual(data["target"]["requested"], "HEAD^..HEAD")
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertNotIn("test credential", self.output.read_text())
        self.assertEqual(
            self.git("for-each-ref", "--format=%(refname)", "refs/ralph/local"), ""
        )

    def test_local_backport_retains_unresolved_provenance_without_network(self):
        original = self.git("rev-parse", "HEAD")
        self.git("commit", "--amend", "-qm", "Backport\n\nRebased-From: " + original)
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = self.result()["output"]["data"]["backport"]
        self.assertEqual(evidence["classification"]["mode"], "backport")
        self.assertEqual(evidence["commits"][0]["original"], original)
        self.assertFalse(evidence["complete"])
        self.assertTrue(evidence["limitations"])
        self.assertEqual(self.git("for-each-ref", "refs/ralph/upstream"), "")

    def test_high_profile_scales_only_openai_allowance(self):
        from unittest.mock import patch

        from ralph import cli, current_engine, engine

        def run(case, runtime, configured):
            self.assertEqual(configured["profile"]["name"], "high")
            self.assertEqual(runtime.budget.ledger.review_limit_micros, 10_000_000)
            self.assertEqual(runtime.ppq_budget.ledger.review_limit_micros, 3_000_000)
            return engine.Output("Review", {}, {})

        with patch.object(current_engine, "run", side_effect=run), patch("sys.stdout"):
            code = cli.main(
                [
                    "review",
                    "HEAD^..HEAD",
                    str(self.repo),
                    "--openai-key-file",
                    str(self.key),
                    "--ppq-key-file",
                    str(self.key),
                    "--review-budget-usd",
                    "2",
                    "--ppq-review-budget-usd",
                    "3",
                    "--profile",
                    "high",
                    "--output",
                    str(self.output),
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(
            self.result()["run"]["allowances"], {"openai_usd": 10.0, "ppq_usd": 3.0}
        )

    def test_failure_artifact(self):
        self.configure(fail=True)
        result = self.cli()
        self.assertEqual(result.returncode, 1, result.stderr)
        data = self.result()
        self.assertEqual(data["run"]["status"], "failed")
        self.assertEqual(data["error"]["type"], "RuntimeError")
        self.assertIn("fake", data["run"]["attempt"])
        self.assertFalse(result.stdout)

    def test_cooperative_signal_retains_artifact_and_exit_code(self):
        self.configure(cancel=True)
        result = self.cli()
        self.assertEqual(result.returncode, 128 + signal.SIGTERM, result.stderr)
        self.assertEqual(self.result()["run"]["status"], "cancelled")
        self.assertEqual(
            self.git("for-each-ref", "--format=%(refname)", "refs/ralph/local"), ""
        )


if __name__ == "__main__":
    unittest.main()
