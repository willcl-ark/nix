import json
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from ralph.evidence import ToolFailure
from ralph.repository import MAX_TOOL_BYTES
from ralph_service import bkb


class BkbTests(unittest.TestCase):
    def request(self, value):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(value).encode()
        opener = MagicMock()
        opener.open.return_value = response
        return opener, response

    def test_search_bounds_and_escapes_request_and_preserves_provenance(self):
        item = {
            "id": "github_pr:bitcoin/bitcoin:123",
            "url": "https://example.org/123",
            "created_at": "2020-01-01T00:00:00Z",
            "snippet": "Retrieved passage",
            "provenance": [{"id": "original", "url": "https://example.org/original"}],
        }
        opener, response = self.request({"results": [item] * 6, "total_count": 20})
        with patch.object(bkb.urllib.request, "build_opener", return_value=opener):
            result = json.loads(bkb.search("a & b", source_repo="bitcoin/bitcoin"))
        request = opener.open.call_args.args[0]
        self.assertEqual(
            request.full_url,
            bkb.BASE_URL + "/search?q=a+%26+b&limit=5&source_repo=bitcoin%2Fbitcoin",
        )
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 20)
        response.__enter__.return_value.read.assert_called_once_with(
            bkb.MAX_RESPONSE_BYTES + 1
        )
        self.assertEqual(len(result["results"]), 5)
        self.assertTrue(result["results_omitted"])
        for key in ("id", "url", "created_at", "snippet", "provenance"):
            self.assertEqual(result["results"][0][key], item[key])

    def test_document_escapes_id_and_bounds_output(self):
        value = {
            "document": {"id": "x", "created_at": "2020-01-01", "body": "€" * 20_000},
            "url": "https://example.org/x",
            "provenance": [{"id": "original"}],
        }
        opener, _ = self.request(value)
        with patch.object(bkb.urllib.request, "build_opener", return_value=opener):
            result = bkb.get_document("a/b?#&")
        self.assertEqual(
            opener.open.call_args.args[0].full_url,
            bkb.BASE_URL + "/document/a%2Fb%3F%23%26",
        )
        self.assertLessEqual(len(result.encode()), MAX_TOOL_BYTES)
        self.assertIn("truncated", result)
        self.assertIn("https://example.org/x", result)
        self.assertIn("original", result)
        self.assertIn("2020-01-01", result)

    def test_bad_network_and_responses_are_unavailable(self):
        opener, response = self.request({})
        with patch.object(bkb.urllib.request, "build_opener", return_value=opener):
            for body in (
                b"x" * (bkb.MAX_RESPONSE_BYTES + 1),
                b"not json",
                b"null",
                b'{"results":null}',
            ):
                response.__enter__.return_value.read.return_value = body
                with self.assertRaises(ToolFailure) as failure:
                    bkb.search("query")
                self.assertEqual(failure.exception.outcome, "unavailable")
            for error in (
                URLError("offline"),
                TimeoutError(),
                HTTPError(bkb.BASE_URL, 404, "missing", {}, None),
            ):
                opener.open.side_effect = error
                with self.assertRaises(ToolFailure) as failure:
                    bkb.get_document("missing")
                self.assertEqual(failure.exception.outcome, "unavailable")

    def test_invalid_arguments_do_not_request_network(self):
        with patch.object(bkb, "_request") as request:
            for kwargs in (
                {"limit": True},
                {"limit": 11},
                {"limit": 0},
                {"query": ""},
                {"query": "x" * 501},
                {"source_type": []},
                {"source_repo": "a\nb"},
            ):
                arguments = {"query": "valid", **kwargs}
                with self.assertRaises(ToolFailure) as failure:
                    bkb.search(**arguments)
                self.assertEqual(failure.exception.outcome, "invalid_request")
            for id in ("", "..", "x" * 1001, None, "a\nb"):
                with self.assertRaises(ToolFailure):
                    bkb.get_document(id)
        request.assert_not_called()

    def test_redirects_are_not_followed(self):
        handler = bkb._NoRedirect()
        self.assertIsNone(
            handler.redirect_request(None, None, 302, "", {}, "http://localhost/")
        )


if __name__ == "__main__":
    unittest.main()
