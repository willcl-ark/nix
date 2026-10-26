"""Subprocess coverage for the local CLI without providers or forge access."""

import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import unittest


PACKAGE = Path(__file__).resolve().parent
FAKE_ENGINE = """
from pathlib import Path
import os
import signal
from ralph.engine import Output

def configure(value):
    if value.get('invalid'):
        raise ValueError('Invalid fake configuration')
    if value.get('cancel_input'):
        from ralph import repository
        original = repository.build_review_input
        def cancelled_input(*args, **kwargs):
            os.kill(os.getpid(), signal.SIGTERM)
            return original(*args, **kwargs)
        repository.build_review_input = cancelled_input
    if value.get('cancel_snapshot'):
        from ralph import repository
        original_snapshot = repository.snapshot_repository
        def cancelled_snapshot(*args, **kwargs):
            os.kill(os.getpid(), signal.SIGTERM)
            return original_snapshot(*args, **kwargs)
        repository.snapshot_repository = cancelled_snapshot
    return value

def run(case, runtime, configured):
    Path(configured['marker']).write_text('called')
    runtime.attempt['fake'] = {'snapshot': case.snapshot.head_sha}
    if configured.get('race'):
        Path(configured['race']).write_text('other process')
    if configured.get('fail'):
        raise RuntimeError('deterministic failure')
    if configured.get('cancel'):
        os.kill(os.getpid(), signal.SIGTERM)
        runtime.check_current()
    data = {'native': 'arbitrary engine schema', 'source': case.source}
    if configured.get('non_json'):
        data['bad'] = {1, 2}
    if configured.get('nan'):
        data['bad'] = float('nan')
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
        self.marker = self.root / "called"
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
        self.settings.write_text(json.dumps({"marker": str(self.marker), **kwargs}))

    def cli(self, *options, target="HEAD^..HEAD", output=True, engine="fake_engine"):
        env = {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join((str(PACKAGE), str(self.root))),
        }
        command = [
            sys.executable,
            str(PACKAGE / "review.py"),
            "review",
            target,
            str(self.repo),
            "--openai-key-file",
            str(self.key),
            "--engine",
            engine,
            "--engine-config",
            str(self.settings),
            *map(str, options),
        ]
        if output:
            command += ["--output", str(self.output)]
        return subprocess.run(
            command, capture_output=True, text=True, env=env, timeout=20
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

    def test_output_is_optional(self):
        result = self.cli(output=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "# Test review\n")
        self.assertFalse(self.output.exists())

    def test_branch_comparison_ignores_dirty_worktree(self):
        (self.repo / "file").write_text("uncommitted changes\n")
        result = self.cli("--base", "HEAD^", target="HEAD")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = self.result()
        self.assertEqual(data["target"]["comparison"], "merge_base")
        self.assertEqual(data["target"]["base"], "HEAD^")
        self.assertEqual(data["diff_base_sha"], self.git("rev-parse", "HEAD^"))
        self.assertEqual((self.repo / "file").read_text(), "uncommitted changes\n")

    def test_failure_artifact(self):
        self.configure(fail=True)
        result = self.cli()
        self.assertEqual(result.returncode, 1, result.stderr)
        data = self.result()
        self.assertEqual(data["run"]["status"], "failed")
        self.assertEqual(data["error"]["type"], "RuntimeError")
        self.assertIn("fake", data["run"]["attempt"])
        self.assertFalse(result.stdout)

    def test_non_json_engine_outputs_fail(self):
        for flag in ("non_json", "nan"):
            with self.subTest(flag=flag):
                self.output.unlink(missing_ok=True)
                self.configure(**{flag: True})
                result = self.cli()
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(self.result()["run"]["status"], "failed")

    def test_existing_destination_prevents_execution(self):
        self.output.write_text("private existing artifact")
        result = self.cli()
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.output.read_text(), "private existing artifact")

    def test_destination_race_does_not_clobber(self):
        self.configure(race=str(self.output))
        result = self.cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.output.read_text(), "other process")
        self.assertFalse(list(self.root.glob(".result.json.*")))

    def test_invalid_inputs_do_not_execute(self):
        for target, options in [
            ("HEAD", ()),
            ("HEAD...", ()),
            ("..HEAD", ()),
            ("HEAD^..HEAD", ("--base", "HEAD^")),
            ("HEAD^..HEAD", ("--archaeology", "on")),
            ("HEAD^..HEAD", ("--ppq-key-file", self.key)),
            ("HEAD^..HEAD", ("--review-budget-usd", "nan")),
        ]:
            with self.subTest(target=target, options=options):
                result = self.cli(*options, target=target)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(self.marker.exists())

    def test_engine_configuration_checked_before_execution(self):
        self.configure(invalid=True)
        result = self.cli()
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.marker.exists())

    def test_context_validation_before_execution(self):
        context = self.root / "context.json"
        for value in (
            {"schema_version": 2},
            {"schema_version": 1, "title": []},
            {
                "schema_version": 1,
                "source": {
                    "kind": "pull_request",
                    "url": "https://example.invalid/pr/1",
                },
            },
        ):
            context.write_text(json.dumps(value))
            result = self.cli("--context", context)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(self.marker.exists())

    def test_current_engine_forced_archaeology_requires_context(self):
        result = self.cli("--archaeology", "on", engine="current")
        self.assertEqual(result.returncode, 2)
        self.assertIn("frozen discussion evidence", result.stderr)
        self.assertFalse(self.marker.exists())

    def test_invalid_research_evidence_before_execution(self):
        evidence = self.root / "research.json"
        evidence.write_text('{"schema_version": 1}')
        result = self.cli("--research-evidence", evidence)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.marker.exists())

    def test_no_changes_does_not_execute(self):
        result = self.cli(target="HEAD..HEAD")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.result()["run"]["status"], "no_changes")

    def test_oversized_input_is_recorded_without_execution(self):
        context = self.root / "context.json"
        context.write_text(
            json.dumps({"schema_version": 1, "rationale": "x" * 210_000})
        )
        result = self.cli("--context", context)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.result()["run"]["status"], "skipped")

    def test_cooperative_signal_retains_artifact_and_exit_code(self):
        self.configure(cancel=True)
        result = self.cli()
        self.assertEqual(result.returncode, 128 + signal.SIGTERM, result.stderr)
        self.assertEqual(self.result()["run"]["status"], "cancelled")
        self.assertEqual(
            self.git("for-each-ref", "--format=%(refname)", "refs/ralph/local"), ""
        )

    def test_signal_during_input_cleans_pins_and_writes_diagnostics(self):
        self.configure(cancel_input=True)
        result = self.cli()
        self.assertEqual(result.returncode, 128 + signal.SIGTERM, result.stderr)
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.result()["run"]["status"], "cancelled")
        self.assertEqual(
            self.git("for-each-ref", "--format=%(refname)", "refs/ralph/local"), ""
        )
        self.assertFalse(list(self.root.glob(".result.json.*")))

    def test_signal_during_git_preflight_cleans_pins_and_writes_diagnostics(self):
        self.configure(cancel_snapshot=True)
        result = self.cli()
        self.assertEqual(result.returncode, 128 + signal.SIGTERM, result.stderr)
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.result()["run"]["status"], "cancelled")
        self.assertEqual(
            self.git("for-each-ref", "--format=%(refname)", "refs/ralph/local"), ""
        )
        self.assertFalse(list(self.root.glob(".result.json.*")))


if __name__ == "__main__":
    unittest.main()
