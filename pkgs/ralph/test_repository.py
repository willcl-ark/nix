"""Committed local comparisons never depend on a checkout or a remote."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
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
        return self.git("for-each-ref", "--format=%(refname)", "refs/ralph/local").splitlines()

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
                self.assertIn("head", repository.read_file(
                    self.repo, first.head_files, "code.txt", 1))
            self.assertEqual(self.pins(), original_pins)
        self.assertEqual(self.pins(), [])
        for failure in (RuntimeError, KeyboardInterrupt):
            with self.assertRaises(failure):
                with repository.local_snapshot(self.repo, "target..topic"):
                    raise failure()
            self.assertEqual(self.pins(), [])

    def test_dirty_bare_and_linked_worktrees(self):
        (self.repo / "code.txt").write_text("uncommitted\n")
        (self.repo / "untracked").write_text("ignored\n")
        index = self.git("write-tree").strip()
        with repository.local_snapshot(self.repo, "target..topic") as snapshot:
            self.assertIn("head", repository.read_file(self.repo, snapshot.head_files, "code.txt", 1))
            self.assertNotIn("untracked", snapshot.head_files)
        self.assertEqual(self.git("write-tree").strip(), index)
        bare = self.root / "bare.git"
        self.git("clone", "--bare", str(self.repo), str(bare))
        linked = self.root / "linked"
        self.git("worktree", "add", "--detach", str(linked), self.head)
        for checkout in (bare, linked):
            with repository.local_snapshot(checkout, f"{self.base}..{self.head}") as snapshot:
                self.assertEqual(snapshot.diff_base_sha, self.base)

    def test_missing_blob_never_attempts_network_and_removes_pins(self):
        blob = self.git("rev-parse", f"{self.head}:code.txt").strip()
        (self.repo / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
        self.git("config", "remote.origin.url", "forbidden://must-not-contact")
        self.git("config", "remote.origin.promisor", "true")
        trace = self.root / "trace"
        with patch.dict(os.environ, {"GIT_TRACE": str(trace)}):
            with self.assertRaisesRegex(ValueError, "missing"):
                with repository.local_snapshot(self.repo, "target..topic"):
                    self.fail("missing blob reached review")
        self.assertNotIn("fetch", trace.read_text())
        self.assertNotIn("remote-forbidden", trace.read_text())
        self.assertEqual(self.pins(), [])

    def test_shallow_history_rejected(self):
        shallow = self.root / "shallow"
        self.git("clone", "--depth=1", f"file://{self.repo}", str(shallow))
        with self.assertRaisesRegex(ValueError, "shallow"):
            with repository.local_snapshot(shallow, "HEAD..HEAD"):
                pass

    def test_invalid_targets_fail_before_evidence(self):
        for target, base in (("topic", None), ("..topic", None), ("target..", None),
                             ("target...topic", None), ("target..topic", "target"),
                             ("--all..topic", None), ("code.txt..topic", None)):
            with self.subTest(target=target, base=base), self.assertRaises(ValueError):
                with repository.local_snapshot(self.repo, target, base=base):
                    self.fail("invalid target accepted")
        self.assertEqual(self.pins(), [])

    def test_disconnected_exact_range_records_unavailable_merge_base(self):
        self.git("checkout", "--orphan", "unrelated")
        self.git("rm", "-rf", ".")
        (self.repo / "other").write_text("other\n")
        self.git("add", ".")
        self.git("commit", "-qm", "unrelated root")
        with repository.local_snapshot(self.repo, "unrelated..topic") as snapshot:
            self.assertIsNone(snapshot.merge_base)
            self.assertEqual(snapshot.diff_base_sha, self.git("rev-parse", "HEAD").strip())
        with self.assertRaisesRegex(ValueError, "unique merge base"):
            with repository.local_snapshot(self.repo, "topic", base="unrelated"):
                pass

    def test_missing_history_fails_before_evidence(self):
        (self.repo / ".git" / "objects" / self.ancestor[:2] / self.ancestor[2:]).unlink()
        with self.assertRaisesRegex(ValueError, "history.*missing"):
            with repository.local_snapshot(self.repo, "target..topic"):
                self.fail("incomplete history reached review")
        self.assertEqual(self.pins(), [])

    def test_large_patch_and_long_diff_lines_remain_bounded(self):
        self.git("checkout", "-q", "topic")
        (self.repo / "large.txt").write_text("x" * (repository.MAX_REVIEW_BYTES * 2) + "\n")
        self.git("add", ".")
        self.git("commit", "-qm", "large patch")
        with repository.local_snapshot(self.repo, "target..topic") as snapshot:
            review, skip = repository.build_review_input(snapshot)
            self.assertIsNone(skip)
            self.assertLessEqual(len(review.encode()), repository.MAX_REVIEW_BYTES)
            self.assertIn("Patch exceeds", review)
            excerpt = repository.read_diff(self.repo, snapshot.changed_paths,
                snapshot.diff_base_sha, snapshot.head_sha, "large.txt", 1)
            self.assertLess(len(excerpt.encode()), repository.MAX_TOOL_BYTES + 200)
            self.assertIn("exceeds output limit", excerpt)
            self.assertLessEqual(len(repository.diff_between_heads(
                self.repo, self.base, snapshot.head_sha).encode()),
                repository.MAX_FOLLOWUP_DIFF_BYTES + 100)

    def test_untracked_attributes_cannot_change_pinned_patch(self):
        with repository.local_snapshot(self.repo, "target..topic") as snapshot:
            expected, _ = repository.build_review_input(snapshot)
            expected_diff = repository.read_diff(self.repo, snapshot.changed_paths,
                snapshot.diff_base_sha, snapshot.head_sha, "code.txt", 1)
            (self.repo / ".gitattributes").write_text("* -diff\n")
            actual, _ = repository.build_review_input(snapshot)
            self.assertEqual(actual, expected)
            self.assertEqual(repository.read_diff(self.repo, snapshot.changed_paths,
                snapshot.diff_base_sha, snapshot.head_sha, "code.txt", 1), expected_diff)

    def test_local_attributes_are_ignored_in_worktree_bare_and_linked_repositories(self):
        self.git("checkout", "-q", "topic")
        (self.repo / ".gitattributes").write_text("*.txt diff\n")
        self.git("add", ".gitattributes")
        self.git("commit", "-qm", "committed text attributes")
        head = self.git("rev-parse", "HEAD").strip()
        bare = self.root / "bare repo.git"
        linked = self.root / "linked tree"
        self.git("clone", "--bare", str(self.repo), str(bare))
        self.git("worktree", "add", "--detach", str(linked), head)
        with repository.local_snapshot(self.repo, f"{self.base}..{head}") as snapshot:
            expected, _ = repository.build_review_input(snapshot)
            expected_diff = repository.read_diff(self.repo, snapshot.changed_paths,
                snapshot.diff_base_sha, snapshot.head_sha, "code.txt", 1)
            expected_focused = repository.focused_review_input(
                f"Patch exceeds {repository.MAX_REVIEW_BYTES} input bytes. Use read_diff", snapshot, "tests")
        # Highest-priority repository attributes and default global attributes must
        # not override the committed tree. Also stage conflicting attributes.
        (self.repo / ".git" / "info" / "attributes").write_text("* -diff\n")
        (bare / "info" / "attributes").write_text("* -diff\n")
        (self.repo / ".gitattributes").write_text("* -diff\n")
        self.git("add", ".gitattributes")
        (linked / ".gitattributes").write_text("* -diff\n")
        xdg = self.root / "global config"
        (xdg / "git").mkdir(parents=True)
        (xdg / "git" / "attributes").write_text("* -diff\n")
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}):
            for checkout in (self.repo, bare, linked):
                with self.subTest(checkout=checkout), repository.local_snapshot(
                        checkout, f"{self.base}..{head}") as snapshot:
                    self.assertEqual(repository.build_review_input(snapshot)[0], expected)
                    self.assertEqual(repository.read_diff(checkout, snapshot.changed_paths,
                        snapshot.diff_base_sha, snapshot.head_sha, "code.txt", 1), expected_diff)
                    self.assertEqual(repository.focused_review_input(
                        f"Patch exceeds {repository.MAX_REVIEW_BYTES} input bytes. Use read_diff",
                        snapshot, "tests"), expected_focused)
                    self.assertIn("+head", repository.diff_between_heads(checkout, self.base, head))
                    self.assertIn("+head", repository.diff_path_between_heads(
                        checkout, self.base, head, "code.txt"))

    def test_committed_binary_attributes_are_respected(self):
        self.git("checkout", "-q", "topic")
        (self.repo / ".gitattributes").write_text("code.txt -diff\n")
        self.git("add", ".gitattributes")
        self.git("commit", "-qm", "committed binary attribute")
        (self.repo / ".gitattributes").write_text("code.txt diff\n")
        with repository.local_snapshot(self.repo, "target..topic") as snapshot:
            self.assertIn("GIT binary patch", repository.build_review_input(snapshot)[0])
            self.assertIn("Binary files", repository.read_diff(
                self.repo, snapshot.changed_paths, snapshot.diff_base_sha,
                snapshot.head_sha, "code.txt", 1))

    def test_ambiguous_ref_is_rejected(self):
        self.git("tag", "topic", self.base)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            with repository.local_snapshot(self.repo, "target..topic"):
                pass
        self.assertEqual(self.pins(), [])

    def test_multiple_merge_bases_only_allowed_for_exact_endpoints(self):
        tree = self.git("rev-parse", f"{self.head}^{{tree}}").strip()
        left = self.git("commit-tree", tree, "-p", self.base, "-p", self.head,
                        "-m", "left").strip()
        right = self.git("commit-tree", tree, "-p", self.head, "-p", self.base,
                         "-m", "right").strip()
        with repository.local_snapshot(self.repo, f"{left}..{right}") as snapshot:
            self.assertIsNone(snapshot.merge_base)
            self.assertEqual(snapshot.diff_base_sha, left)
        with self.assertRaisesRegex(ValueError, "unique merge base"):
            with repository.local_snapshot(self.repo, right, base=left):
                pass

    def test_regular_file_maps_exclude_symlinks_and_submodules(self):
        self.git("checkout", "-q", "topic")
        (self.repo / "link").symlink_to("code.txt")
        (self.repo / ":(glob)*").write_text("literal file\n")
        self.git("add", ".")
        self.git("update-index", "--add", "--cacheinfo", f"160000,{self.base},submodule")
        self.git("commit", "-qm", "nonregular and literal paths")
        with repository.local_snapshot(self.repo, "target..topic") as snapshot:
            self.assertNotIn("link", snapshot.head_files)
            self.assertNotIn("submodule", snapshot.head_files)
            self.assertIn("literal file", repository.read_file(
                self.repo, snapshot.head_files, ":(glob)*", 1))
            self.assertIn("literal file", repository.read_diff(
                self.repo, snapshot.changed_paths, snapshot.diff_base_sha,
                snapshot.head_sha, ":(glob)*", 1))

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
            repository.read_diff(self.repo, snapshot.changed_paths, snapshot.diff_base_sha,
                                 snapshot.head_sha, "code.txt", 1)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
