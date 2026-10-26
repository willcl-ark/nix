import io
import json
import unittest
import urllib.error
from http.client import IncompleteRead
from unittest.mock import patch

from ralph_service import telegram


class TelegramTests(unittest.TestCase):
    def result(self, content="Review text", findings=()):
        return {
            "schema_version": 1,
            "content": content,
            "output": {"schema_version": 1, "exports": {"findings": list(findings)}},
            "run": {
                "pr_title": "Fix <handling> & parsing",
                "report_url": "https://reports.example/42.html",
            },
        }

    def render(self, result):
        return telegram.review_message("https://forge.example/owner/repo", 42, result)

    def test_short_review_includes_title_links_and_entire_content(self):
        text = self.render(self.result())
        for part in (
            "Fix <handling> & parsing",
            "https://forge.example/owner/repo/pulls/42",
            "Review text",
            "https://reports.example/42.html",
        ):
            self.assertIn(part, text)
        self.assertNotIn("🚨", text)

    def test_exact_limit_includes_review_one_more_uses_links(self):
        overhead = telegram.message_length(self.render(self.result("")))
        content = "x" * (4096 - overhead)
        text = self.render(self.result(content))
        self.assertEqual(telegram.message_length(text), 4096)
        self.assertIn(content, text)
        longer = self.render(self.result(content + "x"))
        self.assertNotIn(content, longer)
        self.assertIn("Read full review: https://reports.example/42.html", longer)

    def test_major_export_marks_both_full_and_link_only_posts(self):
        for content in ("Short review", "x" * 4096):
            text = self.render(self.result(content, [{"severity": "major"}]))
            self.assertTrue(text.startswith("🚨 Major finding\n"))
        for findings in ([{"severity": "minor"}], [{"severity": "Major"}], []):
            self.assertNotIn("🚨", self.render(self.result("Major issue?", findings)))

    def test_unicode_limit_and_oversized_title_keep_links(self):
        self.assertEqual(telegram.message_length("🚨"), 2)
        result = self.result("🚨" * 2048)
        self.assertNotIn("🚨" * 2048, self.render(result))
        result["run"]["pr_title"] = "🚨" * 5000
        text = self.render(result)
        self.assertLessEqual(telegram.message_length(text), 4096)
        self.assertIn("https://forge.example/owner/repo/pulls/42", text)
        self.assertIn("https://reports.example/42.html", text)

    def test_send_uses_plain_text_and_disables_previews(self):
        response = io.BytesIO(b'{"ok": true, "result": {"message_id": 1}}')
        with patch.object(
            telegram.urllib.request, "urlopen", return_value=response
        ) as send:
            telegram.send_message("123:secret", "@channel", "<raw> _markdown_")
        request = send.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["text"], "<raw> _markdown_")
        self.assertEqual(payload["chat_id"], "@channel")
        self.assertNotIn("parse_mode", payload)
        self.assertEqual(payload["link_preview_options"], {"is_disabled": True})
        self.assertEqual(send.call_args.kwargs["timeout"], 30)

    def test_http_rate_limit_is_sanitized_and_preserves_retry_delay(self):
        error = urllib.error.HTTPError(
            "https://api.telegram.org/botsecret/sendMessage",
            429,
            "secret",
            {},
            io.BytesIO(b'{"ok": false, "parameters": {"retry_after": 123}}'),
        )
        with (
            patch.object(telegram.urllib.request, "urlopen", side_effect=error),
            self.assertRaises(telegram.TelegramError) as raised,
        ):
            telegram.send_message("123:secret", "@channel", "review")
        self.assertEqual(raised.exception.retry_after, 123)
        self.assertNotIn("secret", str(raised.exception))

    def test_api_failure_and_malformed_response_are_not_success(self):
        for body in (b'{"ok": false}', b"not json"):
            with (
                patch.object(
                    telegram.urllib.request, "urlopen", return_value=io.BytesIO(body)
                ),
                self.assertRaises(telegram.TelegramError),
            ):
                telegram.send_message("123:secret", "@channel", "review")

    def test_interrupted_response_becomes_retryable_delivery_error(self):
        with (
            patch.object(
                telegram.urllib.request,
                "urlopen",
                side_effect=IncompleteRead(b"partial"),
            ),
            self.assertRaises(telegram.TelegramError),
        ):
            telegram.send_message("123:secret", "@channel", "review")


if __name__ == "__main__":
    unittest.main()
