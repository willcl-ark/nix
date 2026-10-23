import contextlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ralph import config as core_config
from ralph import research as frozen_research
from ralph import trace as core_trace
from ralph_service import config, evaluate, forgejo, research, trace
from ralph import engine, pipeline, spend


class StatsCliTests(unittest.TestCase):
    def test_stats_command_writes_a_dashboard_without_credentials_or_state_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "missing-state"
            reports = root / "public"
            self.assertEqual(evaluate.main([
                "stats", "--state-dir", str(state), "--report-dir", str(reports),
                "--repository-url", "https://git.example.org/owner/repo",
                "--report-base-url", "https://review.example.org/traces",
            ]), 0)
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
        self.bot_config = config.BotConfig(str(self.origin), "example/repo",
            "https://git.example.org/api/v1/repos/example/repo")
        self.prompt_config = core_config.PromptConfig.load()
        self.review_engine = engine.load("current", {
            "prompts": {"instructions": self.prompt_config.instructions,
                        "audit_prompts": self.prompt_config.audit_prompts,
                        "models": self.prompt_config.models}})

    def git(self, cwd, *args):
        return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def capture(self):
        details = {"title": "Pinned input", "body": "Review this.",
                   "base": {"ref": "master"},
                   "head": {"sha": self.git(self.work, "rev-parse", "HEAD")}}
        def discussion(_bot_config, path):
            now = "2026-09-30T00:00:00Z"
            if path == "/issues/42":
                return {"number": 42, "title": "Pinned input", "body": "Review this.",
                        "pull_request": {}, "created_at": now, "updated_at": now}
            if path in {"/issues/42/comments?limit=10&page=1",
                        "/pulls/42/reviews?limit=10&page=1"}:
                return []
            raise AssertionError(f"unexpected discussion request {path}")

        with patch.object(forgejo, "forgejo_request", return_value=details) as request, \
                patch.object(forgejo, "public_discussion_request",
                             side_effect=discussion):
            path = evaluate.capture_case("token", self.checkout, self.root / "cases", 42,
                                         self.bot_config, self.review_engine)
        request.assert_called_once_with(self.bot_config, "token", "/pulls/42")
        return path

    def test_capture_pins_complete_git_objects_and_frozen_configuration(self):
        path = self.capture()
        case = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(case["checkout"], str(self.checkout.resolve()))
        self.assertEqual(case["head_sha"], self.git(self.work, "rev-parse", "HEAD"))
        self.assertEqual(case["merge_base"], case["base_sha"])
        self.assertIn("Pinned input", case["review_input"])
        self.assertIn("+head", case["review_input"])
        self.assertEqual(case["title"], "Pinned input")
        self.assertEqual(case["description"], "Review this.")
        self.assertEqual(case["config"]["engine"]["config"]["prompts"]["models"],
                         self.prompt_config.models)
        self.assertEqual(case["config_sha256"], evaluate.digest(case["config"]))
        self.assertEqual(case["research_evidence_sha256"],
                         evaluate.digest(case["research_evidence"]))
        self.assertIn("read_current_pr_discussion",
                      frozen_research.inventory(case["research_evidence"]))
        self.assertEqual(case["source_code_sha256"], evaluate.source_code_sha256())
        self.assertEqual(self.git(self.checkout, "rev-parse", case["head_pin"]),
                         case["head_sha"])
        self.assertEqual(case["manifest_sha256"], evaluate.digest({
            key: value for key, value in case.items() if key != "manifest_sha256"}))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        with evaluate.offline_git():
            evaluate.verify_local_objects(self.checkout.resolve(),
                                          case["base_sha"], case["head_sha"])

    def test_replay_needs_no_forgejo_or_remote_git_and_keeps_labels_separate(self):
        manifest = self.capture()
        case = json.loads(manifest.read_text(encoding="utf-8"))
        labels = {case["case_id"]: ["Expected issue held out of model input"]}
        self.git(self.work, "commit", "--allow-empty", "-qm", "New PR head")
        self.git(self.work, "push", "--force", "origin", "HEAD:refs/pull/42/head")
        self.git(self.checkout, "fetch", "origin",
                 "+refs/pull/42/head:refs/ralph/head")
        self.git(self.checkout, "reflog", "expire", "--expire=now", "--all")
        self.git(self.checkout, "gc", "--prune=now")
        self.assertEqual(self.git(self.checkout, "rev-parse", case["head_pin"]),
                         case["head_sha"])
        self.origin.rename(self.root / "unavailable-origin.git")
        seen = []
        ledger = spend.Ledger(self.root / "state" / "spend.sqlite3")

        def review(review_case, runtime, prompt_config, routing_mode,
                   blind_alternatives, archaeology):
            self.assertEqual(review_case.review, case["review_input"])
            self.assertNotIn("Expected issue", review_case.review)
            self.assertEqual(review_case.snapshot.head_sha, case["head_sha"])
            self.assertEqual(review_case.snapshot.merge_base, case["merge_base"])
            self.assertEqual(prompt_config.models, self.prompt_config.models)
            self.assertIsNone(runtime.discussion_lookup)
            self.assertIs(runtime.allow_web_search, False)
            self.assertIsNotNone(review_case.research_evidence)
            self.assertIn("read_current_pr_discussion",
                          frozen_research.inventory(review_case.research_evidence))
            self.assertEqual(routing_mode, "shadow")
            runtime.debug["stage_outputs"] = {"verifier": "raw verifier"}
            seen.append(review_case.snapshot)
            return "Final review."

        with patch.object(forgejo, "forgejo_request", side_effect=AssertionError("Forgejo used")), \
                patch.object(pipeline, "review_with_independent_passes", side_effect=review):
            first = evaluate.run_case(manifest, "openai-key", self.root / "runs", ledger,
                                      routing_mode="shadow", labels=labels)
            second = evaluate.run_case(manifest, "openai-key", self.root / "runs", ledger,
                                       routing_mode="shadow", labels=labels)
        self.assertEqual(len(seen), 2)
        self.assertNotEqual(first, second)
        self.assertIsNone(os.environ.get("GIT_NO_LAZY_FETCH"))
        for path in (first, second):
            result = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(result["run"]["status"], "completed", result.get("error"))
            self.assertEqual(result["output"]["data"]["stage_outputs"]["verifier"],
                             "raw verifier")
            self.assertEqual(result["expected_findings"], labels[case["case_id"]])
            self.assertIn("Final review.", result["content"])
            self.assertEqual(result["effective_config_sha256"],
                             evaluate.digest(result["effective_config"]))
            self.assertEqual(result["effective_config"]["engine"]["config"][
                "routing_mode"], "shadow")
            self.assertFalse(result["code_changed_since_capture"])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_missing_objects_write_private_failure_artifact(self):
        manifest = self.capture()
        case = json.loads(manifest.read_text(encoding="utf-8"))
        empty = self.root / "empty"
        self.git(self.root, "init", str(empty))
        case["checkout"] = str(empty)
        case["manifest_sha256"] = evaluate.digest({
            key: value for key, value in case.items() if key != "manifest_sha256"})
        broken = self.root / "cases" / "broken.json"
        evaluate.write_private_json(broken, case)
        with patch.object(forgejo, "forgejo_request", side_effect=AssertionError("Forgejo used")), \
                patch.object(pipeline, "review_with_independent_passes") as review:
            path = evaluate.run_case(broken, "openai-key", self.root / "runs", object())
        review.assert_not_called()
        result = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(result["run"]["status"], "failed")
        self.assertIn("Git objects are unavailable", result["error"]["message"])
        self.assertEqual(result["run"]["attempt"], {})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_capture_rejects_historical_cutoff_without_historical_pr_text(self):
        details = {"title": "Edited title", "body": "Edited body",
                   "base": {"ref": "master"},
                   "head": {"sha": self.git(self.work, "rev-parse", "HEAD")},
                   "created_at": "2026-09-30T00:00:00Z",
                   "updated_at": "2026-10-01T00:00:00Z"}
        with patch.object(forgejo, "forgejo_request", return_value=details), \
                patch.object(forgejo, "public_discussion_request",
                             side_effect=AssertionError("discussion used")):
            with self.assertRaisesRegex(ValueError, "cannot be frozen at cutoff"):
                evaluate.capture_case(
                    "token", self.checkout, self.root / "cases", 42,
                    self.bot_config, self.review_engine,
                    research_cutoff="2026-09-30T12:00:00Z")

    def test_failed_model_call_keeps_raw_debug(self):
        manifest = self.capture()

        ledger = spend.Ledger(self.root / "state" / "spend.sqlite3")

        def fail(review_case, runtime, prompt_config, routing_mode,
                 blind_alternatives, archaeology):
            budget = runtime.budget
            token = budget.reserve("router", {"model": "gpt-6-luna",
                                              "max_output_tokens": 100})
            budget.fail(token, charged_unknown=True)
            runtime.debug["pipeline_stage"] = "verification"
            runtime.debug["stage_outputs"] = {"independent": "candidate text"}
            raise RuntimeError("model response failed")

        with patch.object(pipeline, "review_with_independent_passes", side_effect=fail):
            path = evaluate.run_case(manifest, "openai-key", self.root / "runs", ledger)
        result = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(result["run"]["status"], "failed")
        self.assertEqual(result["error"]["type"], "RuntimeError")
        self.assertEqual(result["run"]["attempt"]["pipeline_stage"], "verification")
        self.assertEqual(result["run"]["attempt"]["stage_outputs"]["independent"],
                         "candidate text")
        self.assertTrue(result["run"]["review_id"].startswith("eval:"))
        self.assertEqual(result["run"]["budget"]["unknown_request_count"], 1)

    def test_model_override_changes_effective_config_hash(self):
        manifest = self.capture()
        frozen = json.loads(manifest.read_text(encoding="utf-8"))
        models = dict(self.prompt_config.models)
        models["verifier"] = "gpt-6.1-sol"
        override = self.root / "models.json"
        override.write_text(json.dumps(models), encoding="utf-8")
        ledger = spend.Ledger(self.root / "state" / "spend.sqlite3")

        def review(review_case, runtime, prompt_config, routing_mode,
                   blind_alternatives, archaeology):
            self.assertEqual(review_case.review, frozen["review_input"])
            self.assertEqual(prompt_config.models["verifier"], "gpt-6.1-sol")
            return "Review with override."

        with patch.object(pipeline, "review_with_independent_passes", side_effect=review):
            path = evaluate.run_case(manifest, "openai-key", self.root / "runs",
                                     ledger, models_json=override)
        result = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(result["run"]["status"], "completed", result.get("error"))
        self.assertNotEqual(result["effective_config_sha256"], frozen["config_sha256"])
        self.assertIn(result["effective_config_sha256"][:12], path.name)

    def test_old_frozen_manifest_requires_recapture(self):
        manifest = self.capture()
        frozen = json.loads(manifest.read_text(encoding="utf-8"))
        frozen["schema_version"] = evaluate.SCHEMA_VERSION - 1
        frozen["config_sha256"] = evaluate.digest(frozen["config"])
        frozen["manifest_sha256"] = evaluate.digest({
            key: value for key, value in frozen.items() if key != "manifest_sha256"})
        old_manifest = self.root / "cases" / "old-shape.json"
        evaluate.write_private_json(old_manifest, frozen)

        with patch.object(pipeline, "review_with_independent_passes") as review:
            failed = evaluate.run_case(old_manifest, "openai-key", self.root / "runs",
                                       object())
        review.assert_not_called()
        result = json.loads(failed.read_text(encoding="utf-8"))
        self.assertEqual(result["run"]["status"], "failed")
        self.assertIn("Unsupported frozen case schema", result["error"]["message"])

    def test_routing_budget_and_code_each_identify_a_distinct_run(self):
        manifest = self.capture()
        ledger = spend.Ledger(self.root / "state" / "spend.sqlite3",
                              review_limit_usd=0.60)
        smaller = spend.Ledger(self.root / "state" / "spend.sqlite3",
                               review_limit_usd=0.40)
        with patch.object(pipeline, "review_with_independent_passes",
                          return_value="Review."):
            standard = evaluate.run_case(manifest, "key", self.root / "runs", ledger)
            routed = evaluate.run_case(manifest, "key", self.root / "runs", ledger,
                                       routing_mode="full")
            lower_budget = evaluate.run_case(manifest, "key", self.root / "runs", smaller)
            with patch.object(evaluate, "source_code_sha256", return_value="f" * 64):
                revised_code = evaluate.run_case(manifest, "key", self.root / "runs", ledger)
        results = [json.loads(path.read_text(encoding="utf-8"))
                   for path in (standard, routed, lower_budget, revised_code)]
        self.assertEqual(len({item["effective_config_sha256"] for item in results}), 4)
        self.assertEqual(results[2]["effective_config"]["review_budget_usd"], 0.40)
        self.assertTrue(results[3]["code_changed_since_capture"])

    def test_blind_alternatives_defaults_on_and_can_be_disabled_for_eval(self):
        manifest = self.capture()
        ledger = spend.Ledger(self.root / "state" / "spend.sqlite3")
        seen = []

        def review(review_case, runtime, prompt_config, routing_mode,
                   blind_alternatives, archaeology):
            seen.append(blind_alternatives)
            return "Review."

        with patch.object(pipeline, "review_with_independent_passes", side_effect=review):
            blind = evaluate.run_case(manifest, "key", self.root / "runs", ledger)
            regular = evaluate.run_case(manifest, "key", self.root / "runs", ledger,
                                        blind_alternatives=False)
        results = [json.loads(path.read_text(encoding="utf-8"))
                   for path in (blind, regular)]
        self.assertEqual(seen, [True, False])
        self.assertNotEqual(results[0]["effective_config_sha256"],
                            results[1]["effective_config_sha256"])
        self.assertTrue(results[0]["effective_config"]["engine"]["config"][
            "blind_alternatives"])
        self.assertFalse(results[1]["effective_config"]["engine"]["config"][
            "blind_alternatives"])

    def test_spend_cli_reads_local_ledger_without_keys(self):
        state = self.root / "state"
        spend.Ledger(state / "spend.sqlite3")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(evaluate.main(["spend", "--state-dir", str(state)]), 0)
        self.assertIn("estimated_total_usd", json.loads(output.getvalue()))

    def write_run(self, name, artifact):
        runs = self.root / "runs"
        runs.mkdir(exist_ok=True)
        path = runs / name
        evaluate.write_private_json(path, artifact)
        return path

    def schema1_run(self, artifact, *, engine_name="current", findings=None,
                    native_data=None):
        artifact = dict(artifact)
        status = artifact.pop("status")
        debug = artifact.pop("raw_debug", {})
        artifact["schema_version"] = 1
        artifact["engine"] = {"name": engine_name,
                              "code_sha256": "c" * 64,
                              "config_sha256": "d" * 64,
                              "config": {}}
        if status == "completed":
            data = (native_data if native_data is not None else
                    {"schema_version": 1, **debug} if engine_name == "current" else {})
            exports = ({"findings": findings} if findings is not None else {})
            artifact["output"] = {"schema_version": 1, "data": data,
                                  "exports": exports}
        else:
            artifact["output"] = None
        run = {"status": status,
               "metrics": core_trace.review_metrics(debug, None)
               if engine_name == "current" else
               {"estimated_cost_usd": 0.0, "unknown_usage_count": 0,
                "incomplete_usage_count": 0},
               "budget": artifact.pop("budget", {}),
               "attempt": ({"schema_version": 1, **debug}
                           if status != "completed" and engine_name == "current" else {})}
        artifact["run"] = run
        return artifact

    def test_partial_coverage_uses_code_summary_not_advisory_stage(self):
        artifact = {"schema_version": 1,
                    "engine": {"name": "current"},
                    "output": {"schema_version": 1,
                               "data": {"schema_version": 1,
                                        "coverage": {"status": "complete"},
                                        "stages": {"archaeologist": {
                                            "coverage": {"status": "partial"}}}},
                               "exports": {}},
                    "run": {"status": "completed", "attempt": {}}}
        self.assertFalse(evaluate._run_has_partial_coverage(artifact))
        artifact["output"]["data"]["coverage"]["status"] = "failed"
        self.assertTrue(evaluate._run_has_partial_coverage(artifact))

    def test_summarize_runs_groups_offline_costs_and_outcomes(self):
        first = self.write_run("run-first.json", self.schema1_run({
            "status": "completed",
            "effective_config_sha256": "a" * 64,
            "started_at": "2026-09-30T00:00:00+00:00",
            "finished_at": "2026-09-30T00:00:10+00:00",
            "expected_findings": ["human-held label"],
            "budget": {"estimated_total_usd": 0.12},
            "raw_debug": {
                "coverage": {"status": "partial"},
                "concept_assessment": {
                    "status": "verified",
                    "stage": "archaeologist",
                    "summary": "Fix it in the policy layer.",
                    "candidate": {
                        "assessment": "risky",
                        "proposed_review": "stop",
                        "review_reason": "Wait for design clarity.",
                    },
                    "verification": {
                        "disposition": "publish",
                        "proposed_review": "continue",
                        "review_reason": "Verifier accepted the path.",
                        "reason": "Verified conceptual concern.",
                        "assessment": {
                            "problem": "The PR changes peer eviction policy.",
                            "baseline": "Keep existing behavior.",
                            "delivered_benefit": "Removes a workaround.",
                            "relevant_history": "A prior attempt chose another layer.",
                            "recommendation": "Fix it in the policy layer.",
                            "decisive_question": "Whether the workaround is still common.",
                            "technical_assumptions": ["Verifier checks the changed path."],
                            "alternatives": [{
                                "name": "Do nothing",
                                "concept": "Keep status quo",
                                "benefit": "No churn",
                                "cost": "Workaround remains",
                                "unresolved": "Impact",
                                "provenance": "history",
                                "citations": [],
                            }],
                            "citations": [],
                        },
                    },
                },
                "candidate_sources": {
                    "state:1": "state",
                    "state:2": "state",
                    "state:3": "state",
                    "state:4": "state",
                    "tests:1": "tests",
                    "tests:2": "tests",
                },
                "decisions": [
                    {"disposition": "publish",
                     "candidate_ids": ["state:1", "tests:1"]},
                    {"disposition": "publish",
                     "candidate_ids": ["state:3"]},
                    {"disposition": "drop",
                     "candidate_ids": ["state:2"]},
                    {"disposition": "unresolved",
                     "candidate_ids": ["tests:2"]},
                ],
                "finding_attribution": [
                    {"finding_id": "finding-1",
                     "candidate_ids": ["state:1", "tests:1"],
                     "raised_by": ["state", "tests"]},
                    {"finding_id": "finding-2",
                     "candidate_ids": ["state:3"],
                     "raised_by": ["state"]},
                ],
                "stages": {
                    "state": {"model": "gpt-6-luna", "status": "completed",
                              "turns": [], "tools": []},
                    "tests": {"model": "gpt-6-luna", "status": "completed",
                              "turns": [], "tools": []},
                    "verifier": {
                        "model": "gpt-6-luna",
                        "status": "completed",
                        "turns": [{
                            "model": "gpt-6-luna",
                            "request_bytes": 100,
                            "input_tokens": 10,
                            "cached_tokens": 0,
                            "cache_write_tokens": 0,
                            "output_tokens": 20,
                            "elapsed_seconds": 1.5,
                        }],
                    },
                    "archaeologist": {"model": "gpt-6-luna", "status": "completed",
                                      "turns": [], "tools": [{"name": "web_search"}]},
                },
            },
        }, findings=[{"id": "finding-1"}, {"id": "finding-2"}]))
        self.write_run("run-second.json", self.schema1_run({
            "status": "failed",
            "effective_config_sha256": "a" * 64,
            "started_at": "2026-09-30T00:00:00+00:00",
            "finished_at": "2026-09-30T00:00:05+00:00",
            "error": {"type": "RuntimeError", "message": "model failed"},
            "budget": {"estimated_total_usd": 0.20},
            "raw_debug": {
                "concept_assessment": {
                    "status": "no_concern",
                    "stage": "archaeologist",
                    "verification": {
                        "disposition": "no_concern",
                        "reason": "No conceptual objection verified.",
                        "assessment": None,
                    },
                },
                "stages": {
                    "archaeologist": {"model": "gpt-6-luna",
                                      "status": "completed",
                                      "turns": [], "tools": []},
                    "design": {
                        "model": "gpt-unknown",
                        "status": "budget_exhausted",
                        "incomplete_reason": "max_output_tokens",
                        "turns": [{
                            "model": "gpt-unknown",
                            "request_bytes": 100,
                            "input_tokens": 5,
                            "cached_tokens": 0,
                            "cache_write_tokens": 0,
                            "output_tokens": 7,
                            "elapsed_seconds": 2.0,
                            "incomplete_reason": "max_output_tokens",
                        }],
                    },
                },
            },
        }))
        self.write_run("run-third.json", self.schema1_run({
            "status": "completed",
            "effective_config_sha256": "b" * 64,
        }, engine_name="narrative", native_data={"text": "private native output"}))

        summary = evaluate.summarize_runs([first.parent])
        group = summary["groups"]["a" * 64]
        self.assertEqual(group["runs"], 2)
        self.assertEqual(group["completed"], 1)
        self.assertEqual(group["failed"], 1)
        self.assertEqual(group["partial_coverage"], 1)
        self.assertEqual(group["budget_exhausted"], 1)
        self.assertEqual(group["labels_present"], 1)
        self.assertEqual(group["decision_counts"],
                         {"published": 2, "dropped": 1, "unresolved": 1})
        self.assertIsNone(group["accepted_findings"])
        self.assertIsNone(group["cost_usd"]["known_per_accepted_finding"])
        self.assertEqual(group["findings_export_status_counts"],
                         {"supported": 1, "unsupported": 1})
        self.assertEqual(group["archaeology_research_completed"], 2)
        self.assertEqual(group["published_conceptual_concerns"], 1)
        self.assertEqual(group["conceptual_concern_status_counts"],
                         {"no_concern": 1, "verified": 1})
        self.assertEqual(group["would_stop"]["candidate"]["counts"], {"stop": 1})
        self.assertEqual(group["would_stop"]["candidate"]["would_stop_runs"], 1)
        self.assertEqual(
            group["would_stop"]["candidate"]["downstream_accepted_findings"], 2)
        self.assertEqual(group["would_stop"]["verified"]["counts"], {"continue": 1})
        self.assertEqual(group["cost_usd"]["total"], 0.000011)
        self.assertEqual(group["unknown_usage_runs"], 1)
        self.assertEqual(group["unknown_usage_turns"], 1)
        self.assertEqual(group["wall_seconds"]["total"], 15.0)
        self.assertEqual(group["stage_metrics"]["design"]["status_counts"],
                         {"budget_exhausted": 1})
        self.assertEqual(
            group["stage_metrics"]["design"]["max_output_token_incomplete_turns"], 1)
        self.assertEqual(
            group["stage_metrics"]["verifier"]["known_cost_usd"], 0.000011)
        self.assertEqual(group["stage_metrics"]["state"]["published_candidates"], 2)
        self.assertEqual(group["stage_metrics"]["state"]["dropped_candidates"], 1)
        self.assertEqual(group["stage_metrics"]["state"]["undisposed_candidates"], 1)
        self.assertEqual(group["stage_metrics"]["state"]["sole_accepted_findings"], 1)
        self.assertEqual(group["stage_metrics"]["state"]["shared_accepted_findings"], 1)
        self.assertEqual(group["stage_metrics"]["tests"]["published_candidates"], 1)
        self.assertEqual(group["stage_metrics"]["tests"]["unresolved_candidates"], 1)
        self.assertEqual(group["stage_metrics"]["tests"]["shared_accepted_findings"], 1)
        self.assertTrue(group["stage_metrics"]["archaeologist"]["concept_stage"])
        self.assertEqual(
            group["stage_metrics"]["archaeologist"]["conceptual_concerns"], 1)
        self.assertEqual(group["stage_metrics"]["archaeologist"]["accepted_findings"], 0)
        alternate = summary["groups"]["b" * 64]
        self.assertEqual(alternate["findings_export_status_counts"],
                         {"unsupported": 1})
        self.assertIsNone(alternate["accepted_findings"])
        self.assertIsNone(alternate["cost_usd"]["known_per_accepted_finding"])
        self.assertEqual(alternate["stage_metrics"], {})
        self.assertEqual(alternate["current_diagnostics_status_counts"],
                         {"unsupported": 1})
        self.assertIsNone(alternate["partial_coverage"])
        self.assertIsNone(alternate["max_output_token_incomplete_turns"])
        self.assertIsNone(alternate["decision_counts"])
        self.assertIsNone(alternate["would_stop"])
        self.assertIn("does not match labels", summary["labels_note"])

    def test_summarize_cli_reads_artifacts_without_credentials(self):
        path = self.write_run("run-cli.json", self.schema1_run({
            "status": "completed",
            "effective_config_sha256": "c" * 64,
            "raw_debug": {},
        }, findings=[]))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(evaluate.main(["summarize", str(path)]), 0)
        summary = json.loads(output.getvalue())
        self.assertEqual(summary["run_artifacts"], 1)
        self.assertEqual(summary["groups"]["c" * 64]["runs"], 1)

    def test_capture_and_run_cli_keep_credentials_separate(self):
        token_file = self.root / "forgejo-token"
        key_file = self.root / "openai-key"
        token_file.write_text("forgejo-token\n", encoding="utf-8")
        key_file.write_text("openai-key\n", encoding="utf-8")
        details = {"title": "Pinned input", "body": "Review this.",
                   "base": {"ref": "master"},
                   "head": {"sha": self.git(self.work, "rev-parse", "HEAD")}}
        output = io.StringIO()
        def discussion(_bot_config, path):
            now = "2026-09-30T00:00:00Z"
            if path == "/issues/42":
                return {"number": 42, "title": "Pinned input", "body": "Review this.",
                        "pull_request": {}, "created_at": now, "updated_at": now}
            if path in {"/issues/42/comments?limit=10&page=1",
                        "/pulls/42/reviews?limit=10&page=1"}:
                return []
            raise AssertionError(f"unexpected discussion request {path}")

        with patch.object(forgejo, "forgejo_request", return_value=details), \
                patch.object(forgejo, "public_discussion_request",
                             side_effect=discussion), \
                contextlib.redirect_stdout(output):
            self.assertEqual(evaluate.main([
                "capture", "--state-dir", str(self.root / "state"),
                "--output-dir", str(self.root / "cases"),
                "--origin", str(self.origin), "--repository", "example/repo",
                "--forgejo-api", self.bot_config.forgejo_api,
                "--forgejo-token-file", str(token_file), "42",
            ]), 0)
        manifest = Path(output.getvalue().strip())
        self.origin.rename(self.root / "unavailable-origin.git")
        output = io.StringIO()
        with patch.object(forgejo, "forgejo_request", side_effect=AssertionError("Forgejo used")), \
                patch.object(pipeline, "review_with_independent_passes",
                             return_value="Local replay."), \
                contextlib.redirect_stdout(output):
            self.assertEqual(evaluate.main([
                "run", "--state-dir", str(self.root / "state"),
                "--output-dir", str(self.root / "runs"),
                "--openai-key-file", str(key_file),
                "--ppq-key-file", str(key_file), str(manifest),
            ]), 0)
        artifact = json.loads(Path(output.getvalue().strip()).read_text(encoding="utf-8"))
        self.assertEqual(artifact["run"]["status"], "completed")
        self.assertIn("Local replay.", artifact["content"])


if __name__ == "__main__":
    unittest.main()
