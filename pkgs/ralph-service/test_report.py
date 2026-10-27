import json
import tempfile
import unittest
from pathlib import Path

from ralph_service import report


class ReportTests(unittest.TestCase):
    def test_finding_summary_and_explanation_survive_public_report(self):
        finding = {
            "id": "finding:1",
            "title": "Handle a zero divisor",
            "summary": "A zero divisor crashes the operation.",
            "body": "Reject zero before dividing the supplied value.",
        }
        for engine_name in ("current", "external"):
            with (
                self.subTest(engine=engine_name),
                tempfile.TemporaryDirectory() as directory,
            ):
                result = {
                    "schema_version": 1,
                    "engine": {"name": engine_name},
                    "output": {
                        "schema_version": 2,
                        "data": {
                            "schema_version": 2,
                            "decisions": [
                                {"disposition": "publish", "finding": finding}
                            ],
                        },
                        "exports": {"findings": [finding]},
                    },
                }
                root = Path(directory)
                name = report.save_report(root, 123, result, "finding-review")
                rendered = (root / name).read_text()
                public = json.loads((root / "finding-review.json").read_text())
                for field in ("summary", "body"):
                    self.assertIn(finding[field], rendered)
                    self.assertEqual(public["findings"][0][field], finding[field])

    def test_experiment_artifacts_are_visible_without_exposing_runner_internals(self):
        command = {
            "id": "command:1",
            "finding_id": "finding:1",
            "side": "head",
            "revision": "a" * 40,
            "command": "python test_repro.py",
            "status": "completed",
            "exit_code": 1,
            "output": "AssertionError: <expected>",
            "patch": "+++ test_repro.py\n+assert False\n",
            "capture_complete": True,
            "private_environment": "PRIVATE-CREDENTIAL",
        }
        result = {
            "schema_version": 1,
            "engine": {"name": "current"},
            "run": {"status": "completed"},
            "output": {
                "schema_version": 2,
                "data": {
                    "schema_version": 2,
                    "coverage": {
                        "status": "complete",
                        "experiments": {
                            "status": "partial",
                            "limitations": ["Control unavailable"],
                        },
                    },
                    "stages": {
                        "write_verifier": {
                            "status": "completed",
                            "results": [
                                {
                                    "finding_id": "finding:1",
                                    "outcome": "inconclusive",
                                    "reason": "Control unavailable",
                                    "command_ids": ["command:1"],
                                }
                            ],
                            "execution": {
                                "head_sha": "a" * 40,
                                "commands": [command],
                                "private_path": "PRIVATE-CREDENTIAL",
                            },
                        }
                    },
                },
                "exports": {"findings": []},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            name = report.save_report(root, 123, result, "experiment-review")
            html = (root / name).read_text()
            public = json.loads((root / "experiment-review.json").read_text())
        execution = public["stages"]["write_verifier"]["execution"]
        self.assertEqual(execution["commands"][0]["patch"], command["patch"])
        self.assertEqual(execution["commands"][0]["exit_code"], 1)
        self.assertEqual(public["status"], "completed")
        self.assertEqual(public["coverage"]["experiments"]["status"], "partial")
        self.assertIn("AssertionError: &lt;expected&gt;", html)
        self.assertIn("Experiment evidence", html)
        self.assertNotIn("PRIVATE-CREDENTIAL", html + json.dumps(public))

    def test_failed_review_exports_status_and_safe_provider_diagnostic(self):
        result = {
            "schema_version": 1,
            "engine": {"name": "current"},
            "run": {"status": "failed"},
            "output": {
                "schema_version": 2,
                "data": {
                    "schema_version": 2,
                    "stages": {
                        "verifier": {
                            "status": "failed",
                            "error_message": "Unsupported parameter: max_tool_calls <bad>",
                            "http_status": 400,
                            "response_body": "PRIVATE-CREDENTIAL",
                            "headers": {"Authorization": "PRIVATE-CREDENTIAL"},
                        }
                    },
                },
                "exports": {"findings": []},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            name = report.save_report(root, 123, result, "failed-review")
            html = (root / name).read_text()
            public = json.loads((root / "failed-review.json").read_text())
        self.assertEqual(public["status"], "failed")
        self.assertEqual(public["stages"]["verifier"]["http_status"], 400)
        self.assertIn("Review failed", html)
        self.assertIn("Unsupported parameter: max_tool_calls &lt;bad&gt;", html)
        self.assertNotIn("PRIVATE-CREDENTIAL", html + json.dumps(public))

    def test_unknown_schema_does_not_export_retained_findings(self):
        result = {
            "schema_version": 1,
            "output": {
                "schema_version": 1,
                "exports": {
                    "review_memory": [{"severity": "major", "status": "unclear"}],
                },
            },
        }
        self.assertEqual(len(report.core_trace.unresolved_memory(result)), 1)
        result["schema_version"] = True
        self.assertEqual(report.core_trace.review_memory_export(result), [])
        result["schema_version"] = 1
        result["output"]["schema_version"] = 0
        self.assertEqual(report.core_trace.review_memory_export(result), [])

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
                    "review_memory": [
                        {"title": "Prior concern", "status": "unclear", "body": hostile}
                    ],
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
            json.loads(public)["review_memory"],
            result["output"]["exports"]["review_memory"],
        )
        self.assertEqual(
            json.loads(public)["backport_assessment"]["verdict"], "Needs clarification"
        )


if __name__ == "__main__":
    unittest.main()
