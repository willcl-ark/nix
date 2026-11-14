import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import final_comment
from ralph import protocol


class ProtocolTests(unittest.TestCase):
    def test_historical_context_keeps_supported_claims(self):
        url = "https://github.com/bitcoin/bitcoin/pull/5161"
        comment = url + "#issuecomment-60883023"
        source = {
            "name": "read_github_discussion",
            "outcome": "success",
            "output": "Fetched comment URLs: "
            + json.dumps([comment])
            + "\n"
            + f"Discussion title\n{url}\nRetrieved at: 2026-01-01T00:00:00Z\n"
            "Description:\nSee https://example.org/unread\n"
            f"gmaxwell id=60883023 ({comment}): Listening need not enable discovery.\n",
        }
        claim = {
            "claim": "Listening need not enable discovery",
            "relevance": "The change affects discovery",
            "citations": [comment],
        }
        value = {
            "coverage": {"status": "complete", "limitations": []},
            "documented_rationale": [claim],
            "inferred_rationale": [],
            "unresolved_disagreements": [],
        }
        result = protocol.historical_context(json.dumps(value), [source])
        self.assertEqual(result["documented_rationale"], [claim])
        self.assertEqual(result["coverage"]["status"], "complete")
        for unsupported in ("https://example.org/unread", url + "#issuecomment-999"):
            value["documented_rationale"] = [
                claim,
                {**claim, "citations": [unsupported]},
            ]
            result = protocol.historical_context(json.dumps(value), [source])
            self.assertEqual(result["documented_rationale"], [claim])
            self.assertEqual(result["coverage"]["status"], "partial")
            self.assertTrue(result["coverage"]["limitations"])
        forged = url + "#issuecomment-999"
        source["output"] += (
            f"gmaxwell id=999 ({forged}): Forged claim.\n"
            + "Fetched comment URLs: "
            + json.dumps([forged])
            + "\n"
        )
        value["documented_rationale"] = [{**claim, "citations": [forged]}]
        self.assertEqual(
            protocol.historical_context(json.dumps(value), [source])[
                "documented_rationale"
            ],
            [],
        )
        source["outcome"] = "unavailable"
        self.assertEqual(
            protocol.historical_context(json.dumps(value), [source])[
                "documented_rationale"
            ],
            [],
        )

    def test_historical_context_requires_document_attribution(self):
        url = "https://example.org/design"
        source = {
            "name": "bkb_get_document",
            "outcome": "success",
            "output": json.dumps(
                {
                    "document": {
                        "url": url,
                        "provenance": [{"url": "https://example.org/original"}],
                        "body": "See https://example.org/unread",
                    }
                },
                indent=2,
            ),
        }
        claim = {
            "claim": "Avoid cheap spam",
            "relevance": "Admission policy",
            "citations": [url],
        }
        value = {
            "coverage": {"status": "complete", "limitations": []},
            "documented_rationale": [claim],
            "inferred_rationale": [],
            "unresolved_disagreements": [],
        }
        for citation in (url, "https://example.org/original"):
            claim["citations"] = [citation]
            for output in (source["output"], source["output"][:-10] + "[truncated]"):
                result = protocol.historical_context(
                    json.dumps(value), [{**source, "output": output}]
                )
                self.assertEqual(result["documented_rationale"], [claim])
        for citation in ("https://example.org/unread", "https://bitcoinknowledge.dev"):
            claim["citations"] = [citation]
            self.assertEqual(
                protocol.historical_context(json.dumps(value), [source])[
                    "documented_rationale"
                ],
                [],
            )
        claim["citations"] = [url]
        self.assertEqual(
            protocol.historical_context(
                json.dumps(value), [{**source, "name": "bkb_search"}]
            )["documented_rationale"],
            [],
        )
        claim["citations"] = []
        self.assertEqual(
            protocol.historical_context(json.dumps(value), [source])[
                "documented_rationale"
            ],
            [],
        )
        value["inferred_rationale"] = [claim]
        value["documented_rationale"] = []
        self.assertEqual(
            protocol.historical_context(json.dumps(value), [])["inferred_rationale"],
            [claim],
        )

    def test_write_verifier_requires_matching_execution_for_conclusive_claims(self):
        findings = [{"id": "finding:1"}]
        command = {
            "id": "cmd-1",
            "finding_id": "finding:1",
            "side": "head",
            "status": "completed",
            "capture_complete": True,
        }
        result = {
            "finding_id": "finding:1",
            "outcome": "reproduced",
            "reason": "Regression assertion failed",
            "command_ids": ["cmd-1"],
        }

        def verify(item=result, commands=None):
            return protocol.write_verification(
                json.dumps({"results": [item]}),
                findings,
                {"commands": [command] if commands is None else commands},
            )

        self.assertEqual(verify()["results"], [result])
        with self.assertRaises(protocol.InvalidReview):
            verify(commands=[])
        with self.assertRaises(protocol.InvalidReview):
            verify(commands=[{**command, "finding_id": "finding:2"}])
        with self.assertRaises(protocol.InvalidReview):
            verify(commands=[{**command, "status": "timeout"}])
        with self.assertRaises(protocol.InvalidReview):
            verify(commands=[{**command, "capture_complete": False}])
        with self.assertRaises(protocol.InvalidReview):
            verify(commands=[{**command, "side": "base"}])
        with self.assertRaises(protocol.InvalidReview):
            verify({**result, "outcome": "refuted", "command_ids": []})
        inconclusive = {**result, "outcome": "inconclusive", "command_ids": []}
        self.assertEqual(verify(inconclusive, [])["results"], [inconclusive])
        with self.assertRaises(protocol.InvalidReview):
            protocol.write_verification('{"results": []}', findings, {"commands": []})

    def setUp(self):
        self.snapshot = SimpleNamespace(
            changed_paths={"src/example.cpp"},
            head_files={"src/example.cpp": "blob"},
            base_files={"src/example.cpp": "old"},
        )
        self.finding = {
            "kind": "suggestion",
            "severity": "suggestion",
            "path": "src/example.cpp",
            "line": 5,
            "side": "head",
            "title": "Consolidate duplicate setup",
            "summary": "Duplicate setup makes the regression test harder to maintain.",
            "body": "Reuse the existing fixture while keeping the regression assertion.",
            "security": {
                "classification": "non_security",
                "reason": "This only removes duplicate fixture setup.",
            },
            "source_evidence": [
                {
                    "path": "src/example.cpp",
                    "line": 5,
                    "side": "head",
                    "excerpt": "setup();",
                }
            ],
        }
        self.snapshot.checkout = "/unused"
        reader = patch.object(
            protocol,
            "read_file",
            return_value="src/example.cpp (6 lines):\n5: setup();\n",
        )
        reader.start()
        self.addCleanup(reader.stop)
        self.coverage = {"status": "complete", "limitations": []}

    def verification(self, decisions, coverage=None):
        return json.dumps(
            {
                "coverage": coverage or self.coverage,
                "decisions": decisions,
            }
        )

    def test_alternative_verdicts_reference_supplied_originals(self):
        option = {
            "name": "Reuse existing fixture",
            "concept": "Use the shared fixture for setup.",
            "benefit": "Keep maintenance in one place.",
            "cost": "Requires extracting the existing setup.",
            "unresolved": "No remaining questions.",
            "provenance": "agent_inference",
            "citations": [],
        }
        decision = {
            "source": "blind_alternatives",
            "index": 0,
            "disposition": "publish",
            "reason": "The existing fixture supports the proposed reuse.",
        }

        def verify(decisions):
            payload = {
                "coverage": self.coverage,
                "concept": {
                    "disposition": "no_concern",
                    "reason": "No concept candidate",
                    "proposed_review": "continue",
                    "review_reason": "No material concern",
                },
                "alternatives": decisions,
            }
            return protocol.concept_verification(
                json.dumps(payload), blind_alternatives=[option]
            )

        result = verify([decision])
        self.assertIsNone(result["alternatives_validation_error"])
        self.assertEqual(result["alternatives"], [decision])
        for decisions in (
            [],
            [decision, decision],
            [{**decision, "index": 1}],
            [{**decision, "source": "invented"}],
            [{**decision, "index": True}],
        ):
            with self.subTest(decisions=decisions):
                result = verify(decisions)
                self.assertIsNotNone(result["alternatives_validation_error"])
                self.assertEqual(result["alternatives"], [])

    def coverage_response(self, assessments, findings=()):
        return json.dumps(
            {
                "coverage": self.coverage,
                "requires_sensitive_review": False,
                "findings": list(findings),
                "test_coverage": assessments,
            }
        )

    def checked_test(self):
        return {
            "paths": ["src/example.cpp"],
            "status": "checked",
            "evidence": self.finding["source_evidence"],
            "regression": "Removing setup would leave the fixture uninitialized.",
            "reason": "The existing assertion requires the initialized fixture.",
        }

    def test_tests_coverage_cannot_omit_a_changed_functional_test(self):
        self.snapshot.changed_paths.add("test/functional/activation.py")
        result = protocol.tests_discovery(
            self.coverage_response([self.checked_test()]),
            self.snapshot,
            self.snapshot.changed_paths,
        )
        self.assertEqual(result["coverage"]["status"], "partial")
        self.assertIn(
            "test/functional/activation.py", " ".join(result["coverage"]["limitations"])
        )
        self.assertEqual(len(result["test_coverage"]), 1)

    def test_tests_coverage_can_use_unchanged_tests_and_confirm_a_gap(self):
        path = "test/functional/existing.py"
        self.snapshot.head_files[path] = "unchanged-test-blob"
        assessment = self.checked_test()
        assessment["evidence"] = [{**self.finding["source_evidence"][0], "path": path}]
        assessment["regression"] = (
            "Activating one block late would still pass the existing assertion."
        )
        assessment["reason"] = "The test only checks blocks after activation."
        result = protocol.tests_discovery(
            self.coverage_response([assessment]),
            self.snapshot,
            self.snapshot.changed_paths,
        )
        self.assertEqual(result["coverage"], self.coverage)
        self.assertEqual(result["test_coverage"], [assessment])

    def test_deferred_tests_force_partial_but_explained_mechanical_changes_do_not(self):
        assessment = {
            "paths": ["src/example.cpp"],
            "status": "deferred",
            "evidence": [],
            "regression": "",
            "reason": "The activation fixture and its boundary assertion remain unread.",
        }
        result = protocol.tests_discovery(
            self.coverage_response([assessment]),
            self.snapshot,
            self.snapshot.changed_paths,
        )
        self.assertEqual(result["coverage"]["status"], "partial")
        self.assertIn(assessment["reason"], " ".join(result["coverage"]["limitations"]))
        assessment.update(
            status="not_applicable", reason="Only the local variable name changes."
        )
        result = protocol.tests_discovery(
            self.coverage_response([assessment]),
            self.snapshot,
            self.snapshot.changed_paths,
        )
        self.assertEqual(result["coverage"], self.coverage)

    def test_invalid_test_evidence_does_not_discard_a_supported_candidate(self):
        assessment = self.checked_test()
        assessment["evidence"] = [
            {**self.finding["source_evidence"][0], "excerpt": "invented_assertion();"}
        ]
        candidate = {
            "kind": "suggestion",
            "path": "src/example.cpp",
            "line": 5,
            "side": "head",
            "title": "Add an activation-boundary assertion",
            "claim": "The first active block is not checked.",
            "consequence": "A delayed activation would pass the test.",
            "evidence": "The test advances beyond activation before checking violations.",
            "correction": "Check a violation in the first active block.",
            "uncertainty": "",
        }
        result = protocol.tests_discovery(
            self.coverage_response([assessment], [candidate]),
            self.snapshot,
            self.snapshot.changed_paths,
        )
        self.assertEqual(result["findings"], [{**candidate, "id": "tests:1"}])
        self.assertEqual(result["test_coverage"], [])
        self.assertEqual(result["coverage"]["status"], "partial")
        self.assertIn("does not match", " ".join(result["coverage"]["limitations"]))

    def test_backport_verdict_requires_complete_evidence(self):
        assessment = {
            "verdict": "Faithful",
            "summary": "All originals checked",
            "evidence": ["abc mapped to def"],
            "required_fixes": [],
            "limitations": [],
        }
        complete = protocol.backport_assessment(
            assessment, {"complete": True, "limitations": []}
        )
        self.assertEqual(complete["verdict"], "Faithful")
        incomplete = protocol.backport_assessment(
            assessment, {"complete": False, "limitations": []}
        )
        self.assertEqual(incomplete["verdict"], "Needs clarification")
        self.assertTrue(incomplete["limitations"])
        with self.assertRaises(protocol.InvalidReview):
            protocol.backport_assessment(
                {**assessment, "evidence": "invented"}, {"complete": True}
            )

    def judgment_response(self, decisions, concept=None):
        return json.dumps(
            {"coverage": self.coverage, "decisions": decisions, "concept": concept}
        )

    def judge_decision(self, identifier, disposition="publish"):
        return {
            "finding_id": identifier,
            "disposition": disposition,
            "reason": "The source supports the finding and its severity.",
            "source_evidence": self.finding["source_evidence"],
            "title": "Short title",
            "summary": "Edited impact",
            "body": "Edited wording",
        }

    def concept_assessment(self, citations=None):
        return {
            "goal": "Reduce review friction for repeated manual checks.",
            "problem": "The proposal solves a narrow workflow problem.",
            "baseline": "Maintainers can keep the current workflow.",
            "delivered_benefit": "The PR removes one manual step.",
            "relevant_history": "Prior discussion raised a layering concern.",
            "assessment": "rework_approach",
            "alternatives": [
                {
                    "name": "Do nothing",
                    "concept": "Keep the current behavior.",
                    "benefit": "No extra maintenance.",
                    "cost": "The manual step remains.",
                    "unresolved": "Impact is not measured.",
                    "provenance": "agent_inference",
                    "citations": [],
                }
            ],
            "recommendation": "The alternative is conceptually stronger.",
            "proposed_review": "would_stop",
            "review_reason": "The alternative preserves the benefit with less maintenance cost.",
            "decisive_question": "Whether the manual step affects common review paths.",
            "technical_assumptions": ["Verifier should check the call path."],
            "citations": citations or ["https://code.example.org/org/repo/pulls/1"],
        }

    def test_concept_verification_requires_supplied_publishable_assessment(self):
        candidate = {**self.concept_assessment(), "alternatives": []}
        payload = {
            "coverage": self.coverage,
            "concept": {
                "disposition": "publish",
                "reason": "The original objection is supported as written.",
                "proposed_review": "would_stop",
                "review_reason": "The maintenance burden outweighs the benefit.",
            },
            "alternatives": [],
        }
        checked = protocol.concept_verification(json.dumps(payload), candidate)
        self.assertEqual(checked["concept"], payload["concept"])
        self.assertIsNone(checked["concept_validation_error"])
        for supplied in (None, {**candidate, "assessment": "worth_pursuing"}):
            checked = protocol.concept_verification(json.dumps(payload), supplied)
            self.assertEqual(checked["concept"]["disposition"], "unresolved")
            self.assertEqual(checked["coverage"]["status"], "partial")
        with self.assertRaises(protocol.InvalidReview):
            protocol.verification(json.dumps(payload), [], self.snapshot)

    def test_judge_withholds_missing_duplicate_and_invented_finding_decisions(self):
        verified = [
            {**self.finding, "id": "finding:1"},
            {**self.finding, "id": "finding:2"},
        ]
        result, published, summary = protocol.judgment(
            self.judgment_response(
                [self.judge_decision("finding:1")] * 2
                + [self.judge_decision("invented")]
            ),
            verified,
            None,
            self.snapshot,
        )
        self.assertEqual(published, [])
        self.assertIsNone(summary)
        self.assertEqual(result["coverage"]["status"], "partial")
        self.assertEqual(
            [decision["finding_id"] for decision in result["decisions"]],
            ["finding:1", "finding:2"],
        )
        self.assertTrue(
            all(item["disposition"] == "unresolved" for item in result["decisions"])
        )

    def test_judge_controls_publication_and_only_changes_wording(self):
        verified = [
            {**self.finding, "id": "finding:1"},
            {**self.finding, "kind": "design", "id": "finding:2"},
        ]
        decision = self.judge_decision("finding:1")
        drop = self.judge_decision("finding:2", "drop")
        drop.update(title="", summary="", body="", source_evidence=[])
        result, published, summary = protocol.judgment(
            self.judgment_response([decision, drop]), verified, None, self.snapshot
        )
        self.assertEqual(result["coverage"], self.coverage)
        self.assertIsNone(summary)
        self.assertEqual(
            published,
            [
                {
                    **verified[0],
                    **{
                        field: decision[field] for field in ("title", "summary", "body")
                    },
                }
            ],
        )

    def test_judge_requires_exact_evidence_for_publication(self):
        verified = [
            {**self.finding, "id": "finding:1"},
            {**self.finding, "id": "finding:2"},
        ]
        invalid = self.judge_decision("finding:2")
        invalid["source_evidence"] = [
            {**self.finding["source_evidence"][0], "excerpt": "invented();"}
        ]
        result, published, _ = protocol.judgment(
            self.judgment_response([self.judge_decision("finding:1"), invalid]),
            verified,
            None,
            self.snapshot,
        )
        self.assertEqual([finding["id"] for finding in published], ["finding:1"])
        self.assertEqual(result["decisions"][1]["disposition"], "unresolved")
        self.assertIn("does not match", result["decisions"][1]["reason"])

    def test_challenger_preserves_valid_counterevidence_and_accounts_for_every_candidate(
        self,
    ):
        good = {
            "candidate_id": "tests:1",
            "outcome": "refuted",
            "reason": "The caller already initializes the fixture.",
            "source_evidence": self.finding["source_evidence"],
        }
        duplicate = {**good, "candidate_id": "tests:2", "outcome": "not_refuted"}
        result = protocol.challenge(
            json.dumps(
                {
                    "coverage": self.coverage,
                    "challenges": [
                        good,
                        duplicate,
                        duplicate,
                        {**good, "candidate_id": "invented"},
                    ],
                }
            ),
            [{"id": f"tests:{i}"} for i in range(1, 4)],
            self.snapshot,
        )
        self.assertEqual(result["challenges"][0], good)
        self.assertEqual(
            [item["outcome"] for item in result["challenges"]],
            ["refuted", "unresolved", "unresolved"],
        )
        self.assertEqual(result["coverage"]["status"], "partial")

    def test_challenger_refutation_requires_matching_source(self):
        item = {
            "candidate_id": "tests:1",
            "outcome": "refuted",
            "reason": "The caller initializes the fixture.",
            "source_evidence": [
                {**self.finding["source_evidence"][0], "excerpt": "invented();"}
            ],
        }
        result = protocol.challenge(
            json.dumps({"coverage": self.coverage, "challenges": [item]}),
            [{"id": "tests:1"}],
            self.snapshot,
        )
        self.assertEqual(result["challenges"][0]["outcome"], "unresolved")
        self.assertEqual(result["challenges"][0]["source_evidence"], [])
        item.update(outcome="not_refuted", source_evidence=[])
        result = protocol.challenge(
            json.dumps({"coverage": self.coverage, "challenges": [item]}),
            [{"id": "tests:1"}],
            self.snapshot,
        )
        self.assertEqual(result["coverage"], self.coverage)

    def test_omitted_null_finding_preserves_verified_findings(self):
        decisions = [
            {
                "candidate_ids": ["tests:1"],
                "disposition": "supported",
                "reason": "Verified",
                "finding": self.finding,
            },
            {
                "candidate_ids": ["tests:2"],
                "disposition": "refuted",
                "reason": "Duplicate suggestion",
            },
            {
                "candidate_ids": ["tests:3"],
                "disposition": "unresolved",
                "reason": "Insufficient evidence",
            },
        ]
        candidates = [{"id": f"tests:{index}"} for index in range(1, 4)]
        result, accepted = protocol.verification(
            self.verification(decisions), candidates, self.snapshot
        )
        self.assertEqual(accepted, [{**self.finding, "id": "finding:1"}])
        self.assertEqual(
            [decision["disposition"] for decision in result["decisions"]],
            ["supported", "refuted", "unresolved"],
        )
        self.assertIsNone(result["decisions"][1]["finding"])
        self.assertIsNone(result["decisions"][2]["finding"])

        decisions[1]["disposition"] = "supported"
        with self.assertRaisesRegex(protocol.InvalidReview, "missing.*finding"):
            protocol.verification(
                self.verification(decisions), candidates, self.snapshot
            )

        decisions[1]["disposition"] = "refuted"
        decisions[1]["findings"] = []
        with self.assertRaisesRegex(protocol.InvalidReview, "unexpected.*findings"):
            protocol.verification(
                self.verification(decisions), candidates, self.snapshot
            )

    def test_public_findings_require_explicit_nonempty_summary(self):
        decision = {
            "candidate_ids": ["tests:1"],
            "disposition": "supported",
            "reason": "Verified",
            "finding": self.finding,
        }
        _, accepted = protocol.verification(
            self.verification([decision]), [{"id": "tests:1"}], self.snapshot
        )
        self.assertEqual(accepted[0]["summary"], self.finding["summary"])
        for summary in (None, "", "  "):
            finding = dict(self.finding)
            if summary is None:
                del finding["summary"]
            else:
                finding["summary"] = summary
            with self.subTest(summary=summary):
                result, accepted = protocol.verification(
                    self.verification([{**decision, "finding": finding}]),
                    [{"id": "tests:1"}],
                    self.snapshot,
                )
                self.assertEqual(accepted, [])
                self.assertEqual(result["decisions"][0]["disposition"], "unresolved")

                publication = self.judge_decision("finding:1")
                if summary is None:
                    del publication["summary"]
                else:
                    publication["summary"] = summary
                result, published, _ = protocol.judgment(
                    self.judgment_response([publication]),
                    [{**self.finding, "id": "finding:1"}],
                    None,
                    self.snapshot,
                )
                self.assertEqual(published, [])
                self.assertEqual(result["decisions"][0]["disposition"], "unresolved")

    def test_finding_must_refer_to_changed_file_on_correct_side(self):
        decision = {
            "candidate_ids": ["tests:1"],
            "disposition": "supported",
            "reason": "Verified",
            "finding": {**self.finding, "path": "missing.cpp"},
        }
        result, accepted = protocol.verification(
            self.verification([decision]), [{"id": "tests:1"}], self.snapshot
        )
        self.assertEqual(accepted, [])
        self.assertEqual(result["coverage"]["status"], "partial")
        self.assertIn("changed file", result["decisions"][0]["reason"])
        self.assertEqual(result["decisions"][0]["disposition"], "unresolved")
        self.assertIsNone(result["decisions"][0]["finding"])
        self.assertEqual(result["validation_errors"][0]["candidate_ids"], ["tests:1"])
        self.assertIn("missing.cpp", result["validation_errors"][0]["error"])

    def test_support_without_candidate_ids_withholds_new_verifier_finding(self):
        decision = {
            "candidate_ids": [],
            "disposition": "supported",
            "reason": "New issue",
            "finding": self.finding,
        }

        result, accepted = protocol.verification(
            self.verification([decision]), [], self.snapshot
        )

        self.assertEqual(accepted, [])
        self.assertEqual(result["coverage"]["status"], "partial")
        self.assertEqual(result["decisions"][0]["disposition"], "unresolved")
        self.assertIsNone(result["decisions"][0]["finding"])
        self.assertEqual(result["validation_errors"][0]["candidate_ids"], [])

    def test_judge_cannot_add_unverified_concept_sources(self):
        concept = self.concept_assessment()
        summary = protocol.concept_summary(concept, "The cost outweighs the benefit.")
        decision = {
            "disposition": "publish",
            "reason": "The verified objection remains material.",
            "summary": summary + " https://example.invalid/invented",
        }
        result, published, rendered_summary = protocol.judgment(
            self.judgment_response([], decision), [], concept, self.snapshot
        )
        self.assertEqual(published, [])
        self.assertIsNone(rendered_summary)
        self.assertEqual(result["concept"]["disposition"], "unresolved")
        self.assertIn("unverified concept citation", result["concept"]["reason"])
        decision["summary"] = summary
        result, _, rendered_summary = protocol.judgment(
            self.judgment_response([], decision), [], concept, self.snapshot
        )
        self.assertEqual(result["concept"]["disposition"], "publish")
        self.assertEqual(rendered_summary, summary)
        decision.update(disposition="drop", summary="")
        _, _, rendered_summary = protocol.judgment(
            self.judgment_response([], decision), [], concept, self.snapshot
        )
        self.assertIsNone(rendered_summary)


