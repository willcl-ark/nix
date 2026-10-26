import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from ralph import repository as local_repository
from ralph_service import config, forgejo, repository


class BotTests(unittest.TestCase):
    def setUp(self):
        self.bot_config = config.BotConfig(
            "https://git.fish.foo/bitcoin/bitcoin.git",
            "bitcoin/bitcoin",
            "https://git.fish.foo/api/v1/repos/bitcoin/bitcoin",
        )

    def test_pull_request_context_uses_mirror_issue_metadata(self):
        issue = {
            "number": 36670,
            "title": "Fix review context",
            "body": None,
            "labels": [{"name": "Wallet"}, {"name": "Tests"}, {"name": "Wallet"}],
            "pull_request": {"merged": False, "draft": False},
        }
        with patch.object(forgejo, "forgejo_request", return_value=issue) as request:
            context = forgejo.pull_request_context(self.bot_config, "token", 36670)
            request.assert_called_once_with(self.bot_config, "token", "/issues/36670")
            self.assertEqual(
                context,
                {
                    "title": "Fix review context",
                    "body": "",
                    "labels": ["Tests", "Wallet"],
                },
            )
            issue["body"] = "Review the mirrored PR."
            self.assertEqual(
                forgejo.pull_request_context(self.bot_config, "token", 36670)["body"],
                issue["body"],
            )
            del issue["pull_request"]
            with self.assertRaisesRegex(ValueError, "invalid pull request$"):
                forgejo.pull_request_context(self.bot_config, "token", 36670)

    def test_concurrent_review_refs_preserve_pinned_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin = root / "origin.git"
            work = root / "work"
            checkout = root / "checkout"
            local_repository.git(root, "init", "--bare", str(origin))
            local_repository.git(root, "init", str(work))
            local_repository.git(work, "config", "user.name", "Test")
            local_repository.git(work, "config", "user.email", "test@example.com")
            local_repository.git(work, "remote", "add", "origin", str(origin))
            path = work / "code.cpp"
            path.write_text("base behavior\n")
            local_repository.git(work, "add", "code.cpp")
            local_repository.git(work, "commit", "-qm", "Base")
            base = local_repository.git(work, "rev-parse", "HEAD").strip()
            local_repository.git(work, "push", "origin", "HEAD:refs/heads/master")

            path.write_text("first PR behavior\n")
            local_repository.git(work, "commit", "-qam", "First PR")
            first_head = local_repository.git(work, "rev-parse", "HEAD").strip()
            local_repository.git(work, "push", "origin", "HEAD:refs/pull/1/head")
            local_repository.git(work, "reset", "--hard", base)
            path.write_text("second PR behavior\n")
            local_repository.git(work, "commit", "-qam", "Second PR")
            second_head = local_repository.git(work, "rev-parse", "HEAD").strip()
            local_repository.git(work, "push", "origin", "HEAD:refs/pull/2/head")

            bot_config = config.BotConfig(
                str(origin),
                "example/repo",
                "https://git.example.org/api/v1/repos/example/repo",
            )
            first_prefix = "refs/ralph/jobs/1-1"
            second_prefix = "refs/ralph/jobs/2-1"
            first = repository.collect_review(
                checkout,
                1,
                "master",
                first_head,
                "First",
                "First PR",
                bot_config,
                ref_prefix=first_prefix,
            )
            self.assertIsNone(first[3])
            first_snapshot = local_repository.snapshot_repository(
                checkout, first[0], first[1]
            )
            second = repository.collect_review(
                checkout,
                2,
                "master",
                second_head,
                "Second",
                "Second PR",
                bot_config,
                ref_prefix=second_prefix,
            )
            self.assertIsNone(second[3])
            second_snapshot = local_repository.snapshot_repository(
                checkout, second[0], second[1]
            )
            local_repository.git(checkout, "update-ref", "refs/ralph/head", base)

            self.assertIn(
                "first PR behavior",
                local_repository.read_file(
                    checkout, first_snapshot.head_files, "code.cpp", 1
                ),
            )
            self.assertIn(
                "+first PR behavior",
                local_repository.read_diff(
                    checkout,
                    first_snapshot.changed_paths,
                    first_snapshot.merge_base,
                    first_snapshot.head_sha,
                    "code.cpp",
                    1,
                ),
            )
            self.assertIn(
                "second PR behavior",
                local_repository.read_file(
                    checkout, second_snapshot.head_files, "code.cpp", 1
                ),
            )
            local_repository.git(checkout, "gc", "--prune=now")
            self.assertIn(
                "first PR behavior",
                local_repository.read_file(
                    checkout, first_snapshot.head_files, "code.cpp", 1
                ),
            )
            local_repository.git(
                checkout, "update-ref", "refs/review-cases/example/head", first_head
            )

            local_repository.release_review_refs(checkout, first_prefix)
            self.assertEqual(
                local_repository.git(
                    checkout, "for-each-ref", "--format=%(refname)", first_prefix
                ),
                "",
            )
            self.assertEqual(
                local_repository.git(
                    checkout, "rev-parse", f"{second_prefix}/head"
                ).strip(),
                second_head,
            )
            self.assertEqual(
                local_repository.git(
                    checkout, "rev-parse", f"{second_prefix}/base"
                ).strip(),
                base,
            )
            self.assertEqual(
                local_repository.git(checkout, "rev-parse", "refs/ralph/head").strip(),
                base,
            )
            self.assertEqual(
                local_repository.git(
                    checkout, "rev-parse", "refs/review-cases/example/head"
                ).strip(),
                first_head,
            )

    def test_public_discussion_request_sends_no_token_and_limits_response(self):
        class Response:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, size):
                return self.body[:size]

        with patch.object(
            urllib.request, "urlopen", return_value=Response(b'{"number": 42}')
        ) as send:
            self.assertEqual(
                forgejo.public_discussion_request(self.bot_config, "/issues/42"),
                {"number": 42},
            )
        request = send.call_args.args[0]
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(
            request.full_url,
            "https://git.fish.foo/api/v1/repos/bitcoin/bitcoin/issues/42",
        )
        with (
            patch.object(
                urllib.request,
                "urlopen",
                return_value=Response(
                    b"x" * (forgejo.MAX_DISCUSSION_RESPONSE_BYTES + 1)
                ),
            ),
            self.assertRaisesRegex(ValueError, "context limit"),
        ):
            forgejo.public_discussion_request(self.bot_config, "/issues/42")

    def test_publish_creates_then_edits_one_bot_comment(self):
        comments = [
            {"id": 1, "user": {"login": "someone-else"}, "body": "Unrelated comment"}
        ]
        calls = []

        def request(bot_config, token, path, method="GET", data=None):
            calls.append((path, method, data))
            if path == "/issues/42/comments?limit=50&page=1":
                return comments
            if method == "POST":
                comments.append(
                    {"id": 2, "user": {"login": "ralph"}, "body": data["body"]}
                )
                return comments[-1]
            if method == "PATCH":
                comments[-1]["body"] = data["body"]
                return comments[-1]
            self.fail(f"Unexpected API call {path}")

        with (
            patch.object(forgejo, "forgejo_request", side_effect=request),
            patch.object(forgejo, "current_head", return_value="a" * 40),
        ):
            args = ("token", 42, "ralph", "b" * 40, "a" * 40)
            self.assertEqual(
                forgejo.publish_review(self.bot_config, *args, "First review."),
                "created",
            )
            self.assertEqual(
                forgejo.publish_review(self.bot_config, *args, "First review."),
                "unchanged",
            )
            self.assertEqual(
                forgejo.publish_review(self.bot_config, *args, "Updated review."),
                "updated",
            )
        self.assertEqual(
            [method for _, method, _ in calls if method != "GET"], ["POST", "PATCH"]
        )
        self.assertEqual(comments[-1]["id"], 2)
        self.assertIn("Updated review.", comments[-1]["body"])


if __name__ == "__main__":
    unittest.main()
