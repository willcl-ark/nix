import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ralph_service import history


class HistoryTests(unittest.TestCase):
    def test_history_preserves_runs_and_only_indexes_complete_public_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = {
                "pr": 123,
                "head": "a" * 40,
                "status": "completed",
                "findings": [],
            }
            for name, record, timestamp in (
                ("old", first, 100),
                ("new", {**first, "head": "b" * 40}, 200),
                ("rerun", {**first, "head": "b" * 40}, 300),
                ("other", {**first, "pr": 456}, 400),
            ):
                path = root / f"{name}.json"
                path.write_text(json.dumps({**record, "report_id": name}))
                os.utime(path, (timestamp, timestamp))
                (root / f"{name}.html").write_text("original report")
            (root / "incomplete.json").write_text(
                json.dumps({**first, "report_id": "incomplete"})
            )
            (root / "private.json").write_text(
                json.dumps({**first, "content": "secret"})
            )
            before = {path: path.read_bytes() for path in root.iterdir()}
            self.assertEqual(history.save_history(root), 2)
            page = (root / "123/index.html").read_text()
            self.assertLess(page.index("../rerun.html"), page.index("../new.html"))
            self.assertLess(page.index("../new.html"), page.index("../old.html"))
            self.assertEqual(page.count('data-commit="' + "b" * 40), 2)
            self.assertEqual(page.count("🟠"), 1)
            self.assertIn("?commit=" + "a" * 40, page)
            self.assertNotIn("incomplete", page)
            self.assertNotIn("other.html", page)
            self.assertNotIn("secret", page)
            for path, content in before.items():
                self.assertEqual(path.read_bytes(), content)
            catalog = json.loads((root / "history.json").read_text())
            self.assertEqual(
                {item["report_id"] for item in catalog},
                {"old", "new", "rerun", "other"},
            )
            self.assertNotIn("secret", (root / "history.json").read_text())
            self.assertNotIn("review", catalog[0])
            self.assertNotIn("Full report", (root / "index.html").read_text())
            self.assertIn(
                '<script src="history.js" defer>', (root / "index.html").read_text()
            )
            self.assertIn('<script src="../history.js" defer>', page)
            self.assertNotIn("<script>", page)
            (root / "history.js").unlink()
            self.assertEqual(history.save_history(root, 123), 1)
            self.assertTrue((root / "history.js").is_file())
            self.assertEqual(json.loads((root / "history.json").read_text()), catalog)

    def test_incremental_history_matches_full_and_retries_failed_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "public"
            root.mkdir()
            cache = Path(directory) / "private"
            source = root / "review.json"
            source.write_text(
                json.dumps(
                    {"pr": 123, "report_id": "review", "findings": [], "head": "a" * 40}
                )
            )
            (root / "review.html").touch()
            history.save_history(root, cache_dir=cache)
            public_files = [
                root / "123/index.html",
                root / "history.json",
                root / "history.js",
                root / "theme.js",
            ]
            mtimes = {path: path.stat().st_mtime_ns for path in public_files}
            original_read = Path.read_text

            def read(path, *args, **kwargs):
                if path == source:
                    self.fail("unchanged source JSON was read")
                return original_read(path, *args, **kwargs)

            with mock.patch.object(Path, "read_text", read):
                history.save_history(root, cache_dir=cache)
            self.assertEqual(mtimes, {p: p.stat().st_mtime_ns for p in public_files})
            self.assertEqual((cache / "history.json").stat().st_mode & 0o777, 0o600)
            page = root / "123/index.html"
            original_page = page.read_bytes()
            page.unlink()
            with mock.patch.object(Path, "read_text", read):
                history.save_history(root, cache_dir=cache)
            self.assertEqual(page.read_bytes(), original_page)

            # A report moving between PRs must remove the old generated page.
            source.write_text(
                json.dumps(
                    {
                        "pr": 456,
                        "report_id": "review",
                        "findings": [{}],
                        "head": "b" * 40,
                    }
                )
            )
            original_write = history.report._write_public_file

            def fail_catalog(path, content, **kwargs):
                if path == root / "history.json":
                    raise OSError("publication interrupted")
                return original_write(path, content, **kwargs)

            with (
                mock.patch.object(history.report, "_write_public_file", fail_catalog),
                self.assertRaises(OSError),
            ):
                history.save_history(root, cache_dir=cache)
            history.save_history(root, cache_dir=cache)
            self.assertFalse((root / "123/index.html").exists())
            before = {
                p.relative_to(root): p.read_bytes()
                for p in root.rglob("*")
                if p.is_file()
            }
            history.save_history(root, cache_dir=cache, full=True)
            self.assertEqual(
                before,
                {
                    p.relative_to(root): p.read_bytes()
                    for p in root.rglob("*")
                    if p.is_file()
                },
            )
            (root / "review.html").unlink()
            (cache / "history.json").unlink()
            history.save_history(root, cache_dir=cache, full=True)
            self.assertFalse((root / "456/index.html").exists())
            self.assertEqual(json.loads((root / "history.json").read_text()), [])

    def test_recorded_time_and_html_escaping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, date, mtime in (
                ("earlier", "2026-01-01T00:00:00Z", 200),
                ("later", "2026-02-01T00:00:00Z", 100),
                ("backfilled", "unknown", 300),
            ):
                path = root / f"{name}.json"
                path.write_text(
                    json.dumps(
                        {
                            "pr": 123,
                            "report_id": name,
                            "head": '<script>alert("head")</script>',
                            "status": "<img src=x onerror=alert(1)>",
                            "review_completed_at": date,
                        }
                    )
                )
                os.utime(path, (mtime, mtime))
                (root / f"{name}.html").touch()
            history.save_history(root)
            page = (root / "123/index.html").read_text()
            self.assertLess(page.index("../later.html"), page.index("../earlier.html"))
            self.assertLess(
                page.index("../earlier.html"), page.index("../backfilled.html")
            )
            self.assertIn("Unknown", page)
            self.assertIn("🟠", page)
            self.assertIn("2026-02-01 00:00:00 UTC", page)
            self.assertNotIn('<script>alert("head")', page)
            self.assertNotIn("<img src=x", page)
            (root / "earlier.html").unlink()
            (root / "later.html").unlink()
            history.save_history(root)
            self.assertNotIn("🟠", (root / "123/index.html").read_text())


if __name__ == "__main__":
    unittest.main()
