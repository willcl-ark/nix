import tempfile
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from ralph import repository as local_repository
from ralph_service import config, delivery, forgejo, jobs, repository


class BotTests(unittest.TestCase):
    def setUp(self):
        self.bot_config = config.BotConfig(
            "https://git.fish.foo/bitcoin/bitcoin.git",
            "bitcoin/bitcoin",
            "https://git.fish.foo/api/v1/repos/bitcoin/bitcoin",
        )

    def session(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = jobs.JobStore(Path(directory.name) / "jobs.sqlite3")
        store.enqueue(42, "master", "a" * 40, "synchronize", engine_identity=None)
        return delivery.Session(store, store.claim(), "forgejo")

    def test_pull_request_context_captures_authoritative_target(self):
        issue = {
            "number": 36670,
            "title": "Fix review context",
            "body": None,
            "labels": [{"name": "Wallet"}, {"name": "Tests"}, {"name": "Wallet"}],
            "base": {"ref": "master"},
        }
        with patch.object(forgejo, "forgejo_request", return_value=issue) as request:
            context = forgejo.pull_request_context(self.bot_config, "token", 36670)
            request.assert_called_once_with(self.bot_config, "token", "/pulls/36670")
            self.assertEqual(
                context,
                {
                    "title": "Fix review context",
                    "body": "",
                    "labels": ["Tests", "Wallet"],
                    "base_ref": "master",
                },
            )
            issue["body"] = "Review the mirrored PR."
            self.assertEqual(
                forgejo.pull_request_context(self.bot_config, "token", 36670)["body"],
                issue["body"],
            )
            del issue["base"]
            with self.assertRaisesRegex(ValueError, "invalid pull request$"):
                forgejo.pull_request_context(self.bot_config, "token", 36670)

    def test_publication_rechecks_generation_after_comment_lookup(self):
        current = True

        def lookup(*args):
            nonlocal current
            current = False
            return None

        with (
            patch.object(forgejo, "find_comment", side_effect=lookup),
            patch.object(forgejo, "current_head", return_value="a" * 40),
            patch.object(forgejo, "forgejo_request") as request,
        ):
            outcome = forgejo.publish_review(
                self.bot_config,
                "token",
                42,
                "ralph",
                "b" * 40,
                "a" * 40,
                "Review",
                source_base_sha="c" * 40,
                is_current=lambda: current,
                delivery=self.session(),
            )
        self.assertEqual(outcome, "stale")
        request.assert_not_called()

    def test_same_head_retarget_prevents_publication(self):
        context = {"title": "Title", "body": "", "labels": [], "base_ref": "master"}
        with (
            patch.object(forgejo, "find_comment", return_value=None),
            patch.object(
                forgejo,
                "pull_request_context",
                return_value={
                    **context,
                    "base_ref": "other",
                },
            ),
            patch.object(forgejo, "current_head", return_value="a" * 40),
            patch.object(forgejo, "forgejo_request") as request,
        ):
            self.assertEqual(
                forgejo.publish_review(
                    self.bot_config,
                    "token",
                    42,
                    "ralph",
                    "b" * 40,
                    "a" * 40,
                    "Review",
                    source_base_sha="c" * 40,
                    expected_context=context,
                    is_current=lambda: True,
                    delivery=self.session(),
                ),
                "stale",
            )
        request.assert_not_called()

    def test_same_head_metadata_edits_allow_publication(self):
        context = {"title": "Title", "body": "", "labels": [], "base_ref": "master"}
        with (
            patch.object(forgejo, "find_comment", return_value=None),
            patch.object(
                forgejo,
                "pull_request_context",
                return_value={
                    **context,
                    "title": "Edited title",
                    "body": "Edited description",
                    "labels": ["backport"],
                },
            ),
            patch.object(forgejo, "current_head", return_value="a" * 40),
            patch.object(
                forgejo, "forgejo_request", return_value={"user": {"login": "ralph"}}
            ) as request,
        ):
            self.assertEqual(
                forgejo.publish_review(
                    self.bot_config,
                    "token",
                    42,
                    "ralph",
                    "b" * 40,
                    "a" * 40,
                    "Review",
                    source_base_sha="c" * 40,
                    expected_context=context,
                    is_current=lambda: True,
                    delivery=self.session(),
                ),
                "created",
            )
        self.assertEqual(request.call_args.args[3], "POST")

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

    def test_discussion_reads_exclude_ralphs_github_comments(self):
        ralph = {
            "id": 1,
            "user": {"login": "ralph-account", "type": "User"},
            "body": "<!-- ralph-major:bitcoin-core/gui-qml -->\n\nOwn finding",
        }
        human = {
            "id": 2,
            "user": {"login": "reviewer", "type": "User"},
            "body": "Human comment",
        }
        self.assertTrue(forgejo._is_bot_comment(ralph, self.bot_config))
        self.assertFalse(forgejo._is_bot_comment(human, self.bot_config))
        with patch.object(
            forgejo,
            "public_discussion_request",
            side_effect=[{"number": 7, "title": "Title"}, [ralph, human]],
        ):
            discussion = forgejo.read_discussion(self.bot_config, 7, 42)
        self.assertIn("Human comment", discussion)
        self.assertNotIn("Own finding", discussion)

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
        with patch.object(
            urllib.request, "urlopen", return_value=Response(b"[]")
        ) as send:
            path = "/issues/42/comments?limit=10&page=2"
            forgejo.public_discussion_request(self.bot_config, path)
            forgejo.public_discussion_request(
                replace(
                    self.bot_config,
                    forgejo_api="https://api.github.com/repos/bitcoin/bitcoin",
                    repository_url="https://github.com/bitcoin/bitcoin",
                ),
                path,
            )
        self.assertEqual(
            [call.args[0].full_url for call in send.call_args_list],
            [
                "https://git.fish.foo/api/v1/repos/bitcoin/bitcoin" + path,
                "https://api.github.com/repos/bitcoin/bitcoin/issues/42/comments?per_page=10&page=2",
            ],
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

    def test_duplicate_owned_markers_block_publication(self):
        comments = [
            {
                "id": number,
                "user": {"login": "ralph"},
                "body": self.bot_config.comment_marker,
            }
            for number in (1, 2)
        ]
        with patch.object(forgejo, "forgejo_request", return_value=comments) as request:
            with self.assertRaisesRegex(ValueError, "Multiple bot review comments"):
                forgejo.find_comment(self.bot_config, "token", 42, "ralph")
        self.assertEqual(request.call_count, 1)

    def test_acknowledgment_loss_reconciles_without_rerendering(self):
        session = self.session()
        comments = []
        writes = []

        def request(bot_config, token, path, method="GET", data=None):
            if method == "GET":
                return comments
            writes.append(data["body"])
            comments.append({"id": 2, "user": {"login": "ralph"}, "body": data["body"]})
            raise TimeoutError("acknowledgment lost")

        def publish(content):
            return forgejo.publish_review(
                self.bot_config,
                "token",
                42,
                "ralph",
                "b" * 40,
                "a" * 40,
                content,
                source_base_sha="c" * 40,
                is_current=lambda: True,
                delivery=session,
            )

        with (
            patch.object(forgejo, "forgejo_request", side_effect=request),
            patch.object(forgejo, "current_head", return_value="a" * 40),
        ):
            with self.assertRaises(delivery.DeliveryHeld):
                publish("First review")
            with patch.object(
                forgejo, "review_body", side_effect=AssertionError("rerendered")
            ):
                self.assertEqual(
                    publish("New rendering must not replace saved bytes"), "unchanged"
                )
        self.assertEqual(len(writes), 1)
        self.assertEqual(comments[0]["body"], writes[0])

    def test_rejected_write_retries_saved_body_after_renderer_changes(self):
        session = self.session()
        writes = []

        def request(bot_config, token, path, method="GET", data=None):
            if method == "GET":
                return []
            writes.append(data["body"])
            if len(writes) == 1:
                raise urllib.error.HTTPError(path, 429, "retry later", {}, None)
            return {"id": 2, "user": {"login": "ralph"}, "body": data["body"]}

        def publish(content):
            return forgejo.publish_review(
                self.bot_config,
                "token",
                42,
                "ralph",
                "b" * 40,
                "a" * 40,
                content,
                source_base_sha="c" * 40,
                is_current=lambda: True,
                delivery=session,
            )

        with (
            patch.object(forgejo, "forgejo_request", side_effect=request),
            patch.object(forgejo, "current_head", return_value="a" * 40),
        ):
            with self.assertRaises(urllib.error.HTTPError):
                publish("First review")
            with patch.object(
                forgejo, "review_body", side_effect=AssertionError("rerendered")
            ):
                self.assertEqual(publish("Changed content"), "created")
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[0], writes[1])

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
                forgejo.publish_review(
                    self.bot_config,
                    *args,
                    "First review.",
                    source_base_sha="c" * 40,
                    is_current=lambda: True,
                    delivery=self.session(),
                ),
                "created",
            )
            self.assertEqual(
                forgejo.publish_review(
                    self.bot_config,
                    *args,
                    "First review.",
                    source_base_sha="c" * 40,
                    is_current=lambda: True,
                    delivery=self.session(),
                ),
                "unchanged",
            )
            self.assertEqual(
                forgejo.publish_review(
                    self.bot_config,
                    *args,
                    "Updated review.",
                    source_base_sha="c" * 40,
                    is_current=lambda: True,
                    delivery=self.session(),
                ),
                "updated",
            )
        self.assertEqual(
            [method for _, method, _ in calls if method != "GET"], ["POST", "PATCH"]
        )
        self.assertEqual(comments[-1]["id"], 2)
        self.assertIn("Updated review.", comments[-1]["body"])


if __name__ == "__main__":
    unittest.main()
