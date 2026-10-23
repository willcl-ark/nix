from types import SimpleNamespace
import hashlib
import hmac
import http.client
import io
import json
import sqlite3
import struct
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zlib
from http.server import ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


from ralph import config as core_config
from ralph import repository as local_repository
from ralph_service import badge, config, forgejo, repository, service
from ralph import engine, model, pipeline, spend
from ralph_service.jobs import JobStore, migrate_state


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_dir = Path(self.directory.name)
        self.jobs = JobStore(self.state_dir / "jobs.sqlite3", retry_base_seconds=0)
        self.ledger = spend.Ledger(self.state_dir / "spend.sqlite3")
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
                    "models": self.prompt_config.models,
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

    def process(self, job):
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
                "budget": {},
                "ppq_budget": None,
                "attempt": debug,
            },
        }

    def test_migrate_state_upgrades_all_ledgers_without_a_jobs_database(self):
        (self.state_dir / "jobs.sqlite3").unlink()
        filenames = ("spend.sqlite3", "ppq-spend.sqlite3", "followup/spend.sqlite3")
        for filename in filenames:
            path = self.state_dir / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.unlink(missing_ok=True)
            with sqlite3.connect(path) as db:
                db.execute("""CREATE TABLE requests (
                    token TEXT PRIMARY KEY, review_id TEXT NOT NULL,
                    stage TEXT NOT NULL, model TEXT NOT NULL, month TEXT NOT NULL,
                    response_id TEXT, status TEXT NOT NULL, usage_json TEXT,
                    cost_micros INTEGER, reserved_micros INTEGER NOT NULL
                )""")
                db.execute(
                    "INSERT INTO requests VALUES ('old', 'pr:1:1:1', 'audit', "
                    "'gpt-6-luna', '2026-09', NULL, 'settled', NULL, 100, 0)"
                )
        args = ["ralph-service", "--state-dir", str(self.state_dir), "--migrate-state"]
        with patch("sys.argv", args), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(service.main(), 0)
            self.assertEqual(service.main(), 0)
        self.assertFalse((self.state_dir / "jobs.sqlite3").exists())
        for filename in filenames:
            path = self.state_dir / filename
            self.assertTrue(Path(f"{path}.pre-timestamps.bak").is_file())
            with sqlite3.connect(path) as db:
                self.assertEqual(
                    db.execute(
                        "SELECT month, cost_micros, requested_at FROM requests"
                    ).fetchone(),
                    ("2026-09", 100, None),
                )

    def test_migrate_state_accepts_fresh_state_without_creating_databases(self):
        state_dir = self.state_dir / "fresh"
        args = ["ralph-service", "--state-dir", str(state_dir), "--migrate-state"]
        with patch("sys.argv", args), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(service.main(), 0)
        self.assertFalse(state_dir.exists())

    def test_migrate_saved_results_preserves_findings_and_concept_concerns(self):
        path = self.state_dir / "pre-engine.sqlite3"
        security = {"classification": "non_security", "reason": "Ordinary bug."}
        old = {
            "base_sha": "b" * 40,
            "head_sha": "a" * 40,
            "content": "Edited review",
            "debug": {
                "decisions": [
                    {"disposition": "drop", "finding": None},
                    {
                        "disposition": "publish",
                        "finding": {
                            "title": "Verified title",
                            "body": "Verified body",
                            "severity": "minor",
                            "security": security,
                        },
                    },
                ],
                "finding_attribution": [
                    {
                        "finding_id": "finding:1",
                        "title": "Edited title",
                        "presented_body": "Edited body",
                        "raised_by": ["tests"],
                    }
                ],
                "concept_assessment": {
                    "status": "verified",
                    "summary": "Material concern",
                    "verification": {
                        "disposition": "publish",
                        "assessment": {"assessment": "rework_approach"},
                    },
                },
            },
        }
        with sqlite3.connect(path) as db:
            db.execute("""CREATE TABLE jobs (
                id INTEGER PRIMARY KEY, number INTEGER, generation INTEGER,
                status TEXT, attempts INTEGER, review_result TEXT
            )""")
            db.execute(
                "INSERT INTO jobs VALUES (1, 42, 2, 'completed', 0, ?)",
                (json.dumps(old),),
            )

        self.assertTrue(migrate_state(path))
        self.assertFalse(migrate_state(path))
        with sqlite3.connect(path) as db:
            result = json.loads(
                db.execute("SELECT review_result FROM jobs").fetchone()[0]
            )
        exported = result["output"]["exports"]
        self.assertEqual(exported["actionable"], ["finding:1", "concept_objection"])
        self.assertEqual(len(exported["findings"]), 1)
        finding = exported["findings"][0]
        self.assertEqual(finding["id"], "finding:1")
        self.assertEqual(finding["title"], "Verified title")
        self.assertEqual(finding["body"], "Verified body")
        self.assertEqual(finding["presented_title"], "Edited title")
        self.assertEqual(finding["presented_body"], "Edited body")
        self.assertEqual(finding["raised_by"], ["tests"])
        self.assertEqual(finding["security"], security)
        self.assertEqual(result["content"], old["content"])
        self.assertEqual(result["run"]["review_id"], "pr:42:1:2")
        with sqlite3.connect(f"{path}.pre-engine.bak") as db:
            self.assertEqual(
                json.loads(db.execute("SELECT review_result FROM jobs").fetchone()[0]),
                old,
            )

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

    def test_review_badge_tracks_latest_published_feedback(self):
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            service.make_handler(
                b"secret", self.jobs, self.bot_config, self.review_engine.identity
            ),
        )
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def get(path="/assets/owner/repo/42.png"):
            with closing(
                http.client.HTTPConnection("127.0.0.1", server.server_port)
            ) as connection:
                connection.request("GET", path)
                response = connection.getresponse()
                body = response.read()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Content-Type"), "image/png")
                self.assertEqual(response.getheader("Cache-Control"), "no-store")
                self.assertEqual(int(response.getheader("Content-Length")), len(body))
                self.assertTrue(body.startswith(b"\x89PNG\r\n\x1a\n"))
                return body

        grey = get()
        self.assertEqual(struct.unpack("!II", grey[16:24]), (16, 16))
        offset = 8
        compressed = bytearray()
        while offset < len(grey):
            size = struct.unpack("!I", grey[offset : offset + 4])[0]
            kind = grey[offset + 4 : offset + 8]
            data = grey[offset + 8 : offset + 8 + size]
            crc = struct.unpack("!I", grey[offset + 8 + size : offset + 12 + size])[0]
            self.assertEqual(crc, zlib.crc32(kind + data))
            if kind == b"IDAT":
                compressed.extend(data)
            offset += size + 12
        pixels = zlib.decompress(compressed)
        self.assertEqual(len(pixels), 16 * (1 + 16 * 4))
        self.assertEqual(pixels[1:5], bytes((128, 128, 128, 0)))
        center = 8 * 65 + 1 + 8 * 4
        self.assertEqual(pixels[center : center + 4], bytes((128, 128, 128, 255)))
        job = self.enqueue()
        self.jobs.save_result(
            job,
            self.saved_result(
                debug={
                    "decisions": [
                        {
                            "disposition": "publish",
                            "finding": {"severity": "suggestion"},
                        }
                    ]
                },
                findings=[{"id": "finding:1", "severity": "suggestion"}],
            ),
        )
        self.assertEqual(get(), grey)  # Saved but not published.
        self.jobs.complete(job)
        orange = get()
        self.assertEqual(orange, badge._png((247, 147, 26)))
        self.assertNotEqual(orange, grey)
        self.assertEqual(get("/assets/owner/repo/999.png"), grey)
        job = self.enqueue("b" * 40)
        self.assertEqual(get(), grey)  # A new head invalidates the badge.
        self.jobs.save_result(
            job,
            self.saved_result(
                debug={"decisions": [{"disposition": "drop", "finding": None}]}
            ),
        )
        self.jobs.complete(job)
        self.assertEqual(get(), grey)
        job = self.enqueue("c" * 40)
        self.jobs.save_result(
            job,
            self.saved_result(
                debug={
                    "decisions": [],
                    "concept_assessment": {
                        "status": "verified",
                        "summary": "A material concern.",
                        "verification": {
                            "disposition": "publish",
                            "assessment": {"assessment": "rework_approach"},
                        },
                    },
                },
                concept_concern=True,
            ),
        )
        self.jobs.complete(job)
        self.assertEqual(get(), orange)
        job = self.enqueue("d" * 40)
        self.jobs.save_result(
            job,
            self.saved_result(
                debug={
                    "decisions": [
                        {
                            "disposition": "publish",
                            "finding": {"severity": "suggestion"},
                        }
                    ]
                },
                findings=[{"id": "finding:1", "severity": "suggestion"}],
            ),
        )
        self.jobs.fail(job, "Publication failed")
        self.assertEqual(get(), grey)
        job = self.enqueue("e" * 40)
        self.jobs.save_result(
            job, self.saved_result(debug={"skip": "Input exceeds limit"})
        )
        self.jobs.complete(job)
        self.assertEqual(get(), grey)
        job = self.enqueue("f" * 40)
        self.jobs.save_result(job, self.saved_result(debug={"finding_attribution": []}))
        self.jobs.complete(job)
        self.assertEqual(get(), grey)  # Verification did not produce decisions.
        for path in (
            "/assets/other/repo/42.png",
            "/assets/owner/repo/0.png",
            "/assets/owner/repo/42.svg",
            "/assets/owner/repo/" + "9" * 100 + ".png",
        ):
            with closing(
                http.client.HTTPConnection("127.0.0.1", server.server_port)
            ) as connection:
                connection.request("GET", path)
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 404)

    def test_webhook_rejects_failed_persistence(self):
        with patch.object(
            self.jobs, "enqueue", side_effect=sqlite3.OperationalError("full")
        ):
            with self.assertLogs(level="ERROR"):
                self.assertEqual(self.post_webhook(self.jobs), 503)
        self.assertIsNone(self.jobs.claim())

    def test_distinct_pr_reviews_overlap_outside_git_lock(self):
        (self.state_dir / "checkout" / ".git").mkdir(parents=True)
        for number in (42, 43):
            self.jobs.enqueue(
                number,
                "master",
                "a" * 40,
                "synchronize",
                engine_identity=self.review_engine.identity,
            )
        claimed = [self.jobs.claim(), self.jobs.claim()]
        barrier = threading.Barrier(2)
        prefixes = []

        def collect(*args, ref_prefix):
            self.assertTrue(service.CHECKOUT_LOCK.locked())
            prefixes.append(ref_prefix)
            return "b" * 40, "a" * 40, "review input", None

        def snapshot(*args):
            self.assertTrue(service.CHECKOUT_LOCK.locked())
            return SimpleNamespace(
                base_sha="b" * 40,
                head_sha="a" * 40,
                merge_base="c" * 40,
                diff_base_sha="c" * 40,
            )

        def review(*args, **kwargs):
            barrier.wait(timeout=5)
            return pipeline.ReviewResult("Review content", [], False)

        with (
            patch.object(service.repository, "current_head", return_value="a" * 40),
            patch.object(service.forgejo, "find_comment", return_value=None),
            patch.object(
                service.forgejo,
                "pull_request_context",
                return_value=("Title", "Description"),
            ),
            patch.object(service.repository, "collect_review", side_effect=collect),
            patch.object(
                service.local_repository, "snapshot_repository", side_effect=snapshot
            ),
            patch.object(
                pipeline, "review_with_independent_passes", side_effect=review
            ),
            patch.object(
                service.forgejo, "publish_review", return_value="published"
            ) as publish,
            patch.object(service.local_repository, "release_review_refs") as release,
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            self.assertEqual(
                list(pool.map(self.process, claimed)), ["published", "published"]
            )
        self.assertEqual(len(set(prefixes)), 2)
        self.assertEqual(
            {call.args[1] for call in release.call_args_list}, set(prefixes)
        )
        self.assertEqual({call.args[2] for call in publish.call_args_list}, {42, 43})
        self.assertIsNone(self.jobs.claim())

    def test_publication_retry_reuses_saved_result(self):
        job = self.enqueue()
        with (
            patch.object(service.repository, "current_head", return_value="a" * 40),
            patch.object(service.forgejo, "find_comment", return_value=None),
            patch.object(
                service.forgejo,
                "pull_request_context",
                return_value=("Title", "Description"),
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
                self.assertEqual(self.process(job), "retry")
            self.assertEqual(review.call_count, 1)
            self.assertEqual(
                review.call_args.args[1].budget.review_id,
                f"pr:42:{job['id']}:{job['generation']}",
            )
            self.assertEqual(review.call_args.args[3], "enabled")
            case, runtime = review.call_args.args[:2]
            self.assertEqual(
                case.source,
                {
                    "kind": "pull_request",
                    "number": 42,
                    "url": f"{self.bot_config.repository_url}/pulls/42",
                },
            )
            self.assertTrue(callable(runtime.discussion_lookup))

        restarted = JobStore(self.state_dir / "jobs.sqlite3", retry_base_seconds=0)
        retry_job = restarted.claim()
        self.assertEqual(retry_job["review_result"]["content"], "Review content")
        saved = retry_job["review_result"]
        self.assertEqual(
            set(saved),
            {
                "schema_version",
                "engine",
                "base_sha",
                "head_sha",
                "merge_base",
                "content",
                "output",
                "run",
            },
        )
        self.assertEqual(saved["base_sha"], "b" * 40)
        self.assertEqual(saved["merge_base"], "c" * 40)
        self.assertEqual(
            saved["run"]["review_id"], f"pr:42:{job['id']}:{job['generation']}"
        )
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
        self.assertIsNone(restarted.claim())

    def test_narrative_engine_output_is_saved_and_retried_without_exposing_native_data(
        self,
    ):
        identity = {
            "name": "narrative",
            "code_sha256": "c" * 64,
            "config_sha256": "d" * 64,
            "config": {},
        }
        private_text = "NARRATIVE_PRIVATE_NATIVE_DATA"

        class NarrativeEngine:
            def __init__(self):
                self.identity = identity
                self.calls = 0

            def run(self, case, runtime):
                self.calls += 1
                return engine.Output(
                    "Narrative review body",
                    {
                        "kind": "narrative-v7",
                        "internal": private_text,
                    },
                    {},
                )

        narrative = NarrativeEngine()
        self.jobs.enqueue(
            42, "master", "a" * 40, "synchronize", engine_identity=identity
        )
        job = self.jobs.claim()
        self.bot_config = replace(
            self.bot_config,
            report_dir=self.state_dir / "public",
            report_base_url="https://review.example.org/traces",
        )
        with (
            patch.object(service.repository, "current_head", return_value="a" * 40),
            patch.object(service.forgejo, "find_comment", return_value=None),
            patch.object(
                service.forgejo,
                "pull_request_context",
                return_value=("Title", "Description"),
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
                service.forgejo,
                "publish_review",
                side_effect=[urllib.error.URLError("connection lost"), "unchanged"],
            ) as publish,
        ):
            with self.assertLogs(level="ERROR"):
                self.assertEqual(
                    service.process_job(
                        job,
                        self.jobs,
                        self.state_dir,
                        "api-key",
                        "forgejo-token",
                        "ralph",
                        self.bot_config,
                        self.ledger,
                        narrative,
                    ),
                    "retry",
                )
            saved = self.jobs.claim()
            result = saved["review_result"]
            self.assertEqual(result["engine"], identity)
            self.assertEqual(result["content"], "Narrative review body")
            self.assertEqual(
                result["output"],
                {
                    "schema_version": 1,
                    "data": {"kind": "narrative-v7", "internal": private_text},
                    "exports": {},
                },
            )
            report = next(self.bot_config.report_dir.glob("*.html"))
            self.assertNotIn(private_text, report.read_text(encoding="utf-8"))
            self.assertEqual(
                service.process_job(
                    saved,
                    self.jobs,
                    self.state_dir,
                    "api-key",
                    "forgejo-token",
                    "ralph",
                    self.bot_config,
                    self.ledger,
                    None,
                ),
                "unchanged",
            )
        self.assertEqual(narrative.calls, 1)
        self.assertEqual(publish.call_args.args[6], "Narrative review body")

    def test_report_exists_before_publication_and_retry_reuses_url(self):
        self.bot_config = replace(
            self.bot_config,
            report_dir=self.state_dir / "public",
            report_base_url="https://review.example.org/traces",
        )
        job = self.enqueue()
        # A durable result is enough to retry publication without another model call.
        job["review_result"] = self.saved_result()
        self.jobs.save_result(job, job["review_result"])
        urls = []

        def publish(*args, **kwargs):
            url = kwargs["report_url"]
            urls.append(url)
            filename = urls[-1].rsplit("/", 1)[-1]
            self.assertTrue((self.bot_config.report_dir / filename).is_file())
            if len(urls) == 1:
                raise urllib.error.URLError("connection lost")
            return "published"

        with (
            patch.object(service.repository, "current_head", return_value="a" * 40),
            patch.object(service.forgejo, "publish_review", side_effect=publish),
        ):
            with self.assertLogs(level="ERROR"):
                self.assertEqual(self.process(job), "retry")
            retry_job = self.jobs.claim()
            self.assertEqual(self.process(retry_job), "published")
        self.assertEqual(urls[0], urls[1])

    def test_report_failure_publishes_review_without_broken_link(self):
        self.bot_config = replace(
            self.bot_config,
            report_dir=self.state_dir / "public",
            report_base_url="https://review.example.org/traces",
        )
        job = self.enqueue()
        job["review_result"] = self.saved_result()
        with (
            patch.object(service.repository, "current_head", return_value="a" * 40),
            patch.object(
                service.report, "save_report", side_effect=OSError("disk full")
            ),
            patch.object(
                service.forgejo, "publish_review", return_value="published"
            ) as publish,
        ):
            with self.assertLogs(level="WARNING"):
                self.assertEqual(self.process(job), "published")
        self.assertIsNone(publish.call_args.kwargs["report_url"])

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
                return_value=("Title", "Description"),
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

    def test_same_head_on_new_base_is_reviewed(self):
        job = self.enqueue()
        old_comment = {
            "body": (
                f"<!-- ralph:owner/repo -->\nBase: `{'b' * 40}`  \nHead: `{'a' * 40}`\n"
            )
        }
        with (
            patch.object(service.repository, "current_head", return_value="a" * 40),
            patch.object(service.forgejo, "find_comment", return_value=old_comment),
            patch.object(
                service.forgejo,
                "pull_request_context",
                return_value=("Title", "Description"),
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
                pipeline,
                "review_with_independent_passes",
                return_value=pipeline.ReviewResult("New base review", [], False),
            ) as review,
            patch.object(service.forgejo, "publish_review", return_value="updated"),
        ):
            self.assertEqual(self.process(job), "updated")
        self.assertEqual(review.call_count, 1)

    def test_fetch_head_change_is_queued_even_if_head_lookup_lags(self):
        old = self.enqueue()
        with (
            patch.object(service.repository, "current_head", return_value="a" * 40),
            patch.object(service.forgejo, "find_comment", return_value=None),
            patch.object(
                service.forgejo,
                "pull_request_context",
                return_value=("Title", "Description"),
            ),
            patch.object(
                service.repository,
                "collect_review",
                return_value=(
                    "c" * 40,
                    "b" * 40,
                    None,
                    "PR head changed before review",
                ),
            ),
        ):
            self.assertEqual(self.process(old), "stale")
        self.assertEqual(self.jobs.claim()["head"], "b" * 40)

    def test_fetch_head_change_does_not_replace_newer_pending_job(self):
        old = self.enqueue()

        def collect(*_args, **_kwargs):
            self.jobs.enqueue(
                42,
                "master",
                "c" * 40,
                "synchronize",
                engine_identity=self.review_engine.identity,
            )
            return "d" * 40, "b" * 40, None, "PR head changed before review"

        with (
            patch.object(service.repository, "current_head", return_value="a" * 40),
            patch.object(service.forgejo, "find_comment", return_value=None),
            patch.object(
                service.forgejo,
                "pull_request_context",
                return_value=("Title", "Description"),
            ),
            patch.object(service.repository, "collect_review", side_effect=collect),
        ):
            self.assertEqual(self.process(old), "stale")
        self.assertEqual(self.jobs.claim()["head"], "c" * 40)

    def test_stale_requeue_timeout_retries_and_then_completes(self):
        old = self.enqueue()
        with (
            patch.object(
                service.repository,
                "current_head",
                side_effect=["a" * 40, TimeoutError("git timeout")],
            ),
            patch.object(service.forgejo, "find_comment", return_value=None),
            patch.object(
                service.forgejo,
                "pull_request_context",
                return_value=("Title", "Description"),
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
                pipeline,
                "review_with_independent_passes",
                side_effect=model.StaleReview("head changed"),
            ),
        ):
            with self.assertLogs(level="ERROR"):
                self.assertEqual(self.process(old), "retry")
        retry = self.jobs.claim()
        self.assertEqual((retry["id"], retry["attempts"]), (old["id"], 1))
        with (
            patch.object(service.repository, "current_head", return_value="a" * 40),
            patch.object(service.forgejo, "find_comment", return_value=None),
            patch.object(
                service.forgejo,
                "pull_request_context",
                return_value=("Title", "Description"),
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
                pipeline,
                "review_with_independent_passes",
                return_value=pipeline.ReviewResult("Review content", [], False),
            ),
            patch.object(service.forgejo, "publish_review", return_value="updated"),
        ):
            self.assertEqual(self.process(retry), "updated")
        self.assertIsNone(self.jobs.claim())

    def test_permanent_http_error_fails_and_transient_error_retries(self):
        self.assertFalse(
            service.retryable_error(
                urllib.error.HTTPError("https://git.example.org", 400, "bad", {}, None)
            )
        )
        self.assertTrue(
            service.retryable_error(
                urllib.error.HTTPError(
                    "https://git.example.org", 429, "rate limit", {}, None
                )
            )
        )
        self.assertTrue(
            service.retryable_error(
                urllib.error.HTTPError(
                    "https://git.example.org", 503, "unavailable", {}, None
                )
            )
        )
        self.assertTrue(service.retryable_error(TimeoutError("timeout")))

        job = self.enqueue()
        error = urllib.error.HTTPError("https://git.example.org", 400, "bad", {}, None)
        with (
            patch.object(service.repository, "current_head", return_value="a" * 40),
            patch.object(service.forgejo, "find_comment", side_effect=error),
        ):
            with self.assertLogs(level="ERROR"):
                self.assertEqual(self.process(job), "failed")
        self.assertIsNone(self.jobs.claim())
        with closing(sqlite3.connect(self.state_dir / "jobs.sqlite3")) as db:
            self.assertEqual(
                db.execute(
                    "SELECT status FROM jobs WHERE id=?", (job["id"],)
                ).fetchone()[0],
                "failed",
            )

    def test_worker_can_stop_after_claiming_one_job(self):
        self.jobs.enqueue(
            42,
            "master",
            "a" * 40,
            "opened",
            engine_identity=self.review_engine.identity,
        )
        stop_event = threading.Event()

        def process(job, *_args):
            self.assertEqual(job["number"], 42)
            stop_event.set()

        with patch.object(service, "process_job", side_effect=process) as called:
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
        self.assertEqual(called.call_count, 1)

    def test_worker_restarts_unfinished_review_with_current_engine(self):
        previous = {
            **self.review_engine.identity,
            "code_sha256": "old code",
            "config_sha256": "old configuration",
            "config": {},
        }
        self.jobs.enqueue(42, "master", "a" * 40, "opened", engine_identity=previous)
        interrupted = self.jobs.claim()
        self.assertEqual(self.jobs.recover(), 1)
        stop_event = threading.Event()

        def review(*_args):
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
                return_value=("Title", "Description"),
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

    def test_worker_retries_saved_publication_with_original_engine_identity(self):
        previous = {
            **self.review_engine.identity,
            "code_sha256": "old code",
            "config_sha256": "old configuration",
            "config": {},
        }
        saved = {**self.saved_result(), "engine": previous}
        self.jobs.enqueue(42, "master", "a" * 40, "opened", engine_identity=previous)
        interrupted = self.jobs.claim()
        self.assertTrue(self.jobs.save_result(interrupted, saved))
        self.assertEqual(self.jobs.recover(), 1)
        stop_event = threading.Event()

        def publish(*_args, **_kwargs):
            stop_event.set()
            return "unchanged"

        with (
            patch.object(service.repository, "current_head", return_value="a" * 40),
            patch.object(
                service.engine, "load", side_effect=AssertionError("old engine loaded")
            ),
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
                self.jobs,
                "bind_engine",
                side_effect=AssertionError("saved identity changed"),
            ),
            patch.object(
                service.forgejo, "publish_review", side_effect=publish
            ) as published,
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
        published.assert_called_once()
        self.assertEqual(published.call_args.kwargs["engine_identity"], previous)
        self.assertEqual(self.jobs.latest_completed_result(42), saved)

    def test_worker_waits_after_claim_database_error(self):
        stop_event = threading.Event()

        def stop_after_wait(_seconds):
            stop_event.set()

        with (
            patch.object(
                self.jobs,
                "claim",
                side_effect=sqlite3.OperationalError("database locked"),
            ) as claim,
            patch.object(stop_event, "wait", side_effect=stop_after_wait) as wait,
            self.assertLogs(level="ERROR"),
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
        self.assertEqual(claim.call_count, 1)
        wait.assert_called_once_with(1)

    def test_worker_survives_unexpected_processing_error(self):
        self.jobs.enqueue(
            42,
            "master",
            "a" * 40,
            "opened",
            engine_identity=self.review_engine.identity,
        )
        stop_event = threading.Event()
        calls = []

        def process(job, *_args):
            calls.append(job)
            if len(calls) == 1:
                raise TimeoutError("worker interrupted")
            self.jobs.complete(job)
            stop_event.set()

        with (
            patch.object(service, "process_job", side_effect=process),
            patch.object(stop_event, "wait", return_value=None),
            self.assertLogs(level="ERROR"),
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
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["attempts"], 1)
        self.assertIsNone(self.jobs.claim())


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
                    "models": self.prompt_config.models,
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
        ledger = spend.Ledger(state_dir / "spend.sqlite3")
        return jobs.claim(), jobs, state_dir, ledger

    def test_signature_checks_raw_body(self):
        body = b'{"action":"opened"}'
        sig = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        self.assertTrue(service.valid_signature(body, sig, b"secret"))
        self.assertFalse(service.valid_signature(body + b" ", sig, b"secret"))
        self.assertFalse(service.valid_signature(body, "bad", b"secret"))

    def test_event_filters_and_rejects_other_repository(self):
        self.assertEqual(
            service.parse_event(self.bot_config, "pull_request", payload()),
            (42, "master", "a" * 40, "opened", False),
        )
        self.assertEqual(
            service.parse_event(
                self.bot_config, "pull_request", payload("synchronize")
            ),
            (42, "master", "a" * 40, "synchronize", False),
        )
        forced = payload()
        forced["ralph_force"] = True
        self.assertEqual(
            service.parse_event(self.bot_config, "pull_request", forced)[-1], True
        )
        self.assertIsNone(service.parse_event(self.bot_config, "push", payload()))
        self.assertIsNone(
            service.parse_event(self.bot_config, "pull_request", payload("closed"))
        )
        wrong = payload()
        wrong["repository"]["full_name"] = "someone/bitcoin"
        with self.assertRaises(ValueError):
            service.parse_event(self.bot_config, "pull_request", wrong)

    def test_historical_base_requires_force_and_valid_sha(self):
        event = payload()
        event["pull_request"]["base"]["sha"] = "b" * 40
        self.assertEqual(
            service.parse_event(self.bot_config, "pull_request", event)[1], "master"
        )
        event["ralph_force"] = True
        self.assertEqual(
            service.parse_event(self.bot_config, "pull_request", event)[1],
            "sha:" + "b" * 40,
        )
        for invalid in (None, "master", "-option", "b" * 39):
            event["pull_request"]["base"]["sha"] = invalid
            with self.assertRaisesRegex(ValueError, "historical base"):
                service.parse_event(self.bot_config, "pull_request", event)

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

    def test_wrong_path_log_redacts_query_and_identifies_non_webhook(self):
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
            body = b"{}"
            url = f"http://127.0.0.1:{server.server_port}/?api_key=do-not-log"
            request = urllib.request.Request(
                url, body, headers={"X-Next-Action": "probe"}
            )
            with self.assertLogs(level="WARNING") as logs:
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 404)
            output = "\n".join(logs.output)
            self.assertIn("reason=unexpected_path path=/", output)
            self.assertIn("signature_present=False", output)
            self.assertNotIn("do-not-log", output)
            self.assertEqual(jobs.enqueued, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_browser_gets_do_not_log_info(self):
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
            url = f"http://127.0.0.1:{server.server_port}/?api_key=do-not-log"
            with (
                patch.object(service.logging, "info") as info,
                patch.object(service.logging, "debug") as debug,
            ):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(url)
            self.assertEqual(error.exception.code, 404)
            info.assert_not_called()
            self.assertTrue(debug.called)
            self.assertNotIn("do-not-log", str(debug.call_args))
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
                engine_identity=self.review_engine.identity,
            )
        }
        with (
            patch.object(repository, "current_head", return_value="a" * 40),
            patch.object(forgejo, "find_comment", return_value=existing) as find,
            patch.object(
                forgejo, "pull_request_context", return_value=("Title", "Body")
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

    def test_repeated_head_skips_model_call(self):
        job, jobs, state_dir, ledger = self.queued_job()
        existing = {
            "body": forgejo.review_body(
                self.bot_config,
                "b" * 40,
                "a" * 40,
                "Reviewed.",
                engine_identity=self.review_engine.identity,
            )
        }
        with (
            patch.object(repository, "current_head", return_value="a" * 40),
            patch.object(forgejo, "find_comment", return_value=existing),
            patch.object(
                forgejo, "pull_request_context", return_value=("Title", "Body")
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
        snapshot.assert_not_called()
        review_model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
