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

    def test_failed_verification_retains_references_without_republishing_bodies(self):
        example = json.loads(
            (COMMENT_DIR / "examples" / "verification-failed.json").read_text()
        )
        rendered = final_comment.render_report(**example)
        for previous in example["review_memory"]:
            self.assertIn(previous["title"], rendered)
            self.assertIn(previous["status"], rendered)
            self.assertNotIn(previous["body"], rendered)
        for limitation in example["limitations"]:
            self.assertNotIn(limitation, rendered)

    def test_findings_show_summary_before_collapsed_explanation(self):
        example = json.loads((COMMENT_DIR / "examples" / "mixed.json").read_text())
        rendered = final_comment.render_report(**example)
        for limitation in example["limitations"]:
            self.assertNotIn(limitation, rendered)
        for alternative in example["alternatives"]:
            self.assertIn(alternative["concept"], rendered)
            self.assertIn(
                "<summary>Alternative approach: "
                + final_comment.summary_label(alternative["name"])
                + "</summary>",
                rendered,
            )
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
            self.assertIn(memory["title"], report)
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

    def test_confirmed_history_competes_with_new_findings_for_the_same_slots(self):
        original = json.loads((COMMENT_DIR / "examples" / "mixed.json").read_text())[
            "findings"
        ][0]
        history = [
            {
                **original,
                "key": f"old:{i}",
                "title": f"Earlier issue {i}",
                "severity": "minor",
                "status": "still_present",
                "checked_head": "head",
                "reason": f"Current consequence {i}.",
                "line": 999,
                "current_candidate_ids": [],
                "current_finding_ids": [],
                "source_evidence": [
                    {"path": "current.cpp", "line": i + 1, "side": "head"}
                ],
            }
            for i in range(5)
        ]
        rendered = final_comment.render([], review_memory=history, head_sha="head")
        for entry in history:
            self.assertIn(entry["title"], rendered)
            self.assertIn(entry["reason"], rendered)
        self.assertNotIn("999", rendered)
        self.assertNotIn("Earlier concerns remain unresolved", rendered)
        self.assertIn("current.cpp:1", rendered)

        duplicate = {**history[0], "key": "old:duplicate", "duplicate_of": "old:0"}
        current = [
            {
                **original,
                "id": f"new:{i}",
                "title": f"New issue {i}",
                "severity": "suggestion",
            }
            for i in range(3)
        ]
        rendered = final_comment.render(
            current, review_memory=[*history, duplicate], head_sha="head"
        )
        self.assertEqual(rendered.count("Earlier issue 0"), 1)
        self.assertIn("New issue 0", rendered)
        self.assertNotIn("New issue 1", rendered)
        self.assertIn("2 additional verified findings", rendered)
        self.assertLess(
            rendered.index("Earlier issue 4"), rendered.index("New issue 0")
        )

        # A rejected current candidate cannot be revived via an unlinked duplicate.
        duplicate["current_candidate_ids"] = ["independent:1"]
        rendered = final_comment.render(
            [], review_memory=[history[0], duplicate], head_sha="head"
        )
        self.assertNotIn("Earlier issue 0", rendered)
        for changes in (
            {"status": "unclear"},
            {"status": "fixed"},
            {"status": "invalid"},
            {"checked_head": "old"},
        ):
            with self.subTest(changes=changes):
                rendered = final_comment.render(
                    [], review_memory=[{**history[0], **changes}], head_sha="head"
                )
                self.assertNotIn("Earlier issue 0", rendered)
        rendered = final_comment.render(
            [], review_memory=history, head_sha="head", verification_failed=True
        )
        self.assertNotIn("Earlier issue 0", rendered)

    def test_unverified_history_does_not_look_like_a_clean_review(self):
        example = json.loads(
            (COMMENT_DIR / "examples" / "verification-failed.json").read_text()
        )
        rendered = final_comment.render(**example)
        self.assertIn("Review failed", rendered)
        self.assertNotIn("I found no actionable issues", rendered)
        for previous in example["review_memory"]:
            self.assertNotIn(previous["title"], rendered)
            self.assertNotIn(previous["body"], rendered)
        example["verification_failed"] = False
        example["limitations"] = []
        rendered = final_comment.render(**example)
        self.assertIn("Review coverage was incomplete", rendered)
        self.assertNotIn("I found no actionable issues", rendered)


if __name__ == "__main__":
    unittest.main()
