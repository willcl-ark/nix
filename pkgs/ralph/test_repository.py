"""Committed local comparisons never depend on a checkout or a remote."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ralph import repository


class LocalRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.org")
        (self.repo / "code.txt").write_text("original\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.ancestor = self.git("rev-parse", "HEAD").strip()
        (self.repo / "code.txt").write_text("head\n")
        self.git("commit", "-qam", "head")
        self.head = self.git("rev-parse", "HEAD").strip()
        self.git("branch", "topic")
        self.git("checkout", "-qb", "target", self.ancestor)
        (self.repo / "target.txt").write_text("target only\n")
        self.git("add", ".")
        self.git("commit", "-qm", "target")
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args):
        return repository.git(self.repo, *args)

    def pins(self):
        return self.git(
            "for-each-ref", "--format=%(refname)", "refs/ralph/local"
        ).splitlines()

    def test_divergent_exact_range_and_branch_have_different_baselines(self):
        with repository.local_snapshot(self.repo, "target..topic") as exact:
            self.assertEqual(exact.base_sha, self.base)
            self.assertEqual(exact.merge_base, self.ancestor)
            self.assertEqual(exact.diff_base_sha, self.base)
            self.assertEqual(exact.changed_paths, {"code.txt", "target.txt"})
            review, skip = repository.build_review_input(exact)
            self.assertIsNone(skip)
            self.assertIn("-target only", review)
            self.assertIn("target.txt", exact.base_files)
        with repository.local_snapshot(self.repo, "topic", base="target") as branch:
            self.assertEqual(branch.base_sha, self.base)
            self.assertEqual(branch.diff_base_sha, self.ancestor)
            self.assertEqual(branch.changed_paths, {"code.txt"})

    def test_unique_pins_survive_ref_movement_and_cleanup_owns_only_its_refs(self):
        with repository.local_snapshot(self.repo, "target..topic") as first:
            original_pins = self.pins()
            self.git("branch", "-f", "topic", self.ancestor)
            with repository.local_snapshot(self.repo, "target..topic"):
                self.assertEqual(len(self.pins()), 4)
                self.git("gc", "--prune=now")
                self.assertIn(
                    "head",
                    repository.read_file(self.repo, first.head_files, "code.txt", 1),
                )
            self.assertEqual(self.pins(), original_pins)
        self.assertEqual(self.pins(), [])
        for failure in (RuntimeError, KeyboardInterrupt):
            with (
                self.assertRaises(failure),
                repository.local_snapshot(self.repo, "target..topic"),
            ):
                raise failure()
            self.assertEqual(self.pins(), [])

    def test_missing_blob_never_attempts_network_and_removes_pins(self):
        blob = self.git("rev-parse", f"{self.head}:code.txt").strip()
        (self.repo / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
        self.git("config", "remote.origin.url", "forbidden://must-not-contact")
        self.git("config", "remote.origin.promisor", "true")
        trace = self.root / "trace"
        with (
            patch.dict(os.environ, {"GIT_TRACE": str(trace)}),
            self.assertRaisesRegex(ValueError, "missing"),
            repository.local_snapshot(self.repo, "target..topic"),
        ):
            self.fail("missing blob reached review")
        self.assertNotIn("fetch", trace.read_text())
        self.assertNotIn("remote-forbidden", trace.read_text())
        self.assertEqual(self.pins(), [])

    def test_untracked_attributes_cannot_change_pinned_patch(self):
        with repository.local_snapshot(self.repo, "target..topic") as snapshot:
            expected, _ = repository.build_review_input(snapshot)
            expected_diff = repository.read_diff(
                self.repo,
                snapshot.changed_paths,
                snapshot.diff_base_sha,
                snapshot.head_sha,
                "code.txt",
                1,
            )
            (self.repo / ".gitattributes").write_text("* -diff\n")
            actual, _ = repository.build_review_input(snapshot)
            self.assertEqual(actual, expected)
            self.assertEqual(
                repository.read_diff(
                    self.repo,
                    snapshot.changed_paths,
                    snapshot.diff_base_sha,
                    snapshot.head_sha,
                    "code.txt",
                    1,
                ),
                expected_diff,
            )

    def test_regular_file_maps_exclude_symlinks_and_submodules(self):
        self.git("checkout", "-q", "topic")
        (self.repo / "link").symlink_to("code.txt")
        (self.repo / ":(glob)*").write_text("literal file\n")
        self.git("add", ".")
        self.git(
            "update-index", "--add", "--cacheinfo", f"160000,{self.base},submodule"
        )
        self.git("commit", "-qm", "nonregular and literal paths")
        with repository.local_snapshot(self.repo, "target..topic") as snapshot:
            self.assertNotIn("link", snapshot.head_files)
            self.assertNotIn("submodule", snapshot.head_files)
            self.assertIn(
                "literal file",
                repository.read_file(self.repo, snapshot.head_files, ":(glob)*", 1),
            )
            self.assertIn(
                "literal file",
                repository.read_diff(
                    self.repo,
                    snapshot.changed_paths,
                    snapshot.diff_base_sha,
                    snapshot.head_sha,
                    ":(glob)*",
                    1,
                ),
            )

    def test_source_hooks_and_text_conversion_are_never_executed(self):
        marker = self.root / "executed"
        hook = self.repo / ".git" / "hooks" / "reference-transaction"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        hook.chmod(0o755)
        self.git("config", "diff.external", f"touch {marker}")
        self.git("config", "diff.custom.textconv", f"touch {marker}")
        (self.repo / ".gitattributes").write_text("*.txt diff=custom\n")
        with repository.local_snapshot(self.repo, "target..topic") as snapshot:
            review, _ = repository.build_review_input(snapshot)
            self.assertIn("+head", review)
            repository.read_diff(
                self.repo,
                snapshot.changed_paths,
                snapshot.diff_base_sha,
                snapshot.head_sha,
                "code.txt",
                1,
            )
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
