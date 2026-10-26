import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ralph import config as core_config
from ralph import engine, pipeline, spend
from ralph import research as frozen_research
from ralph_service import config, evaluate, forgejo


class StatsCliTests(unittest.TestCase):
    def test_stats_command_writes_a_dashboard_without_credentials_or_state_mutation(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "missing-state"
            reports = root / "public"
            self.assertEqual(
                evaluate.main(
                    [
                        "stats",
                        "--state-dir",
                        str(state),
                        "--report-dir",
                        str(reports),
                        "--repository-url",
                        "https://git.example.org/owner/repo",
                        "--report-base-url",
                        "https://review.example.org/traces",
                    ]
                ),
                0,
            )
            self.assertTrue((reports / "stats" / "index.html").is_file())
            json.loads((reports / "stats" / "stats.json").read_text())
            self.assertFalse(state.exists())


class EvaluateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.origin = self.root / "origin.git"
        self.work = self.root / "work"
        self.checkout = self.root / "state" / "evaluation-checkout"
        self.git(self.root, "init", "--bare", str(self.origin))
        self.git(self.root, "init", str(self.work))
        self.git(self.work, "config", "user.name", "Test")
        self.git(self.work, "config", "user.email", "test@example.com")
        self.git(self.work, "remote", "add", "origin", str(self.origin))
        (self.work / "code.txt").write_text("base\n", encoding="utf-8")
        self.git(self.work, "add", "code.txt")
        self.git(self.work, "commit", "-qm", "Base")
        self.git(self.work, "push", "origin", "HEAD:refs/heads/master")
        (self.work / "code.txt").write_text("head\n", encoding="utf-8")
        self.git(self.work, "add", "code.txt")
        self.git(self.work, "commit", "-qm", "Change")
        self.git(self.work, "push", "origin", "HEAD:refs/pull/42/head")
        self.bot_config = config.BotConfig(
            str(self.origin),
            "example/repo",
            "https://git.example.org/api/v1/repos/example/repo",
        )
        self.prompt_config = core_config.PromptConfig.load()
        self.review_engine = engine.load(
            "current",
            {
                "prompts": {
                    "instructions": self.prompt_config.instructions,
                    "audit_prompts": self.prompt_config.audit_prompts,
                },
                "profile": self.prompt_config.profile.to_dict(),
            },
        )

    def git(self, cwd, *args):
        return subprocess.run(
            ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    def capture(self):
        details = {
            "title": "Pinned input",
            "body": "Review this.",
            "base": {"ref": "master"},
            "head": {"sha": self.git(self.work, "rev-parse", "HEAD")},
        }

        def discussion(_bot_config, path):
            now = "2026-09-30T00:00:00Z"
            if path == "/issues/42":
                return {
                    "number": 42,
                    "title": "Pinned input",
                    "body": "Review this.",
                    "pull_request": {},
                    "created_at": now,
                    "updated_at": now,
                }
            if path in {
                "/issues/42/comments?limit=10&page=1",
                "/pulls/42/reviews?limit=10&page=1",
            }:
                return []
            raise AssertionError(f"unexpected discussion request {path}")

        with (
            patch.object(forgejo, "forgejo_request", return_value=details) as request,
            patch.object(forgejo, "public_discussion_request", side_effect=discussion),
        ):
            path = evaluate.capture_case(
                "token",
                self.checkout,
                self.root / "cases",
                42,
                self.bot_config,
                self.review_engine,
            )
        request.assert_called_once_with(self.bot_config, "token", "/pulls/42")
        return path

    def test_replay_retains_high_profile_and_scales_only_its_review_allowance(self):
        self.review_engine = engine.load("current", {"profile": "high"})
        manifest = self.capture()
        ledger = spend.Ledger(
            self.root / "spend.sqlite3", review_limit_usd=1, monthly_limit_usd=20
        )
        ppq_ledger = spend.Ledger(self.root / "ppq.sqlite3", review_limit_usd=2)

        def review(case, runtime, prompts, *args):
            self.assertEqual(prompts.profile.name, "high")
            self.assertEqual(runtime.budget.ledger.review_limit_micros, 5_000_000)
            self.assertEqual(runtime.budget.ledger.monthly_limit_micros, 20_000_000)
            self.assertEqual(runtime.ppq_budget.ledger.review_limit_micros, 2_000_000)
            return pipeline.ReviewResult("High review.", [], False)

        with patch.object(
            pipeline, "review_with_independent_passes", side_effect=review
        ):
            path = evaluate.run_case(
                manifest,
                "key",
                self.root / "runs",
                ledger,
                ppq_api_key="ppq-key",
                ppq_ledger=ppq_ledger,
            )
        result = json.loads(path.read_text())
        self.assertEqual(result["run"]["status"], "completed", result.get("error"))
        self.assertEqual(result["effective_config"]["review_budget_usd"], 5)
        self.assertEqual(ledger.review_limit_micros, 1_000_000)

    def test_replay_needs_no_forgejo_or_remote_git_and_keeps_labels_separate(self):
        manifest = self.capture()
        case = json.loads(manifest.read_text(encoding="utf-8"))
        labels = {case["case_id"]: ["Expected issue held out of model input"]}
        self.git(self.work, "commit", "--allow-empty", "-qm", "New PR head")
        self.git(self.work, "push", "--force", "origin", "HEAD:refs/pull/42/head")
        self.git(self.checkout, "fetch", "origin", "+refs/pull/42/head:refs/ralph/head")
        self.git(self.checkout, "reflog", "expire", "--expire=now", "--all")
        self.git(self.checkout, "gc", "--prune=now")
        self.assertEqual(
            self.git(self.checkout, "rev-parse", case["head_pin"]), case["head_sha"]
        )
        self.origin.rename(self.root / "unavailable-origin.git")
        seen = []
        ledger = spend.Ledger(self.root / "state" / "spend.sqlite3")

        def review(
            review_case,
            runtime,
            prompt_config,
            routing_mode,
            blind_alternatives,
            archaeology,
        ):
            self.assertEqual(review_case.review, case["review_input"])
            self.assertNotIn("Expected issue", review_case.review)
            self.assertEqual(review_case.snapshot.head_sha, case["head_sha"])
            self.assertEqual(review_case.snapshot.merge_base, case["merge_base"])
            self.assertEqual(prompt_config.models, self.prompt_config.models)
            self.assertIsNone(runtime.discussion_lookup)
            self.assertIs(runtime.allow_web_search, False)
            self.assertIsNotNone(review_case.research_evidence)
            self.assertIn(
                "read_current_pr_discussion",
                frozen_research.inventory(review_case.research_evidence),
            )
            self.assertEqual(routing_mode, "shadow")
            runtime.debug["stage_outputs"] = {"verifier": "raw verifier"}
            seen.append(review_case.snapshot)
            return pipeline.ReviewResult("Final review.", [], False)

        with (
            patch.object(
                forgejo, "forgejo_request", side_effect=AssertionError("Forgejo used")
            ),
            patch.object(
                pipeline, "review_with_independent_passes", side_effect=review
            ),
        ):
            first = evaluate.run_case(
                manifest,
                "openai-key",
                self.root / "runs",
                ledger,
                routing_mode="shadow",
                labels=labels,
            )
            second = evaluate.run_case(
                manifest,
                "openai-key",
                self.root / "runs",
                ledger,
                routing_mode="shadow",
                labels=labels,
            )
        self.assertEqual(len(seen), 2)
        self.assertNotEqual(first, second)
        self.assertIsNone(os.environ.get("GIT_NO_LAZY_FETCH"))
        for path in (first, second):
            result = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(result["run"]["status"], "completed", result.get("error"))
            self.assertEqual(
                result["output"]["data"]["stage_outputs"]["verifier"], "raw verifier"
            )
            self.assertEqual(result["expected_findings"], labels[case["case_id"]])
            self.assertIn("Final review.", result["content"])
            self.assertEqual(
                result["effective_config_sha256"],
                evaluate.digest(result["effective_config"]),
            )
            self.assertEqual(
                result["effective_config"]["engine"]["config"]["routing_mode"], "shadow"
            )
            self.assertFalse(result["code_changed_since_capture"])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
