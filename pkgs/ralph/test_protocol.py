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

    def collation(self, findings, concept_summary=None):
        return json.dumps({"concept_summary": concept_summary, "findings": findings})

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

    def test_collator_cannot_add_remove_or_duplicate_accepted_ids(self):
        accepted = [
            {**self.finding, "id": "finding:1"},
            {**self.finding, "kind": "design", "id": "finding:2"},
        ]
        for edits in (
            [],
            [{"id": "invented", "title": "New", "summary": "Impact", "body": "Claim"}],
            [{"id": "finding:1", "title": "A", "summary": "Impact", "body": "B"}] * 2,
        ):
            with self.subTest(edits=edits), self.assertRaises(protocol.InvalidReview):
                protocol.collation(self.collation(edits), accepted)
        edited, concept_summary = protocol.collation(
            self.collation(
                [
                    {
                        "id": "finding:1",
                        "title": "Short title",
                        "summary": "Edited impact",
                        "body": "Edited wording",
                    },
                    {
                        "id": "finding:2",
                        "title": "Design title",
                        "summary": "Design impact",
                        "body": "Design wording",
                    },
                ]
            ),
            accepted,
        )
        self.assertIsNone(concept_summary)
        self.assertEqual(edited[0]["path"], self.finding["path"])
        self.assertEqual(edited[0]["severity"], "suggestion")
        self.assertEqual(edited[0]["summary"], "Edited impact")
        self.assertEqual(edited[1]["kind"], "design")
        self.assertIn(
            "incomplete",
            final_comment.render(edited, ["A selected audit was unavailable."]),
        )

    def test_public_findings_require_explicit_nonempty_summary(self):
        decision = {
            "candidate_ids": ["tests:1"],
            "disposition": "publish",
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

                edit = {
                    "id": "finding:1",
                    "title": finding["title"],
                    "body": finding["body"],
                }
                if summary is not None:
                    edit["summary"] = summary
                with self.assertRaises(protocol.InvalidReview):
                    protocol.collation(
                        self.collation([edit]), [{**self.finding, "id": "finding:1"}]
                    )

    def test_finding_must_refer_to_changed_file_on_correct_side(self):
        decision = {
            "candidate_ids": ["tests:1"],
            "disposition": "publish",
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

    def test_publish_without_candidate_ids_withholds_new_verifier_finding(self):
        decision = {
            "candidate_ids": [],
            "disposition": "publish",
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

    def test_collator_cannot_add_unverified_concept_sources(self):
        concept = self.concept_assessment()
        summary = protocol.concept_summary(concept, "The cost outweighs the benefit.")
        response = self.collation([], summary + " https://example.invalid/invented")
        with self.assertRaisesRegex(
            protocol.InvalidReview, "unverified concept citation"
        ):
            protocol.collation(response, [], concept)


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

    def test_unchanged_repeat_is_compact_without_author_response(self):
        finding = self.finding()
        # A PR-introduced regression can still repeat the previous report.
        memory = self.memory()
        rendered = final_comment.render([finding], review_memory=[memory])
        self.assertNotIn("##### Findings", rendered)
        self.assertIn(
            "<summary>[Major · still present] Existing index problem</summary>",
            rendered,
        )
        self.assertEqual(rendered.count(finding["body"]), 1)
        self.assertIn(finding["summary"], rendered)
        self.assertNotIn(memory["reason"], rendered)
        self.assertIn("current-revision", rendered)
        self.assertIn("[Original review](https://example.org/old-report)", rendered)

        memory["unchanged_since_report"] = False
        memory["unchanged"] = True
        rendered = final_comment.render([finding], review_memory=[memory])
        self.assertIn("##### Findings", rendered)
        self.assertIn(finding["summary"], rendered.split("<details>", 1)[0])

    def test_uncertain_and_resolved_findings_keep_their_records(self):
        memory = self.memory(
            status="unclear", unchanged_since_report=False, current_finding_ids=[]
        )
        rendered = final_comment.render([], review_memory=[memory])
        self.assertIn("[Major · not confirmed]", rendered)
        self.assertIn("Not confirmed against this revision", rendered)
        self.assertIn(memory["body"], rendered)
        for status in ("fixed", "invalid"):
            with self.subTest(status=status):
                memory["status"] = status
                rendered = final_comment.render([], review_memory=[memory])
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
        self.assertIn(memory["body"], final_comment.render([], review_memory=[memory]))

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
        rendered = final_comment.render([], review_memory=memory)
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
        rendered = final_comment.render([current], review_memory=memory)
        self.assertEqual(rendered.count(current["body"]), 1)
        self.assertNotIn(duplicate["body"], rendered)
        self.assertNotIn("##### Findings", rendered)

    def test_retained_html_labels_are_escaped(self):
        memory = self.memory(title="Check <script> & bounds", current_finding_ids=[])
        rendered = final_comment.render([], review_memory=[memory])
        self.assertIn("Check &lt;script&gt; &amp; bounds</summary>", rendered)
        self.assertNotIn("<script>", rendered)

    def test_each_current_finding_checks_all_linked_prior_groups(self):
        first = self.finding(body="First current explanation.")
        second = self.finding(
            id="finding:2", title="Second issue", body="Second current explanation."
        )
        shared = self.memory(current_finding_ids=["finding:1", "finding:2"])
        rendered = final_comment.render([first, second], review_memory=[shared])
        self.assertNotIn("##### Findings", rendered)
        self.assertEqual(rendered.count(shared["source_head"]), 2)
        for finding in (first, second):
            self.assertEqual(rendered.count(finding["body"]), 1)

        changed = self.memory(
            key="old:2",
            unchanged_since_report=False,
            source_head="another-revision",
            body="Another historical explanation.",
        )
        rendered = final_comment.render(
            [first, second], review_memory=[shared, changed]
        )
        main, history = rendered.split("##### Previous findings", 1)
        self.assertIn(first["body"], main)
        self.assertIn(second["body"], history)
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
        rendered = final_comment.render([], review_memory=[memory])
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
            rendered = final_comment.render(
                [], review_memory=[memory, duplicate], verification_failed=failed
            )
            for finding in memory["last_presented_findings"]:
                self.assertIn(finding["title"], rendered)
                self.assertEqual(finding["body"] in rendered, not failed)
            self.assertNotIn(memory["presented_body"], rendered)
            self.assertNotIn(memory["body"], rendered)
            if not failed:
                self.assertIn("[Critical · still present] Latest title", rendered)


if __name__ == "__main__":
    unittest.main()
