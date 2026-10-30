import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ralph.usage import Ledger
from ralph_service import jobs, report, stats, stats_page


class StatsPageTests(unittest.TestCase):
    def test_daily_trends_accumulate_without_treating_missing_counts_as_zero(self):
        summary = {
            "time_series": [
                {
                    "period": "2026-10-25",
                    "reviews": 2,
                    "verified_reviews": 1,
                    "findings": 3,
                    "addressed": 0,
                },
                {
                    "period": "2026-10-26",
                    "reviews": 0,
                    "verified_reviews": 0,
                    "findings": 0,
                    "addressed": 0,
                },
                {
                    "period": "2026-10-27",
                    "reviews": 4,
                    "verified_reviews": None,
                    "findings": 5,
                    "addressed": 1,
                },
                {
                    "period": "2026-10-28",
                    "reviews": 1,
                    "verified_reviews": 1,
                    "findings": 0,
                    "addressed": 0,
                },
            ]
        }
        records = stats_page._time_series(summary)
        cumulative = stats_page._cumulative_series(records)
        self.assertEqual([row["reviews"] for row in cumulative], [2, 2, 6, 7])
        self.assertEqual([row["findings"] for row in cumulative], [3, 3, 8, 8])
        self.assertEqual(
            [row["verified_reviews"] for row in cumulative], [1, 1, None, None]
        )
        html = stats_page._charts_section(summary)
        self.assertIn("2026-10-27: 4", html)
        self.assertIn("2026-10-27: 6", html)
        self.assertIn('aria-controls="trends-cumulative"', html)

    def test_failed_review_status_is_separate_from_completed_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = jobs.JobStore(root / "jobs.sqlite3")
            store.enqueue(
                42, "master", "a" * 40, "opened", engine_identity={"name": "current"}
            )
            job = store.claim()
            store.save_result(
                job,
                {
                    "schema_version": 1,
                    "run": {"status": "failed"},
                    "output": {
                        "schema_version": 2,
                        "exports": {"findings": [{"severity": "minor"}]},
                    },
                },
            )
            store.complete(job)
            summary = stats.collect_stats(root, root / "reports")
        self.assertEqual(summary["reviews"][0]["status"], "complete")
        self.assertEqual(summary["reviews"][0]["review_status"], "failed")
        self.assertEqual(summary["overview"]["review_status"], {"failed": 1})
        self.assertEqual(summary["overview"]["published_findings"], 1)
        html = stats_page._reviews_table(
            summary["reviews"],
            "https://example.org/repo",
            "https://example.org/reports",
        )
        self.assertIn("failed", html)

    def test_latest_marker_tracks_the_latest_available_report_per_pr(self):
        reviews = [
            {"number": 42, "job_id": 1, "report_name": "older.html"},
            {"number": 42, "job_id": 3},
            {"number": 43, "job_id": 4, "report_name": "other-pr.html"},
            {"number": 42, "job_id": 2, "report_name": "newer.html"},
        ]
        rendered = stats_page._reviews_table(
            reviews, "https://example.org/repo", "https://example.org/reports"
        )
        rows = re.findall(r"<tr>.*?</tr>", rendered, flags=re.S)
        self.assertEqual(rendered.count("🟠"), 2)
        for row in rows:
            if "newer.html" in row or "other-pr.html" in row:
                self.assertIn("🟠", row)
            if "older.html" in row:
                self.assertNotIn("🟠", row)

    def test_adversarial_comparison_keeps_historical_gemini_and_counts_claude(self):
        paired = stats._new_adversarial_comparison()
        for secondary in ("adversarial_gemini", "adversarial_claude"):
            debug = {
                "stages": {
                    name: {"status": "completed"} for name in ("adversarial", secondary)
                },
                "finding_attribution": [
                    {"finding_id": secondary, "raised_by": [secondary]}
                ],
            }
            stats._add_adversarial_comparison(paired, debug)
        self.assertEqual(paired["completed_reviews"], 2)
        self.assertEqual(paired["sole_accepted"]["adversarial_claude"], 1)
        self.assertEqual(paired["sole_accepted"]["adversarial_gemini"], 1)

    def test_usage_keeps_missing_fields_and_model_providers_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Ledger(root / "usage.sqlite3")
            with sqlite3.connect(root / "usage.sqlite3") as db:
                for token, model, usage in (
                    (
                        "claude",
                        "claude-opus-5-5",
                        {"input_tokens": 100, "output_tokens": 20},
                    ),
                    (
                        "openai",
                        "gpt-6.1-sol",
                        {
                            "input_tokens": 200,
                            "output_tokens": 30,
                            "cached_tokens": 100,
                            "cache_write_tokens": 0,
                            "reasoning_tokens": 10,
                            "web_search_calls": 0,
                        },
                    ),
                    ("unknown", "gpt-6.1-sol", None),
                    ("timeout", "gpt-6.1-sol", None),
                ):
                    db.execute(
                        "INSERT INTO requests (token, review_id, stage, model, month, "
                        "status, usage_json, requested_at) "
                        "VALUES (?, 'pr:42:1:1', 'independent', ?, '2026-10', 'completed', ?, 1790812800)",
                        (token, model, json.dumps(usage)),
                    )
                db.execute(
                    "INSERT INTO requests (token, review_id, stage, model, month, "
                    "status, usage_json, requested_at) "
                    "SELECT 'duplicate', review_id, stage, model, month, 'duplicate', "
                    "usage_json, requested_at FROM requests WHERE token = 'openai'"
                )
                db.execute(
                    "UPDATE requests SET status = 'failed', http_status = 429 "
                    "WHERE token = 'unknown'"
                )
                db.execute(
                    "UPDATE requests SET status = 'uncertain' WHERE token = 'timeout'"
                )
            # Retired ledgers must not contribute to subscription telemetry.
            (root / "spend.sqlite3").write_text("retired")
            (root / "ppq-spend.sqlite3").write_text("retired")
            summary = stats.collect_stats(root, root / "reports")
            totals = summary["usage"]["lifetime"]
            self.assertEqual(totals["request_count"], 5)
            self.assertEqual(
                totals["status_counts"],
                {"completed": 2, "duplicate": 1, "failed": 1, "uncertain": 1},
            )
            self.assertEqual(totals["rate_limit_count"], 1)
            self.assertEqual(totals["http_status_counts"], {"429": 1})
            self.assertEqual(totals["tokens"]["reasoning"], 10)
            self.assertEqual(totals["tokens"]["output"], 50)
            self.assertEqual(stats_page._failed_requests(totals), 2)
            for rendered in (
                stats_page._usage_rows(totals["providers"]),
                stats_page._model_section(summary),
            ):
                self.assertRegex(rendered, r"<td[^>]*>2</td><td[^>]*>1</td>")
            self.assertIsNone(totals["providers"]["claude"]["tokens"]["cached"])
            self.assertEqual(totals["tokens"]["input"], 300)
            self.assertEqual(totals["unknown_token_counts"]["cached"], 3)
            self.assertEqual(totals["providers"]["claude"]["request_count"], 1)
            self.assertEqual(totals["providers"]["openai"]["request_count"], 4)
            self.assertFalse(summary["inventory"]["missing_sources"]["review_ledger"])
            self.assertEqual(stats_page._fmt_tokens(totals, "input"), "300 (partial)")
            self.assertEqual(stats_page._cache_rate(totals), "N/A")
            self.assertEqual(
                stats_page._fmt_tokens(totals["providers"]["claude"], "cached"), "N/A"
            )
            public = root / "public"
            stats_page.save_stats(
                public,
                summary,
                "https://code.example.org/org/repo",
                "https://reports.example.org",
            )
            html = "".join(path.read_text() for path in public.rglob("*.html"))
            self.assertNotIn("$", html)
            self.assertIn("Rate limits (429)", html)
            public_json = json.dumps(summary)
            for obsolete in (
                "cost_micros",
                "reserved_micros",
                "budget_limited",
                '"spend"',
            ):
                self.assertNotIn(obsolete, public_json)
            self.assertEqual(
                stats.collect_stats(root / "empty", root / "reports")["usage"][
                    "lifetime"
                ]["tokens"]["input"],
                None,
            )

    def test_subscription_quota_does_not_present_unavailable_accounts_as_unused(self):
        summary = {
            "subscriptions": {
                "status": "partial",
                "checked_at": "2026-10-27T10:05:00Z",
                "accounts": [
                    {
                        "provider": "codex",
                        "status": "ok",
                        "plan": "Pro <script>",
                        "windows": [
                            {
                                "name": "Weekly",
                                "used_percent": 25,
                                "resets_at": "2026-11-01T10:00:00Z",
                            }
                        ],
                    },
                    {
                        "provider": "claude",
                        "status": "unavailable",
                        "plan": None,
                        "windows": [
                            {"name": "Weekly", "used_percent": 0, "resets_at": None}
                        ],
                    },
                ],
            },
        }
        html = stats_page._usage_html(summary)
        self.assertIn("75.0%", html)
        self.assertNotIn("100.0%", html)
        self.assertIn("2026-10-27 10:05 UTC", html)
        self.assertIn("2026-11-01 10:00 UTC", html)
        self.assertIn("Pro &lt;script&gt;", html)
        self.assertNotIn("Pro <script>", html)
        self.assertIn("Unknown plan", html)
        self.assertIn("Remaining capacity is unknown", html)
        self.assertEqual(html.count('aria-label="Remaining quota"'), 1)

    def test_dashboard_renders_collected_stats_and_escapes_untrusted_findings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            reports = root / "reports"
            store = jobs.JobStore(state / "jobs.sqlite3")
            # One capped run, one uncapped run with the same allowance, a high
            # run with another allowance, and an older uninstrumented run.
            for index, (profile, cap, used, hit) in enumerate(
                [
                    ("default", 10, 10, True),
                    ("default", 10, 4, False),
                    ("high", 20, 10, False),
                    ("default", None, None, None),
                ]
            ):
                engine = {
                    "name": "current",
                    "code_sha256": "a" * 64,
                    "config_sha256": "b" * 64,
                    "config": {"profile": {"name": profile}},
                }
                head = "a" * 40
                store.enqueue(
                    42 + index, "master", head, "opened", engine_identity=engine
                )
                job = store.claim()
                stage = {"model": "test-model", "status": "completed"}
                if cap is not None:
                    stage.update(
                        {
                            "limits": {
                                "tool_calls": {"limit": cap, "used": used, "hit": hit},
                                "web_search_calls_per_response": {
                                    "limit": 5,
                                    "used": 5,
                                    "hit": None,
                                },
                            },
                        }
                    )
                result = {
                    "schema_version": 1,
                    "head_sha": head,
                    "engine": engine,
                    "output": {
                        "schema_version": 2,
                        "exports": {"findings": []},
                        "data": {"schema_version": 2, "stages": {"independent": stage}},
                    },
                }
                store.save_result(job, result)
                store.complete(job)
                report.save_report(
                    reports,
                    42 + index,
                    result,
                    f"{42 + index}-{head}-{job['id']}-{job['generation']}",
                )
            summary = stats.collect_stats(state, reports)
            limits = summary["limits"]
            self.assertEqual(limits["uninstrumented_stage_runs"], 1)
            tools = [
                group for group in limits["groups"] if group["name"] == "tool_calls"
            ]
            self.assertEqual(
                [(g["profile"], g["cap"], g["runs"], g["hit_runs"]) for g in tools],
                [("default", 10, 2, 1), ("high", 20, 1, 0)],
            )
            web = [
                group
                for group in limits["groups"]
                if group["name"] == "web_search_calls_per_response"
            ]
            self.assertTrue(all(g["hit_known_runs"] == 0 for g in web))
            public_report = json.loads(next(reports.glob("42-*.json")).read_text())
            self.assertTrue(
                public_report["stages"]["independent"]["limits"]["tool_calls"]["hit"]
            )
            summary["stages"] = [
                {
                    "stage": "independent",
                    "configured_model": "test-model",
                    "findings": [
                        {
                            "number": 42,
                            "candidate_id": "independent:1",
                            "title": "<script>alert('finding')</script>",
                            "disposition": "unresolved",
                            "accepted": False,
                            "report_name": "javascript:alert(1)",
                        }
                    ],
                }
            ]
            public = root / "public"
            stats_page.save_stats(
                public,
                summary,
                "https://code.example.org/org/repo",
                "https://reports.example.org/reviews",
            )
            self.assertTrue((public / "index.html").is_file())
            json.loads((public / "stats.json").read_text())
            html = "".join(path.read_text() for path in public.rglob("*.html"))
            self.assertIn("&lt;script&gt;alert", html)
            self.assertNotIn("<script>alert", html)
            self.assertNotIn('href="javascript:', html)
            limits_html = (public / "limits/index.html").read_text()
            self.assertIn("42-" + "a" * 40, limits_html)
            self.assertIn("Unknown", limits_html)


if __name__ == "__main__":
    unittest.main()
