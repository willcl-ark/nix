import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ralph import research as frozen_research
from ralph_service import forgejo, research


class ResearchEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.bot_config = SimpleNamespace(
            repository_url="https://git.example.org/owner/repo",
            forgejo_api="https://git.example.org/api/v1/repos/owner/repo",
            comment_marker="<!-- ralph-review -->",
        )

    def request(self, path):
        old = "2026-09-30T00:00:00Z"
        new = "2026-10-01T00:00:00Z"
        if path == "/issues/42":
            return {
                "number": 42,
                "title": "Original title",
                "body": "Original body",
                "pull_request": {},
                "created_at": old,
                "updated_at": old,
            }
        if path == "/issues/42/comments?limit=10&page=1":
            return [
                {
                    "id": 1,
                    "user": {"login": "alice"},
                    "body": "Old comment",
                    "created_at": old,
                    "updated_at": old,
                },
                {
                    "id": 2,
                    "user": {"login": "bob"},
                    "body": "Edited too late",
                    "created_at": old,
                    "updated_at": new,
                },
                {"id": 3, "user": {"login": "carol"}, "body": "Missing metadata"},
            ]
        if path == "/pulls/42/reviews?limit=10&page=1":
            return [
                {
                    "id": 7,
                    "user": {"login": "reviewer"},
                    "body": "Old review",
                    "created_at": old,
                    "updated_at": old,
                },
                {
                    "id": 8,
                    "user": {"login": "reviewer"},
                    "body": "Late review",
                    "created_at": new,
                    "updated_at": new,
                },
            ]
        self.fail(path)

    def test_capture_freezes_current_pr_discussion_with_cutoff_metadata(self):
        with patch.object(
            forgejo,
            "public_discussion_request",
            side_effect=lambda _config, path: self.request(path),
        ):
            evidence = research.capture(
                self.bot_config,
                42,
                cutoff="2026-09-30T12:00:00Z",
                captured_at="2026-10-01T00:00:00Z",
            )
        research.validate(evidence)
        self.assertEqual(evidence["schema_version"], research.SCHEMA_VERSION)
        self.assertEqual(evidence["cutoff"], "2026-09-30T12:00:00Z")
        self.assertEqual(len(evidence["entries"]), 2)
        self.assertTrue(
            all(
                entry["captured_at"] == evidence["captured_at"]
                for entry in evidence["entries"]
            )
        )
        comments = frozen_research.lookup(
            evidence,
            "read_current_pr_discussion",
            {"kind": "comments", "page": 1, "review_id": 0},
        )
        self.assertIn("Old comment", comments)
        self.assertNotIn("Edited too late", comments)
        self.assertNotIn("Missing metadata", comments)
        self.assertIn("omitted", comments)
        self.assertIn("read_current_pr_discussion", frozen_research.inventory(evidence))


if __name__ == "__main__":
    unittest.main()
