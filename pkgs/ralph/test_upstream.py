"""Backport readers can inspect only resolved original commits and their parents."""

import tempfile
import unittest
from pathlib import Path

from ralph import repository, upstream


class UpstreamTests(unittest.TestCase):
    def test_reads_pinned_original_and_parent_but_rejects_other_revisions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository.git(root, "init", "-q")
            repository.git(root, "config", "user.name", "Test")
            repository.git(root, "config", "user.email", "test@example.invalid")
            (root / "code").write_text("before\n")
            repository.git(root, "add", ".")
            repository.git(root, "commit", "-qm", "base")
            base = repository.git(root, "rev-parse", "HEAD").strip()
            (root / "code").write_text("after\n")
            repository.git(root, "commit", "-qam", "original")
            original = repository.git(root, "rev-parse", "HEAD").strip()
            evidence = {"commits": [{"original": original}], "sources": {}}
            with upstream.pin(root, evidence):
                self.assertIn(
                    original,
                    repository.git(root, "for-each-ref", "refs/ralph/upstream"),
                )
            self.assertEqual(
                repository.git(root, "for-each-ref", "refs/ralph/upstream"), ""
            )
            args = {
                "commit": original,
                "before": False,
                "path": "code",
                "start_line": 1,
            }
            self.assertIn(
                "after", upstream.lookup(root, evidence, "read_upstream_file", args)
            )
            self.assertIn(
                "before",
                upstream.lookup(
                    root, evidence, "read_upstream_file", {**args, "before": True}
                ),
            )
            self.assertIn(
                "after",
                upstream.lookup(
                    root,
                    evidence,
                    "search_upstream_code",
                    {"commit": original, "before": False, "query": "after"},
                ),
            )
            omitted = {
                "commits": [],
                "sources": {"bitcoin/bitcoin#1": {"commits": [original]}},
            }
            patch = upstream.lookup(
                root, omitted, "read_upstream_commit", {"commit": original}
            )
            self.assertIn("-before", patch)
            self.assertIn("+after", patch)
            for invalid in (base, "HEAD", original[:7], original + "^", "--all"):
                with self.subTest(commit=invalid):
                    self.assertIn(
                        "Invalid",
                        upstream.lookup(
                            root,
                            evidence,
                            "read_upstream_file",
                            {**args, "commit": invalid},
                        ),
                    )
            self.assertIn(
                "Invalid", upstream.lookup(root, None, "read_upstream_file", args)
            )
            self.assertIn(
                "Invalid",
                upstream.lookup(
                    root, evidence, "read_upstream_file", {**args, "path": "../code"}
                ),
            )
