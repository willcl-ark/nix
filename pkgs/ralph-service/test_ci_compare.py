import argparse
import copy
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import final_comment
from ralph import config as core_config
from ralph_service import ci_compare, evaluate


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "trusted"
        self.repository.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        source = self.repository / "pkgs" / "ralph"
        agents = source / "agents"
        shutil.copytree(core_config.DEFAULT_AGENTS_DIR, agents)
        for path in (agents, *agents.rglob("*")):
            path.chmod(0o700 if path.is_dir() else 0o600)
        templates = source / "final_comment"
        templates.mkdir()
        for name in final_comment.load_format():
            shutil.copyfile(
                Path(final_comment.__file__).parent / f"{name}.md",
                templates / f"{name}.md",
            )
        self.git("add", ".")
        self.git("commit", "-qm", "Baseline")
        self.base_sha = self.git("rev-parse", "HEAD")
        (source / "agents" / "independent" / "prompt.md").write_text(
            "Candidate prompt.\n"
        )
        template = templates / "comment.md"
        template.write_text(template.read_text() + "\nCandidate layout.\n")
        hostile_python = (
            "from pathlib import Path\n"
            f"Path({str(self.root / 'executed')!r}).touch()\n"
            "raise RuntimeError('Untrusted candidate code was imported')\n"
        )
        (templates / "__init__.py").write_text(hostile_python)
        (source / "agents" / "sitecustomize.py").write_text(hostile_python)
        (source / "agents" / "engine.json").write_text(
            '{"openai_api_base":"https://attacker.invalid/v1"}'
        )
        (self.repository / "flake.nix").write_text(
            'throw "Untrusted candidate Nix was evaluated"'
        )
        self.head_sha = self.commit_candidate()
        self.args = argparse.Namespace(
            repository_dir=self.repository,
            pull_request=17,
            head_sha=self.head_sha,
            github_repository="owner/repo",
            bitcoin_pr=42,
            proxy_url="https://proxy.example.invalid/v1",
            work_dir=self.root / "comparison",
        )
        self.pull = {
            "number": 17,
            "state": "open",
            "base": {"repo": {"full_name": "owner/repo"}},
            "head": {"sha": self.head_sha},
        }
        original_git = ci_compare._git

        def local_fetch(repository, *arguments):
            arguments = list(arguments)
            if arguments[0] == "fetch":
                arguments[-2] = str(self.repository)
            return original_git(repository, *arguments)

        self.fetch_patch = patch.object(ci_compare, "_git", side_effect=local_fetch)
        self.capture_patch = patch.object(
            evaluate, "capture_case", side_effect=self.capture
        )

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repository), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def commit_candidate(self):
        self.git("add", ".")
        self.git("commit", "-qm", "Candidate")
        head = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/pull/17/head", head)
        self.git("checkout", "--detach", self.base_sha)
        return head

    def capture(self, token, checkout, output_dir, number, bot, selected, **kwargs):
        self.assertIsNone(token)
        self.assertEqual(selected.config["openai_api_base"], self.args.proxy_url)
        self.assertEqual(bot.repository, "bitcoin/bitcoin")
        output_dir.mkdir(parents=True)
        path = output_dir / "case.json"
        evaluate.write_private_json(
            path, {"case_id": "case", "manifest_sha256": "frozen"}
        )
        return path

    def test_candidate_data_uses_trusted_runtime_and_both_sides_keep_artifacts(self):
        with (
            patch.object(ci_compare, "_pull_request", return_value=self.pull),
            self.fetch_patch,
            self.capture_patch as capture,
        ):
            self.assertEqual(ci_compare.prepare(self.args), 0)
        capture.assert_called_once()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base_sha)
        self.assertEqual(self.git("for-each-ref", "refs/ralph-ci"), "")
        data = self.args.work_dir / "data"
        self.assertFalse(list(data.rglob("*.py")))
        self.assertFalse(list(data.rglob("*.nix")))
        self.assertFalse(list(data.rglob("*.json")))
        self.assertFalse((self.root / "executed").exists())
        prepared = json.loads((self.args.work_dir / "prepared.json").read_text())
        base = prepared["engines"]["base"]["config"]
        head = prepared["engines"]["head"]["config"]
        self.assertNotEqual(base["prompts"], head["prompts"])
        self.assertNotEqual(base["comment_format"], head["comment_format"])
        self.assertEqual(base["openai_api_base"], head["openai_api_base"])
        self.assertEqual(head["openai_api_base"], self.args.proxy_url)
        key = self.root / "test-key"
        key.write_text("synthetic-test-key")
        seen = []

        def replay(manifest, api_key, output_dir, ledger, **kwargs):
            seen.append((manifest, kwargs["engine_name"], kwargs["engine_config"]))
            path = output_dir / "run.json"
            evaluate.write_private_json(
                path,
                {"run": {"status": "failed"}, "content": None}
                if len(seen) == 1
                else {"run": {"status": "completed"}, "content": "Head review"},
            )
            return path

        run_args = argparse.Namespace(
            work_dir=self.args.work_dir, openai_key_file=key, review_budget_usd=5
        )
        with patch.object(evaluate, "run_case", side_effect=replay):
            self.assertEqual(
                ci_compare.run(run_args),
                1,
            )
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0][0], seen[1][0])
        self.assertTrue(all(name == "current" for _, name, _ in seen))
        self.assertEqual([settings for _, _, settings in seen], [base, head])
        results = self.args.work_dir / "results"
        summary = json.loads((results / "summary.json").read_text())
        self.assertEqual(summary["sides"]["base"]["status"], "failed")
        self.assertEqual(summary["sides"]["head"]["status"], "completed")
        for side in summary["sides"].values():
            self.assertTrue((results / side["artifact"]).is_file())
            self.assertTrue((results / side["markdown"]).is_file())
        self.assertTrue((results / "summary.md").is_file())
        self.assertIn(
            (results / "head" / "review.md").read_text(),
            (results / "summary.md").read_text(),
        )
        for path in results.rglob("*"):
            if path.is_file():
                self.assertNotIn("synthetic-test-key", path.read_text())
        with (
            patch.object(evaluate, "run_case") as repeated,
            self.assertRaises(FileExistsError),
        ):
            ci_compare.run(run_args)
        repeated.assert_not_called()

    def test_unapproved_head_or_closed_or_wrong_repository_fails_before_capture(self):
        for changed in (
            {"head": {"sha": "0" * 40}},
            {"state": "closed"},
            {"base": {"repo": {"full_name": "another/repo"}}},
        ):
            with (
                self.subTest(changed=changed),
                patch.object(
                    ci_compare, "_pull_request", return_value={**self.pull, **changed}
                ),
                self.fetch_patch as git,
                self.capture_patch as capture,
                self.assertRaises(ValueError),
            ):
                ci_compare.prepare(self.args)
            capture.assert_not_called()
            self.assertFalse(
                any(call.args[1] == "fetch" for call in git.call_args_list)
            )
            self.assertFalse(self.args.work_dir.exists())

    def test_symlinked_prompt_and_fetched_sha_change_are_rejected(self):
        self.git("checkout", "--detach", self.head_sha)
        prompt = self.repository / "pkgs/ralph/agents/independent/prompt.md"
        prompt.unlink()
        prompt.symlink_to(self.root / "outside-prompt")
        symlink_head = self.commit_candidate()
        with (
            patch.object(ci_compare, "_pull_request", return_value=self.pull),
            self.fetch_patch,
            self.capture_patch as capture,
            self.assertRaisesRegex(ValueError, "Fetched PR HEAD"),
        ):
            ci_compare.prepare(self.args)
        capture.assert_not_called()
        args = copy.copy(self.args)
        args.work_dir = self.root / "symlink-comparison"
        args.head_sha = symlink_head
        with (
            patch.object(
                ci_compare,
                "_pull_request",
                return_value={**self.pull, "head": {"sha": symlink_head}},
            ),
            self.fetch_patch,
            self.capture_patch as capture,
            self.assertRaisesRegex(ValueError, "regular Git blob"),
        ):
            ci_compare.prepare(args)
        capture.assert_not_called()
        self.assertFalse((self.root / "outside-prompt").exists())


if __name__ == "__main__":
    unittest.main()
