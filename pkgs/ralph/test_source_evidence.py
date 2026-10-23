import json
import tempfile
import unittest
from pathlib import Path

from ralph import protocol, repository


class SourceEvidenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.checkout = Path(directory.name)
        self.framework = "test/functional/test_framework/test_framework.py"
        self.caller = "test/functional/feature_pruning.py"
        self.call = "        self.sync_blocks([self.nodes[0], self.nodes[5]], wait=5, timeout=300)"
        repository.git(self.checkout, "init", "-q")
        for path, content in (
                (self.framework, "def sync_blocks(self, nodes=None, wait=1, timeout=60):\n    pass\n"),
                (self.caller, "# fixture\n" * 470 + self.call + "\n")):
            target = self.checkout / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        repository.git(self.checkout, "add", ".")
        self.commit()
        base = repository.git(self.checkout, "rev-parse", "HEAD").strip()
        (self.checkout / self.framework).write_text(
            "def sync_blocks(self, nodes=None, *, wait=0.05, timeout=60):\n    pass\n")
        repository.git(self.checkout, "add", ".")
        self.commit()
        head = repository.git(self.checkout, "rev-parse", "HEAD").strip()
        self.snapshot = repository.snapshot_repository(self.checkout, base, head)
        self.evidence = {"path": self.caller, "line": 471, "side": "head", "excerpt": self.call}
        self.finding = {"kind": "defect", "severity": "major", "path": self.framework,
                        "line": 1, "side": "head", "title": "Positional caller breaks",
                        "body": "The caller supplies wait and timeout positionally.",
                        "security": {"classification": "non_security",
                                     "reason": "The issue changes how the test invokes the helper."},
                        "source_evidence": [self.evidence]}

    def commit(self):
        repository.git(self.checkout, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                       "commit", "-qm", "Snapshot")

    def verify(self, finding):
        payload = {"coverage": {"status": "complete", "limitations": []},
                   "concept": {"disposition": "no_concern", "reason": "No concern",
                               "assessment": None, "proposed_review": "continue",
                               "review_reason": "No concept concern"},
                   "alternatives": [],
                   "decisions": [{"candidate_ids": ["independent:1"], "disposition": "publish",
                                  "reason": "Checked caller", "finding": finding}]}
        return protocol.verification(json.dumps(payload), [{"id": "independent:1"}], self.snapshot)

    def test_pr36439_fabricated_positional_call_is_withheld(self):
        fabricated = "        self.sync_blocks([self.nodes[0], self.nodes[5]], 5, 300)"
        finding = {**self.finding, "source_evidence": [{**self.evidence, "excerpt": fabricated}]}
        result, accepted = self.verify(finding)
        self.assertEqual(accepted, [])
        self.assertEqual(result["decisions"][0]["disposition"], "unresolved")
        self.assertEqual(result["coverage"]["status"], "partial")
        self.assertIn("does not match", result["validation_errors"][0]["error"])

    def test_source_evidence_is_required(self):
        for fields in ({}, {"source_evidence": []}):
            finding = {key: value for key, value in self.finding.items() if key != "source_evidence"}
            with self.subTest(fields=fields):
                result, accepted = self.verify({**finding, **fields})
                self.assertEqual(accepted, [])
                self.assertEqual(result["decisions"][0]["disposition"], "unresolved")

    def test_one_real_excerpt_does_not_excuse_a_fabricated_excerpt(self):
        sources = [self.evidence, {**self.evidence, "excerpt": self.call.replace("wait=", "")}]
        _, accepted = self.verify({**self.finding, "source_evidence": sources})
        self.assertEqual(accepted, [])

    def test_real_caller_in_unchanged_file_matches_both_snapshots(self):
        for side in ("base", "head"):
            for ending in ("", "\n"):
                with self.subTest(side=side, ending=ending):
                    finding = {**self.finding, "source_evidence": [
                        {**self.evidence, "side": side, "excerpt": self.call + ending}]}
                    result, accepted = self.verify(finding)
                    self.assertEqual(len(accepted), 1)
                    self.assertEqual(result["coverage"]["status"], "complete")

    def test_excerpts_are_checked_against_pinned_objects(self):
        (self.checkout / self.caller).write_text("overwritten working tree\n")
        repository.git(self.checkout, "checkout", "-q", self.snapshot.merge_base)
        _, accepted = self.verify(self.finding)
        self.assertEqual(len(accepted), 1)

    def test_wrong_location_or_side_and_malformed_evidence_are_withheld(self):
        cases = ({"line": 470}, {"line": 472}, {"line": True}, {"line": 0},
                 {"side": "other"}, {"path": "untracked.py"}, {"excerpt": ""},
                 {"excerpt": "# fixture\n" * 151}, {"excerpt": "x" * 12_001},
                 {"path": self.framework, "line": 1, "side": "base",
                  "excerpt": "def sync_blocks(self, nodes=None, *, wait=0.05, timeout=60):"})
        for fields in cases:
            with self.subTest(fields=fields):
                _, accepted = self.verify({**self.finding, "source_evidence": [
                    {**self.evidence, **fields}]})
                self.assertEqual(accepted, [])

    def test_multiline_excerpt_preserves_whitespace(self):
        excerpt = "def sync_blocks(self, nodes=None, *, wait=0.05, timeout=60):\n    pass"
        source = {"path": self.framework, "line": 1, "side": "head", "excerpt": excerpt}
        _, accepted = self.verify({**self.finding, "source_evidence": [source]})
        self.assertEqual(len(accepted), 1)
        source["excerpt"] = excerpt.replace("    pass", "pass")
        _, accepted = self.verify({**self.finding, "source_evidence": [source]})
        self.assertEqual(accepted, [])


if __name__ == "__main__":
    unittest.main()
