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
    def test_preview_examples_work_from_an_unrelated_directory(self):
        examples = COMMENT_DIR / "examples"
        with tempfile.TemporaryDirectory() as directory:
            for example in sorted(examples.glob("*.json")):
                with self.subTest(example=example.name):
                    if example.name == "github.json":
                        arguments = ["--github"]
                        renderer = final_comment.render_github
                    else:
                        arguments = (
                            [] if example.name == "mixed.json" else [str(example)]
                        )
                        renderer = final_comment.render
                    result = subprocess.run(
                        [sys.executable, str(COMMENT_DIR / "preview.py"), *arguments],
                        cwd=directory,
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    self.assertEqual(
                        result.stdout,
                        renderer(**json.loads(example.read_text())) + "\n",
                    )

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
            rendered = final_comment.render(**example, comment_format=snapshot)
        finding = example["findings"][0]
        self.assertIn(finding["body"] + "\n\n" + finding["title"] + ": ", rendered)
        self.assertLess(
            rendered.index(example["limitations"][0]), rendered.index(finding["body"])
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
        rendered = final_comment.render(**example)
        for previous in example["review_memory"]:
            self.assertIn(previous["title"], rendered)
            self.assertIn(previous["status"], rendered)
            self.assertNotIn(previous["body"], rendered)
        for limitation in example["limitations"]:
            self.assertIn(limitation, rendered)

    def test_findings_show_summary_before_collapsed_explanation(self):
        example = json.loads((COMMENT_DIR / "examples" / "mixed.json").read_text())
        rendered = final_comment.render(**example)
        finding = example["findings"][0]
        before, details = rendered.split("<details>", 1)
        self.assertIn("[Major] " + finding["title"], before)
        self.assertIn(finding["summary"], before)
        self.assertNotIn(finding["body"], before)
        self.assertIn(finding["body"], details)
        self.assertIn("src/index.cpp:42", details)

    def test_github_uses_collated_summary_with_collated_body(self):
        example = json.loads((COMMENT_DIR / "examples" / "github.json").read_text())
        example["findings"][0].update(
            presented_title="Edited title",
            presented_summary="Edited summary.",
            presented_body="Edited explanation.",
        )
        rendered = final_comment.render_github(**example)
        before, details = rendered.split("<details>", 1)
        self.assertIn("Edited summary.", before)
        self.assertIn("Edited explanation.", details)
        self.assertNotIn(example["findings"][0]["summary"], rendered)


if __name__ == "__main__":
    unittest.main()
