"""GitHub publication tests, with no network access."""

import io
import unittest
import urllib.error
from unittest.mock import patch

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
                            "body": "Original body",
                            "presented_title": "Edited title",
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
                            "body": "Minor body",
                        },
                    ]
                },
            },
        }

    def publish(self, result):
        return github.publish_major_findings("owner/repo", "github-token", 42, result)

    def test_delete_accepts_empty_204_response(self):
        response = io.BytesIO(b"")
        response.status = 204
        with patch.object(
            github.urllib.request, "urlopen", return_value=response
        ) as send:
            self.assertIsNone(
                github.github_request(
                    "secret", "/repos/owner/repo/issues/comments/2", "DELETE"
                )
            )
        request = send.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://api.github.com/repos/owner/repo/issues/comments/2",
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        self.assertEqual(request.method, "DELETE")

    def test_only_major_findings_with_report(self):
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
            self.assertEqual(self.publish(self.result()), "created")
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

    def test_skip_without_export_or_on_skipped_review(self):
        skipped = self.result()
        skipped["run"]["skipped"] = True
        for result in (skipped, {"output": {}}):
            with patch.object(github, "github_request") as request:
                self.assertEqual(self.publish(result), "skipped")
                request.assert_not_called()

    def test_unverified_major_batch_does_not_contact_github(self):
        own = {
            "id": 2,
            "user": {"id": 7},
            "body": "<!-- ralph-major:owner/repo -->",
        }
        for verifier in (None, "verifier"):
            for mixed in (False, True):
                for existing in ([], [own]):
                    with self.subTest(
                        verifier=verifier, mixed=mixed, existing=bool(existing)
                    ):
                        result = self.result()
                        findings = result["output"]["exports"]["findings"]
                        unchecked = {**findings[0], "id": "finding:unchecked"}
                        if verifier is None:
                            del unchecked["verified_by"]
                        else:
                            unchecked["verified_by"] = verifier
                        if mixed:
                            findings.append(unchecked)
                        else:
                            findings[0] = unchecked
                        with patch.object(
                            github,
                            "github_request",
                            side_effect=[
                                {"id": 7},
                                existing,
                                {"state": "open", "head": {"sha": "a" * 40}},
                                {},
                            ],
                        ) as request:
                            self.assertEqual(self.publish(result), "unverified")
                            request.assert_not_called()

    def test_missing_report_retries_without_posting(self):
        result = self.result()
        result["run"] = {}
        with patch.object(
            github,
            "github_request",
            side_effect=[
                {"id": 7},
                [],
                {"state": "open", "head": {"sha": "a" * 40}},
            ],
        ) as request:
            with self.assertRaises(urllib.error.URLError):
                self.publish(result)
            self.assertEqual(request.call_count, 3)

    def test_no_major_deletes_existing_comment_only(self):
        for severity in ("minor", "critical"):
            for existing in (
                [],
                [
                    {
                        "id": 2,
                        "user": {"id": 7},
                        "body": "<!-- ralph-major:owner/repo -->",
                    }
                ],
            ):
                with patch.object(
                    github,
                    "github_request",
                    side_effect=[
                        {"id": 7},
                        existing,
                        {"state": "open", "head": {"sha": "a" * 40}},
                        None,
                    ],
                ) as request:
                    self.assertEqual(
                        self.publish(self.result(severity)),
                        "deleted" if existing else "unchanged",
                    )
                    if existing:
                        self.assertEqual(
                            request.call_args.args[1:3],
                            ("/repos/owner/repo/issues/comments/2", "DELETE"),
                        )
                    else:
                        self.assertEqual(request.call_count, 3)

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
