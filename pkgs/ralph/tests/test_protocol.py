import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import final_comment
from ralph import protocol


class ProtocolTests(unittest.TestCase):
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

    def verification(self, decisions, coverage=None, concept=None):
        return json.dumps(
            {
                "coverage": coverage or self.coverage,
                "concept": concept
                or {
                    "disposition": "no_concern",
                    "reason": "No material concept concern",
                    "assessment": None,
                    "proposed_review": "continue",
                    "review_reason": "The verifier found no material concept concern.",
                },
                "alternatives": [],
                "decisions": decisions,
            }
        )

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
        # A PR-introduced regression can still repeat the previous report.
        memory = self.memory()
        rendered = final_comment.render_report([finding], review_memory=[memory])
        self.assertIn(
            "**🔴 Major · Existing index problem**",
            rendered,
        )
        self.assertEqual(rendered.count(finding["body"]), 1)
        self.assertIn(finding["summary"], rendered)
        self.assertNotIn(memory["reason"], rendered)
        self.assertIn("current-revision", rendered)
        self.assertIn("[Original review](https://example.org/old-report)", rendered)

        memory["unchanged_since_report"] = False
        memory["unchanged"] = True
        rendered = final_comment.render_report([finding], review_memory=[memory])
        self.assertIn(finding["summary"], rendered.split("<details>", 1)[0])

    def test_head_verified_history_joins_current_findings_and_overflow(self):
        memory = self.memory(
            current_finding_ids=[],
            source_evidence=[{"path": "current.cpp", "line": 12, "side": "head"}],
        )
        findings = [
            self.finding(id=f"new:{i}", title=f"Suggestion {i}", severity="suggestion")
            for i in range(7)
        ]
        rendered = final_comment.render_report(
            findings,
            review_memory=[memory],
            head_sha="current-revision",
        )
        main, extra = rendered.split(
            "<summary>Additional verified findings (2)</summary>"
        )
        self.assertIn("🔴 Major", main)
        self.assertIn(memory["title"], main)
        self.assertIn(memory["reason"], main)
        self.assertIn("current.cpp:12", main)
        self.assertIn("Suggestion 4", main)
        self.assertIn("Suggestion 5", extra)
        self.assertIn("Suggestion 6", extra)
        self.assertNotIn("Previous findings", rendered)
        self.assertIn(memory["body"], rendered)
        self.assertIn("previously reported", rendered)

        duplicate = {**memory, "key": "old:duplicate", "duplicate_of": memory["key"]}
        rendered = final_comment.render_report(
            [],
            review_memory=[{**memory, "status": "unclear"}, duplicate],
            head_sha="current-revision",
        )
        self.assertIn("🔴 Major", rendered.split("<details>", 1)[0])
        self.assertIn("Original review", rendered)
        self.assertNotIn("Unconfirmed previous findings", rendered)
        self.assertEqual(rendered.count(memory["body"]), 1)

        for changes in (
            {"checked_head": "stale-revision"},
            {"status": "unclear"},
            {"source_evidence": []},
            {"current_candidate_ids": ["withheld:1"]},
        ):
            with self.subTest(changes=changes):
                rendered = final_comment.render_report(
                    [],
                    review_memory=[{**memory, **changes}],
                    head_sha="current-revision",
                )
                main, history = rendered.split("<summary>Unconfirmed previous findings")
                self.assertNotIn(memory["title"], main)
                self.assertIn(memory["title"], history)

    def test_uncertain_and_resolved_findings_keep_their_records(self):
        memory = self.memory(
            status="unclear", unchanged_since_report=False, current_finding_ids=[]
        )
        rendered = final_comment.render_report([], review_memory=[memory])
        clean_result = final_comment.render_report([])
        self.assertNotIn(clean_result, rendered)
        self.assertIn("[Major · not confirmed]", rendered)
        self.assertIn("Not confirmed against this revision", rendered)
        self.assertIn(memory["body"], rendered)
        for status in ("fixed", "invalid"):
            with self.subTest(status=status):
                memory["status"] = status
                rendered = final_comment.render_report([], review_memory=[memory])
                self.assertTrue(rendered.startswith(clean_result))
                self.assertIn("<summary>Resolved findings</summary>", rendered)
                self.assertIn(memory["body"], rendered)
                self.assertIn(
                    "Fixed."
                    if status == "fixed"
                    else "No longer considered actionable.",
                    rendered,
                )
                self.assertIn(memory["reason"], rendered)
                self.assertNotIn("remain unresolved", rendered)

        # Existing review records still retain their explanation.
        del memory["summary"]
        self.assertIn(
            memory["body"], final_comment.render_report([], review_memory=[memory])
        )

    def test_duplicate_reports_share_one_body_and_keep_each_origin(self):
        original = self.memory(
            severity="minor",
            author_position="wontfix",
            author_comments=[{"url": "https://example.org/reply"}],
        )
        duplicate = self.memory(
            key="old:2",
            duplicate_of="old:1",
            source_head="second-revision",
            line=9,
            body="Duplicate concern body",
        )
        memory = [original, duplicate]
        rendered = final_comment.render_report([], review_memory=memory)
        self.assertEqual(rendered.count(duplicate["body"]), 1)
        self.assertNotIn(original["body"], rendered)
        for origin in (
            "src/example.cpp:5",
            "src/example.cpp:9",
            "original-revision",
            "second-revision",
        ):
            self.assertIn(origin, rendered)
        self.assertIn("[Author reply](https://example.org/reply)", rendered)
        current = self.finding(body="Current verified explanation.")
        rendered = final_comment.render_report([current], review_memory=memory)
        self.assertEqual(rendered.count(current["body"]), 1)
        self.assertNotIn(duplicate["body"], rendered)

    def test_retained_html_labels_are_escaped(self):
        memory = self.memory(title="Check `<script>` & bounds", current_finding_ids=[])
        rendered = final_comment.render_report([], review_memory=[memory])
        self.assertIn(
            "Check <code>&lt;script&gt;</code> &amp; bounds</summary>", rendered
        )
        self.assertNotIn("<script>", rendered)

    def test_each_current_finding_checks_all_linked_prior_groups(self):
        first = self.finding(body="First current explanation.")
        second = self.finding(
            id="finding:2", title="Second issue", body="Second current explanation."
        )
        shared = self.memory(current_finding_ids=["finding:1", "finding:2"])
        rendered = final_comment.render_report([first, second], review_memory=[shared])
        self.assertEqual(rendered.count(shared["source_head"]), 2)
        for finding in (first, second):
            self.assertEqual(rendered.count(finding["body"]), 1)

        changed = self.memory(
            key="old:2",
            unchanged_since_report=False,
            source_head="another-revision",
            body="Another historical explanation.",
        )
        rendered = final_comment.render_report(
            [first, second], review_memory=[shared, changed]
        )
        main = rendered
        self.assertIn(first["body"], main)
        self.assertIn(second["body"], main)
        for entry in (shared, changed):
            self.assertIn(entry["source_head"], main)
            self.assertNotIn(entry["body"], rendered)
        self.assertNotIn("Previous reports of the same concern", rendered)

    def test_historical_entries_preserve_their_published_wording(self):
        memory = self.memory(
            presented_title="Published title",
            presented_summary="Published summary.",
            presented_body="Published explanation.",
        )
        rendered = final_comment.render_report([], review_memory=[memory])
        for field in ("title", "summary", "body"):
            self.assertIn(memory["presented_" + field], rendered)
            self.assertNotIn(memory[field], rendered)

        memory["last_presented_findings"] = [
            self.finding(
                title="Latest title",
                summary="Latest summary.",
                body="Latest explanation.",
                severity="critical",
            ),
            self.finding(
                title="Second latest title",
                summary="Second latest summary.",
                body="Second latest explanation.",
                severity="minor",
            ),
        ]
        memory["severity"] = "minor"
        duplicate = self.memory(key="old:2", duplicate_of="old:1")
        for failed in (False, True):
            rendered = final_comment.render_report(
                [], review_memory=[memory, duplicate], verification_failed=failed
            )
            for finding in memory["last_presented_findings"]:
                self.assertIn(finding["title"], rendered)
                self.assertEqual(finding["body"] in rendered, not failed)
            self.assertNotIn(memory["presented_body"], rendered)
            self.assertNotIn(memory["body"], rendered)
            if not failed:
                self.assertIn("[Critical · not confirmed] Latest title", rendered)


if __name__ == "__main__":
    unittest.main()
