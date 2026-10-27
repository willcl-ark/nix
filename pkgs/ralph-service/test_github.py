"""GitHub publication tests, with no network access."""

import unittest
from unittest.mock import patch

from ralph import engine
from ralph_service import github


class GitHubTests(unittest.TestCase):
    def result(self, severity="major"):
        return {
            "schema_version": 1,
            "head_sha": "a" * 40,
            "run": {"report_url": "https://review.example/report.html"},
            "output": {
                "schema_version": 1,
                "exports": {
                    "findings": [
                        {
                            "id": "finding:1",
                            "severity": severity,
                            "title": "Original",
                            "summary": "Original summary",
                            "body": "Original body",
                            "presented_title": "Edited title",
                            "presented_summary": "Edited summary",
                            "presented_body": "Edited body",
                            "verified_by": "major_verifier",
                            "path": "src/test.cpp",
                            "line": 5,
                            "side": "head",
                        },
                        {
                            "id": "finding:2",
                            "severity": "minor",
                            "title": "Minor",
                            "summary": "Minor summary",
                            "body": "Minor body",
                        },
                    ]
                },
            },
        }

    def publish(self, result):
        return github.publish_major_findings("owner/repo", "github-token", 42, result)

    def test_only_major_findings_with_report(self):
        result = self.result()
        result["engine"] = engine.load().identity
        result["engine"]["config"]["comment_format"]["finding"] = (
            "${body}\n\n[${severity}] ${title}: ${summary} (${location}, ${side})"
        )
        with patch.object(
            github,
            "github_request",
            side_effect=[
                {"id": 7},
                [],
                {"state": "open", "head": {"sha": "a" * 40}},
                {},
            ],
        ) as request:
            self.assertEqual(self.publish(result), "created")
        args = request.call_args.args
        self.assertEqual(
            args[:3], ("github-token", "/repos/owner/repo/issues/42/comments", "POST")
        )
        body = args[3]["body"]
        for included in (
            "Edited title",
            "Edited body",
            "src/test.cpp:5",
            "https://review.example/report.html",
        ):
            self.assertIn(included, body)
        for excluded in ("Original", "Minor"):
            self.assertNotIn(excluded, body)
        self.assertLess(body.index("Edited body"), body.index("Edited title"))

    def test_comment_uses_final_selected_tier_or_omits_missing_route(self):
        for routing in (
            {"proposed": {"tier": "light"}, "selected": {"tier": "sensitive"}},
            {"proposed": {"tier": "light"}},
            {},
        ):
            with self.subTest(routing=routing):
                result = self.result()
                result["engine"] = engine.load().identity
                result["output"]["schema_version"] = 2
                result["output"]["data"] = {"schema_version": 2, "routing": routing}
                with patch.object(
                    github,
                    "github_request",
                    side_effect=[
                        {"id": 7},
                        [],
                        {"state": "open", "head": {"sha": "a" * 40}},
                        {},
                    ],
                ) as request:
                    self.assertEqual(self.publish(result), "created")
                body = request.call_args.args[3]["body"]
                self.assertEqual("Review tier:" in body, "selected" in routing)
                if "selected" in routing:
                    self.assertIn("Review tier: sensitive", body)
                self.assertNotIn("Review tier: light", body)

    def test_failed_review_cannot_create_update_or_delete_major_comment(self):
        for findings in (self.result()["output"]["exports"]["findings"], []):
            with self.subTest(findings=bool(findings)):
                result = self.result()
                result["run"]["status"] = "failed"
                result["output"]["exports"]["findings"] = findings
                with patch.object(github, "github_request") as request:
                    self.assertEqual(self.publish(result), "failed")
                request.assert_not_called()

    def test_retained_major_updates_previous_comment_instead_of_deleting(self):
        result = self.result()
        exports = result["output"]["exports"]
        exports["findings"] = []
        exports["review_memory"] = [{"severity": "major", "status": "unclear"}]
        own = {"id": 2, "user": {"id": 7}, "body": "<!-- ralph-major:owner/repo -->"}
        with patch.object(
            github,
            "github_request",
            side_effect=[
                {"id": 7},
                [own],
                {"state": "open", "head": {"sha": "a" * 40}},
                {},
            ],
        ) as request:
            self.assertEqual(self.publish(result), "updated")
        self.assertEqual(request.call_args.args[2], "PATCH")
        body = request.call_args.args[3]["body"]
        self.assertIn("remain unresolved", body)
        self.assertIn(result["run"]["report_url"], body)
        self.assertNotIn("Ralph found major issues", body)

    def test_collapsing_major_does_not_bypass_verification(self):
        result = self.result()
        exports = result["output"]["exports"]
        exports["review_memory"] = [
            {
                "severity": "major",
                "status": "still_present",
                "collapsed": True,
                "current_candidate_ids": ["independent:1"],
                "current_finding_ids": ["finding:1"],
            }
        ]
        exports["findings"][0]["verified_by"] = "verifier"
        with patch.object(github, "github_request") as request:
            self.assertEqual(self.publish(result), "unverified")
        request.assert_not_called()
        exports["findings"][0]["verified_by"] = "major_verifier"
        with patch.object(
            github,
            "github_request",
            side_effect=[
                {"id": 7},
                [],
                {"state": "open", "head": {"sha": "a" * 40}},
                {},
            ],
        ) as request:
            self.assertEqual(self.publish(result), "created")
        body = request.call_args.args[3]["body"]
        self.assertIn("remain unresolved", body)
        self.assertNotIn("Edited title", body)

    def test_stale_or_closed_upstream_does_not_post(self):
        for pull in (
            {"state": "open", "head": {"sha": "b" * 40}},
            {"state": "closed", "head": {"sha": "a" * 40}},
        ):
            with patch.object(
                github, "github_request", side_effect=[{"id": 7}, [], pull]
            ) as request:
                self.assertEqual(self.publish(self.result()), "stale")
                self.assertEqual(request.call_count, 3)

    def test_paginate_and_only_edit_authenticated_users_comment(self):
        spoof = {"id": 1, "user": {"id": 8}, "body": "<!-- ralph-major:owner/repo -->"}
        own = {"id": 2, "user": {"id": 7}, "body": spoof["body"]}
        with patch.object(
            github,
            "github_request",
            side_effect=[
                {"id": 7},
                [spoof] * 100,
                [own],
                {"state": "open", "head": {"sha": "a" * 40}},
                {},
            ],
        ) as request:
            self.assertEqual(self.publish(self.result()), "updated")
            self.assertIn("page=2", request.call_args_list[2].args[1])
            self.assertEqual(
                request.call_args.args[1:3],
                ("/repos/owner/repo/issues/comments/2", "PATCH"),
            )
            body = request.call_args.args[3]["body"]
        own["body"] = body
        with patch.object(
            github,
            "github_request",
            side_effect=[
                {"id": 7},
                [own],
                {"state": "open", "head": {"sha": "a" * 40}},
            ],
        ) as request:
            self.assertEqual(self.publish(self.result()), "unchanged")
            self.assertEqual(request.call_count, 3)
