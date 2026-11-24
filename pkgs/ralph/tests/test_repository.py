"""Committed local comparisons never depend on a checkout or a remote."""

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import final_comment
from ralph import repository, protocol, feedback


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

    def test_diff_anchors_follow_paging_and_literal_paths(self):
        self.git("checkout", "-q", "topic")
        path = "literal[1].txt"
        (self.repo / path).write_text("old\n")
        (self.repo / "literal1.txt").write_text("unrelated\n")
        self.git("add", ".")
        self.git("commit", "-qm", "files")
        base = self.git("rev-parse", "HEAD").strip()
        lines = ["x" * 13000, "--- source-looking header"] + [
            f"new {i}" for i in range(180)
        ]
        (self.repo / path).write_text("\n".join(lines) + "\n")
        (self.repo / "literal1.txt").write_text(
            "must not be attributed to literal[1].txt\n"
        )
        self.git("commit", "-qam", "change")
        snapshot = repository.snapshot_repository(
            self.repo, base, self.git("rev-parse", "HEAD").strip()
        )
        start = 1
        observed = set()
        while True:
            page = repository.read_diff(
                self.repo,
                snapshot.changed_paths,
                snapshot.diff_base_sha,
                snapshot.head_sha,
                path,
                start,
                anchors=snapshot.anchors,
            )
            self.assertLessEqual(len(page.encode()), repository.MAX_TOOL_BYTES)
            self.assertNotIn("must not be attributed", page)
            observed.update(re.findall(r"Anchor: ([0-9a-f]+)", page))
            following = re.search(r"continue at (\d+)", page)
            if following is None:
                break
            start = int(following[1])
        self.assertEqual(observed, set(snapshot.anchors))
        self.assertEqual(
            {a["line"] for a in snapshot.anchors.values() if a["side"] == "head"},
            set(range(2, len(lines) + 1)),
        )
        self.assertEqual(
            [a for a in snapshot.anchors.values() if a["side"] == "base"],
            [{"path": path, "side": "base", "line": 1}],
        )
        other = repository.snapshot_repository(self.repo, base, snapshot.head_sha)
        with self.assertRaises(protocol.InvalidReview):
            protocol._location({"anchor_id": next(iter(observed))}, other)

    def test_rename_anchors_preserve_old_and_new_paths(self):
        self.git("checkout", "-q", "topic")
        self.git("mv", "code.txt", "renamed.txt")
        self.git("commit", "-qm", "rename")
        snapshot = repository.snapshot_repository(
            self.repo, self.head, self.git("rev-parse", "HEAD").strip()
        )
        for path in sorted(snapshot.changed_paths):
            repository.read_diff(
                self.repo,
                snapshot.changed_paths,
                snapshot.diff_base_sha,
                snapshot.head_sha,
                path,
                1,
                anchors=snapshot.anchors,
            )
        self.assertEqual(
            list(snapshot.anchors.values()),
            [
                {"path": "code.txt", "side": "base", "line": 1},
                {"path": "renamed.txt", "side": "head", "line": 1},
            ],
        )

    def test_intermediate_commit_location_survives_verification_and_rendering(self):
        self.git("checkout", "-q", "topic")
        (self.repo / "code.txt").write_text("original\n")
        (self.repo / "other.txt").write_text("unrelated final change\n")
        self.git("add", ".")
        self.git("commit", "-qam", "restore original")
        restored = self.git("rev-parse", "HEAD").strip()
        snapshot = repository.snapshot_repository(self.repo, self.ancestor, restored)
        self.assertNotIn("code.txt", snapshot.changed_paths)
        location = {
            "path": "code.txt",
            "line": 1,
            "side": "commit",
            "commit": self.head,
            "parent": None,
        }
        repository.read_pr_commit(
            self.repo,
            self.ancestor,
            restored,
            self.head,
            "code.txt",
            1,
            anchors=snapshot.anchors,
        )
        anchor = next(
            key for key, value in snapshot.anchors.items() if value == location
        )
        candidate = {
            "kind": "suggestion",
            "anchor_id": anchor,
            **dict.fromkeys(
                (
                    "title",
                    "claim",
                    "consequence",
                    "evidence",
                    "correction",
                    "uncertainty",
                ),
                "Intermediate change",
            ),
        }
        coverage = {"status": "complete", "limitations": []}
        discovery = {
            "coverage": coverage,
            "requires_sensitive_review": False,
            "findings": [candidate],
        }
        parsed = protocol.discovery(json.dumps(discovery), "commit_flow", snapshot)
        candidates = parsed["findings"]
        self.assertIn(
            "1: head",
            repository.read_pr_commit_file(
                self.repo, self.ancestor, restored, self.head, None, "code.txt", 1
            ),
        )
        self.assertIn(
            "1: original",
            repository.read_pr_commit_file(
                self.repo,
                self.ancestor,
                restored,
                self.head,
                self.ancestor,
                "code.txt",
                1,
            ),
        )
        # Normal implementation reviews still cannot cite this net-unchanged file,
        # or escape that rule by supplying a commit-specific location.
        for invalid in ({}, {"side": "head"}):
            result = protocol.discovery(
                json.dumps({**discovery, "findings": [{**candidate, **invalid}]}),
                "independent",
                snapshot,
            )
            self.assertEqual(result["findings"], [])
            self.assertEqual(len(result["rejected_findings"]), 1)
        for invalid in (
            {"commit": self.ancestor},
            {"commit": self.base},
            {"parent": restored},
            {"line": 2},
            {"path": "missing.cpp"},
        ):
            with self.subTest(invalid=invalid):
                result = protocol.discovery(
                    json.dumps({**discovery, "findings": [{**candidate, **invalid}]}),
                    "commit_flow",
                    snapshot,
                )
                self.assertEqual(result["findings"], [])
                self.assertEqual(len(result["rejected_findings"]), 1)
        finding = {
            "kind": "suggestion",
            "severity": "suggestion",
            "anchor_id": anchor,
            "security": {
                "classification": "non_security",
                "reason": "Commit structure",
            },
            "title": "Intermediate change",
            "summary": "Review intermediate behavior",
            "body": "Keep related changes together",
            "source_evidence": [self.citation(snapshot, location)],
        }
        verification = {
            "coverage": coverage,
            "decisions": [
                {
                    "candidate_ids": ["commit_flow:1"],
                    "disposition": "supported",
                    "reason": "Checked commit",
                    "finding": finding,
                }
            ],
        }
        _, accepted = protocol.verification(
            json.dumps(verification), candidates, snapshot
        )
        self.assertEqual(len(accepted), 1)
        # Even a head anchor cannot turn commit-flow evidence into a runtime defect.
        unsupported = {
            **finding,
            "kind": "defect",
            "severity": "major",
            "side": "head",
            "path": "other.txt",
        }

        verification["decisions"][0]["finding"] = unsupported
        _, withheld = protocol.verification(
            json.dumps(verification), candidates, snapshot
        )
        self.assertEqual(withheld, [])
        judgment = {
            "coverage": coverage,
            "concept": None,
            "decisions": [
                {
                    "finding_id": "finding:1",
                    "disposition": "publish",
                    "reason": "Useful sequencing change",
                    "source_evidence": finding["source_evidence"],
                    **{key: finding[key] for key in ("title", "summary", "body")},
                }
            ],
        }
        judged = protocol.judgment(json.dumps(judgment), accepted, None, snapshot)
        # The published result retains its commit identity and renders immutable links.
        published = judged[1]
        self.assertEqual(published[0]["commit"], self.head)
        report = final_comment.render_report(
            published,
            repository_url="https://github.com/o/r",
            head_sha=restored,
            base_sha=self.ancestor,
        )
        self.assertIn(f"/blob/{self.head}/code.txt#L1", report)
        parent_link = final_comment.source_location(
            {**location, "parent": self.ancestor},
            "https://github.com/o/r",
            self.ancestor,
            restored,
        )
        self.assertIn(f"/blob/{self.ancestor}/code.txt#L1", parent_link)
        self.assertIn(f"parent of {self.head[:12]}", parent_link)

    def test_merged_code_finding_drops_commit_citations(self):
        snapshot = repository.snapshot_repository(self.repo, self.ancestor, self.head)
        repository.read_diff(
            self.repo,
            snapshot.changed_paths,
            snapshot.diff_base_sha,
            snapshot.head_sha,
            "code.txt",
            1,
            anchors=snapshot.anchors,
        )
        anchor = next(
            key for key, value in snapshot.anchors.items() if value["side"] == "head"
        )
        location = {"path": "code.txt", "line": 1}
        head = self.citation(snapshot, {**location, "side": "head"})
        commit = self.citation(
            snapshot,
            {
                **location,
                "side": "commit",
                "commit": self.head,
                "parent": self.ancestor,
            },
        )
        candidates = [
            {"id": "script:1", "side": "head"},
            {"id": "commit_flow:1", "side": "commit"},
        ]
        wording = dict.fromkeys(("title", "summary", "body"), "Redundant copy")
        coverage = {"status": "complete", "limitations": []}

        def verify(*evidence):
            finding = {
                "kind": "design",
                "severity": "suggestion",
                "anchor_id": anchor,
                "security": {"classification": "non_security", "reason": "Cost"},
                **wording,
                "source_evidence": list(evidence),
            }
            decision = {
                "candidate_ids": ["script:1", "commit_flow:1"],
                "disposition": "supported",
                "reason": "Same root cause",
                "finding": finding,
            }
            return protocol.verification(
                json.dumps({"coverage": coverage, "decisions": [decision]}),
                candidates,
                snapshot,
            )[1]

        def judge(*evidence):
            decision = {
                "finding_id": "finding:1",
                "disposition": "publish",
                "reason": "Useful",
                **wording,
                "source_evidence": list(evidence),
            }
            return protocol.judgment(
                json.dumps(
                    {"coverage": coverage, "concept": None, "decisions": [decision]}
                ),
                accepted,
                None,
                snapshot,
            )[1]

        accepted = verify(commit, head)
        self.assertEqual(
            [item["side"] for item in accepted[0]["source_evidence"]], ["head"]
        )
        self.assertEqual(len(judge(head, commit)), 1)
        self.assertEqual(verify(commit), [])
        self.assertEqual(judge(commit), [])

    def citation(self, snapshot, location):
        output = repository.cite_file(
            snapshot,
            location["side"],
            location["path"],
            location["line"],
            location["line"],
            commit=location.get("commit"),
            parent=location.get("parent"),
        )
        return {"citation_id": output.splitlines()[0].removeprefix("Citation: ")}

    def test_commit_finding_is_reverified_on_later_run(self):
        location = {
            "path": "code.txt",
            "line": 1,
            "side": "commit",
            "commit": self.head,
            "parent": self.ancestor,
        }
        finding = {
            **location,
            "key": "old:1",
            "title": "Commit sequence",
            "severity": "suggestion",
            "source_evidence": [{**location, "excerpt": "original"}],
        }
        assessment = {
            "prior_key": "old:1",
            "status": "still_present",
            "duplicate_of": None,
            "candidate_ids": [],
            "unchanged": True,
            "unchanged_since_report": True,
            "reason": "The intermediate sequencing problem remains.",
            "source_evidence": [{**location, "excerpt": "original"}],
        }
        snapshot = repository.snapshot_repository(self.repo, self.ancestor, self.head)
        assessment["source_evidence"] = [self.citation(snapshot, location)]
        head_finding = {**finding, "side": "head"}
        head_finding.pop("commit")
        head_finding.pop("parent")
        invalid_packet = {
            "findings": [{**head_finding, "severity": "major"}],
            "comments": [],
        }
        rejected = feedback.reconcile(invalid_packet, [assessment], [], snapshot)
        self.assertEqual(rejected[0]["status"], "unclear")
        for prior in (finding, head_finding):
            with self.subTest(side=prior["side"]):
                packet = {"findings": [prior], "comments": []}
                memory = feedback.reconcile(packet, [assessment], [], snapshot)
                self.assertEqual(memory[0]["status"], "still_present")
                candidates = feedback.publication_candidates(memory, snapshot)
                self.assertEqual(len(candidates), 1)
                self.assertEqual(
                    {field: candidates[0][field] for field in location}, location
                )
                self.assertEqual(
                    final_comment.report_findings([], memory, head_sha=self.head), []
                )
                # A rewritten PR no longer contains the cited commit; history must not
                # resurrect it solely because its source blob remains in the repository.
                rewritten = repository.snapshot_repository(
                    self.repo, self.ancestor, self.base
                )
                stale = feedback.reconcile(packet, [assessment], [], rewritten)
                self.assertEqual(stale[0]["status"], "unclear")
                self.assertEqual(
                    final_comment.report_findings([], stale, head_sha=self.base), []
                )

    def test_commit_source_selects_merge_parent_explicitly(self):
        self.git("checkout", "-q", "topic")
        self.git("merge", "--no-ff", "-qm", "merge target", "target")
        merge = self.git("rev-parse", "HEAD").strip()
        for parent, expected in (
            (None, "head"),
            (self.head, "head"),
            (self.base, "original"),
        ):
            self.assertIn(
                f"1: {expected}",
                repository.read_pr_commit_file(
                    self.repo, self.ancestor, merge, merge, parent, "code.txt", 1
                ),
            )
        with self.assertRaises(repository.ToolFailure):
            repository.read_pr_commit_file(
                self.repo, self.ancestor, merge, merge, self.ancestor, "code.txt", 1
            )

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

    def test_large_review_excerpts_do_not_hide_later_functional_tests(self):
        self.git("checkout", "-q", "topic")
        changes = {
            "src/test/aaa_vectors.cpp": "vector();\n" * 15_000,
            "src/test/data/vectors.json": '"vector",\n' * 40_000,
            "src/validation.cpp": "enforce_new_rule();\n",
            "test/functional/feature_activation.py": "assert_activation_boundary()\n",
        }
        for path, content in changes.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        self.git("add", ".")
        self.git("commit", "-qm", "test activation")
        with repository.local_snapshot(self.repo, "topic", base="target") as snapshot:
            review, skip = repository.build_review_input(snapshot)
            self.assertIsNone(skip)
            focused = repository.focused_review_input(review, snapshot, "tests")
            excerpt = focused[len(review) :]
            self.assertTrue("assert_activation_boundary()" in excerpt)
            self.assertTrue("enforce_new_rule();" in excerpt)
            self.assertLessEqual(
                len(excerpt.encode()), repository.MAX_FOCUSED_DIFF_BYTES + 100
            )
            self.assertIn("read_diff", excerpt)
            self.assertIn("src/test/aaa_vectors.cpp", review)
            self.assertIn("test/functional/feature_activation.py", review)

    def test_diff_paging_does_not_skip_a_line_when_page_is_full(self):
        self.git("checkout", "-q", "topic")
        (self.repo / "code.txt").write_text(
            "".join(f"value_{i}: {'x' * 200}\n" for i in range(120))
        )
        self.git("commit", "-qam", "long patch")
        with repository.local_snapshot(self.repo, "topic", base="target") as snapshot:
            start = 1
            numbers = []
            while True:
                page = repository.read_diff(
                    self.repo,
                    snapshot.changed_paths,
                    snapshot.diff_base_sha,
                    snapshot.head_sha,
                    "code.txt",
                    start,
                )
                self.assertLessEqual(len(page.encode()), repository.MAX_TOOL_BYTES)
                numbers.extend(
                    int(n) for n in re.findall(r"^(\d+):", page, re.MULTILINE)
                )
                next_line = re.search(r"continue at (\d+)", page)
                if not next_line:
                    break
                self.assertGreater(int(next_line[1]), start)
                start = int(next_line[1])
            self.assertEqual(numbers, list(range(1, max(numbers) + 1)))
            self.assertGreater(len(numbers), 120)

    def test_pr_commit_reader_is_pinned_to_the_review_range(self):
        self.git("checkout", "-q", "topic")
        (self.repo / "new.cpp").write_text("new_behavior();\n")
        self.git("add", ".")
        self.git("commit", "-qm", "new behavior")
        commit = self.git("rev-parse", "HEAD").strip()
        with repository.local_snapshot(self.repo, "topic", base="target") as snapshot:

            def read(revision, path):
                return repository.read_pr_commit(
                    self.repo,
                    snapshot.diff_base_sha,
                    snapshot.head_sha,
                    revision,
                    path,
                    1,
                )

            self.assertIn("+new_behavior();", read(commit, "new.cpp"))
            self.assertIn("+head", read(self.head, "code.txt"))
            from ralph.evidence import ToolFailure

            for revision, path in (
                (self.base, "target.txt"),
                (self.ancestor, "code.txt"),
                ("HEAD", "new.cpp"),
                (commit, "code.txt"),
            ):
                with self.assertRaises(ToolFailure) as failure:
                    read(revision, path)
                self.assertEqual(failure.exception.outcome, "invalid_request")
            self.git("checkout", "-q", "target")
            self.git("branch", "-f", "topic", self.ancestor)
            self.assertIn("+new_behavior();", read(commit, "new.cpp"))
        self.git("checkout", "-q", "-B", "topic", commit)
        self.git("merge", "-q", "--no-ff", "target", "-m", "merge target")
        merge = self.git("rev-parse", "HEAD").strip()
        self.assertIn(
            "+target only",
            repository.read_pr_commit(
                self.repo, self.ancestor, merge, merge, "target.txt", 1
            ),
        )


if __name__ == "__main__":
    unittest.main()
