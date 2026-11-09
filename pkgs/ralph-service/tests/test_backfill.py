import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

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
                    "head TEXT, status TEXT, review_result TEXT, repo_id TEXT)"
                )
                for job_id, status in enumerate(
                    ("complete", "failed", "running", "complete"), 1
                ):
                    db.execute(
                        "INSERT INTO jobs VALUES (?, 123, ?, ?, ?, ?, 'core')",
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
            self.assertEqual(len(list(reports.glob("123-*.html"))), 2)

    def test_backfill_namespaces_gui_and_preserves_saved_source_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = root / "reports"
            head = "a" * 40
            result = {
                "head_sha": head,
                "content": "Verified review",
                "engine": {"name": "current"},
                "output": {"exports": {"findings": []}},
                "run": {"status": "completed"},
            }
            with sqlite3.connect(root / "jobs.sqlite3") as db:
                db.execute(
                    "CREATE TABLE jobs (id INTEGER, repo_id TEXT, number INTEGER, generation INTEGER, head TEXT, status TEXT, review_result TEXT)"
                )
                for job_id, repo in ((1, "core"), (2, "gui")):
                    saved = {
                        **result,
                        "run": {
                            **result["run"],
                            "repository_id": repo,
                            "repository": "owner/" + repo,
                            "review_scope": {
                                "repository": "https://forge.example/owner/" + repo
                            },
                        },
                    }
                    db.execute(
                        "INSERT INTO jobs VALUES (?,?,123,1,?,'complete',?)",
                        (job_id, repo, head, json.dumps(saved)),
                    )
            core_name = report.save_report(reports, 123, result, f"123-{head}-1-1")
            core_bytes = (reports / core_name).read_bytes()
            self.assertEqual(backfill.backfill(root, reports)["eligible"], 1)
            self.assertFalse((reports / "gui").exists())
            recovered = backfill.backfill(
                root,
                reports,
                apply=True,
                repositories={
                    "gui": {
                        "repository": "changed/name",
                        "repository_url": "https://changed.example/repo",
                    }
                },
            )
            self.assertEqual(recovered["existing"], 1)
            self.assertEqual(recovered["written"], 1)
            gui_name = f"123-{head}-2-1"
            public = json.loads((reports / "gui" / (gui_name + ".json")).read_text())
            self.assertEqual(public["repository_id"], "gui")
            self.assertEqual(public["repository"], "owner/gui")
            self.assertEqual(
                public["repository_url"], "https://forge.example/owner/gui"
            )
            self.assertFalse((reports / (gui_name + ".html")).exists())
            self.assertTrue((reports / "gui/123/index.html").is_file())
            self.assertEqual((reports / core_name).read_bytes(), core_bytes)
            self.assertEqual(
                backfill.backfill(root, reports, apply=True)["existing"], 2
            )

    def test_public_snapshot_recovers_pages_without_job_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = root / "archive"
            name = report.save_report(
                reports,
                123,
                {"content": "Original approved wording."},
                "saved-run",
                repository_id="gui",
            )
            snapshot = reports / "gui/saved-run.json"
            original = snapshot.read_bytes()
            (reports / "reports" / name).unlink()
            (reports / name).write_text("Old combined report")
            dry = backfill.backfill(root, reports)
            self.assertEqual(dry["eligible"], 1)
            self.assertFalse((reports / "reports" / name).exists())
            self.assertEqual((reports / name).read_text(), "Old combined report")
            write = report._write_public_file

            def interrupt_human_page(path, *args, **kwargs):
                if path == reports / "reports" / name:
                    raise OSError("Interrupted before human page")
                return write(path, *args, **kwargs)

            with patch.object(
                report, "_write_public_file", side_effect=interrupt_human_page
            ):
                interrupted = backfill.backfill(root, reports, apply=True)
            self.assertEqual(interrupted["unrecoverable"], 1)
            self.assertFalse((reports / "reports" / name).exists())
            self.assertIn("Run archive", (reports / name).read_text())
            applied = backfill.backfill(root, reports, apply=True)
            self.assertEqual(applied["written"], 1)
            self.assertEqual(snapshot.read_bytes(), original)
            self.assertIn(
                "Original approved wording.", (reports / "reports" / name).read_text()
            )
            self.assertNotIn("Original approved wording.", (reports / name).read_text())
            self.assertIn("Run archive", (reports / name).read_text())
            self.assertEqual(backfill.backfill(root, reports)["existing"], 1)
            self.assertFalse((root / "jobs.sqlite3").exists())

    def test_missing_and_unsupported_results_are_reported_without_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with sqlite3.connect(root / "jobs.sqlite3") as db:
                db.execute(
                    "CREATE TABLE jobs (id INTEGER, number INTEGER, generation INTEGER, "
                    "head TEXT, status TEXT, review_result TEXT, repo_id TEXT)"
                )
                db.execute(
                    "INSERT INTO jobs VALUES (1,123,1,?,'complete',?,'core')",
                    ("a" * 40, "{}"),
                )
            result = backfill.backfill(root, root / "reports")
            self.assertEqual(result["unrecoverable"], 1)
            self.assertEqual(result["eligible"], 0)
            self.assertFalse((root / "reports").exists())


if __name__ == "__main__":
    unittest.main()
