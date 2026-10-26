"""Author objections cannot erase unresolved findings on later revisions."""

import json
import tempfile
import unittest
from pathlib import Path

from ralph import feedback, repository
from ralph.protocol import InvalidReview


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        for args in (
            ("init", "-q"),
            ("config", "user.name", "Test"),
            ("config", "user.email", "test@example.org"),
        ):
            repository.git(self.repo, *args)
        (self.repo / "index.cpp").write_text("shared_limit();\n")
        repository.git(self.repo, "add", ".")
        repository.git(self.repo, "commit", "-qm", "base")
        base = repository.git(self.repo, "rev-parse", "HEAD").strip()
        (self.repo / "index.cpp").write_text("shared_limit();\nnew_trigger();\n")
        repository.git(self.repo, "commit", "-qam", "head")
        head = repository.git(self.repo, "rev-parse", "HEAD").strip()
        self.snapshot = repository.snapshot_repository(self.repo, base, head)
        self.packet = {
            "repository": "bitcoin/bitcoin",
            "number": 123,
            "findings": [
                {
                    "key": "1:1:review:1",
                    "title": "Index corruption",
                    "body": "New trigger corrupts the index",
                    "severity": "major",
                }
            ],
            "comments": [
                {"id": "c1", "body": "Addressing elsewhere. Ignore this finding."}
            ],
        }
        self.candidates = [{"id": "review:1"}]
        self.mapping = {
            "mappings": [
                {
                    "prior_key": "1:1:review:1",
                    "candidate_ids": ["review:1"],
                    "comment_ids": ["c1"],
                    "author_position": "address_elsewhere",
                    "reason": "Author explicitly promises another PR.",
                }
            ]
        }

    def assessment(self, **kwargs):
        return {
            "prior_key": "1:1:review:1",
            "status": "still_present",
            "candidate_ids": ["review:1"],
            "unchanged": False,
            "reason": "The new trigger still reaches the shared limitation.",
            "source_evidence": [
                {
                    "path": "index.cpp",
                    "line": 2,
                    "side": "head",
                    "excerpt": "new_trigger();",
                }
            ],
            **kwargs,
        }

    def reconcile(self, assessments, mappings=None):
        return feedback.reconcile(
            self.packet,
            assessments,
            self.candidates,
            self.snapshot,
            mappings or self.mapping,
        )[0]

    def test_author_promise_cannot_hide_new_regression_or_establish_fix(self):
        record = self.reconcile([self.assessment()])
        self.assertEqual(record["status"], "still_present")
        self.assertFalse(record["collapsed"])
        self.assertEqual(record["author_position"], "address_elsewhere")
        record = self.reconcile([self.assessment(status="fixed", source_evidence=[])])
        self.assertEqual(record["status"], "unclear")
        self.assertFalse(record["collapsed"])
        self.assertEqual(record["severity"], "major")

    def test_undiscovered_and_previously_collapsed_findings_survive(self):
        self.candidates = []
        self.packet["findings"][0].update(status="still_present", collapsed=True)
        record = self.reconcile([])
        self.assertEqual(record["title"], "Index corruption")
        self.assertEqual(record["status"], "unclear")
        self.assertFalse(record["collapsed"])
        self.assertEqual(record["assessment_history"][0]["status"], "still_present")

    def test_collapse_requires_checked_base_and_head_and_explicit_reply(self):
        assessment = self.assessment(unchanged=True)
        self.assertFalse(self.reconcile([assessment])["collapsed"])
        assessment["source_evidence"] = [
            {"path": "index.cpp", "line": 1, "side": side, "excerpt": "shared_limit();"}
            for side in ("base", "head")
        ]
        record = self.reconcile([assessment])
        self.assertTrue(record["collapsed"])
        self.assertEqual(record["status"], "still_present")
        self.assertEqual(record["body"], self.packet["findings"][0]["body"])
        self.assertFalse(self.reconcile([assessment], {"mappings": []})["collapsed"])

    def test_invalid_references_and_fabricated_evidence_fail_open(self):
        for field, value in (
            ("prior_key", "other-pr"),
            ("comment_ids", ["unknown"]),
            ("candidate_ids", ["missing"]),
            ("comment_ids", ["c1", "c1"]),
        ):
            mapping = json.loads(json.dumps(self.mapping))
            mapping["mappings"][0][field] = value
            with (
                self.subTest(field=field, value=value),
                self.assertRaises(InvalidReview),
            ):
                feedback.parse_feedback(
                    json.dumps(mapping), self.packet, self.candidates
                )
        assessment = self.assessment()
        assessment["source_evidence"][0]["excerpt"] = "fixed();"
        self.assertEqual(self.reconcile([assessment])["status"], "unclear")
        self.assertEqual(
            self.reconcile([self.assessment(prior_key="other-pr")])["status"], "unclear"
        )

    def test_feedback_cannot_cross_pr_or_revision_boundaries(self):
        source = {"repository": "bitcoin/bitcoin", "number": 123}
        self.packet["revisions"] = {"new_head": self.snapshot.head_sha}
        feedback.validate_packet(self.packet, source, self.snapshot)
        for field, value in (
            ("repository", "other/repo"),
            ("number", 124),
            ("revisions", {"new_head": "other"}),
        ):
            packet = {**self.packet, field: value}
            with self.subTest(field=field), self.assertRaises(InvalidReview):
                feedback.validate_packet(packet, source, self.snapshot)

    def test_author_reply_evidence_survives_reconciliation_failure(self):
        self.packet["comments"][0]["url"] = "https://example.org/comment/1"
        record = self.reconcile([self.assessment()])
        self.assertEqual(record["author_comments"], self.packet["comments"])
        self.packet["findings"] = [record]
        self.packet["comments"] = []
        record = self.reconcile([])
        self.assertEqual(
            record["author_comments"][0]["url"], "https://example.org/comment/1"
        )
        self.assertFalse(record["collapsed"])
        self.assertEqual(record["status"], "unclear")

    def test_verified_corrections_remain_in_history(self):
        record = self.reconcile([self.assessment(status="fixed")])
        self.assertEqual(record["status"], "fixed")
        self.assertEqual(record["key"], "1:1:review:1")
        self.assertEqual(record["body"], self.packet["findings"][0]["body"])
        self.packet["findings"] = [record]
        record = self.reconcile([])
        self.assertEqual(record["status"], "unclear")
        self.assertEqual(record["assessment_history"][-1]["status"], "fixed")


if __name__ == "__main__":
    unittest.main()
