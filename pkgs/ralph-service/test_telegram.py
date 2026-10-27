import io
import unittest
import urllib.error
from unittest.mock import patch

import final_comment
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

    def test_unicode_limit_and_oversized_title_keep_links(self):
        self.assertEqual(telegram.message_length("🚨"), 2)
        result = self.result("🚨" * 2048)
        self.assertNotIn("🚨" * 2048, self.render(result))
        result["run"]["pr_title"] = "🚨" * 5000
        text = self.render(result)
        self.assertLessEqual(telegram.message_length(text), 4096)
        self.assertIn("https://forge.example/owner/repo/pulls/42", text)
        self.assertIn("https://reports.example/42.html", text)

    def test_retained_major_avoids_new_alarm_without_forgetting_the_concern(self):
        result = self.result(findings=[{"id": "new:1", "severity": "major"}])
        result["output"]["exports"]["review_memory"] = [
            {
                "severity": "major",
                "status": "still_present",
                "collapsed": True,
                "current_candidate_ids": ["independent:1"],
                "current_finding_ids": ["new:1"],
            }
        ]
        self.assertIn("Previously reported major concern", self.render(result))
        self.assertNotIn("🚨 Major finding", self.render(result))
        result["output"]["exports"]["findings"].append(
            {"id": "new:2", "severity": "major"}
        )
        self.assertIn("🚨 Major finding", self.render(result))
        result["output"]["exports"]["findings"] = []
        self.assertIn("Previously reported major concern", self.render(result))
        result["output"]["exports"]["review_memory"][0]["status"] = "fixed"
        self.assertNotIn("major concern", self.render(result))

    def test_retained_review_and_coverage_reach_telegram_as_markdown(self):
        memory = [
            {
                "key": "prior:1",
                "title": "Check height < limit",
                "body": "Guard `block->nStatus`.",
                "severity": "major",
                "status": "unclear",
                "source_head": "old-head",
            }
        ]
        result = self.result(
            final_comment.render([], ["Verifier unavailable"], review_memory=memory)
        )
        result["output"]["exports"]["review_memory"] = memory
        text = self.render(result)
        self.assertIn("Check height < limit", text)
        self.assertIn("Guard `block->nStatus`.", text)
        self.assertIn("old-head", text)
        self.assertIn("Verifier unavailable", text)
        self.assertNotIn("<h6>", text)
        self.assertNotIn("<details>", text)
        self.assertNotIn("<summary>", text)
        self.assertNotIn("&gt;", text)

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


if __name__ == "__main__":
    unittest.main()
