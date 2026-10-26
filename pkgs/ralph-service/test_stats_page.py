import json
import tempfile
import unittest
from pathlib import Path

from ralph_service import stats, stats_page


class StatsPageTests(unittest.TestCase):
    def test_dashboard_renders_collected_stats_and_escapes_untrusted_findings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary = stats.collect_stats(root / "missing-state", root / "reports")
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
            self.assertFalse((root / "missing-state").exists())


if __name__ == "__main__":
    unittest.main()
