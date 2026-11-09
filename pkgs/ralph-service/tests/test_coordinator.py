import hashlib
import hmac
import contextlib
import io
import json
import logging
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from ralph_service import config, coordinator, service
from ralph_service.jobs import JobStore


class CoordinatorTests(unittest.TestCase):
    def test_journal_priorities_apply_to_every_line(self):
        formatter = coordinator.JournalFormatter("%(levelname)s %(message)s")
        for level, prefix in (
            (logging.DEBUG, "<7>"),
            (logging.INFO, "<6>"),
            (logging.ERROR, "<3>"),
        ):
            record = logging.LogRecord(
                "ralph.progress", level, "", 0, "first\nsecond", (), None
            )
            self.assertTrue(
                all(
                    line.startswith(prefix)
                    for line in formatter.format(record).splitlines()
                )
            )

    def test_listing_held_deliveries_works_while_coordinator_runs(self):
        with self.jobs._connection(write=True) as db:
            db.execute(
                "INSERT INTO deliveries (job_id, repo_id, number, destination, state) "
                "VALUES (1, 'gui', 42, 'github:owner/repo', 'uncertain'), "
                "(2, 'core', 43, 'github:owner/repo', 'confirmed')"
            )
        registry = self.root / "repositories.json"
        registry.write_text(
            json.dumps(
                {
                    "core": {
                        "origin": "https://git.example/repo",
                        "repository": "owner/repo",
                        "forgejo_api": "https://git.example/api/v1/repos/owner/repo",
                    }
                }
            )
        )
        arguments = ["--state-dir", str(self.root), "--repositories", str(registry)]
        output = io.StringIO()
        with (
            service.coordinator_lock(self.root),
            contextlib.ExitStack() as resources,
            contextlib.redirect_stdout(output),
            patch("sys.argv", ["ralph-service", *arguments, "--list-held-deliveries"]),
        ):
            self.assertEqual(coordinator.main(resources), 0)
        self.assertEqual(
            [(row["repo_id"], row["number"]) for row in json.loads(output.getvalue())],
            [("gui", 42)],
        )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.jobs = JobStore(
            self.root / "jobs.sqlite3",
            repository_limits={"core": 1, "gui": 1},
            global_limit=2,
        )
        self.contexts = {}
        for repo_id, repo in [
            ("core", "bitcoin/bitcoin"),
            ("gui", "bitcoin-core/gui-qml"),
        ]:
            bot = config.BotConfig(
                "https://git.example/" + repo,
                repo,
                "https://git.example/api/v1/repos/" + repo,
                repo_id=repo_id,
            )
            self.contexts[repo_id] = {
                "bot": bot,
                "secret": repo_id.encode(),
                "engine": SimpleNamespace(identity={"name": "test", "config": {}}),
            }

    def test_report_configuration_requires_both_public_views(self):
        entry = {
            "origin": "https://git.example/owner/repo.git",
            "repository": "owner/repo",
            "forgejo_api": "https://git.example/api/v1/repos/owner/repo",
        }
        config.repository_config(entry)
        with self.assertRaisesRegex(ValueError, "must be set together"):
            config.repository_config(entry, self.root, "https://review.example/report")
        bot = config.repository_config(
            entry,
            self.root,
            "https://review.example/report/",
            "https://review.example/traces/",
        )
        self.assertEqual(bot.report_base_url, "https://review.example/report")
        self.assertEqual(bot.archive_base_url, "https://review.example/traces")
        for report_url, archive_url in (
            ("https://review.example/report?token=secret", bot.archive_base_url),
            (bot.report_base_url, "https://user:secret@review.example/traces"),
        ):
            with self.assertRaisesRegex(ValueError, "must be an HTTP URL"):
                config.repository_config(entry, self.root, report_url, archive_url)

    def test_registry_requires_explicit_valid_publication_mode(self):
        path = self.root / "repositories.json"
        entry = {
            "origin": "https://git.example/owner/repo.git",
            "repository": "owner/repo",
            "forgejo_api": "https://git.example/api/v1/repos/owner/repo",
            "github_repository": "owner/repo",
            "github_token_file": "/run/secrets/github",
        }
        for mode in ("off", "major-only", "full"):
            path.write_text(json.dumps({"gui": {**entry, "github_publication": mode}}))
            self.assertEqual(
                config.load_repositories(path)["gui"]["github_publication"], mode
            )
        path.write_text(json.dumps({"gui": {**entry, "github_publication": True}}))
        with self.assertRaisesRegex(ValueError, "github_publication must be"):
            config.load_repositories(path)
        path.write_text(
            json.dumps({"gui": {**entry, "github_post_major_findings": True}})
        )
        with self.assertRaisesRegex(ValueError, "replace github_post_major_findings"):
            config.load_repositories(path)

    def test_webhooks_authenticate_selected_repository_and_isolate_equal_numbers(self):
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), coordinator.make_handler(self.contexts, self.jobs)
        )
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}
        )
        thread.start()
        try:

            def post(repo_id, secret):
                bot = self.contexts[repo_id]["bot"]
                payload = {
                    "action": "opened",
                    "repository": {
                        "full_name": bot.repository,
                        "html_url": bot.repository_url,
                    },
                    "pull_request": {
                        "number": 42,
                        "state": "open",
                        "base": {"ref": "master"},
                        "head": {"sha": "a" * 40},
                    },
                }
                body = json.dumps(payload).encode()
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/webhooks/forgejo",
                    data=body,
                    headers={
                        "X-Forgejo-Event": "pull_request",
                        "X-Forgejo-Signature": hmac.new(
                            secret, body, hashlib.sha256
                        ).hexdigest(),
                    },
                )
                try:
                    with urllib.request.urlopen(request) as response:
                        return response.status
                except urllib.error.HTTPError as error:
                    return error.code

            self.assertEqual(post("gui", b"core"), 401)
            self.assertEqual(post("gui", b"gui"), 202)
            self.assertEqual(post("core", b"core"), 202)
            self.assertEqual(
                self.jobs.for_repository("core").claim_reconciliation()["number"], 42
            )
            self.assertEqual(
                self.jobs.for_repository("gui").claim_reconciliation()["number"], 42
            )
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_repository_identity_cannot_be_reassigned(self):
        entries = {
            key: {
                "repository": ctx["bot"].repository,
                "repository_url": ctx["bot"].repository_url,
            }
            for key, ctx in self.contexts.items()
        }
        coordinator.bind_repositories(self.jobs, entries)
        with self.assertRaisesRegex(ValueError, "already belongs"):
            coordinator.bind_repositories(self.jobs, {"renamed": entries["core"]})
        entries["core"] = entries["gui"]
        with self.assertRaisesRegex(ValueError, "identity changed"):
            coordinator.bind_repositories(self.jobs, entries)

    def test_worker_dispatches_repository_context_and_scoped_store(self):
        contexts = {}
        for repo_id, context in self.contexts.items():
            identity = context["engine"].identity
            self.jobs.for_repository(repo_id).enqueue(
                42, "master", "a" * 40, "opened", engine_identity=identity
            )
            contexts[repo_id] = {
                **context,
                "state_dir": config.repository_state_dir(self.root, repo_id),
                "entry": {
                    "bot_login": repo_id,
                    "github_publication": "full" if repo_id == "gui" else "off",
                },
                "forgejo_token": repo_id,
                "github_token": None,
            }
        stop = threading.Event()
        seen = []

        def process(
            job,
            store,
            state_dir,
            api,
            token,
            login,
            bot,
            ledger,
            engine,
            discussions,
            github_token,
            publication,
        ):
            self.assertEqual(job["repo_id"], store.repo_id)
            self.assertEqual(bot.repo_id, store.repo_id)
            self.assertEqual(publication, "full" if bot.repo_id == "gui" else "off")
            seen.append((bot.repo_id, state_dir))
            store.complete(job)
            if len(seen) == 2:
                stop.set()

        with patch.object(service, "process_job", side_effect=process):
            for _ in range(2):
                job = self.jobs.claim()
                service.run_job(job, self.jobs, "key", None, contexts)
                self.jobs.release_attempt(job["id"], job["token"])
        self.assertEqual({repo for repo, _ in seen}, {"core", "gui"})
        self.assertEqual(dict(seen)["core"], self.root)
        self.assertEqual(dict(seen)["gui"], self.root / "repos" / "gui")
