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
            (
                self.framework,
                "def sync_blocks(self, nodes=None, wait=1, timeout=60):\n    pass\n",
            ),
            (self.caller, "# fixture\n" * 470 + self.call + "\n"),
        ):
            target = self.checkout / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        repository.git(self.checkout, "add", ".")
        self.commit()
        base = repository.git(self.checkout, "rev-parse", "HEAD").strip()
        (self.checkout / self.framework).write_text(
            "def sync_blocks(self, nodes=None, *, wait=0.05, timeout=60):\n    pass\n"
        )
        repository.git(self.checkout, "add", ".")
        self.commit()
        head = repository.git(self.checkout, "rev-parse", "HEAD").strip()
        self.snapshot = repository.snapshot_repository(self.checkout, base, head)
        self.evidence = {
            "path": self.caller,
            "line": 471,
            "side": "head",
            "excerpt": self.call,
        }
        self.finding = {
            "kind": "defect",
            "severity": "major",
            "path": self.framework,
            "line": 1,
            "side": "head",
            "title": "Positional caller breaks",
            "summary": "Existing positional arguments no longer match the helper.",
            "body": "The caller supplies wait and timeout positionally.",
            "security": {
                "classification": "non_security",
                "reason": "The issue changes how the test invokes the helper.",
            },
            "source_evidence": [self.evidence],
        }

    def commit(self):
        repository.git(
            self.checkout,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "Snapshot",
        )

    def verify(self, finding):
        payload = {
            "coverage": {"status": "complete", "limitations": []},
            "concept": {
                "disposition": "no_concern",
                "reason": "No concern",
                "assessment": None,
                "proposed_review": "continue",
                "review_reason": "No concept concern",
            },
            "alternatives": [],
            "decisions": [
                {
                    "candidate_ids": ["independent:1"],
                    "disposition": "publish",
                    "reason": "Checked caller",
                    "finding": finding,
                }
            ],
        }
        return protocol.verification(
            json.dumps(payload), [{"id": "independent:1"}], self.snapshot
        )

    def test_pr36439_fabricated_positional_call_is_withheld(self):
        fabricated = "        self.sync_blocks([self.nodes[0], self.nodes[5]], 5, 300)"
        finding = {
            **self.finding,
            "source_evidence": [{**self.evidence, "excerpt": fabricated}],
        }
        result, accepted = self.verify(finding)
        self.assertEqual(accepted, [])
        self.assertEqual(result["decisions"][0]["disposition"], "unresolved")
        self.assertEqual(result["coverage"]["status"], "partial")
        self.assertIn("does not match", result["validation_errors"][0]["error"])

    def test_real_caller_in_unchanged_file_matches_both_snapshots(self):
        for side in ("base", "head"):
            with self.subTest(side=side):
                finding = {
                    **self.finding,
                    "source_evidence": [{**self.evidence, "side": side}],
                }
                result, accepted = self.verify(finding)
                self.assertEqual(len(accepted), 1)
                self.assertEqual(result["coverage"]["status"], "complete")

    def test_excerpts_are_checked_against_pinned_objects(self):
        (self.checkout / self.caller).write_text("overwritten working tree\n")
        repository.git(self.checkout, "checkout", "-q", self.snapshot.merge_base)
        _, accepted = self.verify(self.finding)
        self.assertEqual(len(accepted), 1)


if __name__ == "__main__":
    unittest.main()
