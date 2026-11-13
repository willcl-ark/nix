"""Repository registry, shared admission, and authenticated webhook routing."""

import argparse
import io
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ralph import engine, progress
from . import config, delivery, finding_feedback, monitor
from .jobs import JobStore


class JournalFormatter(logging.Formatter):
    """Give systemd's stream reader the Python record's actual priority."""

    def format(self, record):
        priority = {
            logging.CRITICAL: 2,
            logging.ERROR: 3,
            logging.WARNING: 4,
            logging.INFO: 6,
            logging.DEBUG: 7,
        }.get(record.levelno, 6)
        return "\n".join(
            f"<{priority}>{line}" for line in super().format(record).splitlines()
        )


def bind_repositories(jobs, entries):
    """A stable ID cannot silently acquire a different repository's history."""
    with jobs._connection(write=True) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS repositories (id TEXT PRIMARY KEY, source TEXT NOT NULL UNIQUE)"
        )
        for repo_id, entry in entries.items():
            source = json.dumps(
                {
                    key: entry.get(key)
                    for key in (
                        "repository",
                        "repository_url",
                        "origin",
                        "forgejo_api",
                        "github_repository",
                    )
                },
                sort_keys=True,
            )
            prior = db.execute(
                "SELECT source FROM repositories WHERE id = ?", (repo_id,)
            ).fetchone()
            if prior and prior[0] != source:
                raise ValueError(
                    f"repository identity changed for {repo_id}; explicit migration required"
                )
            if not prior:
                owner = db.execute(
                    "SELECT id FROM repositories WHERE source = ?", (source,)
                ).fetchone()
                if owner:
                    raise ValueError(f"repository source already belongs to {owner[0]}")
                db.execute("INSERT INTO repositories VALUES (?, ?)", (repo_id, source))


def make_handler(contexts, jobs):
    from . import service

    feedback = finding_feedback.FeedbackStore(
        Path(jobs.path).with_name("feedback.sqlite3")
    )
    handlers = {
        repo_id: service.make_handler(
            ctx["secret"],
            jobs.for_repository(repo_id),
            ctx["bot"],
            ctx["engine"].identity,
        )
        for repo_id, ctx in contexts.items()
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if finding_feedback.handle_request(self, contexts, feedback):
                return
            path = urllib.parse.urlsplit(self.path).path
            for repo_id, ctx in contexts.items():
                if path.startswith(f"/assets/{ctx['bot'].repository}/"):
                    return handlers[repo_id].do_GET(self)
            self.send_error(404)

        def do_POST(self):
            if finding_feedback.handle_request(self, contexts, feedback):
                return
            path = urllib.parse.urlsplit(self.path).path
            if path != "/webhooks/forgejo" and not path.startswith(
                "/webhooks/forgejo/"
            ):
                self.send_error(404)
                return
            size = self.headers.get("Content-Length", "")
            if not size.isdecimal() or int(size) > service.MAX_BODY:
                self.send_error(413)
                return
            body = self.rfile.read(int(size))
            try:
                payload = json.loads(body)
                source = payload["repository"]
                matches = [
                    key
                    for key, ctx in contexts.items()
                    if source.get("full_name") == ctx["bot"].repository
                    and source.get("html_url", "").rstrip("/")
                    == ctx["bot"].repository_url
                ]
            except (ValueError, KeyError, TypeError, AttributeError):
                self.send_error(400)
                return
            if len(matches) != 1 or (
                path != "/webhooks/forgejo"
                and path != f"/webhooks/forgejo/{matches[0]}"
            ):
                self.send_error(404)
                return
            # The unsigned selector chooses only a verifier. The existing handler
            # verifies this repository's signature and complete event identity.
            original = self.rfile
            self.rfile = io.BytesIO(body)
            self.path = "/webhooks/forgejo"
            try:
                handlers[matches[0]].do_POST(self)
            finally:
                self.rfile = original

        def log_message(self, format, *args):
            logging.debug("Webhook request: %s", format % args)

    return Handler


def command_worker(store, jobs, contexts, stop):
    from . import service

    while not stop.is_set():
        for repo_id, ctx in contexts.items():
            if stop.is_set():
                break
            scoped = jobs.for_repository(repo_id)
            try:
                service.consume_review_requests(
                    store,
                    scoped,
                    ctx["bot"],
                    ctx["forgejo_token"],
                    ctx["default_engine"],
                    ctx["high_engine"],
                )
            except Exception:
                logging.exception("Could not process commands repo=%s", repo_id)
            try:
                service.reconcile_next(scoped, ctx["bot"], ctx["forgejo_token"])
            except Exception:
                logging.exception("Could not reconcile repo=%s", repo_id)
        stop.wait(5)


def _secret(entry, key, *, optional=False):
    path = entry.get(key)
    if optional and path is None:
        return None
    if not path:
        raise ValueError(f"{entry['repo_id']}: {key} is required")
    value = Path(path).read_text().strip()
    if not value:
        raise ValueError(f"{entry['repo_id']}: {key} must not be empty")
    return value


def _write_manifest(path, legacy_repo_id, status):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"legacy_repository": legacy_repo_id, "status": status}) + "\n"
    )
    temporary.replace(path)


