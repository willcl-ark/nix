import json
from threading import Barrier
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ralph import config, engine, model, pipeline, protocol, repository

COMPLETE = {"status": "complete", "limitations": []}


def discovery(findings=(), sensitive=False):
    return json.dumps({"coverage": COMPLETE, "findings": list(findings),
                       "requires_sensitive_review": sensitive})


def assessment():
    return {
        "goal": "Keep review effort focused on changes worth pursuing.",
        "problem": "Reviewers do not have enough context on prior attempts.",
        "baseline": "Reviewers can read the PR discussion manually.",
        "delivered_benefit": "The bot can summarize prior conceptual objections.",
        "relevant_history": "Prior discussion raised a layering concern.",
        "assessment": "worth_pursuing",
        "alternatives": [{
            "name": "Do nothing",
            "concept": "Keep the current review flow.",
            "benefit": "No extra review cost.",
            "cost": "The bot may miss repeated conceptual objections.",
            "unresolved": "Whether reviewers already catch this reliably.",
            "provenance": "agent_inference",
            "citations": [],
        }],
        "recommendation": "The submitted concept is acceptable.",
        "proposed_review": "continue",
        "review_reason": "No material concept concern is established.",
        "decisive_question": "Whether maintainers want discussion history in bot output.",
        "technical_assumptions": ["The discussion API returns human comments."],
        "citations": ["https://example.invalid/o/r/pulls/42"],
    }


def archaeology(concept=None):
    return json.dumps({"coverage": COMPLETE, "assessment": concept or assessment()})


def verification(decisions=(), concept=None):
    return json.dumps({
        "coverage": COMPLETE,
        "concept": concept or {"disposition": "no_concern",
                               "reason": "No material concept concern",
                               "assessment": None,
                               "proposed_review": "continue",
                               "review_reason": "The verifier found no material concept concern."},
        "alternatives": [],
        "decisions": list(decisions),
    })


def candidate(title="Simplify fixture"):
    return {"kind": "suggestion", "path": "src/example.cpp", "line": 3, "side": "head",
            "title": title, "claim": "Duplicate setup", "consequence": "Two fixtures to maintain",
            "evidence": "The same setup is repeated", "correction": "Reuse the fixture",
            "uncertainty": ""}


