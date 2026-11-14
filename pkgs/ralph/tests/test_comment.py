import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import final_comment

COMMENT_DIR = Path(final_comment.__file__).parent


class CommentTests(unittest.TestCase):
    def test_report_ends_with_cited_historical_context(self):
        context = {
            "coverage": {"status": "partial", "limitations": ["internal detail"]},
            "documented_rationale": [
                {
                    "claim": "Listening and address discovery are separate controls.",
                    "relevance": "The changed selection logic must preserve Tor privacy.",
                    "citations": [
                        "https://github.com/bitcoin/bitcoin/pull/5161#issuecomment-60883023"
                    ],
                }
            ],
            "inferred_rationale": [
                {
                    "claim": "This may explain the current default.",
                    "relevance": "The patch preserves that default.",
                    "citations": [],
                }
            ],
            "unresolved_disagreements": [
                {
                    "claim": "Participants disagreed about discovery defaults.",
                    "relevance": "A default change needs a separate justification.",
                    "citations": ["https://example.org/discussion"],
                }
            ],
        }
        rendered = final_comment.render_report(
            [], historical_context=context, coverage_url="https://example.org/coverage"
        )
        self.assertLess(
            rendered.index("Coverage details"), rendered.index("## Background")
        )
        for text in (
            "Documented decisions",
            "Ralph's inferences",
            "Unresolved disagreements",
        ):
            self.assertIn(text, rendered)
        for field in (
            "documented_rationale",
            "inferred_rationale",
            "unresolved_disagreements",
        ):
            item = context[field][0]
            self.assertIn(item["claim"], rendered)
            self.assertIn(item["relevance"], rendered)
            for url in item["citations"]:
                self.assertIn(f"](<{url}>)", rendered)
        self.assertIn("historical record reviewed here is incomplete", rendered)
        self.assertNotIn("internal detail", rendered)
        context["coverage"]["status"] = "complete"
        self.assertNotIn(
            "historical record reviewed here is incomplete",
            final_comment.render_report([], historical_context=context),
        )

    def test_empty_historical_context_has_no_report_section(self):
        for context in (
            None,
            {},
            {"coverage": {"status": "partial"}, "documented_rationale": []},
        ):
            self.assertNotIn(
                "Background",
                final_comment.render_report([], historical_context=context),
            )

    def test_preview_works_from_an_unrelated_directory(self):
        example = json.loads((COMMENT_DIR / "examples" / "mixed.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, str(COMMENT_DIR / "preview.py")],
                cwd=directory,
                capture_output=True,
                text=True,
                check=True,
            )
        self.assertIn(example["findings"][0]["title"], result.stdout)

    def test_review_tier_preserves_empty_review_and_is_optional(self):
        rendered = final_comment.render([], review_tier="sensitive")
        self.assertIn("Review tier: sensitive", rendered)
        self.assertIn("I found no actionable issues", rendered)
        self.assertNotIn("Review tier:", final_comment.render([]))
        for tier in ("sensitive", None):
            with self.subTest(tier=tier):
                rendered = final_comment.render_github(
                    [],
                    head_sha="abc",
                    report_url="https://example.com/report",
                    review_tier=tier,
                )
                self.assertEqual("Review tier:" in rendered, tier is not None)
                if tier:
                    self.assertIn("Review tier: sensitive", rendered)

    def test_layout_edits_use_the_saved_template_snapshot(self):
        comment_format = final_comment.load_format()
        comment_format["finding"] = (
            "${body}\n\n${title}: ${location} (${side}) [${severity}]\n\n${summary}"
        )
        comment_format["comment"] = (
            comment_format["comment"]
            .replace("${limitations}", "")
            .replace("${findings}", "${limitations}\n\n${findings}")
        )
        snapshot = json.loads(json.dumps(comment_format))
        example = json.loads((COMMENT_DIR / "examples" / "mixed.json").read_text())
        with patch.object(final_comment, "load_format", side_effect=AssertionError):
            rendered = final_comment.render_report(**example, comment_format=snapshot)
        finding = example["findings"][0]
        self.assertIn(finding["body"] + "\n\n" + finding["title"] + ": ", rendered)
        self.assertLess(
            rendered.index("Review coverage was incomplete."),
            rendered.index(finding["body"]),
        )

    def test_template_validation_rejects_omitted_notices_and_invalid_placeholders(self):
        comment_format = final_comment.load_format()
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            for name, template in comment_format.items():
                (directory / f"{name}.md").write_text(template)
            for template in (
                comment_format["comment"].replace("${verification_failed}", ""),
                comment_format["comment"] + "\n${unknown}",
                comment_format["comment"] + "\n${unfinished",
            ):
                with self.subTest(template=template):
                    (directory / "comment.md").write_text(template)
                    with self.assertRaisesRegex(ValueError, r"comment\.md"):
                        final_comment.load_format(directory)

    def test_failed_verification_keeps_coverage_notice_without_republishing_claims(
        self,
    ):
        example = json.loads(
            (COMMENT_DIR / "examples" / "verification-failed.json").read_text()
        )
        example["findings"] = json.loads(
            (COMMENT_DIR / "examples" / "mixed.json").read_text()
        )["findings"]
        rendered = final_comment.render_report(**example)
        self.assertIn("Review failed", rendered)
        self.assertIn("Review coverage was incomplete", rendered)
        self.assertNotIn("I found no actionable issues", rendered)
        for finding in example["findings"]:
            self.assertNotIn(finding["title"], rendered)
            self.assertNotIn(finding["body"], rendered)
        for previous in example["review_memory"]:
            self.assertNotIn(previous["title"], rendered)
            self.assertNotIn(previous["body"], rendered)
        for limitation in example["limitations"]:
            self.assertNotIn(limitation, rendered)

    def test_findings_show_summary_before_collapsed_explanation(self):
        example = json.loads((COMMENT_DIR / "examples" / "mixed.json").read_text())
        rendered = final_comment.render_report(**example)
        for limitation in example["limitations"]:
            self.assertNotIn(limitation, rendered)
        finding = example["findings"][0]
        before, details = rendered.split("<details>", 1)
        self.assertIn(finding["title"], before)
        self.assertIn(finding["summary"], before)
        self.assertNotIn(finding["body"], before)
        self.assertIn(finding["body"], details)
        self.assertIn("src/index.cpp:42", before)
        minor = next(
            item for item in example["findings"] if item["severity"] == "minor"
        )
        suggestion = next(
            item for item in example["findings"] if item["severity"] == "suggestion"
        )
        self.assertLess(
            rendered.index(minor["title"]), rendered.index(suggestion["title"])
        )

    def test_report_links_source_revision(self):
        finding = json.loads((COMMENT_DIR / "examples" / "mixed.json").read_text())[
            "findings"
        ][0]
        finding.update(side="base", path="src/file name.cpp", line=17)
        rendered = final_comment.render_report(
            [finding],
            repository_url="https://github.com/example/project",
            base_sha="base-revision",
            head_sha="head-revision",
        )
        self.assertIn("/blob/base-revision/src/file%20name.cpp#L17", rendered)

    def test_github_uses_verified_summary_without_full_explanation(self):
        example = json.loads((COMMENT_DIR / "examples" / "github.json").read_text())
        example["findings"][0].update(
            presented_title="Edited title",
            presented_summary="Edited summary.",
            presented_body="Edited explanation.",
        )
        example.update(
            repository_url="https://github.com/example/project",
            base_sha="base-revision",
        )
        example["findings"][0].update(side="base", path="src/file name.cpp", line=17)
        rendered = final_comment.render_github(**example)
        self.assertIn("Edited summary.", rendered)
        self.assertIn("/blob/base-revision/src/file%20name.cpp#L17", rendered)
        self.assertNotIn("Edited explanation.", rendered)
        self.assertNotIn(example["findings"][0]["summary"], rendered)

    def test_comment_caps_current_findings_without_discarding_report_details(self):
        example = json.loads((COMMENT_DIR / "examples" / "mixed.json").read_text())
        original = example["findings"][0]
        findings = [
            {
                **original,
                "id": f"finding:{i}",
                "title": f"Issue {i}",
                "summary": f"Summary {i}.",
                "body": f"Explanation {i}.",
                "severity": "major" if i == 7 else "minor",
            }
            for i in range(8)
        ]
        example["findings"] = findings
        report = final_comment.render_report(**example)
        rendered = final_comment.render(
            **example, report_url="https://example.org/report"
        )
        github = final_comment.render_github(
            findings, head_sha="head", report_url="https://example.org/report"
        )
        for comment in (rendered, github):
            for i in (7, 0, 1, 2, 3, 4):
                self.assertIn(f"Summary {i}.", comment)
            for i in (5, 6):
                self.assertNotIn(f"Summary {i}.", comment)
            self.assertLess(comment.index("Issue 7"), comment.index("Issue 0"))
            self.assertIn("2 additional verified findings", comment)
            self.assertIn("https://example.org/report", comment)
            for finding in findings:
                self.assertNotIn(finding["body"], comment)
                self.assertIn(finding["body"], report)
        for memory in example["review_memory"]:
            self.assertNotIn(memory["title"], rendered)
            self.assertNotIn(memory["title"], report)
        # Repeated but currently verified findings still compete for the six slots.
        example["review_memory"] = [
            {
                **original,
                "key": "old:1",
                "status": "still_present",
                "unchanged_since_report": True,
                "current_finding_ids": ["finding:7"],
            }
        ]
        self.assertIn("Issue 7", final_comment.render(**example))
        self.assertEqual(len(findings), 8)

    def test_reconciled_history_cannot_publish_without_an_approved_finding(self):
        original = json.loads((COMMENT_DIR / "examples" / "mixed.json").read_text())[
            "findings"
        ][0]
        retained = {
            **original,
            "key": "old:1",
            "title": "Earlier issue",
            "status": "still_present",
            "checked_head": "head",
            "reason": "Current consequence.",
            "source_evidence": [{"path": "current.cpp", "line": 1, "side": "head"}],
        }
        history = [retained, {**retained, "key": "old:2", "duplicate_of": "old:1"}]
        for render in (final_comment.render, final_comment.render_report):
            with self.subTest(render=render.__name__):
                rendered = render([], review_memory=history, head_sha="head")
                self.assertNotIn(retained["title"], rendered)
                self.assertNotIn(retained["reason"], rendered)
                self.assertIn("Earlier concerns remain unresolved", rendered)
                approved = {**original, "id": "current:1"}
                linked = [{**retained, "current_finding_ids": ["current:1"]}]
                rendered = render([approved], review_memory=linked, head_sha="head")
                self.assertIn(approved["title"], rendered)
                self.assertNotIn(retained["title"], rendered)
                self.assertNotIn("Earlier concerns remain unresolved", rendered)
        self.assertEqual(
            final_comment.report_findings([], history, head_sha="head"), []
        )
        self.assertEqual(
            final_comment.report_findings([original], history, head_sha="head"),
            [original],
        )
        self.assertEqual(
            final_comment.report_findings(
                [original], history, verification_failed=True
            ),
            [],
        )

    def test_unverified_history_does_not_look_like_a_clean_review(self):
        example = json.loads(
            (COMMENT_DIR / "examples" / "verification-failed.json").read_text()
        )
        rendered = final_comment.render(**example)
        self.assertIn("Review failed", rendered)
        self.assertIn("Review coverage was incomplete", rendered)
        self.assertNotIn("I found no actionable issues", rendered)
        for previous in example["review_memory"]:
            self.assertNotIn(previous["title"], rendered)
            self.assertNotIn(previous["body"], rendered)
        example["verification_failed"] = False
        example["limitations"] = []
        rendered = final_comment.render(**example)
        self.assertIn("Review coverage was incomplete", rendered)
        self.assertNotIn("I found no actionable issues", rendered)

    def test_unconfirmed_history_is_only_a_notice_in_the_report(self):
        history = {
            "key": "old:1",
            "title": "Historical fee conversion concern",
            "severity": None,
            "summary": None,
            "body": None,
            "path": None,
            "line": None,
            "side": None,
            "status": "fixed",
            "checked_head": "head",
            "reason": "The earlier concern was checked.",
            "source_evidence": [{"path": "current.cpp", "line": 1, "side": "head"}],
        }
        rendered = final_comment.render_report([], review_memory=[history])
        self.assertNotIn(history["title"], rendered)
        self.assertNotIn("unresolved", rendered)
        self.assertNotIn("Suggestion", rendered)
        self.assertNotIn("None", rendered)

        history["status"] = "still_present"
        rendered = final_comment.render_report(
            [], review_memory=[history], head_sha="head"
        )
        self.assertNotIn(history["title"], rendered)
        self.assertIn("Earlier concerns remain unresolved", rendered)
        self.assertNotIn("Suggestion", rendered)
        comment = final_comment.render([], review_memory=[history], head_sha="head")
        self.assertNotIn(history["title"], comment)
        self.assertNotIn("I found no actionable issues", comment)


if __name__ == "__main__":
    unittest.main()