def migrate(state_dir, legacy_repo_id, entries):
    from .repository_migration import migrate_repositories

    if legacy_repo_id not in entries:
        raise ValueError("legacy repository must be present in the registry")
    manifest = state_dir / "repository-migration.json"
    if manifest.exists():
        previous = json.loads(manifest.read_text())
        if previous["legacy_repository"] != legacy_repo_id:
            raise ValueError("migration repository does not match the saved manifest")
    else:
        _write_manifest(manifest, legacy_repo_id, "started")
    jobs_path = state_dir / "jobs.sqlite3"
    if jobs_path.exists():
        migrate_repositories(jobs_path, legacy_repo_id=legacy_repo_id)
    notification_path = state_dir / "telegram.sqlite3"
    if notification_path.exists():
        monitor.migrate_notification_repositories(
            notification_path, legacy_repo_id=legacy_repo_id
        )
    jobs = JobStore(jobs_path)
    bind_repositories(jobs, entries)
    _write_manifest(manifest, legacy_repo_id, "complete")


def load_contexts(
    entries, root, report_dir=None, report_base_url=None, archive_base_url=None
):
    """Load the same repository configuration in coordinator and job processes."""
    contexts = {}
    for repo_id, entry in entries.items():
        if not entry.get("bot_login"):
            raise ValueError(f"{repo_id}: bot_login is required")
        bot = config.repository_config(
            entry, report_dir, report_base_url, archive_base_url
        )
        if entry.get("github_publication", "off") != "off" and report_dir is None:
            raise ValueError("GitHub publication requires public reports")
        engine_name = entry.get("engine", "current")
        engine_config = dict(entry.get("engine_config", {}))
        if engine_name == "current":
            engine_config.setdefault("archaeology", True)
            engine_config["strategy"] = entry.get("strategy", "core")
        state_dir = config.repository_state_dir(root, repo_id)
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        contexts[repo_id] = {
            "entry": entry,
            "bot": bot,
            "state_dir": state_dir,
            "engine": engine.load(engine_name, engine_config),
            "default_engine": engine.load(
                engine_name,
                {**engine_config, "profile": "default"}
                if engine_name == "current"
                else engine_config,
            ),
            "high_engine": engine.load("current", {**engine_config, "profile": "high"})
            if engine_name == "current"
            else None,
            "secret": _secret(entry, "webhook_secret_file").encode(),
            "forgejo_token": _secret(entry, "forgejo_token_file"),
            "github_token": _secret(entry, "github_token_file", optional=True),
        }
    return contexts


