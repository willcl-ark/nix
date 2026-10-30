import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ralph_service import backfill, report


class BackfillTests(unittest.TestCase):
    def test_backfill_only_completed_reviews_and_preserves_existing_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = root / "reports"
            head = "a" * 40
            result = {
                "head_sha": head,
                "content": "Historical verified review",
                "engine": {"name": "current", "config": {"secret": "PRIVATE"}},
                "output": {"exports": {"findings": []}},
                "run": {"status": "completed", "attempt": {}},
                "private_request": "PRIVATE",
            }
            with sqlite3.connect(root / "jobs.sqlite3") as db:
                db.execute(
                    "CREATE TABLE jobs (id INTEGER, number INTEGER, generation INTEGER, "
                    "head TEXT, status TEXT, review_result TEXT)"
                )
                for job_id, status in enumerate(
                    ("complete", "failed", "running", "complete"), 1
                ):
                    db.execute(
                        "INSERT INTO jobs VALUES (?, 123, ?, ?, ?, ?)",
                        (job_id, job_id, head, status, json.dumps(result)),
                    )
            old_name = report.save_report(reports, 123, result, f"123-{head}-4-4")
            before = {p.name: p.read_bytes() for p in reports.glob("*.*")}
            dry = backfill.backfill(root, reports)
            self.assertEqual(dry["eligible"], 1)
            self.assertEqual(dry["existing"], 1)
            self.assertEqual(dry["written"], 0)
            self.assertEqual(
                before, {p.name: p.read_bytes() for p in reports.glob("*.*")}
            )
            applied = backfill.backfill(root, reports, apply=True)
            self.assertEqual(applied["written"], 1)
            self.assertEqual((reports / old_name).read_bytes(), before[old_name])
            new_json = (reports / f"123-{head}-1-1.json").read_text()
            self.assertEqual(json.loads(new_json)["review_completed_at"], "unknown")
            self.assertNotIn("PRIVATE", new_json)
            self.assertIn(result["content"], new_json)
            self.assertTrue((reports / "123/index.html").is_file())
            self.assertEqual(backfill.backfill(root, reports, apply=True)["written"], 0)
            self.assertEqual(len(list(reports.glob("*.html"))), 2)

    def test_missing_and_unsupported_results_are_reported_without_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with sqlite3.connect(root / "jobs.sqlite3") as db:
                db.execute(
                    "CREATE TABLE jobs (id INTEGER, number INTEGER, generation INTEGER, "
                    "head TEXT, status TEXT, review_result TEXT)"
                )
                db.execute(
                    "INSERT INTO jobs VALUES (1,123,1,?,'complete',?)", ("a" * 40, "{}")
                )
            result = backfill.backfill(root, root / "reports")
            self.assertEqual(result["unrecoverable"], 1)
            self.assertEqual(result["eligible"], 0)
            self.assertFalse((root / "reports").exists())


if __name__ == "__main__":
    unittest.main()