class ReviewMemoryRenderingTests(unittest.TestCase):
    def finding(self, **changes):
        return {
            "id": "finding:1",
            "kind": "bug",
            "title": "Existing index problem",
            "summary": "An interrupted write loses the index.",
            "body": "Keep the old index until replacement completes.",
            "severity": "major",
            "path": "src/example.cpp",
            "line": 5,
            "side": "head",
            **changes,
        }

    def memory(self, **changes):
        return {
            **self.finding(),
            "key": "old:1",
            "status": "still_present",
            "unchanged": False,
            "unchanged_since_report": True,
            "source_head": "original-revision",
            "checked_head": "current-revision",
            "source_report_url": "https://example.org/old-report",
            "author_position": "none",
            "current_finding_ids": ["finding:1"],
            "reason": "Lengthy repeated verification proof.",
            **changes,
        }

    def test_unchanged_repeat_is_a_visible_current_finding(self):
        finding = self.finding()
        memory = self.memory()
        rendered = final_comment.render_report([finding], review_memory=[memory])
        self.assertEqual(rendered.count(finding["body"]), 1)
        self.assertIn(finding["summary"], rendered)
        self.assertNotIn(memory["reason"], rendered)
        self.assertNotIn(memory["source_report_url"], rendered)

        memory["unchanged_since_report"] = False
        memory["unchanged"] = True
        rendered = final_comment.render_report([finding], review_memory=[memory])
        self.assertIn(finding["summary"], rendered.split("<details>", 1)[0])

    def test_report_includes_all_current_findings_without_promoting_history(self):
        memory = self.memory(current_finding_ids=[])
        findings = [
            self.finding(id=f"new:{i}", title=f"Suggestion {i}", severity="suggestion")
            for i in range(7)
        ]
        rendered = final_comment.render_report(
            findings, review_memory=[memory], head_sha="current-revision"
        )
        self.assertNotIn(memory["title"], rendered)
        self.assertNotIn(memory["reason"], rendered)
        self.assertIn("Earlier concerns remain unresolved", rendered)
        for finding in findings:
            self.assertIn(finding["title"], rendered)

    def test_history_is_not_republished_without_current_verification(self):
        memory = self.memory(
            status="unclear", unchanged_since_report=False, current_finding_ids=[]
        )
        rendered = final_comment.render_report([], review_memory=[memory])
        self.assertNotIn("I found no actionable issues", rendered)
        self.assertIn("Earlier concerns remain unresolved", rendered)
        self.assertNotIn(memory["body"], rendered)
        for status in ("fixed", "invalid"):
            with self.subTest(status=status):
                memory["status"] = status
                rendered = final_comment.render_report([], review_memory=[memory])
                self.assertIn("no actionable findings", rendered)
                self.assertNotIn(memory["body"], rendered)
                self.assertNotIn("remain unresolved", rendered)


if __name__ == "__main__":
    unittest.main()