def main(resources):
    from . import service

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--repositories", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--report-base-url")
    parser.add_argument("--archive-base-url")
    parser.add_argument("--openai-key-file", type=Path)
    parser.add_argument("--migrate-state", action="store_true")
    parser.add_argument("--legacy-repository")
    parser.add_argument("--list-held-deliveries", action="store_true")
    parser.add_argument("--list-job-units", action="store_true")
    parser.add_argument("--cancel-job", type=int)
    parser.add_argument("--repository-id")
    parser.add_argument("--release-delivery", type=int)
    parser.add_argument("--confirm-no-pending-write", action="store_true")
    args = parser.parse_args()
    handler = logging.StreamHandler()
    formatter = (
        JournalFormatter if "JOURNAL_STREAM" in os.environ else logging.Formatter
    )
    handler.setFormatter(formatter("%(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    progress.LOGGER.setLevel(logging.DEBUG)
    try:
        entries = config.load_repositories(args.repositories)
        if args.list_held_deliveries:
            print(
                json.dumps(
                    delivery.list_held(args.state_dir / "jobs.sqlite3"), indent=2
                )
            )
            return 0
        if args.list_job_units or args.cancel_job is not None:
            jobs = JobStore(args.state_dir / "jobs.sqlite3", initialize=False)
            attempts = jobs.active_attempts()
            if args.list_job_units:
                print(
                    json.dumps(
                        [
                            {
                                key: job[key]
                                for key in (
                                    "id",
                                    "repo_id",
                                    "number",
                                    "status",
                                    "unit_name",
                                )
                            }
                            for job in attempts
                        ],
                        indent=2,
                    )
                )
                return 0
            matches = [job for job in attempts if job["id"] == args.cancel_job]
            if not matches:
                raise ValueError("job has no active execution attempt")
            job = matches[0]
            claim = jobs.get_claim(job["id"], job["attempt_token"])
            if claim is not None:
                jobs.for_repository(job["repo_id"]).supersede(claim)
            # Retain execution ownership/capacity until systemd confirms exit.
            subprocess.run(
                ["systemctl", "stop", "--no-block", job["unit_name"]],
                check=True,
                timeout=3,
            )
            return 0
        resources.enter_context(service.coordinator_lock(args.state_dir))
        args.state_dir.chmod(0o700)
        if args.migrate_state:
            if not args.legacy_repository:
                raise ValueError("--migrate-state requires --legacy-repository")
            migrate(args.state_dir, args.legacy_repository, entries)
            print("repository migration complete")
            return 0
        if args.workers < 1:
            raise ValueError("workers must be positive")
        jobs = JobStore(
            args.state_dir / "jobs.sqlite3",
            global_limit=args.workers,
            repository_limits={
                key: entry.get("max_workers", 1) for key, entry in entries.items()
            },
        )
        bind_repositories(jobs, entries)
        if args.release_delivery is not None:
            if (
                args.repository_id not in entries
                or args.release_delivery < 1
                or not args.confirm_no_pending_write
            ):
                raise ValueError(
                    "release requires --repository-id, positive PR and --confirm-no-pending-write"
                )
            print(
                f"released {delivery.release(jobs.for_repository(args.repository_id), args.release_delivery)} uncertain deliveries"
            )
            return 0
        if args.openai_key_file is None:
            raise ValueError("--openai-key-file is required")
        if not args.openai_key_file.read_text().strip():
            raise ValueError("OpenAI key must not be empty")
        contexts = load_contexts(
            entries,
            args.state_dir,
            args.report_dir,
            args.report_base_url,
            args.archive_base_url,
        )
        command_store = monitor.NotificationStore(args.state_dir / "telegram.sqlite3")
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        parser.error(str(exc))
    from .job_units import JobUnits

    jobs.recover_reconciliation()
    units = JobUnits(
        jobs,
        args.state_dir,
        entries,
        args.openai_key_file,
        args.report_dir,
        args.report_base_url,
        args.archive_base_url,
    )
    logging.info(
        "review scheduler workers=%d repositories=%s", args.workers, ",".join(contexts)
    )
    drain, stop = threading.Event(), threading.Event()
    command_thread = threading.Thread(
        target=command_worker, args=(command_store, jobs, contexts, stop), daemon=True
    )
    workers = [
        threading.Thread(
            target=units.serve, args=(drain,), daemon=True, name="review-scheduler"
        )
    ]
    server = ThreadingHTTPServer((args.listen, args.port), make_handler(contexts, jobs))
    server.daemon_threads = True
    service.serve_until_stopped(server, workers, command_thread, drain, stop)
