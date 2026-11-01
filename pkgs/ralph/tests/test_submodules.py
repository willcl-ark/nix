"""Dependency evidence uses configured remotes and each endpoint's gitlink."""

import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ralph import experiments, repository


class SubmoduleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.upstream = self.make_repository("upstream")
        self.base_pin = self.commit(self.upstream, "api.h", "old interface\n")
        self.head_pin = self.commit(self.upstream, "api.h", "new interface\n")
        self.repo = self.make_repository("gui")
        self.commit(
            self.repo,
            ".gitmodules",
            '[submodule "bitcoin"]\npath=bitcoin\nurl=forbidden://untrusted\n',
        )
        self.base = self.gitlink(self.base_pin)
        self.head = self.gitlink(self.head_pin)
        self.allowed = {"bitcoin": str(self.upstream)}

    def make_repository(self, name):
        repo = self.root / name
        repo.mkdir()
        repository.git(repo, "init", "--template=", "-q")
        repository.git(repo, "config", "user.name", "Test")
        repository.git(repo, "config", "user.email", "test@example.org")
        return repo

    def commit(self, repo, path, content):
        (repo / path).write_text(content)
        repository.git(repo, "add", path)
        repository.git(repo, "commit", "-qm", "change")
        return repository.resolve_commit(repo, "HEAD")

    def gitlink(self, commit):
        repository.git(
            self.repo,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{commit},bitcoin",
        )
        repository.git(self.repo, "commit", "-qm", "dependency pin")
        return repository.resolve_commit(self.repo, "HEAD")

    def snapshot(self):
        repository.prepare_submodules(self.repo, [self.base, self.head], self.allowed)
        return repository.snapshot_repository(
            self.repo, self.base, self.head, submodules=self.allowed
        )

    def test_endpoint_evidence_and_archives_ignore_untrusted_gitmodules(self):
        trace = self.root / "trace"
        with patch.dict(os.environ, {"GIT_TRACE": str(trace)}):
            snapshot = self.snapshot()
        self.assertNotIn("remote-forbidden", trace.read_text())
        self.assertEqual(
            repository.resolve_commit(self.repo / "bitcoin", "HEAD"), self.head_pin
        )
        # Moving the checkout cannot change either frozen endpoint.
        repository.git(self.repo / "bitcoin", "checkout", "--detach", self.base_pin)
        for side, files, expected, pin in (
            ("base", snapshot.base_files, "old interface", self.base_pin),
            ("head", snapshot.head_files, "new interface", self.head_pin),
        ):
            evidence = repository.read_file(self.repo, files, "bitcoin/api.h", 1)
            self.assertIn(expected, evidence)
            self.assertIn(pin, evidence)
            self.assertIn(pin, repository.read_file(self.repo, files, "bitcoin", 1))
            results = repository.search_code(
                self.repo,
                self.base if side == "base" else self.head,
                "interface",
                dependencies=snapshot.dependencies,
                side=side,
            )
            self.assertIn(f"{pin}:bitcoin/api.h:1:{expected}", results)
            session = experiments.ExperimentSession(snapshot, [])
            with tempfile.TemporaryDirectory() as directory:
                session.directory = type("Directory", (), {"name": directory})()
                with tarfile.open(session._archive(side)) as archive:
                    self.assertEqual(
                        archive.extractfile("bitcoin/api.h").read().decode(),
                        expected + "\n",
                    )
        blame = repository.blame_base(
            self.repo, snapshot.base_files, self.base, "bitcoin/api.h", 1
        )
        self.assertIn(self.base_pin, blame)
        self.assertIn(
            "old interface",
            repository.read_commit(
                self.repo,
                snapshot.base_files,
                self.base,
                self.base_pin,
                "bitcoin/api.h",
            ),
        )
        self.assertIn(
            "old interface",
            repository.file_excerpt_at(
                self.repo, self.base, "bitcoin/api.h", 1, submodules=self.allowed
            ),
        )
        # Persistent acquisition refs retain source after checkout/ref movement.
        repository.git(self.repo / "bitcoin", "gc", "--prune=now")
        self.assertIn(
            "new interface",
            repository.read_file(self.repo, snapshot.head_files, "bitcoin/api.h", 1),
        )

    def test_disabled_by_default_and_missing_objects_fail_offline(self):
        snapshot = repository.snapshot_repository(self.repo, self.base, self.head)
        self.assertNotIn("bitcoin", snapshot.head_files)
        self.assertFalse((self.repo / "bitcoin").exists())
        self.snapshot()
        child = self.repo / "bitcoin"
        blob = repository.git(child, "rev-parse", f"{self.head_pin}:api.h").strip()
        # Git unpacks this small fetched fixture into loose objects.
        loose = child / ".git" / "objects" / blob[:2] / blob[2:]
        if not loose.exists():
            self.fail("fixture did not create loose source objects")
        loose.unlink()
        trace = self.root / "offline-trace"
        with (
            patch.dict(os.environ, {"GIT_TRACE": str(trace)}),
            self.assertRaises(ValueError),
        ):
            repository.snapshot_repository(
                self.repo, self.base, self.head, submodules=self.allowed
            )
        self.assertNotIn("fetch", trace.read_text())

    def test_symlink_checkout_and_remote_change_are_rejected(self):
        (self.repo / "bitcoin").symlink_to(self.upstream, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "escapes"):
            self.snapshot()
        (self.repo / "bitcoin").unlink()
        self.snapshot()
        repository.git(
            self.repo / "bitcoin",
            "remote",
            "set-url",
            "origin",
            "forbidden://replacement",
        )
        with self.assertRaisesRegex(ValueError, "origin"):
            self.snapshot()
