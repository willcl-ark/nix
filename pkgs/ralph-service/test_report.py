import json
import tempfile
import unittest
from pathlib import Path

from ralph_service import report


class ReportTests(unittest.TestCase):
    def test_public_report_escapes_markup_and_excludes_private_data(self):
        secret = "PRIVATE-CREDENTIAL"
        hostile = "<script>alert('review')</script>"
        debug = {
            "instructions": secret,
            "review_input": secret,
            "private_state": {"token": secret},
            "coverage": {"status": "partial", "private": secret},
            "decisions": [{"disposition": "reject", "private": secret}],
            "finding_attribution": [{"finding_id": "finding:1", "private": secret}],
            "concept_assessment": {"status": "verified", "private": secret},
            "stages": {
                "independent": {
                    "status": "completed",
                    "raw_output": hostile,
                    "private": secret,
                    "turns": [{"input": secret, "instructions": secret}],
                    "tools": [
                        {"name": "read_file", "arguments": secret, "output": secret}
                    ],
                }
            },
        }
        result = {
            "schema_version": 1,
            "head_sha": "a" * 40,
            "content": hostile,
            "engine": {"name": "current"},
            "output": {
                "schema_version": 2,
                "data": {"schema_version": 2, **debug},
                "exports": {
                    "findings": [],
                    "backport_assessment": {
                        "verdict": "Needs clarification",
                        "summary": hostile,
                        "limitations": ["Original PR unavailable"],
                        "private": secret,
                    },
                },
            },
            "run": {"status": "completed", "metrics": {}, "attempt": {}},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            name = report.save_report(root, 123, result, "123-abcdef")
            html = (root / name).read_text()
            public = (root / "123-abcdef.json").read_text()
        self.assertNotIn(secret, html + public)
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;alert", html)
        self.assertEqual(json.loads(public)["review"], hostile)
        self.assertEqual(
            json.loads(public)["backport_assessment"]["verdict"], "Needs clarification"
        )


if __name__ == "__main__":
    unittest.main()
