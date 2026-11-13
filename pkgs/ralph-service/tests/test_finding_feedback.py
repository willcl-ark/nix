import hashlib
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from ralph_service import coordinator
from ralph_service.config import BotConfig
from ralph_service.finding_feedback import FeedbackStore, main
from ralph_service.jobs import JobStore


class FindingFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = FeedbackStore(self.root / "feedback.sqlite3")
        self.reports = self.root / "reports"
        self.reports.mkdir()
        self.record = {
            "repository_id": "core",
            "repository": "bitcoin/bitcoin",
            "report_id": "123-head-1",
            "head": "a" * 40,
            "status": "completed",
            "findings_export_status": "supported",
            "findings": [{"id": "finding:1", "title": "A claim", "severity": "major"}],
        }
        self.write_record(self.record)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        contexts = {
            repo_id: {
                "bot": self.bot(repo_id, repository),
                "secret": b"test",
                "engine": SimpleNamespace(identity=None),
            }
            for repo_id, repository in [
                ("core", "bitcoin/bitcoin"),
                ("gui", "bitcoin-core/gui"),
            ]
        }
        jobs = JobStore(self.root / "jobs.sqlite3")
        self.server.RequestHandlerClass = coordinator.make_handler(contexts, jobs)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.token = "a" * 64
        self.path = "/feedback/core/123-head-1"

    def bot(self, repo_id, repository):
        return BotConfig(
            origin="https://example.com/repo.git",
            repository=repository,
            forgejo_api=f"https://example.com/api/v1/repos/{repository}",
            report_dir=self.reports,
            report_base_url=self.origin + "/report",
            archive_base_url=self.origin + "/traces",
            repo_id=repo_id,
        )

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def write_record(self, record):
        directory = (
            self.reports
            if record["repository_id"] == "core"
            else self.reports / record["repository_id"]
        )
        directory.mkdir(exist_ok=True)
        (directory / (record["report_id"] + ".json")).write_text(json.dumps(record))

    def request(self, path=None, payload=None, token=None, origin=None):
        headers = {"X-Feedback-Token": token or self.token}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers.update(
                {"Content-Type": "application/json", "Origin": origin or self.origin}
            )
        request = urllib.request.Request(
            self.origin + (path or self.path), data=data, headers=headers
        )
        try:
            response = urllib.request.urlopen(request)
        except urllib.error.HTTPError as response:
            return response.code, json.load(response)
        with response:
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            return response.status, json.load(response)

    def vote(self, vote, comment="", **kwargs):
        return self.request(
            self.path + "/finding%3A1", {"vote": vote, "comment": comment}, **kwargs
        )

    def test_edit_withdraw_persist_and_triage_without_disclosing_explanations(self):
        self.assertEqual(self.vote(1, "<script>private</script>")[0], 200)
        self.assertEqual(self.vote(1, "<script>private</script>")[0], 200)
        first = self.store.pending()[0]
        self.assertEqual(first["revision"], 1)
        _, public = self.request(token="b" * 64)
        self.assertEqual(
            public["findings"]["finding:1"], {"up": 1, "down": 0, "mine": None}
        )
        self.assertNotIn("private", json.dumps(public))
        self.assertTrue(self.store.mark_reviewed(first["id"], first["revision"]))
        self.assertEqual(self.store.pending(), [])
        _, result = self.vote(-1, "The prerequisite cannot occur.")
        self.assertEqual(
            result["findings"]["finding:1"],
            {
                "up": 0,
                "down": 1,
                "mine": {"vote": -1, "comment": "The prerequisite cannot occur."},
            },
        )
        pending = FeedbackStore(self.store.path).pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["revision"], 2)
        self.assertFalse(self.store.mark_reviewed(first["id"], first["revision"]))
        self.assertEqual(pending[0]["head"], self.record["head"])
        self.assertEqual(pending[0]["repository"], "bitcoin/bitcoin")
        with self.store._connection() as db:
            self.assertEqual(
                db.execute("SELECT voter FROM feedback").fetchone()[0],
                hashlib.sha256(self.token.encode()).hexdigest(),
            )
        _, result = self.vote(None)
        self.assertEqual(
            result["findings"]["finding:1"], {"up": 0, "down": 0, "mine": None}
        )
        self.assertEqual(self.store.pending(), [])
        self.vote(1)
        self.assertFalse(self.store.mark_reviewed(first["id"], 1))
        self.assertNotEqual(self.store.pending()[0]["id"], first["id"])
        self.assertEqual(
            json.loads((self.reports / "123-head-1.json").read_text()), self.record
        )

    def test_reports_and_repositories_are_isolated_and_only_published_findings_accept_votes(
        self,
    ):
        self.vote(1)
        other = {**self.record, "report_id": "123-other-2", "head": "b" * 40}
        self.write_record(other)
        _, result = self.request("/feedback/core/123-other-2")
        self.assertEqual(result["findings"]["finding:1"]["up"], 0)
        self.write_record(
            {**self.record, "repository_id": "gui", "repository": "bitcoin-core/gui"}
        )
        _, result = self.request("/feedback/gui/123-head-1")
        self.assertEqual(result["findings"]["finding:1"]["up"], 0)
        self.assertEqual(self.request(self.path + "/finding%3A2", {"vote": 1})[0], 404)
        self.assertEqual(self.request("/feedback/core/../../etc/passwd")[0], 404)
        self.assertEqual(self.request("/feedback/missing/123-head-1")[0], 404)
        self.write_record(
            {**self.record, "coverage": {"verification": {"status": "failed"}}}
        )
        self.assertEqual(self.vote(1)[0], 404)
        self.assertEqual(self.request()[1]["findings"], {})

    def test_rejects_cross_origin_invalid_payload_and_excessive_changes(self):
        self.assertEqual(self.vote(1, origin="https://elsewhere.example")[0], 403)
        self.assertEqual(self.vote(True)[0], 400)
        self.assertEqual(self.vote(0)[0], 400)
        self.assertEqual(self.vote(1, "x" * 2001)[0], 400)
        self.assertEqual(self.vote(1, token="invalid")[0], 400)
        self.assertEqual(self.store.pending(), [])
        for _ in range(30):
            self.assertEqual(self.vote(1)[0], 200)
        self.assertEqual(self.vote(-1)[0], 429)
        self.assertEqual(self.request()[1]["findings"]["finding:1"]["up"], 1)

    def test_private_triage_export_escapes_feedback_and_review_requires_current_revision(
        self,
    ):
        self.vote(-1, "<img src=x onerror=alert(1)>")
        output = self.root / "triage.html"
        self.assertEqual(
            main(["--state-dir", str(self.root), "list", "--html", str(output)]), 0
        )
        self.assertIn("&lt;img", output.read_text())
        self.assertNotIn("<img", output.read_text())
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        row = self.store.pending()[0]
        self.assertEqual(
            main(
                [
                    "--state-dir",
                    str(self.root),
                    "review",
                    str(row["id"]),
                    str(row["revision"]),
                ]
            ),
            0,
        )
        self.assertEqual(self.store.pending(), [])
