import hashlib
import hmac
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ralph import config as core_config
from ralph import engine, model, pipeline, usage
from ralph import repository as local_repository
from ralph_service import config, forgejo, repository, service
from ralph_service.jobs import JobStore


class ServiceTests(unittest.TestCase):
    def setUp(self):
        context = patch.object(
            forgejo,
            "pull_request_context",
            return_value={
                "title": "Title",
                "body": "Description",
                "labels": [],
            },
        )
        context.start()
        self.addCleanup(context.stop)
        evidence = patch.object(
            repository, "collect_backport_evidence", return_value=None
        )
        self.evidence = evidence.start()
        self.addCleanup(evidence.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_dir = Path(self.directory.name)
        self.jobs = JobStore(self.state_dir / "jobs.sqlite3", retry_base_seconds=0)
        self.ledger = usage.Ledger(self.state_dir / "usage.sqlite3")
        self.bot_config = config.BotConfig(
            "https://git.example.org/owner/repo.git",
            "owner/repo",
            "https://git.example.org/api/v1/repos/owner/repo",
        )
        self.prompt_config = core_config.PromptConfig.load()
        self.review_engine = engine.load(
            "current",
            {
                "prompts": {
                    "instructions": self.prompt_config.instructions,
                    "audit_prompts": self.prompt_config.audit_prompts,
                },
                "routing_mode": "enabled",
                "blind_alternatives": True,
            },
        )

    def enqueue(self, head="a" * 40):
        self.jobs.enqueue(
            42,
            "master",
            head,
            "synchronize",
            engine_identity=self.review_engine.identity,
        )
        return self.jobs.claim()

    def process(self, job, **kwargs):
        return service.process_job(
            job,
            self.jobs,
            self.state_dir,
            "api-key",
            "forgejo-token",
            "ralph",
            self.bot_config,
            self.ledger,
            self.review_engine,
            **kwargs,
        )

    def saved_result(
        self,
        content="Review content",
        debug=None,
        *,
        findings=(),
        concept_concern=False,
    ):
        debug = debug or {}
        return {
            "schema_version": 1,
            "engine": self.review_engine.identity,
            "base_sha": "b" * 40,
            "head_sha": "a" * 40,
            "merge_base": "b" * 40,
            "content": content,
            "output": {
                "schema_version": 2,
                "data": {"schema_version": 2, **debug},
                "exports": {
                    "findings": list(findings),
                    "actionable": [
                        *[finding["id"] for finding in findings],
                        *(["concept_objection"] if concept_concern else []),
                    ],
                },
            },
            "run": {
                "review_id": "pr:42:1:1",
                "status": "completed",
                "metrics": {},
                "skipped": bool(debug.get("skip")),
                "usage": {},
                "attempt": debug,
            },
        }

    def post_webhook(self, store):
        secret = b"secret"
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            service.make_handler(
                secret, store, self.bot_config, self.review_engine.identity
            ),
        )
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        try:
            body = json.dumps(
                {
                    "action": "opened",
                    "repository": {
                        "full_name": "owner/repo",
                        "html_url": "https://git.example.org/owner/repo",
                    },
                    "pull_request": {
                        "number": 42,
                        "base": {"ref": "master"},
                        "head": {"sha": "a" * 40},
                    },
                }
            ).encode()
            signature = hmac.new(secret, body, hashlib.sha256).hexdigest()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            connection.request(
                "POST",
                "/webhooks/forgejo",
                body,
                {
                    "Content-Type": "application/json",
                    "X-Forgejo-Event": "pull_request",
                    "X-Forgejo-Signature": "sha256=" + signature,
                },
            )
            response = connection.getresponse()
            response.read()
            connection.close()
            return response.status
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_webhook_202_means_job_is_durable(self):
        self.assertEqual(self.post_webhook(self.jobs), 202)
        restarted = JobStore(self.state_dir / "jobs.sqlite3")
        job = restarted.claim()
        self.assertEqual((job["number"], job["head"]), (42, "a" * 40))

    def test_webhook_rejects_failed_persistence(self):
        with (
            patch.object(
                self.jobs, "enqueue", side_effect=sqlite3.OperationalError("full")
            ),
            self.assertLogs(level="ERROR"),
        ):
            self.assertEqual(self.post_webhook(self.jobs), 503)
        self.assertIsNone(self.jobs.claim())

    def test_publication_retry_reuses_saved_result(self):
        job = self.enqueue()
        with (
            patch.object(service.repository, "current_head", return_value="a" * 40),
            patch.object(service.forgejo, "find_comment", return_value=None),
            patch.object(
                service.forgejo,
                "pull_request_context",
                return_value={"title": "Title", "body": "Description", "labels": []},
            ),
            patch.object(
                service.repository,
                "collect_review",
                return_value=("b" * 40, "a" * 40, "review input", None),
            ),
            patch.object(
                service.local_repository,
                "snapshot_repository",
                return_value=SimpleNamespace(
                    base_sha="b" * 40,
                    head_sha="a" * 40,
                    merge_base="c" * 40,
                    diff_base_sha="c" * 40,
                ),
            ),
            patch.object(
                pipeline,
                "review_with_independent_passes",
                return_value=pipeline.ReviewResult("Review content", [], False),
            ) as review,
            patch.object(
                service.forgejo,
                "publish_review",
                side_effect=urllib.error.URLError("connection lost"),
            ),
        ):
            with self.assertLogs(level="ERROR"):
                self.assertEqual(self.process(job, github_token="read-token"), "retry")
            self.assertEqual(self.evidence.call_args.args[-1], "read-token")
            self.assertEqual(review.call_count, 1)
            self.assertEqual(
                review.call_args.args[1].usage.review_id,
                f"pr:42:{job['id']}:{job['generation']}",
            )

        restarted = JobStore(self.state_dir / "jobs.sqlite3", retry_base_seconds=0)
        retry_job = restarted.claim()
        self.assertEqual(retry_job["review_result"]["content"], "Review content")
        self.assertEqual(retry_job["review_result"]["run"]["pr_title"], "Title")
        self.jobs = restarted
        with (
            patch.object(service.repository, "current_head", return_value="a" * 40),
            patch.object(
                service.runner,
                "run_review",
                side_effect=AssertionError("runner called again"),
            ),
            patch.object(
                pipeline,
                "review_with_independent_passes",
                side_effect=AssertionError("model called again"),
            ),
            patch.object(
                service.forgejo, "publish_review", return_value="unchanged"
            ) as publish,
        ):
            self.assertEqual(self.process(retry_job), "unchanged")
            self.assertEqual(publish.call_count, 1)
            self.assertEqual(publish.call_args.kwargs["source_base_sha"], "c" * 40)
        self.assertIsNone(restarted.claim())

    def test_saved_review_with_changed_labels_is_not_published_or_rerun(self):
        job = self.enqueue()
        result = self.saved_result()
        result["run"]["review_context"] = {
            "title": "Title",
            "body": "Description",
            "labels": ["Backport"],
        }
        self.jobs.save_result(job, result)
        job["review_result"] = result
        with (
            patch.object(repository, "current_head", return_value="a" * 40),
            patch.object(service.runner, "run_review") as run,
            patch.object(forgejo, "publish_review") as publish,
        ):
            self.assertEqual(self.process(job), "stale")
        run.assert_not_called()
        publish.assert_not_called()
        self.assertIsNone(self.jobs.claim())

    def test_github_credentials_do_not_enable_publication(self):
        self.bot_config = replace(self.bot_config, github_repository="owner/repo")
        job = self.enqueue()
        job["review_result"] = self.saved_result()
        self.jobs.save_result(job, job["review_result"])
        with (
            patch.object(repository, "current_head", return_value="a" * 40),
            patch.object(forgejo, "publish_review", return_value="updated"),
            patch.object(service.github, "publish_major_findings") as publish,
        ):
            self.assertEqual(self.process(job, github_token="read-token"), "updated")
        publish.assert_not_called()

    def test_failed_review_delivery_completes_job_and_preserves_outcome(self):
        job = self.enqueue()
        result = self.saved_result("Review failed. Verified minor finding retained.")
        result["run"]["status"] = "failed"
        job["review_result"] = result
        self.jobs.save_result(job, result)
        with (
            patch.object(repository, "current_head", return_value="a" * 40),
            patch.object(forgejo, "publish_review", return_value="updated") as publish,
            self.assertLogs(level="INFO") as logs,
        ):
            self.assertEqual(self.process(job), "updated")
        self.assertIn("Review failed", publish.call_args.args[6])
        self.assertEqual(
            self.jobs.latest_completed_result(42)["run"]["status"], "failed"
        )
        self.assertIsNone(self.jobs.claim())
        self.assertTrue(any("review_status=failed" in line for line in logs.output))

    def test_github_publication_requires_explicit_flag(self):
        self.bot_config = replace(
            self.bot_config,
            github_repository="owner/repo",
            report_dir=self.state_dir / "public",
            report_base_url="https://review.example.org/traces",
        )
        job = self.enqueue()
        job["review_result"] = self.saved_result()
        self.jobs.save_result(job, job["review_result"])
        with (
            patch.object(repository, "current_head", return_value="a" * 40),
            patch.object(forgejo, "publish_review", return_value="updated"),
            patch.object(service.github, "publish_major_findings") as publish,
        ):
            self.assertEqual(
                self.process(
                    job, github_token="write-token", github_post_major_findings=True
                ),
                "updated",
            )
        publish.assert_called_once_with(
            "owner/repo", "write-token", 42, job["review_result"]
        )

    def test_mirror_comment_is_bounded_and_report_keeps_all_findings(self):
        self.bot_config = replace(
            self.bot_config,
            report_dir=self.state_dir / "public",
            report_base_url="https://review.example.org/traces",
        )
        findings = [
            {
                "id": f"finding:{i}",
                "title": f"Issue {i}",
                "severity": "minor",
                "summary": f"Summary {i}.",
                "body": f"Explanation {i}.",
                "path": "src/test.cpp",
                "line": i + 1,
                "side": "head",
            }
            for i in range(8)
        ]
        full_review = "\n\n".join(finding["body"] for finding in findings)
        job = self.enqueue()
        result = self.saved_result(full_review, findings=findings)
        result["output"]["exports"]["review_memory"] = [
            {
                "key": "previous:1",
                "title": "Old claim",
                "body": "Old explanation",
                "severity": "major",
                "status": "still_present",
                "checked_head": "a" * 40,
                "reason": "The earlier failure remains reachable.",
                "source_evidence": [
                    {"path": "current.cpp", "line": 17, "side": "head"}
                ],
            }
        ]
        job["review_result"] = result
        self.jobs.save_result(job, result)
        with (
            patch.object(repository, "current_head", return_value="a" * 40),
            patch.object(forgejo, "publish_review", return_value="updated") as publish,
            patch.object(
                service.runner, "run_review", side_effect=AssertionError("paid rerun")
            ),
        ):
            self.assertEqual(self.process(job), "updated")
        comment = publish.call_args.args[6]
        self.assertIn("Summary 4.", comment)
        self.assertNotIn("Summary 5.", comment)
        self.assertNotIn("Summary 6.", comment)
        self.assertIn("Old claim", comment)
        self.assertIn("current.cpp:17", comment)
        self.assertNotIn("Old explanation", comment)
        self.assertIn("3 additional verified findings", comment)
        self.assertIn("https://review.example.org/traces/", comment)
        reports = list(self.bot_config.report_dir.glob("*.json"))
        self.assertEqual(len(reports), 1)
        report = json.loads(reports[0].read_text())
        self.assertEqual(report["review"], full_review)
        self.assertEqual(len(report["findings"]), 8)
        self.assertEqual(report["review_memory"][0]["title"], "Old claim")

    def test_stale_forgejo_result_never_publishes_to_github(self):
        self.bot_config = replace(
            self.bot_config,
            report_dir=self.state_dir / "public",
            report_base_url="https://review.example.org/traces",
            github_repository="owner/repo",
        )
        job = self.enqueue()
        job["review_result"] = self.saved_result()
        self.jobs.save_result(job, job["review_result"])
        with (
            patch.object(service.repository, "current_head", return_value="a" * 40),
            patch.object(service.forgejo, "publish_review", return_value="stale"),
            patch.object(service.github, "publish_major_findings") as publish,
        ):
            self.assertEqual(
                self.process(
                    job, github_token="write-token", github_post_major_findings=True
                ),
                "stale",
            )
        publish.assert_not_called()

    def test_stale_review_keeps_newer_pending_head(self):
        old = self.enqueue()

        def stale_review(*_args, **_kwargs):
            self.jobs.enqueue(
                42,
                "master",
                "b" * 40,
                "synchronize",
                engine_identity=self.review_engine.identity,
            )
            raise model.StaleReview("new head")

        with (
            patch.object(service.repository, "current_head", return_value="a" * 40),
            patch.object(service.forgejo, "find_comment", return_value=None),
            patch.object(
                service.forgejo,
                "pull_request_context",
                return_value={"title": "Title", "body": "Description", "labels": []},
            ),
            patch.object(
                service.repository,
                "collect_review",
                return_value=("c" * 40, "a" * 40, "input", None),
            ),
            patch.object(
                service.local_repository,
                "snapshot_repository",
                return_value=SimpleNamespace(
                    base_sha="b" * 40,
                    head_sha="a" * 40,
                    merge_base="c" * 40,
                    diff_base_sha="c" * 40,
                ),
            ),
            patch.object(
                pipeline, "review_with_independent_passes", side_effect=stale_review
            ),
        ):
            self.assertEqual(self.process(old), "stale")
        self.assertEqual(self.jobs.claim()["head"], "b" * 40)

    def test_worker_restarts_unfinished_review_with_saved_configuration(self):
        previous = {
            **self.review_engine.identity,
            "code_sha256": "old code",
            "config_sha256": "old configuration",
            "config": self.review_engine.config,
        }
        self.jobs.enqueue(42, "master", "a" * 40, "opened", engine_identity=previous)
        interrupted = self.jobs.claim()
        self.assertEqual(self.jobs.recover(), 1)
        stop_event = threading.Event()

        def review(*_args, comment_format):
            with closing(sqlite3.connect(self.state_dir / "jobs.sqlite3")) as db:
                stored = json.loads(
                    db.execute(
                        "SELECT engine_identity FROM jobs WHERE id = ?",
                        (interrupted["id"],),
                    ).fetchone()[0]
                )
            self.assertEqual(stored, self.review_engine.identity)
            return pipeline.ReviewResult("Updated review", [], False)

        def publish(*_args, **_kwargs):
            stop_event.set()
            return "unchanged"

        with (
            patch.object(service.repository, "current_head", return_value="a" * 40),
            patch.object(service.forgejo, "find_comment", return_value=None),
            patch.object(
                service.forgejo,
                "pull_request_context",
                return_value={"title": "Title", "body": "Description", "labels": []},
            ),
            patch.object(
                service.repository,
                "collect_review",
                return_value=("b" * 40, "a" * 40, "review input", None),
            ),
            patch.object(
                service.local_repository,
                "snapshot_repository",
                return_value=SimpleNamespace(
                    base_sha="b" * 40,
                    head_sha="a" * 40,
                    merge_base="c" * 40,
                    diff_base_sha="c" * 40,
                ),
            ),
            patch.object(
                pipeline, "review_with_independent_passes", side_effect=review
            ) as reviewed,
            patch.object(service.forgejo, "publish_review", side_effect=publish),
            patch.object(stop_event, "wait", side_effect=lambda _: stop_event.set()),
        ):
            service.worker(
                self.jobs,
                self.state_dir,
                "api-key",
                "forgejo-token",
                "ralph",
                self.bot_config,
                self.ledger,
                self.review_engine,
                stop_event=stop_event,
            )
        reviewed.assert_called_once()
        self.assertEqual(reviewed.call_args.args[3], "enabled")
        saved = self.jobs.latest_completed_result(42)
        self.assertEqual(saved["content"], "Updated review")
        self.assertEqual(saved["engine"], self.review_engine.identity)
        with closing(sqlite3.connect(self.state_dir / "jobs.sqlite3")) as db:
            stored = json.loads(
                db.execute(
                    "SELECT engine_identity FROM jobs WHERE id = ?",
                    (interrupted["id"],),
                ).fetchone()[0]
            )
        self.assertEqual(stored, self.review_engine.identity)
        self.assertFalse(self.jobs.complete(interrupted))


