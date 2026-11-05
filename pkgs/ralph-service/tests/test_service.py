import hashlib
import hmac
import http.client
import json
import signal
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, patch

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
                "base_ref": "master",
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
                "profile": self.prompt_config.profile.to_dict(),
                "high_profile": self.prompt_config.high_profile.to_dict(),
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
                "review_context": {
                    "title": "Title",
                    "body": "Description",
                    "labels": [],
                    "base_ref": "master",
                },
                "review_id": "pr:42:1:1",
                "status": "completed",
                "metrics": {},
                "skipped": bool(debug.get("skip")),
                "usage": {},
                "attempt": debug,
            },
        }

    def post_webhook(self, *, force=False, signature=None):
        secret = b"secret"
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            service.make_handler(
                secret, self.jobs, self.bot_config, self.review_engine.identity
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
                    "ralph_force": force,
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
            signature = signature or hmac.new(secret, body, hashlib.sha256).hexdigest()
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

    def test_authenticated_webhook_is_durable_and_can_force_a_review(self):
        self.assertEqual(self.post_webhook(signature="bad"), 401)
        self.assertIsNone(self.jobs.claim())
        self.assertEqual(self.post_webhook(), 202)
        restarted = JobStore(self.state_dir / "jobs.sqlite3")
        self.assertIsNone(restarted.claim())
        pr = {
            "number": 42,
            "state": "open",
            "base": {"ref": "master"},
            "head": {"sha": "a" * 40},
        }
        with patch.object(forgejo, "forgejo_request", return_value=pr):
            service.reconcile_next(restarted, self.bot_config, "token")
        job = restarted.claim()
        self.assertEqual(
            (job["number"], job["base_ref"], job["head"], job["action"], job["force"]),
            (42, "master", "a" * 40, "opened", False),
        )
        restarted.complete(job)
        self.assertEqual(self.post_webhook(force=True), 202)
        with patch.object(forgejo, "forgejo_request", return_value=pr):
            service.reconcile_next(restarted, self.bot_config, "token")
        job = restarted.claim()
        self.assertTrue(job["force"])
        restarted.complete(job)
        self.assertEqual(self.post_webhook(force=True, signature="bad"), 401)
        self.assertIsNone(restarted.claim())

    def test_webhook_rejects_failed_persistence(self):
        with (
            patch.object(
                self.jobs,
                "reconcile_hint",
                side_effect=sqlite3.OperationalError("full"),
            ),
            self.assertLogs(level="ERROR"),
        ):
            self.assertEqual(self.post_webhook(), 503)
        self.assertIsNone(self.jobs.claim())

    def test_fetch_failure_retries_without_models_and_stops_at_attempt_cap(self):
        job = self.enqueue()

        def git(checkout, *args):
            if args[0] == "fetch":
                raise subprocess.CalledProcessError(
                    1,
                    ["git", *args],
                    stderr="fatal: unable to access https://user:credential@example.org/repo: HTTP 503",
                )
            return "false"

        with (
            patch.object(repository, "current_head", return_value="a" * 40) as head,
            patch.object(forgejo, "find_comment", return_value=None),
            patch.object(repository, "prepare_checkout"),
            patch.object(repository.local, "git", side_effect=git),
            patch.object(service.runner, "run_review") as run,
            self.assertLogs(level="WARNING") as logs,
        ):
            for expected in ("retry", "retry", "failed"):
                self.assertEqual(self.process(job), expected)
                self.jobs = JobStore(self.jobs.path, retry_base_seconds=0)
                job = self.jobs.claim()
            self.assertIsNone(job)
        run.assert_not_called()
        self.assertEqual(head.call_count, 3)
        with self.jobs._connection() as db:
            row = db.execute(
                "SELECT attempts, error, review_result FROM jobs"
            ).fetchone()
        self.assertEqual(row["attempts"], 3)
        self.assertIsNone(row["review_result"])
        self.assertIn("HTTP 503", row["error"])
        self.assertNotIn("credential", row["error"])
        self.assertIn("HTTP 503", "\n".join(logs.output))
        self.assertNotIn("credential", "\n".join(logs.output))
        self.assertFalse(
            service.retryable_error(subprocess.CalledProcessError(1, ["git", "diff"]))
        )

    def test_full_terminal_fetch_failure_publishes_status_after_retries(self):
        self.bot_config = replace(
            self.bot_config,
            github_repository="owner/repo",
            report_dir=self.state_dir / "public",
            report_base_url="https://review.example/traces",
        )
        job = self.enqueue()
        with (
            patch.object(repository, "current_head", return_value="a" * 40),
            patch.object(
                forgejo,
                "pull_request_context",
                side_effect=TimeoutError("credential=private"),
            ),
            patch.object(service.runner, "run_review") as review,
            patch.object(
                service.github, "publish_review", return_value="updated"
            ) as publish,
            self.assertLogs(level="ERROR"),
        ):
            for _ in range(self.jobs.max_attempts - 1):
                self.assertEqual(
                    self.process(job, github_token="token", github_publication="full"),
                    "retry",
                )
                publish.assert_not_called()
                job = self.jobs.claim()
            # The upstream/mirror read recovers for publication after the final
            # collection failure, so only the saved failed result is published.
            with patch.object(
                forgejo,
                "pull_request_context",
                side_effect=[
                    TimeoutError("credential=private"),
                    {"base_ref": "master"},
                    {"base_ref": "master"},
                ],
            ):
                self.assertEqual(
                    self.process(job, github_token="token", github_publication="full"),
                    "updated",
                )
        review.assert_not_called()
        publish.assert_called_once()
        saved = publish.call_args.args[3]
        self.assertEqual(saved["run"]["status"], "failed")
        self.assertIsNone(saved["base_sha"])
        self.assertIsNone(saved["merge_base"])
        self.assertNotIn("credential=private", json.dumps(saved))
        self.assertTrue(saved["run"]["report_url"])
        self.assertEqual(len(list(self.bot_config.report_dir.glob("*.json"))), 1)
        self.assertIsNotNone(self.jobs.latest_completed_result(42))

    def test_full_terminal_failure_without_comment_finishes_without_posting(self):
        self.bot_config = replace(
            self.bot_config,
            github_repository="owner/repo",
            report_dir=self.state_dir / "public",
            report_base_url="https://review.example/traces",
        )
        job = self.enqueue()
        with (
            patch.object(repository, "current_head", return_value="a" * 40),
            patch.object(
                repository, "collect_review", side_effect=ValueError("broken")
            ),
            patch.object(service.runner, "run_review") as review,
            patch.object(
                service.github, "github_request", side_effect=[{"id": 7}, []]
            ) as request,
            self.assertLogs(level="WARNING"),
        ):
            self.assertEqual(
                self.process(job, github_token="token", github_publication="full"),
                "failed",
            )
        review.assert_not_called()
        self.assertEqual(request.call_count, 2)
        self.assertIsNone(self.jobs.claim())
        self.assertEqual(
            self.jobs.latest_completed_result(42)["run"]["status"], "failed"
        )
        self.assertEqual(len(list(self.bot_config.report_dir.glob("*.json"))), 1)

    def test_full_terminal_collection_failure_publishes_safe_status(self):
        self.bot_config = replace(
            self.bot_config,
            github_repository="owner/repo",
            report_dir=self.state_dir / "public",
            report_base_url="https://review.example/traces",
        )
        job = self.enqueue()
        with (
            patch.object(repository, "current_head", return_value="a" * 40),
            patch.object(
                repository,
                "collect_review",
                side_effect=ValueError(
                    "configured submodule bitcoin has no gitlink credential=private"
                ),
            ),
            patch.object(service.runner, "run_review") as review,
            patch.object(
                service.github, "publish_review", return_value="updated"
            ) as publish,
            self.assertLogs(level="WARNING") as logs,
        ):
            self.assertEqual(
                self.process(job, github_token="token", github_publication="full"),
                "updated",
            )
        review.assert_not_called()
        publish.assert_called_once()
        saved = publish.call_args.args[3]
        self.assertEqual(saved["run"]["status"], "failed")
        self.assertIsNone(saved["base_sha"])
        self.assertIsNone(saved["merge_base"])
        self.assertNotIn("credential=private", json.dumps(saved))
        self.assertIn("stage=collect error_type=ValueError", "\n".join(logs.output))
        self.assertNotIn("credential=private", "\n".join(logs.output))
        self.assertEqual(len(list(self.bot_config.report_dir.glob("*.json"))), 1)

    def test_full_publication_failure_does_not_replace_completed_review(self):
        self.bot_config = replace(self.bot_config, github_repository="owner/repo")
        job = self.enqueue()
        result = self.saved_result()
        self.jobs.save_result(job, result)
        job["review_result"] = result
        with (
            patch.object(repository, "current_head", return_value="a" * 40),
            patch.object(
                service.github, "publish_review", side_effect=TimeoutError()
            ) as publish,
            patch.object(service.runner, "run_review") as review,
            self.assertLogs(level="ERROR"),
        ):
            for expected in ("retry", "retry", "failed"):
                self.assertEqual(
                    self.process(job, github_token="token", github_publication="full"),
                    expected,
                )
                job = self.jobs.claim()
        review.assert_not_called()
        self.assertEqual(publish.call_count, 3)
        self.assertTrue(
            all(
                call.args[3]["run"]["status"] == "completed"
                for call in publish.call_args_list
            )
        )

    def test_uncertain_github_reconciles_after_restart_without_models(self):
        from ralph_service import delivery
        from unittest.mock import Mock

        self.bot_config = replace(self.bot_config, github_repository="owner/repo")
        job = self.enqueue()
        self.jobs.save_result(job, self.saved_result())
        pending = delivery.Session(self.jobs, job, "github:owner/repo")
        with self.assertRaises(delivery.DeliveryHeld):
            pending.execute(
                "POST",
                "/repos/owner/repo/issues/42/comments",
                "<!-- ralph-major:owner/repo -->\nsaved",
                None,
                Mock(side_effect=TimeoutError()),
                lambda: True,
            )
        self.jobs = JobStore(self.jobs.path, retry_base_seconds=0)
        self.jobs.recover()
        job = self.jobs.claim()
        with (
            patch.object(repository, "current_head", return_value="a" * 40),
            patch.object(service.runner, "run_review") as run,
            patch.object(
                forgejo, "publish_review", return_value="unchanged"
            ) as publish,
            patch.object(service.github, "publish_review", return_value="unchanged"),
            patch.object(service.github, "github_request", side_effect=[{"id": 7}, []]),
            self.assertLogs(level="ERROR"),
        ):
            self.assertEqual(
                self.process(job, github_token="token", github_publication="full"),
                "retry",
            )
        run.assert_not_called()
        publish.assert_not_called()
        job = self.jobs.claim()
        with (
            patch.object(repository, "current_head", return_value="a" * 40),
            patch.object(service.runner, "run_review") as run,
            patch.object(
                forgejo, "publish_review", return_value="unchanged"
            ) as publish,
            patch.object(service.github, "publish_review", return_value="unchanged"),
            patch.object(
                service.github,
                "github_request",
                side_effect=[
                    {"id": 7},
                    [
                        {
                            "id": 9,
                            "user": {"id": 7},
                            "body": "<!-- ralph-major:owner/repo -->\nsaved",
                        }
                    ],
                ],
            ),
        ):
            self.assertEqual(
                self.process(job, github_token="token", github_publication="full"),
                "unchanged",
            )
        run.assert_not_called()
        publish.assert_not_called()
        self.assertEqual(delivery.held(self.jobs), [])

    def test_publication_retry_reuses_saved_result(self):
        job = self.enqueue()
        with (
            patch.object(service.repository, "current_head", return_value="a" * 40),
            patch.object(service.forgejo, "find_comment", return_value=None),
            patch.object(
                service.forgejo,
                "pull_request_context",
                return_value={
                    "title": "Title",
                    "body": "Description",
                    "labels": [],
                    "base_ref": "master",
                },
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
                f"pr:core:42:{job['id']}:{job['generation']}",
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

    def test_saved_review_with_changed_metadata_is_published_without_rerun(self):
        job = self.enqueue()
        result = self.saved_result()
        result["run"]["review_context"] = {
            "title": "Old title",
            "body": "Old description",
            "labels": ["Backport"],
            "base_ref": "master",
        }
        self.jobs.save_result(job, result)
        job["review_result"] = result
        with (
            patch.object(repository, "current_head", return_value="a" * 40),
            patch.object(service.runner, "run_review") as run,
            patch.object(forgejo, "publish_review", return_value="updated") as publish,
        ):
            self.assertEqual(self.process(job), "updated")
        run.assert_not_called()
        publish.assert_called_once()
        self.assertIsNone(self.jobs.claim())

    def test_review_stages_detect_new_work_without_contacting_the_source(self):
        job = self.enqueue()

        def review(case, runtime, review_engine):
            source_reads = head.call_count, forgejo.pull_request_context.call_count
            runtime.check_current()
            self.assertEqual(
                (head.call_count, forgejo.pull_request_context.call_count),
                source_reads,
            )
            self.jobs.enqueue(
                42,
                "master",
                "c" * 40,
                "synchronize",
                engine_identity=self.review_engine.identity,
            )
            runtime.check_current()

        with (
            patch.object(repository, "current_head", return_value="a" * 40) as head,
            patch.object(forgejo, "find_comment", return_value=None),
            patch.object(
                repository,
                "collect_review",
                return_value=("b" * 40, "a" * 40, "review", None),
            ),
            patch.object(
                service.local_repository,
                "snapshot_repository",
                return_value=SimpleNamespace(merge_base="b" * 40),
            ),
            patch.object(service, "collect_review_feedback", return_value=None),
            patch.object(service.runner, "run_review", side_effect=review) as run,
            patch.object(forgejo, "publish_review") as publish,
        ):
            self.assertEqual(self.process(job), "stale")
        run.assert_called_once()
        publish.assert_not_called()

    def test_saved_review_without_target_identity_requires_recapture(self):
        job = self.enqueue()
        result = self.saved_result()
        del result["run"]["review_context"]["base_ref"]
        self.jobs.save_result(job, result)
        job["review_result"] = result
        with (
            patch.object(service.runner, "run_review") as run,
            patch.object(forgejo, "publish_review") as publish,
        ):
            self.assertEqual(self.process(job), "failed")
        run.assert_not_called()
        publish.assert_not_called()
        self.assertIsNone(self.jobs.claim())

    def test_retargeted_pr_is_rejected_before_analysis(self):
        job = self.enqueue()
        with (
            patch.object(repository, "current_head", return_value="a" * 40),
            patch.object(forgejo, "find_comment", return_value=None),
            patch.object(
                forgejo,
                "pull_request_context",
                return_value={
                    "title": "Title",
                    "body": "Description",
                    "labels": [],
                    "base_ref": "other",
                },
            ),
            patch.object(service.runner, "run_review") as run,
            patch.object(forgejo, "publish_review") as publish,
        ):
            self.assertEqual(self.process(job), "stale")
        run.assert_not_called()
        publish.assert_not_called()

    def test_github_credentials_do_not_enable_publication(self):
        self.bot_config = replace(self.bot_config, github_repository="owner/repo")
        job = self.enqueue()
        job["review_result"] = self.saved_result()
        self.jobs.save_result(job, job["review_result"])
        with (
            patch.object(repository, "current_head", return_value="a" * 40),
            patch.object(forgejo, "publish_review", return_value="updated"),
            patch.object(service.github, "publish_review") as publish,
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

    def test_github_publication_requires_explicit_mode(self):
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
            patch.object(service.github, "publish_review") as publish,
        ):
            self.assertEqual(
                self.process(
                    job, github_token="write-token", github_publication="major-only"
                ),
                "updated",
            )
        publish.assert_called_once_with(
            "owner/repo",
            "write-token",
            42,
            job["review_result"],
            publication="major-only",
            expected_context=job["review_result"]["run"]["review_context"],
            is_current=ANY,
            delivery=ANY,
        )

    def test_full_github_review_does_not_use_old_forgejo_comment(self):
        self.bot_config = replace(self.bot_config, github_repository="owner/repo")
        job = self.enqueue()
        result = self.saved_result()
        result["diff_base_sha"] = result["merge_base"]
        with (
            patch.object(repository, "current_head", return_value="a" * 40),
            patch.object(forgejo, "find_comment") as find,
            patch.object(
                repository,
                "collect_review",
                return_value=("b" * 40, "a" * 40, "input", None),
            ),
            patch.object(
                local_repository,
                "snapshot_repository",
                return_value=SimpleNamespace(merge_base="c" * 40),
            ),
            patch.object(service, "collect_review_feedback", return_value=None),
            patch.object(service.runner, "run_review", return_value=result) as review,
            patch.object(forgejo, "publish_review") as forgejo_publish,
            patch.object(service.github, "publish_review", return_value="updated"),
        ):
            self.assertEqual(
                self.process(
                    job, github_token="write-token", github_publication="full"
                ),
                "updated",
            )
        find.assert_not_called()
        review.assert_called_once()
        forgejo_publish.assert_not_called()

    def test_full_github_publication_omits_forgejo_and_keeps_report(self):
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
            patch.object(forgejo, "publish_review") as forgejo_publish,
            patch.object(
                service.github, "publish_review", return_value="created"
            ) as publish,
            patch.object(
                service.runner, "run_review", side_effect=AssertionError("paid rerun")
            ),
        ):
            self.assertEqual(
                self.process(
                    job, github_token="write-token", github_publication="full"
                ),
                "created",
            )
        forgejo_publish.assert_not_called()
        publish.assert_called_once()
        self.assertEqual(publish.call_args.kwargs["publication"], "full")
        self.assertEqual(len(list(self.bot_config.report_dir.glob("*.json"))), 1)
        self.assertIsNotNone(self.jobs.latest_completed_result(42))
        self.assertIsNone(self.jobs.claim())

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
        self.assertIn(findings[0]["summary"], comment)
        self.assertNotIn(findings[-1]["summary"], comment)
        self.assertNotIn(findings[0]["body"], comment)
        self.assertIn("https://review.example.org/traces/", comment)
        reports = list(self.bot_config.report_dir.glob("*.json"))
        self.assertEqual(len(reports), 1)
        report = json.loads(reports[0].read_text())
        self.assertEqual(report["review"], full_review)
        self.assertEqual(len(report["findings"]), 8)

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
            patch.object(service.github, "publish_review") as publish,
        ):
            self.assertEqual(
                self.process(
                    job, github_token="write-token", github_publication="major-only"
                ),
                "stale",
            )
        publish.assert_not_called()

    def test_stale_github_publication_does_not_complete_review(self):
        self.bot_config = replace(self.bot_config, github_repository="owner/repo")
        job = self.enqueue()
        job["review_result"] = self.saved_result()
        self.jobs.save_result(job, job["review_result"])
        with (
            patch.object(repository, "current_head", return_value="a" * 40),
            patch.object(forgejo, "publish_review", return_value="updated"),
            patch.object(service.github, "publish_review", return_value="stale"),
        ):
            self.assertEqual(
                self.process(
                    job, github_token="write-token", github_publication="full"
                ),
                "stale",
            )
        self.assertIsNone(self.jobs.latest_completed_result(42))
        self.assertIsNotNone(self.jobs.claim_reconciliation())
        self.assertFalse(self.jobs.is_current(job))

    def test_stale_review_keeps_newer_pending_head(self):
        old = self.enqueue()

        def stale_review(_case, runtime, *_args, **_kwargs):
            calls = (remote_head.call_count, remote_context.call_count)
            runtime.check_cancelled()
            self.assertEqual((remote_head.call_count, remote_context.call_count), calls)
            self.jobs.enqueue(
                42,
                "master",
                "b" * 40,
                "synchronize",
                engine_identity=self.review_engine.identity,
            )
            with self.assertRaises(model.StaleReview):
                runtime.check_cancelled()
            self.assertEqual((remote_head.call_count, remote_context.call_count), calls)
            runtime.check_cancelled()

        with (
            patch.object(
                service.repository, "current_head", return_value="a" * 40
            ) as remote_head,
            patch.object(service.forgejo, "find_comment", return_value=None),
            patch.object(
                service.forgejo,
                "pull_request_context",
                return_value={
                    "title": "Title",
                    "body": "Description",
                    "labels": [],
                    "base_ref": "master",
                },
            ) as remote_context,
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
                return_value={
                    "title": "Title",
                    "body": "Description",
                    "labels": [],
                    "base_ref": "master",
                },
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
                "api-key",
                self.ledger,
                {
                    "core": {
                        "state_dir": self.state_dir,
                        "forgejo_token": "forgejo-token",
                        "entry": {"bot_login": "ralph"},
                        "bot": self.bot_config,
                        "engine": self.review_engine,
                        "github_token": None,
                    }
                },
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

    def test_shutdown_drains_running_review_while_webhooks_keep_queueing(self):
        child = textwrap.dedent("""
            import sys
            import threading
            import time
            from http.server import ThreadingHTTPServer
            from pathlib import Path
            from types import SimpleNamespace
            from ralph_service import config, coordinator, service
            from ralph_service.jobs import JobStore

            root = Path(sys.argv[1])
            jobs = JobStore(root / "jobs.sqlite3")
            bot = config.BotConfig(
                "https://git.example.org/owner/repo.git", "owner/repo",
                "https://git.example.org/api/v1/repos/owner/repo",
            )

            class RecordedEvent(threading.Event):
                def __init__(self, name):
                    super().__init__()
                    self.name = name

                def set(self):
                    super().set()
                    (root / self.name).touch()

            class ReadyServer(ThreadingHTTPServer):
                def service_actions(self):
                    (root / "ready").touch()

            def process(job, jobs, *args):
                (root / "started").touch()
                while not (root / "finish").exists():
                    time.sleep(0.01)
                assert jobs.is_current(job)
                assert jobs.save_result(job, {"content": "Finished review"})
                (root / "published").touch()
                jobs.complete(job)

            service.process_job = process
            service.consume_review_requests = lambda *args: None
            drain = RecordedEvent("draining")
            commands_stop = RecordedEvent("commands-stopped")
            review_engine = SimpleNamespace(identity={"name": "test"})
            contexts = {"core": {
                "state_dir": root, "forgejo_token": "token", "github_token": None,
                "entry": {"bot_login": "ralph"}, "bot": bot,
                "engine": review_engine, "default_engine": review_engine, "high_engine": None,
            }}
            reviews = [threading.Thread(
                target=service.worker,
                args=(jobs, "key", None, contexts),
                kwargs={"stop_event": drain}, daemon=True,
            )]
            commands = threading.Thread(
                target=coordinator.command_worker,
                args=(None, jobs, contexts, commands_stop), daemon=True,
            )
            server = ReadyServer(
                ("127.0.0.1", 0),
                service.make_handler(b"secret", jobs, bot, review_engine.identity),
            )
            (root / "port").write_text(str(server.server_port))
            service.serve_until_stopped(server, reviews, commands, drain, commands_stop)
        """)
        for busy, signum in ((True, signal.SIGTERM), (False, signal.SIGINT)):
            with self.subTest(busy=busy), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                jobs = JobStore(root / "jobs.sqlite3")
                if busy:
                    for number in (42, 43):
                        jobs.enqueue(
                            number,
                            "master",
                            "a" * 40,
                            "opened",
                            engine_identity={"name": "test"},
                        )
                process = subprocess.Popen(
                    [sys.executable, "-B", "-c", child, directory],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )

                def await_file(name):
                    deadline = time.monotonic() + 5
                    while not (root / name).exists():
                        if process.poll() is not None:
                            self.fail(process.communicate()[1])
                        self.assertLess(time.monotonic(), deadline, name)
                        time.sleep(0.01)

                try:
                    await_file("ready")
                    if busy:
                        await_file("started")
                    process.send_signal(signum)
                    if busy:
                        await_file("draining")
                        self.assertIsNone(process.poll())
                        self.assertFalse((root / "commands-stopped").exists())
                        event = payload()
                        event["repository"]["full_name"] = "owner/repo"
                        event["repository"]["html_url"] = (
                            "https://git.example.org/owner/repo"
                        )
                        event["pull_request"]["number"] = 44
                        body = json.dumps(event).encode()
                        signature = hmac.new(
                            b"secret", body, hashlib.sha256
                        ).hexdigest()
                        connection = http.client.HTTPConnection(
                            "127.0.0.1", int((root / "port").read_text()), timeout=5
                        )
                        connection.request(
                            "POST",
                            "/webhooks/forgejo",
                            body,
                            {
                                "X-Forgejo-Event": "pull_request",
                                "X-Forgejo-Signature": signature,
                            },
                        )
                        response = connection.getresponse()
                        response.read()
                        connection.close()
                        self.assertEqual(response.status, 202)
                        (root / "finish").touch()
                    _, stderr = process.communicate(timeout=5)
                    self.assertEqual(process.returncode, 0, stderr)
                    self.assertTrue((root / "commands-stopped").exists())
                    if busy:
                        self.assertTrue((root / "published").exists())
                        with closing(sqlite3.connect(root / "jobs.sqlite3")) as db:
                            states = db.execute(
                                "SELECT number,status FROM jobs ORDER BY number"
                            ).fetchall()
                        self.assertEqual(states, [(42, "complete"), (43, "pending")])
                        with closing(sqlite3.connect(root / "jobs.sqlite3")) as db:
                            self.assertEqual(
                                db.execute(
                                    "SELECT number FROM reconciliation"
                                ).fetchall(),
                                [(44,)],
                            )
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.communicate()


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


class WebhookTests(unittest.TestCase):
    def setUp(self):
        context = patch.object(
            forgejo,
            "pull_request_context",
            return_value={
                "title": "Title",
                "body": "Description",
                "labels": [],
                "base_ref": "master",
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
                "profile": self.prompt_config.profile.to_dict(),
                "high_profile": self.prompt_config.high_profile.to_dict(),
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
                return_value={
                    "title": "Title",
                    "body": "Body",
                    "labels": [],
                    "base_ref": "master",
                },
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
        self.assertTrue(
            body.endswith(
                "\n\n[permalink](<https://review.example.org/traces/report.html>)\n"
            )
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
            ({**metadata, "labels": [], "base_ref": "master"}, {"complete": False}),
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
                return_value={
                    "title": "Title",
                    "body": "Body",
                    "labels": [],
                    "base_ref": "master",
                },
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
