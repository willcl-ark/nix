import json
import os
import tempfile
import unittest
from pathlib import Path

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
            self.assertIn("?commit=" + "a" * 40, page)
            self.assertNotIn("incomplete", page)
            self.assertNotIn("other.html", page)
            self.assertNotIn("secret", page)
            for path, content in before.items():
                self.assertEqual(path.read_bytes(), content)
            self.assertEqual(history.save_history(root, 123), 1)

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
            self.assertIn("2026-02-01 00:00:00 UTC", page)
            self.assertNotIn('<script>alert("head")', page)
            self.assertNotIn("<img src=x", page)


if __name__ == "__main__":
    unittest.main()
