import json
import os
import shutil
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

    def test_stats_publishes_quota_snapshot_without_publishing_management_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / "management-key"
            key.write_text("private-management-key\n")
            reports = root / "public"
            snapshot = {
                "status": "ok",
                "checked_at": "2026-10-26T12:00:00Z",
                "accounts": [
                    {
                        "provider": "codex",
                        "status": "ok",
                        "plan": "pro",
                        "windows": [
                            {"name": "Weekly", "used_percent": 25, "resets_at": None}
                        ],
                    }
                ],
            }
            args = [
                "stats",
                "--state-dir",
                str(root / "state"),
                "--report-dir",
                str(reports),
                "--repository-url",
                "https://git.example.org/a/b",
                "--report-base-url",
                "https://review.example.org/traces",
                "--proxy-management-url",
                "http://127.0.0.1:8317",
                "--proxy-management-key-file",
                str(key),
            ]
            with patch.object(
                evaluate.proxy_usage, "collect_usage", return_value=snapshot
            ) as collect:
                self.assertEqual(evaluate.main(args), 0)
            collect.assert_called_once_with(
                "http://127.0.0.1:8317", "private-management-key"
            )
            saved = json.loads((reports / "stats/stats.json").read_text())
            self.assertEqual(saved["subscriptions"], snapshot)
            for path in (reports / "stats").rglob("*"):
                if path.is_file():
                    self.assertNotIn("private-management-key", path.read_text())
            key.unlink()
            self.assertEqual(evaluate.main(args), 0)
            saved = json.loads((reports / "stats/stats.json").read_text())
            self.assertEqual(saved["subscriptions"]["status"], "unavailable")


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

    def capture(self, labels=()):
        details = {
            "title": "Pinned input",
            "body": "Review this.",
            "labels": [{"name": label} for label in labels],
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

    def test_backport_replay_preserves_sources_offline_and_checks_packet_hash(self):
        self.git(self.work, "commit", "--allow-empty", "-qm", "Upstream original")
        original = self.git(self.work, "rev-parse", "HEAD")
        self.git(self.work, "push", "origin", "HEAD:refs/heads/upstream")
        packet = {
            "complete": False,
            "limitations": ["One source PR is unavailable"],
            "commits": [{"original": original}],
            "sources": {},
        }

        def acquire(snapshot, metadata, bot_config, prefix):
            self.assertEqual(metadata["labels"], ["Backport"])
            self.git(snapshot.checkout, "fetch", "origin", original)
            return packet

        with patch.object(
            evaluate.repository, "collect_backport_evidence", side_effect=acquire
        ):
            manifest = self.capture(labels=["Backport"])
        case = json.loads(manifest.read_text())
        self.assertEqual(case["backport_evidence"], packet)
        self.git(self.checkout, "reflog", "expire", "--expire=now", "--all")
        self.git(self.checkout, "gc", "--prune=now")
        self.assertEqual(
            self.git(self.checkout, "rev-parse", case["backport_pins"][original]),
            original,
        )
        self.origin.rename(self.root / "unavailable-origin.git")
        ledger = spend.Ledger(self.root / "spend.sqlite3")

        def review(review_case, runtime, *args, comment_format):
            self.assertEqual(review_case.backport_evidence, packet)
            runtime.debug["backport_assessment"] = {"verdict": "Needs clarification"}
            return pipeline.ReviewResult("Backport review", [], False)

        with (
            patch.object(
                forgejo, "_github_api", side_effect=AssertionError("GitHub used")
            ),
            patch.object(
                forgejo, "forgejo_request", side_effect=AssertionError("Forgejo used")
            ),
            patch.object(
                pipeline, "review_with_independent_passes", side_effect=review
            ) as reviewed,
        ):
            result = json.loads(
                evaluate.run_case(
                    manifest, "key", self.root / "runs", ledger
                ).read_text()
            )
            self.assertEqual(result["run"]["status"], "completed", result.get("error"))
            reviewed.assert_called_once()
            case["backport_evidence"]["complete"] = True
            case["manifest_sha256"] = evaluate.digest(
                {key: value for key, value in case.items() if key != "manifest_sha256"}
            )
            manifest.write_text(json.dumps(case))
            result = json.loads(
                evaluate.run_case(
                    manifest, "key", self.root / "runs", ledger
                ).read_text()
            )
            self.assertEqual(
                result["error"]["message"], "Frozen backport evidence hash mismatch"
            )
            reviewed.assert_called_once()
            case["backport_evidence"]["complete"] = False
            case["manifest_sha256"] = evaluate.digest(
                {key: value for key, value in case.items() if key != "manifest_sha256"}
            )
            manifest.write_text(json.dumps(case))
            self.git(self.checkout, "update-ref", "-d", case["backport_pins"][original])
            result = json.loads(
                evaluate.run_case(
                    manifest, "key", self.root / "runs", ledger
                ).read_text()
            )
            self.assertEqual(result["run"]["status"], "failed")
            reviewed.assert_called_once()

    def test_feedback_replay_preserves_memory_offline_and_rejects_tampering(self):
        head = self.git(self.work, "rev-parse", "HEAD")
        packet = {
            "schema_version": 1,
            "repository": self.bot_config.repository,
            "new_head": head,
            "number": 42,
            "findings": [
                {
                    "key": "1:1:F1",
                    "title": "Known limitation",
                    "severity": "major",
                    "body": "Still unresolved",
                }
            ],
            "comments": [{"id": "2", "body": "Addressing elsewhere"}],
            "revisions": {"new_head": head, "diff": "Frozen revision difference"},
        }
        with patch.object(
            evaluate.review_feedback, "collect_review_feedback", return_value=packet
        ) as capture:
            manifest = self.capture()
        self.assertEqual(
            capture.call_args.kwargs["cutoff"],
            json.loads(manifest.read_text())["discussion_cutoff"],
        )
        original = json.loads(manifest.read_text())
        self.origin.rename(self.root / "unavailable-origin.git")
        ledger = spend.Ledger(self.root / "spend.sqlite3")

        def review(case, runtime, *args, comment_format):
            self.assertEqual(case.review_feedback, packet)
            return pipeline.ReviewResult("Known concern remains.", [], False)

        with (
            patch.object(
                forgejo, "forgejo_request", side_effect=AssertionError("Network")
            ),
            patch.object(
                evaluate.review_feedback,
                "collect_review_feedback",
                side_effect=AssertionError("History reread"),
            ),
            patch.object(
                pipeline, "review_with_independent_passes", side_effect=review
            ) as reviewed,
        ):
            result = json.loads(
                evaluate.run_case(
                    manifest, "key", self.root / "runs", ledger
                ).read_text()
            )
            self.assertEqual(result["run"]["status"], "completed", result.get("error"))
            reviewed.assert_called_once()
            for field, value in (
                ("number", 43),
                ("repository", "other/repo"),
                ("head", "a" * 40),
                ("hash", "changed"),
            ):
                with self.subTest(field=field):
                    changed = json.loads(json.dumps(original))
                    feedback = changed["review_feedback"]
                    if field == "head":
                        feedback["revisions"]["new_head"] = value
                    elif field == "hash":
                        feedback["comments"][0]["body"] = value
                    else:
                        feedback[field] = value
                    if field != "hash":
                        changed["review_feedback_sha256"] = evaluate.digest(feedback)
                    changed["manifest_sha256"] = evaluate.digest(
                        {
                            key: value
                            for key, value in changed.items()
                            if key != "manifest_sha256"
                        }
                    )
                    manifest.write_text(json.dumps(changed))
                    result = json.loads(
                        evaluate.run_case(
                            manifest, "key", self.root / "runs", ledger
                        ).read_text()
                    )
                    self.assertEqual(
                        result["error"]["message"],
                        "Frozen review feedback hash mismatch"
                        if field == "hash"
                        else "Frozen review feedback scope mismatch",
                    )
                    reviewed.assert_called_once()

    def test_replay_refuses_source_objects_acquired_after_capture(self):
        self.git(self.work, "commit", "--allow-empty", "-qm", "Uncaptured original")
        original = self.git(self.work, "rev-parse", "HEAD")
        self.git(self.work, "push", "origin", "HEAD:refs/heads/upstream")
        packet = {
            "complete": False,
            "limitations": ["Original unavailable"],
            "commits": [],
            "sources": {"example/repo#1": {"commits": [original]}},
        }
        with patch.object(
            evaluate.repository, "collect_backport_evidence", return_value=packet
        ):
            manifest = self.capture(labels=["Backport"])
        captured = json.loads(manifest.read_text())
        self.assertEqual(captured["backport_pins"], {})
        self.git(self.checkout, "fetch", "origin", original)
        ledger = spend.Ledger(self.root / "spend.sqlite3")
        with patch.object(pipeline, "review_with_independent_passes") as review:
            result = json.loads(
                evaluate.run_case(
                    manifest, "key", self.root / "runs", ledger
                ).read_text()
            )
        review.assert_not_called()
        self.assertEqual(
            result["error"]["message"],
            "Previously unavailable frozen backport object is now present",
        )

    def test_replay_retains_high_profile_and_scales_only_its_review_allowance(self):
        agents = self.root / "agents"
        shutil.copytree(
            core_config.DEFAULT_AGENTS_DIR, agents, copy_function=shutil.copyfile
        )
        prompt = agents / "independent" / "prompt.md"
        prompt.write_text("Captured review instructions.\n")
        self.review_engine = engine.load(
            "current", {"profile": "high", "agents_dir": agents}
        )
        manifest = self.capture()
        prompt.write_text("Edited review instructions.\n")
        settings = agents / "verifier" / "config.toml"
        captured_timeout = self.review_engine.config["profile"]["stages"]["verifier"][
            "timeout"
        ]
        settings.write_text(
            settings.read_text().split("[profiles.high]", 1)[0]
            + f"\n[profiles.high]\ntimeout = {captured_timeout + 1}\n"
        )
        ledger = spend.Ledger(
            self.root / "spend.sqlite3", review_limit_usd=1, monthly_limit_usd=20
        )
        seen = []

        def review(case, runtime, prompts, *args, comment_format):
            self.assertEqual(prompts.profile.name, "high")
            self.assertEqual(runtime.budget.ledger.review_limit_micros, 5_000_000)
            self.assertEqual(runtime.budget.ledger.monthly_limit_micros, 20_000_000)
            seen.append(
                (prompts.instructions, prompts.profile.stages["verifier"].timeout)
            )
            return pipeline.ReviewResult("High review.", [], False)

        with patch.object(
            pipeline, "review_with_independent_passes", side_effect=review
        ):
            path = evaluate.run_case(
                manifest,
                "key",
                self.root / "runs",
                ledger,
            )
            override_path = evaluate.run_case(
                manifest,
                "key",
                self.root / "runs",
                ledger,
                agents_dir=agents,
            )
        result = json.loads(path.read_text())
        self.assertEqual(result["run"]["status"], "completed", result.get("error"))
        override_result = json.loads(override_path.read_text())
        self.assertEqual(
            override_result["run"]["status"], "completed", override_result.get("error")
        )
        self.assertEqual(
            seen,
            [
                ("Captured review instructions.", captured_timeout),
                ("Edited review instructions.", captured_timeout + 1),
            ],
        )
        self.assertNotEqual(
            result["effective_config_sha256"],
            override_result["effective_config_sha256"],
        )
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
            *,
            comment_format,
        ):
            self.assertEqual(review_case.review, case["review_input"])
            self.assertNotIn("Expected issue", review_case.review)
            self.assertEqual(review_case.snapshot.head_sha, case["head_sha"])
            self.assertEqual(review_case.snapshot.merge_base, case["merge_base"])
            self.assertEqual(prompt_config.models, self.prompt_config.models)
            self.assertIsNone(runtime.discussion_lookup)
            self.assertIs(runtime.allow_web_search, False)
            self.assertIsNone(review_case.review_feedback)
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
