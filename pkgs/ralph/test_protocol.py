import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ralph import protocol


class ProtocolTests(unittest.TestCase):
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
            [{"id": "invented", "title": "New", "body": "Claim"}],
            [{"id": "finding:1", "title": "A", "body": "B"}] * 2,
        ):
            with self.subTest(edits=edits), self.assertRaises(protocol.InvalidReview):
                protocol.collation(self.collation(edits), accepted)
        edited, concept_summary = protocol.collation(
            self.collation(
                [
                    {
                        "id": "finding:1",
                        "title": "Short title",
                        "body": "Edited wording",
                    },
                    {
                        "id": "finding:2",
                        "title": "Design title",
                        "body": "Design wording",
                    },
                ]
            ),
            accepted,
        )
        self.assertIsNone(concept_summary)
        self.assertEqual(edited[0]["path"], self.finding["path"])
        self.assertEqual(edited[0]["severity"], "suggestion")
        self.assertEqual(edited[1]["kind"], "design")
        self.assertIn(
            "incomplete", protocol.render(edited, ["A selected audit was unavailable."])
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
    def test_unresolved_history_survives_absent_rediscovery(self):
        memory = [
            {
                "title": "Existing index problem",
                "body": "Check `block->nStatus` before use.",
                "severity": "major",
                "status": "unclear",
                "author_position": "none",
                "path": "src/example.cpp",
                "line": 5,
                "side": "head",
                "source_head": "original-revision",
            }
        ]
        rendered = protocol.render(
            [], ["Verifier request failed."], review_memory=memory
        )
        self.assertIn("##### Findings", rendered)
        self.assertIn("**Major**", rendered)
        self.assertIn("**Existing index problem** (src/example.cpp:5, head)", rendered)
        self.assertIn("original-revision", rendered)
        self.assertIn("not confirmed against this revision", rendered)
        self.assertIn("Check `block->nStatus` before use.", rendered)
        self.assertIn(
            "##### Review limitations\n\n- Verifier request failed.", rendered
        )
        self.assertNotIn("no actionable", rendered)
        self.assertNotIn("<", rendered)
        self.assertNotIn("&gt;", rendered)

        for status in ("fixed", "invalid"):
            memory[0]["status"] = status
            self.assertNotIn(
                "Existing index problem", protocol.render([], review_memory=memory)
            )
        memory[0]["status"] = "unclear"

        memory.append(
            {
                "title": "Author discussed this finding",
                "body": "A separate concern",
                "severity": "minor",
                "status": "still_present",
                "author_position": "wontfix",
                "author_comments": [{"url": "https://example.org/reply"}],
            }
        )
        rendered = protocol.render([], review_memory=memory)
        main, history = rendered.split(
            "##### Previous findings and author responses", 1
        )
        self.assertIn("Existing index problem", main)
        self.assertNotIn("Existing index problem", history)
        self.assertNotIn("Author discussed this finding", main)
        self.assertIn("Author discussed this finding", history)
        self.assertIn("[Author reply](https://example.org/reply)", history)
        self.assertIn("verified as still present", history)
        self.assertNotIn("<", rendered)

    def test_only_confirmed_repeat_is_collapsed(self):
        finding = {
            "id": "finding:1",
            "kind": "bug",
            "severity": "major",
            "title": "Repeated issue",
            "path": "a.cpp",
            "line": 1,
            "side": "head",
            "body": "Repeated body",
        }
        memory = [
            {
                **finding,
                "status": "still_present",
                "collapsed": True,
                "author_position": "address_elsewhere",
                "author_comments": [{"url": "https://example.org/reply"}],
                "current_finding_ids": ["finding:1"],
            }
        ]
        rendered = protocol.render([finding], review_memory=memory)
        self.assertNotIn("##### Findings", rendered)
        self.assertIn("Repeated body", rendered)
        memory[0]["collapsed"] = False
        self.assertIn(
            "##### Findings", protocol.render([finding], review_memory=memory)
        )
        memory[0].update(collapsed=True, author_position="none", author_comments=[])
        rendered = protocol.render([finding], review_memory=memory)
        self.assertIn("##### Findings", rendered)
        self.assertNotIn("<details>", rendered)
        self.assertEqual(rendered.count("Repeated body"), 1)

    def test_duplicate_reports_share_one_body_and_keep_each_origin(self):
        original = {
            "key": "old:1",
            "title": "Original report",
            "body": "Original concern body",
            "severity": "minor",
            "path": "a.cpp",
            "line": 1,
            "source_head": "first-revision",
            "status": "still_present",
            "author_position": "wontfix",
            "author_comments": [{"url": "https://example.org/reply"}],
            "collapsed": True,
            "current_finding_ids": ["finding:1"],
        }
        duplicate = {
            **original,
            "key": "old:2",
            "duplicate_of": "old:1",
            "title": "Duplicate report",
            "body": "Duplicate concern body",
            "severity": "major",
            "line": 9,
            "source_head": "second-revision",
            "author_position": "none",
            "author_comments": [],
            "collapsed": False,
        }
        memory = [original, duplicate]
        rendered = protocol.render([], review_memory=memory)
        self.assertIn("##### Findings", rendered)
        self.assertNotIn("##### Previous findings", rendered)
        self.assertIn("**Major**", rendered)
        self.assertNotIn("Original concern body", rendered)
        self.assertEqual(rendered.count("Duplicate concern body"), 1)
        for origin in ("a.cpp:1", "a.cpp:9", "first-revision", "second-revision"):
            self.assertIn(origin, rendered)
        self.assertIn("https://example.org/reply", rendered)

        current = {
            **duplicate,
            "id": "finding:1",
            "kind": "bug",
            "side": "head",
            "body": "Current verified concern body",
        }
        rendered = protocol.render([current], review_memory=memory)
        self.assertEqual(rendered.count("Current verified concern body"), 1)
        self.assertNotIn("Duplicate concern body", rendered)
        self.assertNotIn("Original concern body", rendered)
        self.assertIn("##### Findings", rendered)

        duplicate["current_finding_ids"].append("finding:2")
        second_current = {
            **current,
            "id": "finding:2",
            "body": "A separate verified concern body",
        }
        rendered = protocol.render([current, second_current], review_memory=memory)
        self.assertIn("Current verified concern body", rendered)
        self.assertIn("A separate verified concern body", rendered)
        self.assertNotIn("Duplicate concern body", rendered)

        duplicate.update(
            author_position="address_elsewhere",
            author_comments=[{"url": "https://example.org/second-reply"}],
            collapsed=True,
        )
        rendered = protocol.render([current], review_memory=memory)
        self.assertNotIn("##### Findings", rendered)
        self.assertIn("##### Previous findings and author responses", rendered)
        self.assertEqual(rendered.count("Duplicate concern body"), 1)
        self.assertNotIn("Current verified concern body", rendered)
        self.assertIn("https://example.org/second-reply", rendered)

        original["status"] = "fixed"
        duplicate.update(author_position="none", author_comments=[], collapsed=False)
        rendered = protocol.render([], review_memory=memory)
        self.assertIn("Duplicate concern body", rendered)
        self.assertIn("##### Findings", rendered)
        self.assertIn("Status: fixed", rendered)


if __name__ == "__main__":
    unittest.main()
