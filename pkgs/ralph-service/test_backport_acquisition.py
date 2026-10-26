"""Source acquisition contracts at the network/local review boundary."""

import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

from ralph import backports
from ralph_service import forgejo, repository


class BackportAcquisitionTests(unittest.TestCase):
    def collect(self, requests, api):
        with (
            patch.object(backports, "classify", return_value={"mode": "backport"}),
            patch.object(backports, "source_requests", return_value=requests),
            patch.object(
                backports, "collect", return_value={"complete": True, "limitations": []}
            ) as collector,
            patch.object(forgejo, "_github_api", side_effect=api),
            patch.object(repository.local, "resolve_commit", return_value="a" * 40),
            patch.object(repository.local, "git") as git,
        ):
            evidence = repository.collect_backport_evidence(
                SimpleNamespace(checkout="/checkout"),
                {"repository": "bitcoin/bitcoin"},
                SimpleNamespace(github_repository=None, repository="bitcoin/bitcoin"),
                "refs/ralph/jobs/1-1",
            )
        return evidence, collector.call_args.args[2], git

    def test_paginated_source_membership_and_gui_are_acquired(self):
        sha = "a" * 40
        paths = []

        def api(path, token):
            paths.append(path)
            if path.endswith("/commits/abcdef0"):
                return {"sha": sha}
            if "/commits/" in path and "/pulls?" in path:
                return [
                    {"number": 1, "base": {"ref": "master"}},
                    {"number": 999, "base": {"ref": "29.x"}},
                ]
            if path.endswith(("/pulls/1", "/pulls/2")):
                return {"commits": 101}
            if path.endswith("page=1"):
                return [{"sha": f"{index:040x}"} for index in range(1, 101)]
            if path.endswith("page=2"):
                return [{"sha": f"{101:040x}"}]
            self.fail(path)

        evidence, origins, git = self.collect(
            {
                "prs": ["bitcoin-core/gui#2", "attacker/repo#3"],
                "originals": [
                    {
                        "commit": "b" * 40,
                        "original": "abcdef0",
                        "pr": "bitcoin/bitcoin#1",
                    }
                ],
            },
            api,
        )
        self.assertTrue(evidence["complete"])
        self.assertEqual(len(origins["bitcoin/bitcoin#1"]["commits"]), 101)
        self.assertEqual(len(origins["bitcoin-core/gui#2"]["commits"]), 101)
        self.assertIn("error", origins["attacker/repo#3"])
        self.assertFalse(any("attacker" in path or "999" in path for path in paths))
        self.assertTrue(
            any(call.args[1] == "update-ref" for call in git.call_args_list)
        )

    def test_unavailable_original_remains_explicit_evidence_gap(self):
        def api(path, token):
            raise urllib.error.URLError("unavailable")

        evidence, origins, _ = self.collect(
            {
                "prs": ["bitcoin/bitcoin#1"],
                "originals": [{"commit": "b" * 40, "original": "abcdef0", "pr": None}],
            },
            api,
        )
        self.assertFalse(evidence["complete"])
        self.assertIn("abcdef0", evidence["limitations"][0])
        self.assertIn("error", origins["bitcoin/bitcoin#1"])

    def test_truncated_github_commit_list_cannot_be_complete(self):
        def api(path, token):
            if path.endswith("/pulls/1"):
                return {"commits": 301}
            return [{"sha": "a" * 40}] * (50 if "page=3" in path else 100)

        _, origins, _ = self.collect(
            {"prs": ["bitcoin/bitcoin#1"], "originals": []}, api
        )
        self.assertIn("error", origins["bitcoin/bitcoin#1"])


if __name__ == "__main__":
    unittest.main()
