import json
import tempfile
import unittest
from pathlib import Path

from ralph_service import report


class ReportTests(unittest.TestCase):
    def test_challenger_comparison_preserves_disagreement_and_safe_evidence(self):
        evidence = {
            "path": "src/example.cpp",
            "line": 12,
            "side": "head",
            "excerpt": "if (height < activation) return;",
        }
        debug = {
            "schema_version": 2,
            "candidate_sources": {"tests:1": "tests"},
            "challenges": [
                {
                    "candidate_id": "tests:1",
                    "outcome": "refuted",
                    "reason": "The guard already covers activation.",
                    "source_evidence": [
                        {**evidence, "private": "unpublished-runner-value"}
                    ],
                }
            ],
            "stages": {
                "tests": {
                    "raw_output": json.dumps(
                        {
                            "findings": [
                                {
                                    "title": "<script>candidate()</script>",
                                    "path": "src/example.cpp",
                                    "line": 12,
                                    "side": "head",
                                }
                            ]
                        }
                    )
                },
                "verifier": {
                    "decisions": [
                        {
                            "candidate_ids": ["tests:1"],
                            "disposition": "supported",
                            "reason": "The guard applies to another entry point.",
                        }
                    ]
                },
            },
            "decisions": [
                {
                    "candidate_ids": ["tests:1"],
                    "disposition": "publish",
                    "reason": "The uncovered entry point is reachable.",
                    "finding": {"id": "finding:1", "title": "Check both entry points"},
                }
            ],
        }
        result = {
            "schema_version": 1,
            "engine": {"name": "current"},
            "output": {"schema_version": 2, "data": debug},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            name = report.save_report(root, 123, result, "challenge-review")
            public = json.loads((root / "challenge-review.json").read_text())
            rendered = (root / name).read_text()
        challenge = public["challenges"][0]
        self.assertEqual(challenge["source_evidence"], [evidence])
        self.assertEqual(challenge["verifier_disposition"], "supported")
        self.assertEqual(challenge["final_disposition"], "publish")
        self.assertIn('id="challenger"', rendered)
        self.assertIn("Refuted, then approved", rendered)
        self.assertIn("The guard applies to another entry point.", rendered)
        self.assertIn("&lt;script&gt;candidate()&lt;/script&gt;", rendered)
        self.assertNotIn("<script>candidate()</script>", rendered)
        self.assertNotIn("unpublished-runner-value", rendered)
        self.assertNotIn("unpublished-runner-value", json.dumps(public))

    def test_automatic_high_profile_reports_effective_settings(self):
        result = {
            "schema_version": 1,
            "engine": {
                "name": "current",
                "config": {
                    "profile": {
                        "name": "default",
                        "stages": {"tests": {"model": "cheap"}},
                    },
                    "high_profile": {
                        "name": "high",
                        "stages": {"tests": {"model": "strong"}},
                    },
                },
            },
            "output": {
                "schema_version": 2,
                "data": {
                    "schema_version": 2,
                    "profile_selection": {
                        "requested": "default",
                        "effective": "high",
                        "reason": "Consensus review requires high settings.",
                    },
                    "stages": {
                        "router": {"model": "router-choice", "status": "completed"}
                    },
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            name = report.save_report(root, 123, result, "auto-high")
            public = json.loads((root / "auto-high.json").read_text())
            self.assertEqual(public["profile"], "high")
            self.assertEqual(
                public["models"], {"tests": "strong", "router": "router-choice"}
            )
            self.assertIn("Requested profile: default", (root / name).read_text())

    def test_retry_preserves_snapshot_and_recovers_missing_html(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = {
                "content": "Original review",
                "head_sha": "a" * 40,
                "run": {"review_completed_at": "2026-10-30T12:00:00Z"},
            }
            name = report.save_report(root, 123, result, "123-first")
            original_html = (root / name).read_bytes()
            original_json = (root / "123-first.json").read_bytes()
            self.assertIn(b'href="123/"', original_html)
            result["content"] = "Changed on retry"
            report.save_report(root, 123, result, "123-first")
            self.assertEqual((root / name).read_bytes(), original_html)
            self.assertEqual((root / "123-first.json").read_bytes(), original_json)
            (root / name).unlink()
            report.save_report(root, 123, result, "123-first")
            self.assertEqual((root / name).read_bytes(), original_html)
            second = report.save_report(root, 123, result, "123-rerun")
            self.assertIn("Changed on retry", (root / second).read_text())
            self.assertEqual((root / name).read_bytes(), original_html)
            self.assertEqual(
                json.loads(original_json)["review_completed_at"],
                "2026-10-30T12:00:00Z",
            )

    def test_review_markdown_is_rendered_and_sanitized(self):
        review = """##### Findings

**[Major] Handle a zero divisor**

See [`divide()`](https://example.org/fix).

<details>
<summary>Details for <code>divide()</code></summary>

Reject `zero` before dividing.

- Validate input
- Return an error

```cpp
if (value < 1) return;
```

</details>

<script>alert('review')</script>

<a href="javascript:alert(1)" onclick="alert(2)">unsafe link</a>
<img src="x" onerror="alert(3)">
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            name = report.save_report(root, 123, {"content": review}, "markdown-review")
            rendered = (root / name).read_text()
            public = json.loads((root / "markdown-review.json").read_text())
        self.assertIn("<h5>Findings</h5>", rendered)
        self.assertIn("<strong>[Major] Handle a zero divisor</strong>", rendered)
        self.assertIn('<a href="https://example.org/fix"', rendered)
        self.assertIn("<details>", rendered)
        self.assertIn("<summary>Details for <code>divide()</code></summary>", rendered)
        self.assertIn("Reject <code>zero</code> before dividing.", rendered)
        self.assertIn("<li>Validate input</li>", rendered)
        self.assertIn("if (value &lt; 1) return;", rendered)
        self.assertNotIn(
            "<script", rendered.replace('<script src="theme.js" defer></script>', "")
        )
        self.assertNotIn("javascript:", rendered)
        self.assertNotIn("onclick", rendered)
        self.assertNotIn("onerror", rendered)
        self.assertEqual(public["review"], review)

    def test_finding_summary_and_explanation_survive_public_report(self):
        finding = {
            "id": "finding:1",
            "title": "Handle a zero divisor",
            "summary": "A zero divisor crashes the operation.",
            "body": "Reject zero before dividing the supplied value.",
        }
        source = {
            "path": "src/example.cpp",
            "line": 12,
            "side": "head",
            "excerpt": "return value / divisor;",
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
                                {
                                    "disposition": "publish",
                                    "finding": finding,
                                    "source_evidence": [{**source, "private": "omit"}],
                                }
                            ],
                            "finding_attribution": [
                                {
                                    "finding_id": "finding:1",
                                    "verified_by": "verifier",
                                    "judged_by": "judge",
                                    "judged_by_model": "judge-model",
                                    "edited_by": "judge",
                                }
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
                if engine_name == "current":
                    self.assertEqual(
                        public["decisions"][0]["source_evidence"], [source]
                    )
                    self.assertEqual(
                        public["finding_attribution"][0]["judged_by"], "judge"
                    )
                    self.assertEqual(
                        public["finding_attribution"][0]["judged_by_model"],
                        "judge-model",
                    )

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
            "coverage": {
                "status": "partial",
                "limitations": ["tests: Output failed validation"],
                "private": secret,
            },
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
            "base_sha": "b" * 40,
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
        self.assertEqual(json.loads(public)["base"], result["base_sha"])
        self.assertIn(result["base_sha"], html)
        for limitation in debug["coverage"]["limitations"]:
            self.assertIn(limitation, html)
            self.assertIn(limitation, json.loads(public)["coverage"]["limitations"])
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
