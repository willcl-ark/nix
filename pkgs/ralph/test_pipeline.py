import json
import unittest
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock, patch

from ralph import config, engine, model, pipeline, protocol, repository

COMPLETE = {"status": "complete", "limitations": []}


def discovery(findings=(), sensitive=False):
    return json.dumps(
        {
            "coverage": COMPLETE,
            "findings": list(findings),
            "requires_sensitive_review": sensitive,
        }
    )


def assessment():
    return {
        "goal": "Keep review effort focused on changes worth pursuing.",
        "problem": "Reviewers do not have enough context on prior attempts.",
        "baseline": "Reviewers can read the PR discussion manually.",
        "delivered_benefit": "The bot can summarize prior conceptual objections.",
        "relevant_history": "Prior discussion raised a layering concern.",
        "assessment": "worth_pursuing",
        "alternatives": [
            {
                "name": "Do nothing",
                "concept": "Keep the current review flow.",
                "benefit": "No extra review cost.",
                "cost": "The bot may miss repeated conceptual objections.",
                "unresolved": "Whether reviewers already catch this reliably.",
                "provenance": "agent_inference",
                "citations": [],
            }
        ],
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
    return json.dumps(
        {
            "coverage": COMPLETE,
            "concept": concept
            or {
                "disposition": "no_concern",
                "reason": "No material concept concern",
                "assessment": None,
                "proposed_review": "continue",
                "review_reason": "The verifier found no material concept concern.",
            },
            "alternatives": [],
            "decisions": list(decisions),
        }
    )


def candidate(title="Simplify fixture"):
    return {
        "kind": "suggestion",
        "path": "src/example.cpp",
        "line": 3,
        "side": "head",
        "title": title,
        "claim": "Duplicate setup",
        "consequence": "Two fixtures to maintain",
        "evidence": "The same setup is repeated",
        "correction": "Reuse the fixture",
        "uncertainty": "",
    }


class PipelineTests(unittest.TestCase):
    def test_major_verifier_checks_only_major_final_wording(self):
        self.tier, self.audits = "routine", []
        findings = [
            {**self.published, "severity": severity, "kind": kind}
            for severity, kind in (
                ("major", "defect"),
                ("major", "design"),
                ("minor", "defect"),
                ("critical", "defect"),
            )
        ]
        order = []

        def review(*args, **kwargs):
            stage = kwargs["stage_name"]
            order.append(stage)
            if stage == "independent":
                return discovery([candidate() for _ in findings])
            if stage == "verifier":
                return verification(
                    [
                        {
                            "candidate_ids": [f"independent:{i}"],
                            "disposition": "publish",
                            "reason": "Supported",
                            "finding": finding,
                        }
                        for i, finding in enumerate(findings, 1)
                    ]
                )
            self.assertEqual(stage, "major_verifier")
            payload = json.loads(args[1].split("Major verification input:\n")[1])
            self.assertEqual(
                [f["id"] for f in payload["findings"]], ["finding:1", "finding:2"]
            )
            self.assertTrue(
                all(f["body"] == "Final public wording" for f in payload["findings"])
            )
            return json.dumps(
                {
                    "coverage": COMPLETE,
                    "decisions": [
                        {
                            "finding_id": "finding:1",
                            "disposition": "publish",
                            "reason": "Independently confirmed",
                            "source_evidence": self.published["source_evidence"],
                        },
                        {
                            "finding_id": "finding:2",
                            "disposition": "drop",
                            "reason": "Fixture disproves the premise",
                            "source_evidence": [],
                        },
                    ],
                }
            )

        def edit(*args, **kwargs):
            order.append("collator")
            text, response = self.edit(*args, **kwargs)
            result = json.loads(text)
            for finding in result["findings"]:
                finding["body"] = "Final public wording"
            return json.dumps(result), response

        budget = Mock()
        self.run_review(review, edit, budget=budget)
        self.assertEqual(
            order, ["independent", "verifier", "collator", "major_verifier"]
        )
        self.assertIn(
            "major_verifier", budget.protect.call_args.kwargs["exempt_stages"]
        )
        self.assertEqual(
            [f["id"] for f in self.result.findings],
            ["finding:1", "finding:3", "finding:4"],
        )
        self.assertEqual(self.result.findings[0]["verified_by"], "major_verifier")
        self.assertEqual(self.result.findings[0]["raised_by"], ["independent"])
        self.assertEqual(self.result.findings[1]["verified_by"], "verifier")
        self.assertEqual(self.debug["decisions"][1]["disposition"], "drop")
        self.assertEqual(
            self.debug["stages"]["verifier"]["decisions"][1]["disposition"], "publish"
        )

    def test_major_verifier_failures_withhold_only_majors(self):
        self.debug = {}
        self.tier, self.audits = "routine", []

        def review(*args, **kwargs):
            stage = kwargs["stage_name"]
            if stage == "independent":
                return discovery([candidate(), candidate()])
            if stage == "verifier":
                return verification(
                    [
                        {
                            "candidate_ids": [f"independent:{i}"],
                            "disposition": "publish",
                            "reason": "Supported",
                            "finding": {**self.published, "severity": severity},
                        }
                        for i, severity in enumerate(("major", "minor"), 1)
                    ]
                )
            self.assertEqual(stage, "major_verifier")
            raise TimeoutError()

        self.run_review(review)
        self.assertEqual([f["severity"] for f in self.result.findings], ["minor"])
        self.assertEqual(self.debug["decisions"][0]["disposition"], "unresolved")
        self.assertIsNone(self.debug["decisions"][0]["finding"])
        self.assertIn("major_verifier", self.result.content)
        self.assertNotEqual(
            self.debug["coverage"]["verification"]["status"], "complete"
        )

    def test_failed_editor_cannot_restore_major_rejected_by_second_verifier(self):
        self.tier, self.audits = "routine", []

        def review(*args, **kwargs):
            stage = kwargs["stage_name"]
            if stage == "independent":
                return discovery([candidate()])
            if stage == "verifier":
                return verification(
                    [
                        {
                            "candidate_ids": ["independent:1"],
                            "disposition": "publish",
                            "reason": "Supported",
                            "finding": {**self.published, "severity": "major"},
                        }
                    ]
                )
            self.assertEqual(stage, "major_verifier")
            payload = json.loads(args[1].split("Major verification input:\n")[1])
            self.assertEqual(payload["findings"][0]["body"], self.published["body"])
            return json.dumps(
                {
                    "coverage": COMPLETE,
                    "decisions": [
                        {
                            "finding_id": "finding:1",
                            "disposition": "drop",
                            "reason": "The fixture disproves it",
                            "source_evidence": [],
                        }
                    ],
                }
            )

        self.run_review(review, editor=Mock(side_effect=TimeoutError))
        self.assertEqual(self.result.findings, [])
        self.assertNotIn(self.published["body"], self.result.content)
        self.assertEqual(self.debug["decisions"][0]["disposition"], "drop")
        self.assertTrue(self.debug["stages"]["collator"]["used_verified_wording"])

    def setUp(self):
        self.source = {
            "kind": "pull_request",
            "url": "https://example.invalid/o/r/pulls/42",
            "number": 42,
        }
        self.prompts = config.PromptConfig.load()
        self.snapshot = repository.RepositorySnapshot(
            Path("/unused"),
            "b" * 40,
            "a" * 40,
            "b" * 40,
            "b" * 40,
            {"src/example.cpp": "blob"},
            {"src/example.cpp": "base"},
            frozenset({"src/example.cpp"}),
        )
        self.published = {
            "kind": "suggestion",
            "severity": "suggestion",
            "path": "src/example.cpp",
            "line": 3,
            "side": "head",
            "title": "Simplify fixture",
            "body": "Reuse the fixture while preserving the regression check.",
            "security": {
                "classification": "non_security",
                "reason": "This only removes duplicate fixture setup.",
            },
            "source_evidence": [
                {
                    "path": "src/example.cpp",
                    "line": 3,
                    "side": "head",
                    "excerpt": "setup();",
                }
            ],
        }
        self.debug = {}

    def plan(self, *args, **kwargs):
        result = {
            "tier": self.tier,
            "audits": self.audits,
            "profiles": getattr(self, "profiles", []),
            "evidence": [],
            "missing_context": [],
        }
        args[4]["routing"] = {"selected": result}
        return result

    def edit(self, api_key, name, prompt, input_text, **kwargs):
        self.assertEqual(name, "collator")
        payload = json.loads(input_text)
        findings = payload["findings"]
        self.assertNotIn("Rejected claim", input_text)
        concept_summary = None
        if payload.get("concept_assessment") is not None:
            concept_summary = (
                payload["concept_assessment"]["recommendation"]
                + " https://example.invalid/o/r/pulls/42"
            )
        return json.dumps(
            {
                "concept_summary": concept_summary,
                "findings": [
                    {"id": item["id"], "title": item["title"], "body": item["body"]}
                    for item in findings
                ],
            }
        ), {"status": "completed"}

    def run_review(
        self,
        reviewer,
        editor=None,
        budget=None,
        ppq_api_key=None,
        ppq_budget=None,
        archaeology_enabled=False,
    ):
        with (
            patch.object(pipeline.routing, "plan_review", side_effect=self.plan),
            patch.object(model, "openai_review", side_effect=reviewer),
            patch.object(model, "run_audit", side_effect=editor or self.edit),
            patch.object(
                protocol,
                "read_file",
                return_value="src/example.cpp (3 lines):\n3: setup();\n",
            ),
            patch.object(pipeline, "audit_developer_notes", return_value="Policy"),
        ):
            case = engine.ReviewCase("PR diff", self.snapshot, self.source)
            runtime = engine.Runtime(
                case=case,
                api_key="key",
                ppq_api_key=ppq_api_key,
                budget=budget,
                ppq_budget=ppq_budget,
                discussion_lookup=lambda name, args: "Discussion",
                attempt=self.debug,
            )
            self.result = pipeline.review_with_independent_passes(
                case,
                runtime,
                self.prompts,
                archaeology=archaeology_enabled,
            )
            return self.result.content

    def test_parallel_adversarial_models_share_context_and_attribute_merged_finding(
        self,
    ):
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
                inputs[stage] = (
                    args[1],
                    kwargs["prompt"],
                    kwargs["tools"],
                )
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
                    "candidate_findings"
                ]
                self.assertEqual([item["id"] for item in candidates], candidate_ids)
                return verification(
                    [
                        {
                            "candidate_ids": candidate_ids,
                            "disposition": "publish",
                            "reason": "Shared finding",
                            "finding": self.published,
                        }
                    ]
                )
            return discovery()

        self.run_review(
            review, ppq_api_key="ppq-key", budget=openai_budget, ppq_budget=ppq_budget
        )
        ppq_budget.protect.assert_not_called()
        self.assertEqual(self.debug["ppq_budget"], ppq_budget.summary.return_value)
        self.assertEqual(self.debug["budget"], openai_budget.summary.return_value)
        self.assertEqual(inputs["adversarial"], inputs["adversarial_deepseek"])
        self.assertEqual(inputs["adversarial"], inputs["adversarial_gemini"])
        self.assertEqual(
            self.debug["stages"]["adversarial_gemini"]["status"], "completed"
        )
        finding = self.debug["finding_attribution"][0]
        self.assertEqual(
            finding["raised_by"],
            ["adversarial", "adversarial_deepseek", "adversarial_gemini"],
        )
        self.assertEqual(
            finding["raised_by_models"],
            sorted(self.prompts.models[stage] for stage in pipeline.ADVERSARIAL_STAGES),
        )
        self.assertEqual(
            self.debug["stages"]["adversarial_deepseek"]["status"], "completed"
        )

    def test_omitted_candidate_preserves_valid_finding_and_attribution(self):
        self.tier, self.audits = "routine", []

        def review(*args, **kwargs):
            if kwargs["stage_name"] == "verifier":
                return verification(
                    [
                        {
                            "candidate_ids": ["independent:1"],
                            "disposition": "publish",
                            "reason": "Checked against the caller",
                            "finding": self.published,
                        }
                    ]
                )
            return discovery([candidate(), candidate("Unanswered lead")])

        content = self.run_review(review)
        self.assertIn(self.published["body"], content)
        self.assertNotIn("Unanswered lead", content)
        self.assertEqual(self.debug["coverage"]["verification"]["status"], "partial")
        self.assertEqual(
            self.debug["finding_attribution"][0]["candidate_ids"], ["independent:1"]
        )
        unresolved = [
            decision
            for decision in self.debug["decisions"]
            if decision["disposition"] == "unresolved"
        ]
        self.assertEqual(unresolved[0]["candidate_ids"], ["independent:2"])

    def test_invalid_editor_preserves_verified_wording(self):
        self.tier, self.audits = "routine", []
        design = {
            **self.published,
            "kind": "design",
            "title": "Design concern",
            "body": "Prefer the existing approach for this interface.",
        }

        def review(*args, **kwargs):
            return (
                verification(
                    [
                        {
                            "candidate_ids": ["independent:1"],
                            "disposition": "publish",
                            "reason": "Verified",
                            "finding": design,
                        }
                    ]
                )
                if kwargs["stage_name"] == "verifier"
                else discovery([{**candidate(), "kind": "design"}])
            )

        self.debug = {}

        def edit(*args, **kwargs):
            return '{"concept_summary":null,"findings":[]}', {"status": "completed"}

        content = self.run_review(review, edit)
        self.assertIn(design["body"], content)
        self.assertIn("Design and approach", content)
        self.assertTrue(self.debug["stages"]["collator"]["used_verified_wording"])
        self.assertIsNone(self.debug["finding_attribution"][0]["edited_by"])
        finding = self.result.findings[0]
        self.assertEqual(finding["body"], design["body"])
        self.assertEqual(finding["presented_body"], design["body"])
        self.assertIsNone(finding["edited_by"])

    def test_verified_material_concept_concern_gets_public_paragraph(self):
        self.tier, self.audits = "routine", []
        concern = {
            **assessment(),
            "assessment": "rework_approach",
            "proposed_review": "would_stop",
            "review_reason": "The process cost is decisive.",
            "recommendation": "Doing nothing is conceptually stronger because the PR adds process cost.",
        }

        def review(*args, **kwargs):
            if kwargs["stage_name"] == "archaeologist":
                return archaeology(concern)
            if kwargs["stage_name"] == "verifier":
                return verification(
                    concept={
                        "disposition": "publish",
                        "reason": "The cited discussion supports a material process cost.",
                        "assessment": concern,
                        "proposed_review": "would_stop",
                        "review_reason": "The verified process cost would stop the review.",
                    }
                )
            return discovery()

        content = self.run_review(review, archaeology_enabled=True)

        self.assertIn("Concept and approach", content)
        self.assertIn("process cost", content)
        self.assertIn("https://example.invalid/o/r/pulls/42", content)
        self.assertNotIn("Alternatives considered", content)
        self.assertNotIn("**Do nothing**", content)
        self.assertEqual(self.debug["concept_assessment"]["status"], "verified")
        self.assertTrue(self.result.concept_concern)


if __name__ == "__main__":
    unittest.main()