def payload(action="opened", head="a" * 40):
    return {
        "action": action,
        "repository": {
            "full_name": "bitcoin/bitcoin",
            "html_url": "https://git.fish.foo/bitcoin/bitcoin",
        },
        "pull_request": {
            "number": 42,
            "head": {"sha": head},
            "base": {"ref": "master"},
        },
    }


class CapturingJobs:
    def __init__(self):
        self.enqueued = []

    def enqueue(self, *job, engine_identity):
        self.enqueued.append(job)
        self.engine_identity = engine_identity
        return True


class WebhookTests(unittest.TestCase):
    def setUp(self):
        context = patch.object(
            forgejo,
            "pull_request_context",
            return_value={
                "title": "Title",
                "body": "Description",
                "labels": [],
            },
        )
        context.start()
        self.addCleanup(context.stop)
        evidence = patch.object(
            repository, "collect_backport_evidence", return_value=None
        )
        self.evidence = evidence.start()
        self.addCleanup(evidence.stop)
        self.bot_config = config.BotConfig(
            "https://git.fish.foo/bitcoin/bitcoin.git",
            "bitcoin/bitcoin",
            "https://git.fish.foo/api/v1/repos/bitcoin/bitcoin",
        )
        self.prompt_config = core_config.PromptConfig.load()
        self.review_engine = engine.load(
            "current",
            {
                "prompts": {
                    "instructions": self.prompt_config.instructions,
                    "audit_prompts": self.prompt_config.audit_prompts,
                },
                "routing_mode": "enabled",
                "blind_alternatives": True,
            },
        )

    def snapshot(self, files=None, checkout=Path("/unused")):
        return local_repository.RepositorySnapshot(
            checkout,
            "b" * 40,
            "a" * 40,
            "b" * 40,
            "b" * 40,
            files or {},
            {},
            frozenset(),
        )

    def queued_job(self, *, force=False):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state_dir = Path(directory.name)
        jobs = JobStore(state_dir / "jobs.sqlite3")
        jobs.enqueue(
            42,
            "master",
            "a" * 40,
            "opened",
            force,
            engine_identity=self.review_engine.identity,
        )
        ledger = usage.Ledger(state_dir / "usage.sqlite3")
        return jobs.claim(), jobs, state_dir, ledger

    def test_badge_keeps_unresolved_previous_finding_visible(self):
        result = {
            "schema_version": 1,
            "output": {
                "schema_version": 1,
                "exports": {
                    "findings": [],
                    "review_memory": [{"status": "unclear", "severity": "major"}],
                },
            },
        }
        jobs = SimpleNamespace(latest_completed_result=lambda number: result)
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), service.make_handler(b"secret", jobs, self.bot_config)
        )
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/assets/{self.bot_config.repository}/42.png"
            with urllib.request.urlopen(url) as response:
                self.assertEqual(response.read(), service.badge.ORANGE)
            result["output"]["exports"]["review_memory"][0]["status"] = "fixed"
            with urllib.request.urlopen(url) as response:
                self.assertEqual(response.read(), service.badge.GREY)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_webhook_queues_only_authenticated_target_event(self):
        jobs = CapturingJobs()
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            service.make_handler(
                b"secret", jobs, self.bot_config, self.review_engine.identity
            ),
        )
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            body = json.dumps(payload()).encode()
            sig = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
            url = f"http://127.0.0.1:{server.server_port}/webhooks/forgejo"
            request = urllib.request.Request(
                url,
                body,
                headers={"X-Forgejo-Signature": sig, "X-Forgejo-Event": "pull_request"},
            )
            with self.assertLogs(level="INFO") as logs:
                self.assertEqual(urllib.request.urlopen(request).status, 202)
            self.assertEqual(
                jobs.enqueued[0], (42, "master", "a" * 40, "opened", False)
            )
            self.assertIn(
                "review enqueue pr=42 action=opened head=aaaaaaaaaaaa",
                "\n".join(logs.output),
            )
            forced = payload()
            forced["ralph_force"] = True
            body = json.dumps(forced).encode()
            sig = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
            request = urllib.request.Request(
                url,
                body,
                headers={"X-Forgejo-Signature": sig, "X-Forgejo-Event": "pull_request"},
            )
            self.assertEqual(urllib.request.urlopen(request).status, 202)
            self.assertEqual(jobs.enqueued[1], (42, "master", "a" * 40, "opened", True))
            request.headers["X-Forgejo-Signature"] = "bad"
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 401)
            self.assertEqual(len(jobs.enqueued), 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_force_rechecks_same_head_and_updates_existing_comment(self):
        job, jobs, state_dir, ledger = self.queued_job(force=True)
        existing = {
            "body": forgejo.review_body(
                self.bot_config,
                "b" * 40,
                "a" * 40,
                "Old review.",
                source_base_sha="c" * 40,
                engine_identity=self.review_engine.identity,
            )
        }
        with (
            patch.object(repository, "current_head", return_value="a" * 40),
            patch.object(forgejo, "find_comment", return_value=existing) as find,
            patch.object(
                forgejo,
                "pull_request_context",
                return_value={"title": "Title", "body": "Body", "labels": []},
            ),
            patch.object(
                repository,
                "collect_review",
                return_value=("b" * 40, "a" * 40, "review input", None),
            ),
            patch.object(
                local_repository, "snapshot_repository", return_value=self.snapshot()
            ),
            patch.object(
                pipeline,
                "review_with_independent_passes",
                return_value=pipeline.ReviewResult("New review.", [], False),
            ) as review_model,
            patch.object(forgejo, "publish_review", return_value="updated") as publish,
        ):
            self.assertEqual(
                service.process_job(
                    job,
                    jobs,
                    state_dir,
                    "openai-key",
                    "forgejo-token",
                    "ralph",
                    self.bot_config,
                    ledger,
                    self.review_engine,
                ),
                "updated",
            )
        find.assert_not_called()
        review_model.assert_called_once()
        self.assertEqual(review_model.call_args.args[0].review, "review input")
        publish.assert_called_once()

    def test_comment_links_and_full_revision_deduplication(self):
        base, head = "b" * 40, "a" * 40
        body = forgejo.review_body(
            self.bot_config,
            base,
            head,
            "Finding\n\nLocation: `src/a b.cpp:12` (head).\n\n"
            "Location: src/removed.cpp:8 (base).",
            source_base_sha="c" * 40,
            engine_identity=self.review_engine.identity,
            review_fingerprint="case",
            report_url="https://review.example.org/traces/report.html",
            history_url="https://review.example.org/traces/42/",
        )
        self.assertIn(f"/src/commit/{head}/src/a%20b.cpp#L12", body)
        self.assertIn(f"/src/commit/{'c' * 40}/src/removed.cpp#L8", body)
        self.assertIn(f"Reviewed: [`{head[:12]}`]", body)
        self.assertLess(body.index("Finding"), body.index("Reviewed:"))
        self.assertIn(
            "[Previous reports](<https://review.example.org/traces/42/>)", body
        )
        self.assertIn(
            "[Full review report](<https://review.example.org/traces/report.html>)",
            body,
        )
        comment = {"body": body}
        self.assertTrue(forgejo.comment_matches_head(comment, head))
        self.assertTrue(
            forgejo.comment_matches_engine(
                comment, base, head, self.review_engine.identity, "case"
            )
        )
        changed_head = head[:12] + "c" * 28
        self.assertFalse(forgejo.comment_matches_head(comment, changed_head))
        self.assertFalse(
            forgejo.comment_matches_engine(
                comment,
                base[:12] + "c" * 28,
                head,
                self.review_engine.identity,
                "case",
            )
        )

    def test_case_marker_distinguishes_changed_metadata_and_evidence(self):
        metadata = {"title": "Backports", "body": "Backport #1", "labels": ["Backport"]}
        fingerprint = service.review_case_fingerprint(metadata, {"complete": False})
        comment = {
            "body": forgejo.review_body(
                self.bot_config,
                "b" * 40,
                "a" * 40,
                "Review",
                source_base_sha="c" * 40,
                engine_identity=self.review_engine.identity,
                review_fingerprint=fingerprint,
            )
        }
        for changed_metadata, evidence in (
            ({**metadata, "labels": []}, {"complete": False}),
            (metadata, {"complete": True}),
        ):
            self.assertFalse(
                forgejo.comment_matches_engine(
                    comment,
                    "b" * 40,
                    "a" * 40,
                    self.review_engine.identity,
                    service.review_case_fingerprint(changed_metadata, evidence),
                )
            )

    def test_repeated_head_skips_model_call(self):
        job, jobs, state_dir, ledger = self.queued_job()
        existing = {
            "body": forgejo.review_body(
                self.bot_config,
                "b" * 40,
                "a" * 40,
                "Reviewed.",
                source_base_sha="c" * 40,
                engine_identity=self.review_engine.identity,
                review_fingerprint=service.review_case_fingerprint(
                    {
                        "title": "Title",
                        "body": "Body",
                        "labels": [],
                        "base_ref": "master",
                        "repository": "bitcoin/bitcoin",
                    },
                    None,
                ),
            )
        }
        with (
            patch.object(service, "collect_review_feedback") as feedback,
            patch.object(repository, "current_head", return_value="a" * 40),
            patch.object(forgejo, "find_comment", return_value=existing),
            patch.object(
                forgejo,
                "pull_request_context",
                return_value={"title": "Title", "body": "Body", "labels": []},
            ),
            patch.object(
                repository,
                "collect_review",
                return_value=("b" * 40, "a" * 40, "review input", None),
            ) as collect,
            patch.object(local_repository, "snapshot_repository") as snapshot,
            patch.object(pipeline, "review_with_independent_passes") as review_model,
        ):
            self.assertEqual(
                service.process_job(
                    job,
                    jobs,
                    state_dir,
                    "openai-key",
                    "forgejo-token",
                    "ralph",
                    self.bot_config,
                    ledger,
                    self.review_engine,
                ),
                "already-reviewed",
            )
        collect.assert_called_once()
        snapshot.assert_called_once()
        review_model.assert_not_called()
        feedback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
