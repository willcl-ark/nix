import json
import unittest

from ralph.evidence import ToolFailure
from ralph.research import filter_current_pr


class ResearchContextTests(unittest.TestCase):
    current = "https://github.com/bitcoin/bitcoin/pull/42"

    def test_search_excludes_source_aliases_ids_and_provenance(self):
        results = [
            {"id": "comment", "url": self.current + "#discussion_r12"},
            {
                "id": "comment",
                "url": "https://github.com/Bitcoin/Bitcoin/issues/42#issuecomment-3",
            },
            {"id": "github_pr:bitcoin/bitcoin:42"},
            {"id": "mirror", "provenance": [{"id": "github_issue:bitcoin/bitcoin:42"}]},
            {
                "id": "other",
                "url": self.current + "0",
                "snippet": "Mentions " + self.current,
            },
            {
                "id": "github_comment:bitcoin/bitcoin:42",
                "url": self.current + "0#issuecomment-42",
            },
        ]
        result = json.loads(
            filter_current_pr(
                "bkb_search", {}, json.dumps({"results": results}), self.current
            )
        )
        self.assertEqual(result["results"], results[-2:])
        self.assertEqual(result["current_pr_results_omitted"], 4)

    def test_document_and_discussion_reads_reject_current_pr(self):
        for name, args, output in (
            ("read_github_discussion", {"url": self.current + "/files#r12"}, "text"),
            ("read_current_pr_discussion", {}, "text"),
            (
                "bkb_get_document",
                {"id": "github_pr:bitcoin/bitcoin:42"},
                '{"document":{}}',
            ),
            ("bkb_get_document", {}, json.dumps({"document": {"url": self.current}})),
        ):
            with self.assertRaises(ToolFailure) as failure:
                filter_current_pr(name, args, output, self.current)
            self.assertEqual(failure.exception.outcome, "invalid_request")

    def test_other_document_body_links_are_not_ownership(self):
        output = json.dumps(
            {"document": {"url": self.current + "0", "body": self.current}}
        )
        self.assertEqual(
            filter_current_pr("bkb_get_document", {}, output, self.current), output
        )
        self.assertEqual(
            filter_current_pr("bkb_get_document", {}, output, None), output
        )

    def test_truncated_bkb_cannot_leak_current_pr(self):
        with self.assertRaises(ToolFailure) as failure:
            filter_current_pr("bkb_search", {}, '{"results":[{"url":', self.current)
        self.assertEqual(failure.exception.outcome, "unavailable")


if __name__ == "__main__":
    unittest.main()
