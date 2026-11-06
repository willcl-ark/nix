import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock

from ralph_service import delivery
from ralph_service.jobs import JobStore


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs = JobStore(Path(self.tmp.name) / "jobs.sqlite3")
        self.jobs.enqueue(1, "master", "a" * 40, "synchronize", engine_identity=None)
        self.job = self.jobs.claim()
        self.session = delivery.Session(self.jobs, self.job, "forgejo:repo")

    def execute(self, session=None, dispatch=None, body="saved body"):
        return (session or self.session).execute(
            "POST", "/comments", body, None, dispatch or Mock(), lambda: True
        )

    def test_delayed_create_holds_new_generation_until_visible(self):
        send = Mock(side_effect=TimeoutError("lost acknowledgment"))
        with self.assertRaises(delivery.DeliveryHeld):
            self.execute(dispatch=send)
        self.jobs.complete(self.job)
        self.jobs.release_attempt(self.job["id"], self.job["token"])
        self.jobs.enqueue(1, "master", "b" * 40, "synchronize", engine_identity=None)
        newer = delivery.Session(self.jobs, self.jobs.claim(), "forgejo:repo")
        with self.assertRaises(delivery.DeliveryHeld):
            newer.reconcile(None, Mock())
            newer.check_hold()
        newer.reconcile({"id": 7, "body": "saved body"}, Mock())
        self.assertEqual(self.execute(newer), "created")
        send.assert_called_once()

    def test_retry_reuses_exact_body_after_definite_rejection(self):
        error = urllib.error.HTTPError("url", 429, "limited", {}, None)
        with self.assertRaises(urllib.error.HTTPError):
            self.execute(dispatch=Mock(side_effect=error))
        render = Mock(return_value="new rendering")
        self.assertEqual(self.session.body(render), "saved body")
        render.assert_not_called()
        send = Mock()
        self.execute(dispatch=send, body="new rendering")
        self.assertEqual(send.call_args.args, ("POST", "/comments", "saved body"))

    def test_uncertain_edit_and_delete_reconcile_exact_target(self):
        for operation, body, observed in [
            ("PATCH", "new", {"id": 9, "body": "new"}),
            ("DELETE", None, None),
        ]:
            with self.subTest(operation=operation):
                session = delivery.Session(self.jobs, self.job, operation)
                with self.assertRaises(delivery.DeliveryHeld):
                    session.execute(
                        operation,
                        "/comments/9",
                        body,
                        {"id": 9},
                        Mock(side_effect=TimeoutError()),
                        lambda: True,
                    )
                session.reconcile(None, Mock(return_value=observed))
                self.assertTrue(session.finished())

    def test_crash_before_dispatch_remains_held_after_restart(self):
        with self.assertRaises(KeyboardInterrupt):
            self.execute(dispatch=Mock(side_effect=KeyboardInterrupt()))
        reopened = JobStore(self.jobs.path)
        session = delivery.Session(reopened, self.job, "forgejo:repo")
        session.reconcile(None, Mock())
        with self.assertRaises(delivery.DeliveryHeld):
            session.check_hold()
        self.assertEqual(len(delivery.held(reopened)), 1)

    def test_release_adopts_visible_create_without_duplicate(self):
        with self.assertRaises(delivery.DeliveryHeld):
            self.execute(dispatch=Mock(side_effect=TimeoutError()))
        self.assertEqual(delivery.release(self.jobs, 1), 1)
        send = Mock()
        self.assertEqual(
            self.session.execute(
                "PATCH",
                "/comments/7",
                "changed",
                {"id": 7, "body": "saved body"},
                send,
                lambda: True,
            ),
            "unchanged",
        )
        send.assert_not_called()

    def test_legacy_saved_result_quarantined_until_explicit_release(self):
        self.jobs.save_result(self.job, {"content": "saved"})
        with self.jobs._connection(write=True) as db:
            db.execute("DROP TABLE deliveries")
        reopened = JobStore(self.jobs.path)
        self.assertEqual(delivery.held(reopened)[0]["destination"], "*")
        with self.assertRaises(delivery.DeliveryHeld):
            delivery.Session(reopened, self.job, "forgejo:repo").check_hold()
        delivery.release(reopened, 1)
        self.assertEqual(delivery.held(reopened), [])


class RepositoryDeliveryTests(unittest.TestCase):
    setUp = DeliveryTests.setUp
    execute = DeliveryTests.execute

    def test_gui_uncertainty_does_not_hold_or_release_core(self):
        gui = self.jobs.for_repository("gui")
        gui.enqueue(1, "master", "a" * 40, "opened", engine_identity=None)
        gui_job = gui.claim()
        session = delivery.Session(gui, gui_job, "github:owner/gui")
        with self.assertRaises(delivery.DeliveryHeld):
            self.execute(session, Mock(side_effect=TimeoutError()))
        self.assertEqual(delivery.release(self.jobs, 1), 0)
        self.assertEqual(len(delivery.held(gui)), 1)
        self.assertEqual(delivery.held(self.jobs), [])
        self.assertEqual(self.execute(), "created")
        self.assertEqual(delivery.release(gui, 1), 1)

    def test_saved_rejected_intent_cannot_move_to_new_destination(self):
        error = urllib.error.HTTPError("url", 429, "limited", {}, None)
        with self.assertRaises(urllib.error.HTTPError):
            self.execute(dispatch=Mock(side_effect=error))
        moved = delivery.Session(self.jobs, self.job, "forgejo:other-repo")
        dispatch = Mock()
        with self.assertRaises(delivery.DeliveryHeld):
            self.execute(moved, dispatch)
        dispatch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