class PipelineTests(unittest.TestCase):
    def test_dead_code_inspects_base_and_head_and_sends_removals_for_verification(self):
        for tier in ("routine", "standard", "sensitive"):
            with self.subTest(tier=tier):
                self.tier, self.audits = tier, ["dead_code"]

                def review(*args, **kwargs):
                    stage = kwargs["stage_name"]
                    if stage == "dead_code":
                        self.assertEqual(args[1], "PR diff")
                        self.assertEqual(kwargs["model"], "gpt-6-luna")
                        self.assertEqual(kwargs["reasoning_effort"], "medium")
                        self.assertEqual(kwargs["max_output_tokens"], 8_000)
                        self.assertEqual(kwargs["max_tool_calls"], 48)
                        self.assertTrue(kwargs["first_tool_required"])
                        names = {tool.get("name") for tool in kwargs["tools"]}
                        self.assertTrue({"read_file", "read_base_file", "read_diff",
                                         "search_code", "search_base_code",
                                         "find_paths", "find_base_paths"} <= names)
                        self.assertFalse(names & model.LIVE_RESEARCH_TOOL_NAMES)
                        self.assertFalse(any(tool["type"] in model.WEB_SEARCH_TOOL_TYPES
                                             for tool in kwargs["tools"]))
                        return discovery([candidate("Remove unused helper")])
                    if stage == "verifier":
                        payload = json.loads(args[1].split("Verification input:\n")[1])
                        self.assertEqual(payload["candidate_findings"][0]["id"], "dead_code:1")
                        return verification([{"candidate_ids": ["dead_code:1"],
                                              "disposition": "publish", "reason": "Unused",
                                              "finding": self.published}])
                    return discovery()

                self.run_review(review)
                self.assertEqual(self.debug["finding_attribution"][0]["raised_by"], ["dead_code"])

    def test_script_runs_before_advisory_work_with_required_code_inspection(self):
        self.tier, self.audits = "sensitive", ["script"]

        def review(*args, **kwargs):
            stage = kwargs["stage_name"]
            self.calls.append((stage, kwargs["model"]))
            if stage == "archaeologist":
                return archaeology()
            if stage == "script":
                self.assertEqual(args[1], "PR diff")
                self.assertEqual(kwargs["model"], "gpt-6.1-sol")
                self.assertEqual(kwargs["reasoning_effort"], "high")
                self.assertEqual(kwargs["max_output_tokens"], 25_000)
                self.assertEqual(kwargs["max_tool_calls"], 48)
                self.assertTrue(kwargs["first_tool_required"])
                names = {tool.get("name") for tool in kwargs["tools"]}
                self.assertTrue({"read_file", "read_base_file", "read_diff",
                                 "search_code", "blame_base"} <= names)
                self.assertFalse(names & model.LIVE_RESEARCH_TOOL_NAMES)
            return verification() if stage == "verifier" else discovery()

        self.run_review(review, archaeology_enabled=True)
        self.assertEqual([stage for stage, _ in self.calls[:3]],
                         ["script", "archaeologist", "independent"])
        self.assertEqual(sum(stage == "script" for stage, _ in self.calls), 1)
        self.assertEqual(self.debug["stages"]["script"]["coverage"], COMPLETE)

    def test_script_partial_coverage_reaches_verifier_and_public_report(self):
        self.tier, self.audits = "sensitive", ["script"]
        limitation = "P2A witness policy caller remains unread."

        def review(*args, **kwargs):
            stage = kwargs["stage_name"]
            if stage == "script":
                return json.dumps({"coverage": {"status": "partial",
                                               "limitations": [limitation]},
                                   "findings": [], "requires_sensitive_review": False})
            if stage == "verifier":
                payload = json.loads(args[1].split("Verification input:\n")[1])
                self.assertTrue(any(item["stage"] == "script" and
                                    item["reason"] == limitation
                                    for item in payload["inspection_gaps"]))
                return verification()
            return discovery()

        content = self.run_review(review)
        self.assertIn("script: " + limitation, content)
        self.assertEqual(self.debug["coverage"]["status"], "partial")

    def test_script_findings_use_normal_verification_and_attribution(self):
        self.tier, self.audits = "sensitive", ["script"]

        def review(*args, **kwargs):
            stage = kwargs["stage_name"]
            if stage == "script":
                return discovery([candidate()])
            if stage == "verifier":
                payload = json.loads(args[1].split("Verification input:\n")[1])
                self.assertEqual(payload["candidate_findings"][0]["id"], "script:1")
                return verification([{"candidate_ids": ["script:1"],
                                      "disposition": "publish", "reason": "Confirmed",
                                      "finding": self.published}])
            return discovery()

        content = self.run_review(review)
        self.assertIn(self.published["body"], content)
        self.assertEqual(self.debug["finding_attribution"][0]["raised_by"], ["script"])

    def test_script_budget_failure_is_handed_to_verifier(self):
        from ralph.spend import BudgetExceeded

        self.tier, self.audits = "sensitive", ["script"]

        def review(*args, **kwargs):
            stage = kwargs["stage_name"]
            if stage == "script":
                raise BudgetExceeded("review spend limit would be exceeded")
            if stage == "verifier":
                payload = json.loads(args[1].split("Verification input:\n")[1])
                self.assertTrue(any(item["stage"] == "script"
                                    for item in payload["inspection_gaps"]))
                return verification()
            return discovery()

        content = self.run_review(review)
        self.assertEqual(self.debug["stages"]["script"]["status"], "budget_exhausted")
        self.assertEqual(self.debug["stages"]["verifier"]["status"], "completed")
        self.assertIn("script: Stage budget exhausted", content)

    def test_sensitive_overview_escalation_does_not_run_script(self):
        self.tier, self.audits = "standard", ["tests"]

        def review(*args, **kwargs):
            stage = kwargs["stage_name"]
            self.calls.append(stage)
            if stage == "verifier":
                return verification()
            return discovery(sensitive=stage == "independent")

        self.run_review(review)
        self.assertNotIn("script", self.calls)
        self.assertEqual(self.debug["stages"]["script"]["status"], "skipped")

    def setUp(self):
        self.source = {"kind": "pull_request", "url": "https://example.invalid/o/r/pulls/42", "number": 42}
        self.prompts = config.PromptConfig.load()
        self.snapshot = repository.RepositorySnapshot(
            Path("/unused"), "b"*40, "a"*40, "b"*40, "b"*40,
            {"src/example.cpp": "blob"}, {"src/example.cpp": "base"},
            frozenset({"src/example.cpp"}))
        self.published = {"kind": "suggestion", "severity": "suggestion", "path": "src/example.cpp", "line": 3,
                          "side": "head", "title": "Simplify fixture",
                          "body": "Reuse the fixture while preserving the regression check.",
                          "security": {"classification": "non_security",
                                       "reason": "This only removes duplicate fixture setup."},
                          "source_evidence": [{"path": "src/example.cpp", "line": 3,
                                               "side": "head", "excerpt": "setup();"}]}
        self.debug = {}
        self.calls = []

    def test_adversarial_deepseek_has_extra_output_headroom(self):
        self.assertEqual(pipeline.stage_settings("adversarial_deepseek"),
                         ("high", 32_000))
        self.assertEqual(pipeline.stage_settings("adversarial"),
                         ("high", 25_000))

    def test_verifier_uses_luna_with_xhigh_reasoning(self):
        self.assertEqual(self.prompts.models["verifier"], "gpt-6-luna")
        self.assertEqual(model.MAX_VERIFIER_OUTPUT_TOKENS, 128_000)
        self.assertEqual(pipeline.stage_settings("verifier"),
                         ("xhigh", model.MAX_VERIFIER_OUTPUT_TOKENS))

    def test_public_contract_uses_sol_with_low_reasoning(self):
        self.assertEqual(self.prompts.models["public_contract"], "gpt-6.1-sol")
        self.assertEqual(pipeline.stage_settings("public_contract"), ("low", 25_000))

    def plan(self, *args, **kwargs):
        result = {"tier": self.tier, "audits": self.audits,
                  "profiles": getattr(self, "profiles", []), "evidence": [], "missing_context": []}
        args[4]["routing"] = {"selected": result}
        return result

    def edit(self, api_key, name, prompt, input_text, **kwargs):
        self.assertEqual(name, "collator")
        payload = json.loads(input_text)
        findings = payload["findings"]
        self.assertNotIn("Rejected claim", input_text)
        concept_summary = None
        if payload.get("concept_assessment") is not None:
            concept_summary = (payload["concept_assessment"]["recommendation"]
                               + " https://example.invalid/o/r/pulls/42")
        return json.dumps({"concept_summary": concept_summary, "findings": [
            {"id": item["id"], "title": item["title"], "body": item["body"]} for item in findings
        ]}), {"status": "completed"}

    def run_review(self, reviewer, editor=None, budget=None, ppq_api_key=None, ppq_budget=None,
                   allow_discussions=True, research_evidence=None, blind_alternatives=False,
                   archaeology_enabled=False):
        with patch.object(pipeline.routing, "plan_review", side_effect=self.plan), \
                patch.object(model, "openai_review", side_effect=reviewer), \
                patch.object(model, "run_audit", side_effect=editor or self.edit), \
                patch.object(protocol, "read_file", return_value="src/example.cpp (3 lines):\n3: setup();\n"), \
                patch.object(pipeline, "audit_developer_notes", return_value="Policy"):
            case = engine.ReviewCase("PR diff", self.snapshot,
                                     self.source, research_evidence)
            runtime = engine.Runtime(
                case=case, api_key="key",
                ppq_api_key=ppq_api_key, budget=budget, ppq_budget=ppq_budget,
                discussion_lookup=(lambda name, args: "Discussion") if allow_discussions else None,
                attempt=self.debug)
            self.result = pipeline.review_with_independent_passes(
                case, runtime, self.prompts, blind_alternatives=blind_alternatives,
                archaeology=archaeology_enabled)
            return self.result.content

    def test_parallel_adversarial_models_share_context_and_attribute_merged_finding(self):
        self.tier, self.audits = "sensitive", []
        self.profiles = ["consensus", "wallet"]
        openai_budget = Mock()
        ppq_budget = Mock()
        openai_budget.summary.return_value = {"limit_usd": "1", "provider": "openai"}
        ppq_budget.summary.return_value = {"limit_usd": "1", "provider": "ppq"}
        rendezvous = Barrier(3)
        inputs = {}
        candidate_ids = [f"{stage}:1" for stage in pipeline.ADVERSARIAL_STAGES]

        def review(*args, **kwargs):
            stage = kwargs["stage_name"]
            if stage in pipeline.ADVERSARIAL_STAGES:
                inputs[stage] = (args[1], kwargs["prompt"], kwargs["tools"],
                                 kwargs["reasoning_effort"], kwargs["max_output_tokens"])
                if stage in pipeline.PPQ_ADVERSARIAL_STAGES:
                    self.assertIs(kwargs["budget"], ppq_budget)
                    self.assertEqual(args[0], "ppq-key")
                    self.assertEqual(kwargs["model"], self.prompts.models[stage])
                    self.assertEqual(kwargs["api_base"], "https://api.ppq.ai/v1")
                else:
                    self.assertIs(kwargs["budget"], openai_budget)
                    self.assertEqual(args[0], "key")
                    self.assertNotIn("api_base", kwargs)
                rendezvous.wait(timeout=5)
                return discovery([candidate()])
            if stage == "verifier":
                candidates = json.loads(args[1].split("Verification input:\n")[1])[
                    "candidate_findings"]
                self.assertEqual([item["id"] for item in candidates],
                                 candidate_ids)
                return verification([
                    {"candidate_ids": candidate_ids,
                     "disposition": "publish", "reason": "Shared finding",
                     "finding": self.published}])
            return discovery()

        self.run_review(review, ppq_api_key="ppq-key",
                        budget=openai_budget, ppq_budget=ppq_budget)
        ppq_budget.protect.assert_not_called()
        self.assertEqual(self.debug["ppq_budget"], ppq_budget.summary.return_value)
        self.assertEqual(self.debug["budget"], openai_budget.summary.return_value)
        self.assertEqual(inputs["adversarial"][:-1], inputs["adversarial_deepseek"][:-1])
        self.assertEqual(inputs["adversarial"][:-1], inputs["adversarial_gemini"][:-1])
        self.assertEqual(inputs["adversarial_gemini"][-1], 32_000)
        self.assertEqual(self.debug["stages"]["adversarial_gemini"]["status"], "completed")
        self.assertEqual(inputs["adversarial"][-1], 25_000)
        self.assertEqual(inputs["adversarial_deepseek"][-1], 32_000)
        finding = self.debug["finding_attribution"][0]
        self.assertEqual(finding["raised_by"], ["adversarial", "adversarial_deepseek", "adversarial_gemini"])
        self.assertEqual(finding["raised_by_models"],
                         sorted(self.prompts.models[stage]
                                for stage in pipeline.ADVERSARIAL_STAGES))
        self.assertEqual(self.debug["stages"]["adversarial_deepseek"]["status"], "completed")

    def test_routine_review_keeps_luna_editor_and_sends_only_accepted_findings(self):
        self.tier, self.audits = "standard", ["tests", "design"]

        def review(*args, **kwargs):
            stage = kwargs["stage_name"]
            self.calls.append((stage, kwargs["model"]))
            self.assertEqual(kwargs["reasoning_effort"],
                             "xhigh" if stage in {"design", "verifier"} else "low")
            if stage == "design":
                self.assertEqual(kwargs["max_output_tokens"], 25_000)
                return discovery([candidate()])
            if stage == "tests":
                return discovery([candidate("Rejected claim")])
            if stage == "verifier":
                return verification([
                    {"candidate_ids": ["design:1"], "disposition": "publish",
                     "reason": "Useful simplification", "finding": self.published},
                    {"candidate_ids": ["tests:1"], "disposition": "drop",
                     "reason": "Guard already covers it", "finding": None}])
            return discovery()

        content = self.run_review(review)
        self.assertIn("Reuse the fixture", content)
        self.assertTrue(all(value == "gpt-6-luna" for _, value in self.calls))
        self.assertEqual([name for name, _ in self.calls], ["independent", "tests", "design", "verifier"])
        self.assertIn("Rejected claim", self.debug["stage_outputs"]["tests"])
        self.assertEqual(self.debug["coverage"]["status"], "complete")
        self.assertEqual(self.debug["finding_attribution"][0]["raised_by"], ["design"])
        self.assertEqual(self.debug["finding_attribution"][0]["edited_by"], "collator")

    def test_sensitive_discovery_uses_sol_and_failed_audit_keeps_partial_usage(self):
        self.tier, self.audits = "sensitive", ["design"]
        efforts = {}
        output_limits = {}

        def review(*args, **kwargs):
            stage = kwargs["stage_name"]
            self.calls.append((stage, kwargs["model"]))
            efforts[stage] = kwargs["reasoning_effort"]
            output_limits[stage] = kwargs["max_output_tokens"]
            if stage == "design":
                args[3]["turns"].append({"input_tokens": 100, "output_tokens": 10})
                raise TimeoutError()
            if stage == "verifier":
                return verification()
            return discovery()

        content = self.run_review(review)
        self.assertIn(("adversarial", "gpt-6.1-sol"), self.calls)
        self.assertEqual(efforts["adversarial"], "high")
        self.assertEqual(output_limits["adversarial"], 25_000)
        self.assertEqual(efforts["verifier"], "xhigh")
        self.assertEqual(output_limits["verifier"], model.MAX_VERIFIER_OUTPUT_TOKENS)
        self.assertIn(("verifier", "gpt-6-luna"), self.calls)
        self.assertIn("incomplete", content)
        self.assertEqual(self.debug["stages"]["design"]["turns"][0]["input_tokens"], 100)

    def test_specialist_can_escalate_after_router_and_overview(self):
        self.tier, self.audits = "standard", ["public_contract"]

        def review(*args, **kwargs):
            stage = kwargs["stage_name"]
            self.calls.append(stage)
            if stage == "verifier":
                return verification()
            return discovery(sensitive=stage == "public_contract")

        self.run_review(review)
        self.assertEqual(self.calls.count("adversarial"), 1)
        self.assertIn("state", self.calls)

    def test_inspection_allowance_follows_route_and_escalation(self):
        for tier, expected_limit in (("routine", 12), ("standard", 24), ("sensitive", 48)):
            with self.subTest(tier=tier):
                self.tier, self.audits = tier, ["design", "tests"]
                limits = {}

                def review(*args, **kwargs):
                    stage = kwargs["stage_name"]
                    limits[stage] = kwargs["max_tool_calls"]
                    if stage == "verifier":
                        return verification()
                    return discovery(sensitive=stage == "tests" and tier == "standard")

                self.run_review(review)
                self.assertEqual(limits["independent"], expected_limit)
                self.assertEqual(limits["design"], 48 if tier == "standard" else expected_limit)
                self.assertEqual(limits["tests"], expected_limit)
                self.assertEqual(limits["verifier"], 48)
                if tier == "routine":
                    self.assertNotIn("adversarial", limits)
                else:
                    self.assertEqual(limits["adversarial"], 48)
                if tier == "standard":
                    self.assertEqual(limits["state"], 48)

    def test_domain_checks_precede_design_and_profiles_share_adversarial_call(self):
        self.tier = "sensitive"
        self.audits = ["design", "tests", "concurrency", "state"]
        self.profiles = ["consensus", "wallet"]
        prompts = {}
        inputs = {}

        def review(*args, **kwargs):
            stage = kwargs["stage_name"]
            self.calls.append(stage)
            prompts[stage] = kwargs["prompt"]
            inputs[stage] = args[1]
            if stage == "verifier":
                return verification([
                    {"candidate_ids": ["adversarial:1"], "disposition": "drop",
                     "reason": "Existing guard prevents the trigger", "finding": None}])
            return discovery([candidate()] if stage == "adversarial" else [])

        self.run_review(review)
        self.assertEqual(self.calls, ["independent", "adversarial", "concurrency", "state",
                                     "tests", "design", "verifier"])
        for profile in self.profiles:
            self.assertIn(self.prompts.audit_prompts[profile], prompts["adversarial"])
        self.assertNotIn(self.prompts.audit_prompts["p2p"], prompts["adversarial"])
        self.assertIn("Comparison-base developer notes:\nPolicy", inputs["design"])
        self.assertNotIn("Duplicate setup", inputs["state"])
        self.assertIn("Duplicate setup", inputs["verifier"])

    def test_archaeology_runs_after_routing_before_discovery(self):
        self.tier, self.audits = "standard", ["tests"]

        def review(*args, **kwargs):
            self.calls.append((kwargs["stage_name"], kwargs["model"]))
            if kwargs["stage_name"] == "archaeologist":
                return archaeology()
            return verification() if kwargs["stage_name"] == "verifier" else discovery()

        self.run_review(review, archaeology_enabled=True)

        self.assertEqual([name for name, _model in self.calls],
                         ["archaeologist", "independent", "tests", "verifier"])
        self.assertEqual(self.debug["concept_assessment"]["status"], "no_concern")

    def test_would_stop_advisory_still_runs_discovery_and_verifier_without_contamination(self):
        self.tier, self.audits = "standard", ["tests", "design"]
        stop_candidate = {**assessment(),
                          "assessment": "rework_approach",
                          "recommendation": "Use the smaller interface from the prior attempt.",
                          "proposed_review": "would_stop",
                          "review_reason": "The prior attempt avoids the current layering cost."}
        inputs = {}

        def review(*args, **kwargs):
            stage = kwargs["stage_name"]
            self.calls.append(stage)
            inputs[stage] = args[1]
            if stage == "archaeologist":
                return archaeology(stop_candidate)
            if stage == "verifier":
                self.assertIn("concept_candidate", args[1])
                return verification(concept={
                    "disposition": "no_concern",
                    "reason": "The premises are not decisive.",
                    "assessment": None,
                    "proposed_review": "continue",
                    "review_reason": "The verifier found the submitted approach acceptable.",
                })
            return discovery()

        self.run_review(review, archaeology_enabled=True)

        self.assertEqual(self.calls, ["archaeologist", "independent", "tests", "design", "verifier"])
        for stage in ("independent", "tests", "design"):
            self.assertNotIn("would_stop", inputs[stage])
            self.assertNotIn("Use the smaller interface", inputs[stage])
        concept = self.debug["concept_assessment"]
        self.assertEqual(concept["candidate"]["proposed_review"], "would_stop")
        self.assertEqual(concept["verification"]["proposed_review"], "continue")
        self.assertEqual(concept["status"], "no_concern")

    def test_archaeology_failure_does_not_skip_discovery_or_verification(self):
        self.tier, self.audits = "standard", ["tests"]

        def review(*args, **kwargs):
            self.calls.append(kwargs["stage_name"])
            if kwargs["stage_name"] == "archaeologist":
                return "{not json"
            return verification() if kwargs["stage_name"] == "verifier" else discovery()

        content = self.run_review(review, archaeology_enabled=True)

        self.assertEqual(self.calls, ["archaeologist", "independent", "tests", "verifier"])
        self.assertIn("no actionable issues", content)
        self.assertEqual(self.debug["coverage"]["status"], "complete")
        self.assertEqual(self.debug["coverage"]["context"]["status"], "failed")
        self.assertEqual(self.debug["concept_assessment"]["status"], "failed")
        self.assertEqual(self.debug["stages"]["archaeologist"]["status"], "invalid")

    def test_archaeology_off_skips_dependent_stages_and_preserves_code_review(self):
        self.tier, self.audits = "routine", []
        calls = []

        def review(*args, **kwargs):
            calls.append(kwargs["stage_name"])
            return verification() if kwargs["stage_name"] == "verifier" else discovery()

        content = self.run_review(review, allow_discussions=False, blind_alternatives=True)
        self.assertNotIn("archaeologist", calls)
        self.assertNotIn("alternatives", calls)
        self.assertIn("independent", calls)
        self.assertIn("verifier", calls)
        self.assertEqual(self.debug["stages"]["archaeologist"]["status"], "skipped")
        self.assertEqual(self.debug["stages"]["alternatives"]["status"], "skipped")
        self.assertEqual(self.debug["concept_assessment"]["status"], "skipped")
        self.assertEqual(self.debug["coverage"]["context"]["status"], "not_requested")
        self.assertIn("no actionable issues", content)

    def test_verifier_protection_tracks_candidates_and_sensitive_escalation(self):
        self.tier, self.audits = "standard", ["tests"]

        class Budget:
            def __init__(self):
                self.payloads = []

            def protect(self, payload, exempt_stages=()):
                self.payloads.append(payload)

            def summary(self):
                return {}

        budget = Budget()

        def review(*args, **kwargs):
            stage = kwargs["stage_name"]
            if stage == "independent":
                return discovery([candidate()], sensitive=True)
            if stage == "verifier":
                return verification([
                    {"candidate_ids": ["independent:1"], "disposition": "drop",
                     "reason": "Existing guard", "finding": None}])
            return discovery()

        self.run_review(review, budget=budget)
        self.assertEqual(budget.payloads[0]["max_output_tokens"],
                         model.MAX_VERIFIER_OUTPUT_TOKENS)
        self.assertEqual(budget.payloads[1]["max_output_tokens"],
                         model.MAX_VERIFIER_OUTPUT_TOKENS)
        self.assertEqual(budget.payloads[2]["reasoning"]["effort"], "xhigh")
        self.assertIn("independent:1", budget.payloads[2]["input"][0]["content"])

    def test_research_evidence_is_forwarded_and_removes_hosted_web_from_protection(self):
        self.tier, self.audits = "routine", []
        research_evidence = {"schema_version": 1, "captured_at": "2026-10-23T00:00:00Z",
                             "cutoff": "2026-10-23T00:00:00Z", "entries": []}
        self.research_forwards = []
        self.research_inputs = {}

        class Budget:
            def __init__(self):
                self.payloads = []

            def protect(self, payload, exempt_stages=()):
                self.payloads.append(payload)

            def summary(self):
                return {}

        budget = Budget()

        def review(*args, **kwargs):
            self.research_inputs[kwargs["stage_name"]] = args[1]
            self.research_forwards.append(
                (kwargs["stage_name"], kwargs.get("research_evidence")))
            if kwargs["stage_name"] == "archaeologist":
                return archaeology()
            return verification() if kwargs["stage_name"] == "verifier" else discovery()

        with patch.object(model.research, "inventory", return_value="Known frozen calls"):
            self.run_review(review, budget=budget, research_evidence=research_evidence,
                            archaeology_enabled=True)

        self.assertIn(("archaeologist", research_evidence), self.research_forwards)
        self.assertIn(("verifier", research_evidence), self.research_forwards)
        self.assertTrue(all(item == research_evidence for stage, item in self.research_forwards))
        self.assertIn("Known frozen calls", self.research_inputs["archaeologist"])
        self.assertIn("Known frozen calls", self.research_inputs["verifier"])
        self.assertNotIn("Known frozen calls", self.research_inputs["independent"])
        self.assertTrue(all(tool.get("type") != "web_search"
                            for payload in budget.payloads
                            for tool in payload["tools"]))


    def test_attribution_retains_merged_sources_and_verifier_discovery_after_editing(self):
        self.tier, self.audits = "standard", ["tests"]

        def review(*args, **kwargs):
            if kwargs["stage_name"] == "verifier":
                return verification([
                    {"candidate_ids": ["independent:1", "tests:1"], "disposition": "publish",
                     "reason": "Same root cause", "finding": self.published},
                    {"candidate_ids": [], "disposition": "publish",
                     "reason": "New verified issue", "finding": {
                         **self.published, "title": "Verifier discovery"}}])
            return discovery([candidate()])

        def edit(*args, **kwargs):
            return json.dumps({"concept_summary": None, "findings": [
                {"id": "finding:2", "title": "Edited verifier finding", "body": "Verified text"},
                {"id": "finding:1", "title": "Edited shared finding", "body": "Verified text"},
            ]}), {"status": "completed"}

        self.run_review(review, edit)
        attribution = {item["finding_id"]: item for item in self.debug["finding_attribution"]}
        self.assertEqual(attribution["finding:1"]["raised_by"], ["independent", "tests"])
        self.assertEqual(attribution["finding:1"]["candidate_ids"], ["independent:1", "tests:1"])
        self.assertEqual(attribution["finding:1"]["title"], "Edited shared finding")
        self.assertEqual(attribution["finding:2"]["raised_by"], ["verifier"])
        self.assertEqual(attribution["finding:2"]["candidate_ids"], [])
        self.assertEqual(attribution["finding:2"]["title"], "Edited verifier finding")
        first, second = self.result.findings
        self.assertEqual([first["id"], second["id"]], ["finding:1", "finding:2"])
        self.assertEqual(first["title"], self.published["title"])
        self.assertEqual(first["body"], self.published["body"])
        self.assertEqual(first["presented_title"], "Edited shared finding")
        self.assertEqual(first["presented_body"], "Verified text")
        self.assertEqual(first["raised_by"], ["independent", "tests"])
        self.assertEqual(second["title"], "Verifier discovery")
        self.assertEqual(second["presented_title"], "Edited verifier finding")
        self.assertEqual(second["raised_by"], ["verifier"])
        self.assertEqual(first["security"], self.published["security"])
        self.assertEqual(first["source_evidence"], self.published["source_evidence"])
        self.assertEqual(set(first), {
            "id", "kind", "severity", "security", "path", "line", "side", "source_evidence",
            "title", "body", "presented_title", "presented_body", "raised_by", "raised_by_models",
            "verified_by", "verified_by_model", "edited_by", "edited_by_model"})

    def test_missing_verifier_decision_never_publishes_candidate(self):
        self.tier, self.audits = "routine", []

        def review(*args, **kwargs):
            return (verification()
                    if kwargs["stage_name"] == "verifier" else discovery([candidate()]))

        content = self.run_review(review)
        self.assertNotIn("Simplify fixture", content)
        self.assertIn("omitted", content)
        self.assertEqual(self.debug["stages"]["verifier"]["status"], "completed")
        self.assertEqual(self.debug["coverage"]["verification"]["status"], "partial")
        self.assertEqual(self.debug["decisions"][0]["disposition"], "unresolved")

    def test_omitted_candidate_preserves_valid_finding_and_attribution(self):
        self.tier, self.audits = "routine", []

        def review(*args, **kwargs):
            if kwargs["stage_name"] == "verifier":
                return verification([{
                    "candidate_ids": ["independent:1"], "disposition": "publish",
                    "reason": "Checked against the caller", "finding": self.published}])
            return discovery([candidate(), candidate("Unanswered lead")])

        content = self.run_review(review)
        self.assertIn(self.published["body"], content)
        self.assertNotIn("Unanswered lead", content)
        self.assertEqual(self.debug["coverage"]["verification"]["status"], "partial")
        self.assertEqual(self.debug["finding_attribution"][0]["candidate_ids"], ["independent:1"])
        unresolved = [decision for decision in self.debug["decisions"]
                      if decision["disposition"] == "unresolved"]
        self.assertEqual(unresolved[0]["candidate_ids"], ["independent:2"])

    def test_partial_specialist_and_invalid_finding_preserve_valid_suggestion(self):
        self.tier, self.audits = "standard", ["tests"]

        def review(*args, **kwargs):
            stage = kwargs["stage_name"]
            if stage == "tests":
                result = json.loads(discovery([candidate()]))
                result["coverage"] = {"status": "partial", "limitations": ["Caller not inspected"]}
                return json.dumps(result)
            if stage == "verifier":
                return verification([
                    {"candidate_ids": ["tests:1"], "disposition": "publish",
                     "reason": "Useful simplification", "finding": self.published},
                    {"candidate_ids": [], "disposition": "publish",
                     "reason": "Policy requires release notes", "finding": {
                         **self.published, "path": "doc/developer-notes.md"}}])
            return discovery()

        content = self.run_review(review)
        self.assertIn(self.published["body"], content)
        self.assertIn("withheld", content)
        self.assertNotIn("Verification did not complete", content)
        self.assertEqual(self.debug["coverage"]["status"], "partial")
        self.assertIn("doc/developer-notes.md",
                      self.debug["stages"]["verifier"]["validation_errors"][0]["error"])
        self.assertIn("doc/developer-notes.md", self.debug["stage_outputs"]["verifier"])

    def test_invalid_editor_preserves_verified_wording(self):
        self.tier, self.audits = "routine", []
        design = {**self.published, "kind": "design", "title": "Design concern",
                  "body": "Prefer the existing approach for this interface."}

        def review(*args, **kwargs):
            return (verification([
                {"candidate_ids": ["independent:1"], "disposition": "publish",
                 "reason": "Verified", "finding": design}])
                if kwargs["stage_name"] == "verifier" else discovery([{
                    **candidate(), "kind": "design"}]))

        for status in ("completed", "incomplete"):
            with self.subTest(status=status):
                self.debug = {}

                def edit(*args, **kwargs):
                    return '{"concept_summary":null,"findings":[]}', {"status": status}

                content = self.run_review(review, edit)
                self.assertIn(design["body"], content)
                self.assertIn("Design and approach", content)
                self.assertTrue(self.debug["stages"]["collator"]["used_verified_wording"])
                self.assertIsNone(self.debug["finding_attribution"][0]["edited_by"])
                finding = self.result.findings[0]
                self.assertEqual(finding["body"], design["body"])
                self.assertEqual(finding["presented_body"], design["body"])
                self.assertIsNone(finding["edited_by"])

    def test_sound_concept_brief_stays_out_of_public_review(self):
        self.tier, self.audits = "routine", []

        def review(*args, **kwargs):
            if kwargs["stage_name"] == "archaeologist":
                return archaeology()
            return verification() if kwargs["stage_name"] == "verifier" else discovery()

        content = self.run_review(review, archaeology_enabled=True)

        self.assertNotIn("Concept and approach", content)
        self.assertEqual(self.debug["concept_assessment"]["status"], "no_concern")
        self.assertEqual(self.debug["finding_attribution"], [])
        self.assertEqual(self.result.findings, [])
        self.assertFalse(self.result.concept_concern)

    def test_favorable_concept_and_rejected_alternatives_preserve_public_code_finding(self):
        self.tier, self.audits = "routine", []

        def review(*args, **kwargs):
            stage = kwargs["stage_name"]
            if stage == "archaeologist":
                return archaeology()
            if stage == "alternatives":
                return json.dumps({"coverage": COMPLETE,
                                   "alternatives": assessment()["alternatives"]})
            if stage == "independent":
                return discovery([candidate()])
            if stage == "verifier":
                payload = json.loads(args[1].split("Verification input:\n")[1])
                self.assertEqual(payload["concept_candidate"]["assessment"], "worth_pursuing")
                self.assertEqual(len(payload["blind_alternatives"]), 1)
                return verification([
                    {"candidate_ids": ["independent:1"], "disposition": "publish",
                     "reason": "Useful simplification", "finding": self.published}])
            return discovery()

        content = self.run_review(review, blind_alternatives=True, archaeology_enabled=True)

        self.assertIn(self.published["body"], content)
        self.assertNotIn("Concept and approach", content)
        self.assertNotIn("Alternatives considered", content)
        self.assertNotIn("Do nothing", content)
        self.assertEqual(self.debug["concept_assessment"]["status"], "no_concern")
        self.assertEqual(self.debug["stages"]["alternatives"]["status"], "completed")

    def test_verified_material_concept_concern_gets_public_paragraph(self):
        self.tier, self.audits = "routine", []
        concern = {**assessment(),
                   "assessment": "rework_approach",
                   "proposed_review": "would_stop",
                   "review_reason": "The process cost is decisive.",
                   "recommendation": "Doing nothing is conceptually stronger because the PR adds process cost."}

        def review(*args, **kwargs):
            if kwargs["stage_name"] == "archaeologist":
                return archaeology(concern)
            if kwargs["stage_name"] == "verifier":
                return verification(concept={
                    "disposition": "publish",
                    "reason": "The cited discussion supports a material process cost.",
                    "assessment": concern,
                    "proposed_review": "would_stop",
                    "review_reason": "The verified process cost would stop the review.",
                })
            return discovery()

        content = self.run_review(review, archaeology_enabled=True)

        self.assertIn("Concept and approach", content)
        self.assertIn("process cost", content)
        self.assertIn("https://example.invalid/o/r/pulls/42", content)
        self.assertNotIn("Alternatives considered", content)
        self.assertNotIn("**Do nothing**", content)
        self.assertEqual(self.debug["concept_assessment"]["status"], "verified")
        self.assertTrue(self.result.concept_concern)

    def test_discussion_disabled_review_skips_archaeology(self):
        self.tier, self.audits = "routine", []
        archaeology_tools = []

        def review(*args, **kwargs):
            if kwargs["stage_name"] == "archaeologist":
                archaeology_tools.extend(kwargs["tools"])
            return verification() if kwargs["stage_name"] == "verifier" else discovery()

        content = self.run_review(review, allow_discussions=False)

        self.assertNotIn("Concept and approach", content)
        self.assertEqual(self.debug["concept_assessment"]["status"], "skipped")
        self.assertEqual(self.debug["stages"]["archaeologist"]["status"], "skipped")
        self.assertEqual(archaeology_tools, [])


if __name__ == "__main__":
    unittest.main()
