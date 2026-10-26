import json
import tempfile
import unittest
from pathlib import Path

from ralph_service import jobs, report, stats, stats_page


class StatsPageTests(unittest.TestCase):
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
                            "budget_limited": hit,
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
            self.assertEqual(sum(g["limited_runs"] for g in limits["budgets"]), 1)
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
