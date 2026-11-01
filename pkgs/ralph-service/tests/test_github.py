"""GitHub publication tests, with no network access."""

import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from ralph import engine
from ralph_service import delivery, github, jobs


class GitHubTests(unittest.TestCase):
    def result(self, severity="major"):
        return {
            "schema_version": 1,
            "base_sha": "b" * 40,
            "merge_base": "c" * 40,
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
                            "verified_by": "verifier",
                            "judged_by": "judge",
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

    def context(self):
        return {"base_ref": "qt6", "title": "Review", "body": "", "labels": []}

    def pull(self, **changes):
        return {
            "number": 42,
            "state": "open",
            "draft": False,
            "head": {"sha": "a" * 40},
            "base": {"ref": "qt6", "repo": {"full_name": "owner/repo"}},
            "title": "Review",
            "body": None,
            "labels": [],
            **changes,
        }

    def session(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = jobs.JobStore(Path(directory.name) / "jobs.sqlite3")
        store.enqueue(42, "master", "a" * 40, "synchronize", engine_identity=None)
        return delivery.Session(store, store.claim(), "github")

    def publish(self, result):
        return github.publish_major_findings(
            "owner/repo",
            "github-token",
            42,
            result,
            is_current=lambda: True,
            expected_context=self.context(),
            delivery=self.session(),
        )

    def test_local_supersession_during_lookup_prevents_publication(self):
        current = True

        def request(token, path, method="GET", data=None):
            nonlocal current
            self.assertEqual(method, "GET")
            if path == "/user":
                return {"id": 7}
            if "/comments?" in path:
                current = False
                return []
            return self.pull()

        with patch.object(github, "github_request", side_effect=request):
            self.assertEqual(
                github.publish_major_findings(
                    "owner/repo",
                    "token",
                    42,
                    self.result(),
                    is_current=lambda: current,
                    expected_context=self.context(),
                    delivery=self.session(),
                ),
                "stale",
            )

    def test_only_major_findings_with_report(self):
        result = self.result()
        result["engine"] = engine.load().identity
        result["output"].update(
            schema_version=2,
            data={"schema_version": 2, "coverage": {"status": "partial"}},
        )
        result["engine"]["config"]["comment_format"]["brief_finding"] = (
            "${summary}\n\n[${severity}] ${title} (${location}, ${side})"
        )
        with patch.object(
            github,
            "github_request",
            side_effect=[
                {"id": 7},
                [],
                self.pull(),
                self.pull(),
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
            "src/test.cpp:5",
            "https://review.example/report.html",
            f"https://github.com/owner/repo/blob/{result['head_sha']}/src/test.cpp#L5",
        ):
            self.assertIn(included, body)
        for excluded in ("Original", "Minor", "Edited body"):
            self.assertNotIn(excluded, body)
        self.assertLess(body.index("Edited summary"), body.index("Edited title"))

    def test_base_location_uses_reviewed_merge_base(self):
        result = self.result()
        result["output"]["exports"]["findings"][0]["side"] = "base"
        with patch.object(
            github,
            "github_request",
            side_effect=[
                {"id": 7},
                [],
                self.pull(),
                self.pull(),
                {},
            ],
        ) as request:
            self.assertEqual(self.publish(result), "created")
        body = request.call_args.args[3]["body"]
        self.assertIn(f"/blob/{result['merge_base']}/src/test.cpp#L5", body)
        self.assertNotIn(f"/blob/{result['base_sha']}/", body)

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
                        self.pull(),
                        self.pull(),
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

    def test_only_complete_analysis_can_clear_previous_warning(self):
        own = {"id": 2, "user": {"id": 7}, "body": "<!-- ralph-major:owner/repo -->"}
        for coverage in (
            {},
            {"status": "partial", "verification": {"status": "complete"}},
            {"status": "complete", "verification": {"status": "unknown"}},
            {"status": "complete", "verification": {"status": "complete"}},
        ):
            with self.subTest(coverage=coverage):
                result = self.result()
                result["engine"] = engine.load().identity
                result["run"]["status"] = "completed"
                result["output"].update(
                    schema_version=2, data={"schema_version": 2, "coverage": coverage}
                )
                result["output"]["exports"]["findings"] = []
                complete = (
                    coverage.get("status") == "complete"
                    and coverage.get("verification", {}).get("status") == "complete"
                )
                with patch.object(
                    github,
                    "github_request",
                    side_effect=[
                        {"id": 7},
                        [own],
                        self.pull(),
                        self.pull(),
                        {},
                    ],
                ) as request:
                    self.assertEqual(
                        self.publish(result), "deleted" if complete else "incomplete"
                    )
                mutations = [
                    call.args[2]
                    for call in request.call_args_list
                    if len(call.args) > 2
                ]
                self.assertEqual(mutations, ["DELETE"] if complete else [])

    def test_major_publication_requires_source_verification_and_judgment(self):
        for verified_by, judged_by in (
            ("major_verifier", "judge"),
            ("verifier", None),
            (None, "judge"),
        ):
            with self.subTest(verified_by=verified_by, judged_by=judged_by):
                result = self.result()
                finding = result["output"]["exports"]["findings"][0]
                finding.update(verified_by=verified_by, judged_by=judged_by)
                with patch.object(github, "github_request") as request:
                    self.assertEqual(self.publish(result), "unverified")
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
                self.pull(),
                self.pull(),
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
        exports["findings"][0]["judged_by"] = None
        with patch.object(github, "github_request") as request:
            self.assertEqual(self.publish(result), "unverified")
        request.assert_not_called()
        exports["findings"][0]["judged_by"] = "judge"
        with patch.object(
            github,
            "github_request",
            side_effect=[
                {"id": 7},
                [],
                self.pull(),
                self.pull(),
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
            self.pull(draft=True),
            self.pull(base={"ref": "other", "repo": {"full_name": "owner/repo"}}),
            self.pull(title="Changed after mirror sync"),
            self.pull(body="Changed instructions"),
            self.pull(labels=[{"name": "backport"}]),
            self.pull(base={"ref": "qt6", "repo": {"full_name": "other/repo"}}),
        ):
            with patch.object(
                github, "github_request", side_effect=[{"id": 7}, [], pull]
            ) as request:
                self.assertEqual(self.publish(self.result()), "stale")
                self.assertEqual(request.call_count, 3)

    def test_retarget_after_intent_persistence_does_not_send(self):
        session = self.session()
        with patch.object(
            github,
            "github_request",
            side_effect=[
                {"id": 7},
                [],
                self.pull(),
                self.pull(base={"ref": "other", "repo": {"full_name": "owner/repo"}}),
            ],
        ) as request:
            self.assertEqual(
                github.publish_major_findings(
                    "owner/repo",
                    "token",
                    42,
                    self.result(),
                    expected_context=self.context(),
                    is_current=lambda: True,
                    delivery=session,
                ),
                "stale",
            )
        self.assertTrue(all(len(call.args) < 3 for call in request.call_args_list))
        self.assertEqual(session._record()["state"], "ready")

    def test_duplicate_owned_markers_on_later_page_block_publication(self):
        own = {"id": 1, "user": {"id": 7}, "body": "<!-- ralph-major:owner/repo -->"}
        unrelated = {"id": 3, "user": {"id": 8}, "body": "Other comment"}
        with patch.object(
            github,
            "github_request",
            side_effect=[{"id": 7}, [own] + [unrelated] * 99, [{**own, "id": 2}]],
        ) as request:
            with self.assertRaisesRegex(ValueError, "Multiple bot review comments"):
                self.publish(self.result())
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
                self.pull(),
                self.pull(),
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
                self.pull(),
            ],
        ) as request:
            self.assertEqual(self.publish(self.result()), "unchanged")
            self.assertEqual(request.call_count, 3)
