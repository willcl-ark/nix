import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from ralph_service import commands, monitor


class CommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = monitor.NotificationStore(self.root / "telegram.sqlite3")
        self.bot = {"id": 9, "username": "RalphBot"}
        self.admins = [
            {"status": "creator", "user": {"id": 1}},
            {
                "status": "administrator",
                "user": {"id": 9, "is_bot": True},
                "can_post_messages": True,
            },
        ]
        self.updates = []
        self.api = patch.object(commands.telegram, "api_request", create=True).start()
        self.addCleanup(patch.stopall)
        self.api.side_effect = self.request

    def request(self, token, method, data, **kwargs):
        if method == "getUpdates":
            return self.updates
        self.assertEqual(method, "getChatAdministrators")
        self.assertEqual(data, {"chat_id": -100123, "return_bots": True})
        return self.admins

    def update(self, update_id=10, text="/help", **fields):
        post = {
            "chat": {"type": "channel", "id": -100123, "username": "RalphChannel"},
            "text": text,
        }
        post.update(fields)
        return {"update_id": update_id, "channel_post": post}

    def poll(self, destination="-100123"):
        return commands.poll_commands(
            self.store,
            "token",
            destination,
            self.bot,
            self.root,
            self.root,
            "https://reports",
        )

    def messages(self):
        with closing(self.store._connect()) as db:
            return dict(db.execute("SELECT key, text FROM outbox"))

    def offset(self):
        with closing(self.store._connect()) as db:
            row = db.execute(
                "SELECT value FROM metadata WHERE key = 'update_offset'"
            ).fetchone()
            return row[0] if row else None

    def test_high_request_is_durable_and_idempotent(self):
        self.updates = [self.update(text="/high@RalphBot 42")]
        self.assertEqual(self.poll(), 1)
        self.assertEqual(self.poll(), 0)
        with closing(self.store._connect()) as db:
            self.assertEqual(
                db.execute("SELECT id, number FROM review_requests").fetchall(),
                [(10, 42)],
            )
        self.assertIn("high review", self.messages()["command:10"])

    def test_high_invalid_and_unauthorized_requests(self):
        for index, text in enumerate(
            ("/high", "/high -1", "/high https://elsewhere/42", "/high 42 extra")
        ):
            self.updates = [self.update(10 + index, text=text)]
            self.poll()
        self.updates = [self.update(20, text="/high 42", **{"from": {"id": 1234}})]
        self.poll()
        with closing(self.store._connect()) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM review_requests").fetchone()[0], 0
            )
        self.assertNotIn("command:20", self.messages())

    def test_reply_and_offset_survive_restart_without_duplicate(self):
        self.updates = [self.update()]
        self.assertEqual(self.poll(), 1)
        self.assertIn("/status", self.messages()["command:10"])
        self.assertEqual(self.offset(), "11")
        self.store = monitor.NotificationStore(self.store.path)
        self.assertEqual(self.poll(), 0)
        self.assertEqual(len(self.messages()), 1)
        self.api.assert_any_call(
            "token",
            "getUpdates",
            {"offset": 11, "timeout": 20, "allowed_updates": ["channel_post"]},
            timeout=30,
        )

    def test_ignored_chat_private_message_and_edited_post(self):
        stranger = self.update(10)
        stranger["channel_post"]["chat"]["id"] = -999
        private = self.update(11, chat={"type": "private", "id": -100123})
        edited = {"update_id": 12, "edited_channel_post": self.update()["channel_post"]}
        direct = {"update_id": 13, "message": self.update()["channel_post"]}
        self.updates = [stranger, private, edited, direct]
        self.poll()
        self.assertEqual(self.messages(), {})
        self.assertEqual(self.offset(), "14")
        self.assertEqual(self.api.call_count, 1)

    def test_username_and_bot_suffix_case_insensitive(self):
        self.updates = [self.update(text="/help@ralphbot")]
        self.poll("@ralphchannel")
        self.assertEqual(len(self.messages()), 1)

    def test_other_bot_suffix_and_arguments_ignored(self):
        self.updates = [
            self.update(10, "/help@OtherBot"),
            self.update(11, "/help extra"),
            self.update(12, "/restart"),
        ]
        self.poll()
        self.assertEqual(self.messages(), {})
        self.assertEqual(self.api.call_count, 1)

    def test_anonymous_channel_owner_accepted(self):
        self.updates = [self.update(sender_chat={"id": -100123})]
        self.poll()
        self.assertEqual(len(self.messages()), 1)

    def test_other_posting_admin_or_bot_can_issue_commands(self):
        for update_id, is_bot in enumerate((False, True), 10):
            with self.subTest(is_bot=is_bot):
                self.admins.append(
                    {
                        "status": "administrator",
                        "can_post_messages": True,
                        "user": {"id": 2, "is_bot": is_bot},
                    }
                )
                self.updates = [self.update(update_id)]
                self.assertEqual(self.poll(), 1)
                self.assertIn(f"command:{update_id}", self.messages())
                self.admins.pop()

    def test_nonposting_admin_accepted(self):
        self.admins.append({"status": "administrator", "user": {"id": 2}})
        self.updates = [self.update()]
        self.poll()
        self.assertEqual(len(self.messages()), 1)

    def test_identified_sender_must_be_an_administrator(self):
        self.updates = [self.update(10, **{"from": {"id": 2}})]
        self.poll()
        self.assertEqual(self.messages(), {})
        self.admins.append(
            {"status": "administrator", "can_post_messages": True, "user": {"id": 2}}
        )
        self.updates = [
            self.update(11, **{"from": {"id": 2}}),
            self.update(12, **{"from": {"id": 1}}),
            self.update(13, **{"from": {"id": 9, "is_bot": True}}),
        ]
        self.poll()
        self.assertEqual(len(self.messages()), 3)

    def test_foreign_sender_chat_and_missing_owner_or_bot_rejected(self):
        self.updates = [self.update(sender_chat={"id": -999})]
        self.poll()
        self.assertEqual(self.messages(), {})
        self.updates = [self.update(11)]
        self.admins.pop()
        self.poll()
        self.assertEqual(self.messages(), {})
        self.updates = [self.update(12)]
        self.admins = self.admins[1:]
        self.poll()
        self.assertEqual(self.messages(), {})

    def test_authorization_api_failure_does_not_acknowledge_batch(self):
        self.updates = [self.update(10), self.update(11)]
        self.api.side_effect = [
            self.updates,
            self.admins,
            commands.telegram.TelegramError(),
        ]
        with self.assertRaises(commands.telegram.TelegramError):
            self.poll()
        self.assertEqual(self.messages(), {})
        self.assertIsNone(self.offset())

    def test_status_failure_does_not_acknowledge_batch(self):
        self.updates = [self.update(10), self.update(11, "/status")]
        with (
            patch("ralph_service.status.status_message", side_effect=OSError),
            self.assertRaises(OSError),
        ):
            self.poll()
        self.assertEqual(self.messages(), {})
        self.assertIsNone(self.offset())

    def test_status_reply_uses_summary_and_is_truncated(self):
        self.updates = [self.update(text="/status")]
        with patch(
            "ralph_service.status.status_message", return_value="x" * 5000
        ) as render:
            self.poll()
        render.assert_called_once_with(self.root, self.root, "https://reports")
        self.assertEqual(
            commands.telegram.message_length(self.messages()["command:10"]), 4096
        )


if __name__ == "__main__":
    unittest.main()
