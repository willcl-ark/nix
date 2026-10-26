"""Backport provenance is checked against exact local commit sets."""

import tempfile
import unittest
from pathlib import Path

from ralph import backports, repository


class BackportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.org")
        self.base = self.commit("base", "initial")
        self.first = self.commit("fix one", "one")
        self.second = self.commit("fix two", "two")
        self.git("checkout", "-qb", "release", self.base)
        self.metadata = {
            "labels": ["Backport"],
            "base_ref": "29.x",
            "repository": "bitcoin/bitcoin",
            "body": "Backports:\n- #123",
        }
        self.origins = {"bitcoin/bitcoin#123": {"commits": [self.first, self.second]}}

    def git(self, *args):
        return repository.git(self.repo, *args)

    def commit(self, message, content, path="code.txt"):
        (self.repo / path).write_text(content + "\n")
        self.git("add", ".")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD").strip()

    def port(self, original, content, pr="#123"):
        return self.commit(
            f"port\n\nRebased-From: {original}\nGithub-Pull: {pr}", content
        )

    def snapshot(self):
        return repository.snapshot_repository(
            self.repo, self.base, self.git("rev-parse", "HEAD").strip()
        )

    def test_interleaved_ports_compare_individual_commits_and_inventory_maintenance(
        self,
    ):
        first = self.port(self.first[:9], "one")
        maintenance = self.commit("version preparation", "1.0", "version.txt")
        self.port(self.second, "two")
        evidence = backports.collect(self.snapshot(), self.metadata, self.origins)
        self.assertTrue(evidence["complete"], evidence["limitations"])
        self.assertEqual(evidence["untagged"], [maintenance])
        self.assertEqual(evidence["sources"]["bitcoin/bitcoin#123"]["missing"], [])
        row = next(row for row in evidence["commits"] if row["commit"] == first)
        self.assertEqual(row["original"], self.first)
        self.assertNotIn("version preparation", row["range_diff"])
        self.assertNotIn("fix two", row["range_diff"])

    def test_missing_commit_and_entire_declared_pr_are_visible(self):
        self.port(self.first, "adapted one")
        self.metadata["body"] += "\n- #456"
        self.origins["bitcoin/bitcoin#456"] = {"commits": [self.second]}
        evidence = backports.collect(self.snapshot(), self.metadata, self.origins)
        self.assertEqual(
            evidence["sources"]["bitcoin/bitcoin#123"]["missing"], [self.second]
        )
        self.assertEqual(
            evidence["sources"]["bitcoin/bitcoin#456"]["missing"], [self.second]
        )
        self.assertIn("adapted one", evidence["commits"][0]["patch"])

    def test_previous_backport_is_recognized_on_target_branch(self):
        self.port(self.first[:10], "one")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.port(self.second, "two")
        evidence = backports.collect(self.snapshot(), self.metadata, self.origins)
        source = evidence["sources"]["bitcoin/bitcoin#123"]
        self.assertEqual(source["already_in_base"], [self.first])
        self.assertEqual(source["missing"], [])

    def test_wrong_trailer_pr_is_resolved_by_original_membership(self):
        self.port(self.first, "one", "#999")
        origins = {**self.origins, "bitcoin/bitcoin#999": {"commits": []}}
        evidence = backports.collect(self.snapshot(), self.metadata, origins)
        self.assertEqual(evidence["commits"][0]["source_pr"], "bitcoin/bitcoin#123")
        self.assertTrue(evidence["complete"], evidence["limitations"])

    def test_malformed_metadata_stays_backport_and_unavailable_evidence_blocks_completeness(
        self,
    ):
        self.port("--output=/tmp/unsafe", "one")
        snapshot = self.snapshot()
        self.assertEqual(backports.classify(snapshot)["mode"], "backport")
        evidence = backports.collect(snapshot)
        self.assertFalse(evidence["complete"])
        self.assertTrue(any("malformed" in item for item in evidence["limitations"]))

    def test_missing_objects_and_truncated_evidence_cannot_be_complete(self):
        self.port(self.first, "one")
        self.origins["bitcoin/bitcoin#123"]["commits"].append("f" * 40)
        evidence = backports.collect(self.snapshot(), self.metadata, self.origins)
        self.assertFalse(evidence["complete"])
        self.assertTrue(
            any("object unavailable" in item for item in evidence["limitations"])
        )
        self.origins["bitcoin/bitcoin#123"]["body"] = "x" * 10_000
        evidence = backports.collect(self.snapshot(), self.metadata, self.origins)
        self.assertTrue(
            any("truncated body" in item for item in evidence["limitations"])
        )

    def test_local_attributes_cannot_hide_backport_patch(self):
        self.port(self.first, "one")
        (self.repo / ".git/info/attributes").write_text("code.txt binary\n")
        evidence = backports.collect(self.snapshot(), self.metadata, self.origins)
        self.assertIn("+one", evidence["commits"][0]["patch"])
        self.assertIn("+one", evidence["commits"][0]["original_patch"])

    def test_classification_and_source_request_formats(self):
        self.commit("ordinary", "release change")
        snapshot = self.snapshot()
        self.assertEqual(
            backports.classify(snapshot, {"base_ref": "29.x"})["mode"], "normal"
        )
        self.assertEqual(
            backports.classify(
                snapshot, {"base_ref": "29.x", "title": "Backport fixes"}
            )["mode"],
            "backport",
        )
        for trailer, expected in [
            ("gui#899", "bitcoin-core/gui#899"),
            ("#gui901", "bitcoin-core/gui#901"),
            ("https://github.com/bitcoin/bitcoin/pull/123", "bitcoin/bitcoin#123"),
        ]:
            with self.subTest(trailer=trailer):
                self.port(self.first, trailer, trailer)
                requests = backports.source_requests(self.snapshot())
                self.assertIn(expected, requests["prs"])


if __name__ == "__main__":
    unittest.main()
