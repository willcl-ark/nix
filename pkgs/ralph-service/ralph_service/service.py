"""Webhook server and review worker."""

import argparse
from contextlib import closing
import hashlib
import hmac
import json
import logging
import re
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ralph import trace as core_trace
from . import badge, config, forgejo, github, report, repository, trace, monitor
from ralph import repository as local_repository
from ralph import engine, model, spend, runner, profiles
from .research import discussion_lookup
from .review_feedback import collect_review_feedback
from .jobs import JobStore
from ralph.repository import BRANCH, SHA

MAX_BODY = 1024 * 1024
CHECKOUT_LOCK = threading.Lock()


def valid_signature(body, header, secret):
    if not header:
        return False
    supplied = header.removeprefix("sha256=")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", supplied):
        return False
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, supplied.lower())


def parse_event(bot_config, event, payload):
    if event != "pull_request" or payload.get("action") not in {
        "opened",
        "reopened",
        "synchronize",
        "synchronized",
    }:
        return None
    repo = payload.get("repository") or {}
    pr = payload.get("pull_request") or {}
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    number = pr.get("number", payload.get("number"))
    base_ref = base.get("ref")
    head_sha = head.get("sha", "")
    force = payload.get("ralph_force", False)
    html_url = repo.get("html_url", "").rstrip("/")
    if (
        repo.get("full_name") != bot_config.repository
        or (
            bot_config.repository_url is not None
            and html_url != bot_config.repository_url.rstrip("/")
        )
        or not isinstance(number, int)
        or isinstance(number, bool)
        or number < 1
        or not isinstance(base_ref, str)
        or not BRANCH.fullmatch(base_ref)
        or base_ref.startswith("-")
        or ".." in base_ref
        or "//" in base_ref
        or base_ref.endswith("/")
        or base_ref.endswith(".lock")
        or not isinstance(head_sha, str)
        or not SHA.fullmatch(head_sha)
    ):
        raise ValueError("invalid or unexpected pull request payload")
    if not isinstance(force, bool):
        raise ValueError("invalid review override")
    if force and "sha" in base:
        historical_base = base["sha"]
        if not isinstance(historical_base, str) or not SHA.fullmatch(historical_base):
            raise ValueError("invalid historical base")
        base_ref = f"sha:{historical_base}"
    return number, base_ref, head_sha, payload["action"], force


def requeue_latest_head(
    jobs, bot_config, number, base_ref, expected_head, action, engine_identity, force
):
    latest_head = repository.current_head(bot_config, number)
    if latest_head == expected_head:
        return
    logging.info(
        "review stale head pr=%d expected=%s latest=%s; requeueing",
        number,
        short_sha(expected_head),
        short_sha(latest_head),
    )
    jobs.enqueue(
        number, base_ref, latest_head, action, force, engine_identity=engine_identity
    )


def short_sha(sha):
    return sha[:12]


def _budget_summary(budget, attempt, name):
    if budget is None:
        return None
    try:
        return budget.summary()
    except sqlite3.Error:
        return attempt.get(name)


def metric_value(value):
    return "unknown" if value is None else value


def _read_engine_config(path, parser):
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        parser.error(f"could not read engine configuration: {exc}")
    if not isinstance(value, dict):
        parser.error("engine configuration must be a JSON object")
    return value


def log_review_outcome(
    number,
    action,
    head_sha,
    outcome,
    started,
    debug,
    metrics,
    level=logging.INFO,
    stage=None,
    error_type=None,
    http_status=None,
    http_host=None,
    review_status="unknown",
):
    message = (
        "review outcome pr=%d action=%s head=%s outcome=%s "
        "review_status=%s elapsed_seconds=%.2f model_turns=%s tool_calls=%s "
        "input_tokens=%s output_tokens=%s estimated_usd=%s"
    )
    values = [
        number,
        action,
        short_sha(head_sha),
        outcome,
        review_status,
        time.monotonic() - started,
        metric_value(metrics.get("model_turns")),
        metric_value(metrics.get("tool_calls")),
        metric_value(metrics.get("total_input_tokens")),
        metric_value(metrics.get("total_output_tokens")),
        metric_value(metrics.get("estimated_cost_usd")),
    ]
    if stage is not None:
        message += " stage=%s error_type=%s http_status=%s http_host=%s"
        values.extend(
            [stage, error_type, metric_value(http_status), metric_value(http_host)]
        )
    logging.log(level, message, *values)


def retryable_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 403 and exc.headers is not None:
            if (
                exc.headers.get("Retry-After")
                or exc.headers.get("X-RateLimit-Remaining") == "0"
            ):
                return True
        return exc.code == 429 or 500 <= exc.code < 600
    return isinstance(
        exc, (urllib.error.URLError, TimeoutError, subprocess.TimeoutExpired)
    )


def review_case_fingerprint(metadata, evidence):
    return hashlib.sha256(
        json.dumps(
            {"metadata": metadata, "evidence": evidence},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def process_job(
    job,
    jobs,
    state_dir,
    api_key,
    forgejo_token,
    bot_login,
    bot_config,
    ledger,
    review_engine,
    allow_discussions=True,
    github_token=None,
    github_post_major_findings=False,
):
    number = job["number"]
    base_ref = job["base_ref"]
    expected_head = job["head"]
    action = job["action"]
    force = job["force"]
    checkout = state_dir / "checkout"
    ref_prefix = f"refs/ralph/jobs/{job['id']}-{job['generation']}"
    started = time.monotonic()
    debug = {}
    content = ""
    output = None
    result_record = None
    engine_identity = (
        review_engine.identity if review_engine is not None else job["engine_identity"]
    )
    stage = "precheck"
    outcome = "failed"
    failure = None
    runtime = None
    run_status = "failed"
    base_sha = None
    head_sha = expected_head
    merge_base = None
    expected_context = None
    review_fingerprint = None
    backport_evidence = None
    profile = (engine_identity.get("config") or {}).get("profile")
    if engine_identity["name"] == "current" and profile is not None:
        ledger = profiles.ReviewProfile.from_dict(profile).apply_budget(ledger)
    budget = spend.RequestBudget(
        ledger,
        review_id=f"pr:{number}:{job['id']}:{job['generation']}",
    )

    def is_current():
        return (
            jobs.is_current(job)
            and repository.current_head(bot_config, number) == expected_head
            and (
                expected_context is None
                or forgejo.pull_request_context(bot_config, forgejo_token, number)
                == expected_context
            )
        )

    try:
        try:
            logging.info(
                "review start pr=%d action=%s head=%s force=%s",
                number,
                action,
                short_sha(expected_head),
                force,
            )
            saved = job["review_result"]
            if saved is None:
                if review_engine is None:
                    raise ValueError("queued review engine is unavailable")
                if job["engine_identity"] != engine_identity:
                    raise ValueError(
                        "queued engine identity no longer matches this worker"
                    )
                if not is_current():
                    raise model.StaleReview("PR head changed")
                comment = (
                    None
                    if force
                    else forgejo.find_comment(
                        bot_config, forgejo_token, number, bot_login
                    )
                )
                stage = "context"
                expected_context = forgejo.pull_request_context(
                    bot_config, forgejo_token, number
                )
                if (
                    not base_ref.startswith("sha:")
                    and expected_context.get("base_ref")
                    and expected_context["base_ref"] != base_ref
                ):
                    raise model.StaleReview("PR target branch changed")
                title, description = expected_context["title"], expected_context["body"]
                metadata = {
                    **expected_context,
                    "base_ref": expected_context.get("base_ref") or base_ref,
                    "repository": bot_config.github_repository or bot_config.repository,
                }
                stage = "collect"
                with CHECKOUT_LOCK:
                    base_sha, head_sha, review, skip = repository.collect_review(
                        checkout,
                        number,
                        base_ref,
                        expected_head,
                        title,
                        description,
                        bot_config,
                        ref_prefix=ref_prefix,
                    )
                    if head_sha != expected_head:
                        if jobs.is_current(job):
                            jobs.enqueue(
                                number,
                                base_ref,
                                head_sha,
                                action,
                                force,
                                engine_identity=engine_identity,
                            )
                        raise model.StaleReview("PR head changed during fetch")
                    snapshot = (
                        None
                        if skip
                        else local_repository.snapshot_repository(
                            checkout, base_sha, head_sha
                        )
                    )
                    merge_base = getattr(snapshot, "merge_base", None)
                    if snapshot is not None:
                        backport_evidence = repository.collect_backport_evidence(
                            snapshot, metadata, bot_config, ref_prefix, github_token
                        )
                    review_fingerprint = review_case_fingerprint(
                        metadata, backport_evidence
                    )
                    if forgejo.comment_matches_engine(
                        comment, base_sha, head_sha, engine_identity, review_fingerprint
                    ):
                        jobs.complete(job)
                        outcome = "already-reviewed"
                        run_status = "unknown"
                        return outcome
                feedback = (
                    collect_review_feedback(
                        jobs.path,
                        job,
                        bot_config,
                        checkout,
                        head_sha,
                        github_token=github_token,
                        allow_discussions=allow_discussions,
                    )
                    if not skip
                    else None
                )
                debug = {"skip": skip} if skip else {}
                stage = "model"
                if skip:
                    output = engine.Output(f"Skipped: {skip}", {}, {})
                else:
                    case = engine.ReviewCase(
                        review,
                        snapshot,
                        backport_evidence=backport_evidence,
                        review_feedback=feedback,
                        source={
                            **metadata,
                            "kind": "pull_request",
                            "number": number,
                            "url": f"{bot_config.repository_url}/pulls/{number}",
                        },
                    )
                    runtime = engine.Runtime(
                        case=case,
                        api_key=api_key,
                        budget=budget,
                        is_current=is_current,
                        discussion_lookup=(
                            discussion_lookup(bot_config, number)
                            if allow_discussions
                            else None
                        ),
                        allow_web_search=allow_discussions,
                        attempt=debug,
                    )
                    saved = runner.run_review(case, runtime, review_engine)
                    if saved.pop("diff_base_sha") != saved["merge_base"]:
                        raise ValueError("service review must use the merge base")
                debug = runtime.attempt if runtime else debug
                if skip:
                    saved = {
                        "schema_version": 1,
                        "engine": engine_identity,
                        "base_sha": base_sha,
                        "head_sha": head_sha,
                        "merge_base": merge_base,
                        "content": output.content,
                        "output": {
                            "schema_version": output.schema_version,
                            "data": output.data,
                            "exports": output.exports,
                        },
                        "run": {
                            "review_id": budget.review_id,
                            "status": "completed",
                            "metrics": {},
                            "skipped": True,
                            "budget": budget.summary(),
                            "attempt": debug,
                        },
                    }
                content = saved["content"]
                saved["run"]["review_completed_at"] = forgejo._retrieved_at()
                saved["run"]["review_scope"] = {
                    "repository": bot_config.repository_url,
                    "number": number,
                }
                saved["run"]["review_feedback"] = feedback
                saved["run"]["review_feedback_fingerprint"] = (
                    feedback["fingerprint"] if feedback else None
                )
                saved["run"]["pr_title"] = title
                saved["run"]["review_context"] = expected_context
                saved["run"]["review_fingerprint"] = review_fingerprint
                run_status = saved["run"]["status"]
                result_record = saved
                if not jobs.save_result(job, saved):
                    outcome = "superseded"
                    return outcome
            else:
                result_record = saved
                run_status = saved["run"]["status"]
                engine_identity = saved["engine"]
                base_sha = saved["base_sha"]
                head_sha = saved["head_sha"]
                content = saved["content"]
                debug = saved["run"]["attempt"]
                expected_context = saved["run"].get("review_context")
                review_fingerprint = saved["run"].get("review_fingerprint")
            result_record = saved
            if not is_current():
                raise model.StaleReview("PR head changed before publication")
            if bot_config.report_dir is not None:
                stage = "report"
                report_id = f"{number}-{head_sha}-{job['id']}-{job['generation']}"
                try:
                    filename = report.save_report(
                        bot_config.report_dir, number, result_record, report_id
                    )
                    result_record["run"]["report_url"] = (
                        f"{bot_config.report_base_url}/{filename}"
                    )
                except OSError as exc:
                    result_record["run"].pop("report_url", None)
                    logging.warning(
                        "Could not save public review report for PR %d: %s",
                        number,
                        type(exc).__name__,
                    )
                if not jobs.save_result(job, result_record):
                    outcome = "superseded"
                    return outcome
            stage = "publish"
            result = forgejo.publish_review(
                bot_config,
                forgejo_token,
                number,
                bot_login,
                base_sha,
                head_sha,
                content,
                engine_identity=engine_identity,
                report_url=result_record["run"].get("report_url"),
                review_fingerprint=review_fingerprint,
                expected_context=expected_context,
            )
            if result == "stale":
                if jobs.is_current(job):
                    requeue_latest_head(
                        jobs,
                        bot_config,
                        number,
                        base_ref,
                        head_sha,
                        action,
                        engine_identity,
                        force,
                    )
                jobs.supersede(job)
            else:
                if github_post_major_findings:
                    if not is_current():
                        raise model.StaleReview(
                            "PR head changed before GitHub publication"
                        )
                    stage = "github"
                    github.publish_major_findings(
                        bot_config.github_repository,
                        github_token,
                        number,
                        result_record,
                    )
                jobs.complete(job)
            outcome = result
            return outcome
        except model.StaleReview:
            stage = "stale"
            if jobs.is_current(job):
                requeue_latest_head(
                    jobs,
                    bot_config,
                    number,
                    base_ref,
                    expected_head,
                    action,
                    engine_identity,
                    force,
                )
            jobs.supersede(job)
            outcome = "stale"
            run_status = "cancelled"
            return outcome
    except Exception as exc:
        failure = exc
        outcome = (
            jobs.retry(job, exc)
            if retryable_error(exc)
            else "failed"
            if jobs.fail(job, exc)
            else "lost-claim"
        )
        if outcome is None:
            outcome = "lost-claim"
        return outcome
    finally:
        if (checkout / ".git").exists():
            try:
                with CHECKOUT_LOCK:
                    repository.release_source_refs(checkout, ref_prefix)
                    local_repository.release_review_refs(checkout, ref_prefix)
            except (OSError, subprocess.SubprocessError) as exc:
                logging.warning(
                    "Could not release review refs for PR %d: %s",
                    number,
                    type(exc).__name__,
                )
        if runtime is not None and job["review_result"] is None:
            try:
                if result_record is None:
                    result_record = {
                        "schema_version": 1,
                        "engine": engine_identity,
                        "base_sha": base_sha,
                        "head_sha": head_sha,
                        "merge_base": merge_base,
                        "content": content or None,
                        "output": (
                            None
                            if output is None
                            else {
                                "schema_version": output.schema_version,
                                "data": output.data,
                                "exports": output.exports,
                            }
                        ),
                        "run": {
                            "review_id": budget.review_id,
                            "status": run_status,
                            "metrics": runtime.metrics() if runtime else {},
                            "skipped": False,
                            "budget": _budget_summary(budget, debug, "budget"),
                            "attempt": debug,
                        },
                    }
                trace.save_review_trace(state_dir, number, result_record)
            except (OSError, TypeError, ValueError) as exc:
                logging.warning(
                    "Could not save private review trace for PR %d: %s",
                    number,
                    type(exc).__name__,
                )
        http_status = (
            failure.code if isinstance(failure, urllib.error.HTTPError) else None
        )
        http_host = (
            urllib.parse.urlsplit(failure.url or "").hostname
            if isinstance(failure, urllib.error.HTTPError)
            else None
        )
        log_review_outcome(
            number,
            action,
            expected_head,
            outcome,
            started,
            debug,
            runtime.metrics() if runtime else {},
            logging.ERROR if failure or run_status == "failed" else logging.INFO,
            debug.get("pipeline_stage", stage) if failure else None,
            type(failure).__name__ if failure else None,
            http_status,
            http_host,
            review_status=run_status,
        )
        try:
            summary = ledger.summary()
            logging.info(
                "monthly spend month=%s estimated_usd=%.6f reserved_usd=%.6f "
                "unknown_requests=%d usage_complete=%s",
                summary["month"],
                summary["estimated_total_usd"],
                summary["reserved_total_usd"],
                summary["unknown_request_count"],
                summary["usage_complete"],
            )
        except sqlite3.Error as exc:
            logging.error(
                "Could not read monthly spend summary: %s", type(exc).__name__
            )


def consume_review_requests(store, jobs, bot_config, token, high_engine):
    """Replay durable Telegram requests safely after either process restarts."""
    with closing(store._connect()) as db:
        requests = db.execute(
            "SELECT id, number FROM review_requests WHERE processed = 0 ORDER BY id"
        ).fetchall()
    for request_id, number in requests:
        try:
            if high_engine is None:
                raise ValueError("high reviews require the current engine")
            pr = forgejo.forgejo_request(bot_config, token, f"/pulls/{number}")
            if pr.get("number") != number or pr.get("state") != "open":
                raise ValueError("pull request is not open")
            event = parse_event(
                bot_config,
                "pull_request",
                {
                    "action": "opened",
                    "repository": {
                        "full_name": bot_config.repository,
                        "html_url": bot_config.repository_url or "",
                    },
                    "pull_request": pr,
                    "ralph_force": True,
                },
            )
            # Use the current base branch, not a historical base commit.
            number, _, head, action, force = event
            base_ref = pr["base"]["ref"]
            jobs.enqueue(
                number,
                base_ref,
                head,
                action,
                force,
                engine_identity=high_engine.identity,
                request_id=f"telegram:{request_id}",
            )
            message = f"Queued high review of PR #{number}."
        except ValueError as exc:
            message = f"Could not queue high review of PR #{number}: {exc}."
        except urllib.error.HTTPError as exc:
            if exc.code not in {404, 422}:
                logging.warning(
                    "Could not fetch PR %d for Telegram review: HTTP %d; will retry",
                    number,
                    exc.code,
                )
                continue
            message = f"Could not find PR #{number}."
        except (OSError, urllib.error.URLError, sqlite3.Error):
            logging.exception(
                "Could not queue Telegram review of PR %d; will retry", number
            )
            continue
        with closing(store._connect()) as db, db:
            db.execute(
                "UPDATE review_requests SET processed = 1 WHERE id = ?", (request_id,)
            )
            db.execute(
                "INSERT OR IGNORE INTO outbox (key, text) VALUES (?, ?)",
                (f"command:result:{request_id}", message),
            )


def command_worker(store, jobs, bot_config, token, high_engine):
    while True:
        try:
            consume_review_requests(store, jobs, bot_config, token, high_engine)
        except Exception:
            logging.exception("Could not process Telegram review requests")
        time.sleep(5)


def worker(
    jobs,
    state_dir,
    api_key,
    forgejo_token,
    bot_login,
    bot_config,
    ledger,
    review_engine,
    allow_discussions=True,
    stop_event=None,
    github_token=None,
    github_post_major_findings=False,
):
    stop_event = stop_event or threading.Event()
    while not stop_event.is_set():
        try:
            job = jobs.claim()
        except Exception:
            logging.exception("Could not claim review job")
            stop_event.wait(1)
            continue
        if job is None:
            stop_event.wait(1)
            continue
        try:
            if job["review_result"] is None:
                identity = job["engine_identity"]
                job_engine = (
                    review_engine
                    if identity is None
                    or identity.get("config") is None
                    or identity == review_engine.identity
                    else engine.load(
                        identity["name"], identity["config"], configured=True
                    )
                )
                if not jobs.bind_engine(job, job_engine.identity):
                    continue
            else:
                job_engine = None
            process_job(
                job,
                jobs,
                state_dir,
                api_key,
                forgejo_token,
                bot_login,
                bot_config,
                ledger,
                job_engine,
                allow_discussions,
                github_token,
                github_post_major_findings,
            )
        except Exception as exc:
            logging.exception(
                "Review worker failed unexpectedly for PR %d", job["number"]
            )
            try:
                if retryable_error(exc):
                    jobs.retry(job, exc)
                else:
                    jobs.fail(job, exc)
            except Exception:
                logging.exception(
                    "Could not record failed review for PR %d", job["number"]
                )
            stop_event.wait(1)


def make_handler(secret, jobs, bot_config, engine_identity=None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            request_path = urllib.parse.urlsplit(self.path).path
            match = re.fullmatch(
                rf"/assets/{re.escape(bot_config.repository)}/([1-9][0-9]{{0,18}})\.png",
                request_path,
            )
            if match is None or int(match[1]) > 2**63 - 1:
                self.send_error(404)
                return
            try:
                result = jobs.latest_completed_result(int(match[1]))
            except sqlite3.Error:
                logging.exception("Could not read review badge status")
                self.send_error(503)
                return
            debug = core_trace.current_diagnostics(result) if result else {}
            if result is not None and core_trace.unresolved_memory(result):
                image = badge.ORANGE
            elif result is None or debug.get("skip") or "decisions" not in debug:
                image = badge.GREY
            elif any(
                decision.get("disposition") == "publish"
                for decision in debug.get("decisions", [])
            ) or core_trace.published_concept_concern(debug):
                image = badge.ORANGE
            else:
                image = badge.GREY
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(image)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(image)

        def do_POST(self):
            request_path = urllib.parse.urlsplit(self.path).path
            if request_path != "/webhooks/forgejo":

                def safe(value):
                    return (
                        "".join(char for char in value if char.isprintable())[:200]
                        or "-"
                    )

                logging.warning(
                    "webhook rejected reason=unexpected_path path=%s host=%s event=%s signature_present=%s",
                    safe(request_path),
                    safe(self.headers.get("Host", "")),
                    safe(self.headers.get("X-Forgejo-Event", "")),
                    bool(self.headers.get("X-Forgejo-Signature")),
                )
                self.send_error(404)
                return
            size = self.headers.get("Content-Length", "")
            if not size.isdecimal() or int(size) > MAX_BODY:
                self.send_error(413)
                return
            body = self.rfile.read(int(size))
            if not valid_signature(
                body, self.headers.get("X-Forgejo-Signature"), secret
            ):
                self.send_error(401)
                return
            try:
                job = parse_event(
                    bot_config, self.headers.get("X-Forgejo-Event"), json.loads(body)
                )
            except (ValueError, TypeError, AttributeError):
                self.send_error(400)
                return
            if job:
                number, base_ref, head_sha, action, force = job
                try:
                    queued = jobs.enqueue(
                        number,
                        base_ref,
                        head_sha,
                        action,
                        force,
                        engine_identity=engine_identity,
                    )
                except Exception:
                    logging.exception("Could not persist webhook job for PR %d", number)
                    self.send_error(503)
                    return
                logging.info(
                    "review enqueue pr=%d action=%s head=%s force=%s queued=%s",
                    number,
                    action,
                    short_sha(head_sha),
                    force,
                    queued,
                )
            self.send_response(202)
            self.end_headers()

        def log_message(self, format, *args):
            log = logging.debug if self.command == "GET" else logging.info
            message = (format % args).replace(
                self.path, urllib.parse.urlsplit(self.path).path
            )
            log("Webhook request: %s", message)

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--workers", type=int, default=3, help="Maximum concurrent pull request reviews"
    )
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument(
        "--migrate-state",
        action="store_true",
        help="Back up and upgrade existing state databases once",
    )
    parser.add_argument(
        "--engine", default="current", help="Review engine name (default: current)"
    )
    parser.add_argument(
        "--engine-config",
        type=Path,
        help="JSON configuration object for the selected engine",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        help="Public directory for static HTML and JSON review reports",
    )
    parser.add_argument(
        "--report-base-url", help="Public HTTP URL serving the report directory"
    )
    parser.add_argument(
        "--origin", help="Git remote URL used to fetch the base branch and PR heads"
    )
    parser.add_argument(
        "--repository", help="Forgejo repository full name, such as owner/repo"
    )
    parser.add_argument(
        "--forgejo-api",
        help="Forgejo repository API URL, ending in /api/v1/repos/owner/repo",
    )
    parser.add_argument(
        "--repository-url", help="Expected repository HTML URL from webhook payloads"
    )
    parser.add_argument(
        "--comment-marker", help="Hidden marker used to find the bot's editable comment"
    )
    parser.add_argument("--openai-key-file", type=Path)
    parser.add_argument("--webhook-secret-file", type=Path)
    parser.add_argument("--forgejo-token-file", type=Path)
    parser.add_argument("--bot-login")
    parser.add_argument(
        "--github-repository",
        help="Upstream GitHub owner/repo with matching PR numbers",
    )
    parser.add_argument("--github-token-file", type=Path)
    parser.add_argument(
        "--github-post-major-findings",
        action="store_true",
        help="Publish major findings upstream using the GitHub credentials",
    )
    parser.add_argument(
        "--prompt-file", type=Path, help="Markdown file containing the review prompt"
    )
    parser.add_argument(
        "--audit-prompt-dir",
        type=Path,
        help="Directory containing the focused Luna audit prompts",
    )
    parser.add_argument(
        "--models-json", type=Path, help="JSON model routing for each review stage"
    )
    parser.add_argument("--review-budget-usd", type=float, default=5.00)
    parser.add_argument("--monthly-budget-usd", type=float)
    parser.add_argument("--routing-mode", choices=("enabled", "shadow", "full"))
    args = parser.parse_args()
    if args.migrate_state:
        if args.state_dir is None:
            parser.error("--state-dir is required with --migrate-state")
        from .jobs import migrate_state

        try:
            jobs_path = args.state_dir / "jobs.sqlite3"
            migrated = jobs_path.exists() and migrate_state(jobs_path)
            for filename in (
                "spend.sqlite3",
                "ppq-spend.sqlite3",
                "followup/spend.sqlite3",
            ):
                if spend.migrate_state(args.state_dir / filename):
                    migrated = True
        except (OSError, sqlite3.Error, ValueError) as exc:
            parser.error(f"could not migrate state databases: {exc}")
        print(
            "state databases migrated"
            if migrated
            else "state databases already current"
        )
        return 0
    required = (
        "state_dir",
        "origin",
        "repository",
        "forgejo_api",
        "openai_key_file",
        "webhook_secret_file",
        "forgejo_token_file",
        "bot_login",
    )
    missing = [
        f"--{name.replace('_', '-')}"
        for name in required
        if getattr(args, name) is None
    ]
    if missing:
        parser.error("required options: " + ", ".join(missing))
    if args.workers < 1:
        parser.error("workers must be positive")
    if (args.github_repository is None) != (args.github_token_file is None):
        parser.error("--github-repository and --github-token-file must be set together")
    if args.github_post_major_findings:
        if args.github_repository is None:
            parser.error("--github-post-major-findings requires GitHub credentials")
        if args.report_dir is None or args.report_base_url is None:
            parser.error("GitHub publication requires public reports")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    bot_config = config.BotConfig(
        args.origin,
        args.repository,
        args.forgejo_api,
        args.repository_url,
        args.comment_marker,
        args.report_dir,
        args.report_base_url,
        args.github_repository,
    )
    current_only = (
        args.prompt_file is not None
        or args.audit_prompt_dir is not None
        or args.models_json is not None
        or args.routing_mode is not None
    )
    if args.engine != "current" and current_only:
        parser.error(
            "prompt, model, and routing options apply only to --engine current"
        )
    engine_config = _read_engine_config(args.engine_config, parser)
    engine_config.update(
        {
            key: value
            for key, value in {
                "prompt_file": args.prompt_file,
                "audit_prompt_dir": args.audit_prompt_dir,
                "models_json": args.models_json,
                "routing_mode": args.routing_mode,
            }.items()
            if value is not None
        }
    )
    if args.engine == "current":
        engine_config.setdefault("archaeology", True)
    try:
        review_engine = engine.load(args.engine, engine_config)
        high_engine = (
            engine.load("current", {**engine_config, "profile": "high"})
            if args.engine == "current"
            else None
        )
    except (ImportError, TypeError, ValueError) as exc:
        parser.error(f"could not load review engine: {exc}")
    api_key = args.openai_key_file.read_text().strip()
    secret = args.webhook_secret_file.read_bytes().strip()
    forgejo_token = args.forgejo_token_file.read_text().strip()
    github_token = (
        args.github_token_file.read_text().strip() if args.github_token_file else None
    )
    if github_token == "":
        parser.error("GitHub token file must not be empty")
    if not api_key or not secret or not forgejo_token or not args.bot_login:
        parser.error("secret files must not be empty")
    args.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    args.state_dir.chmod(0o700)
    try:
        jobs = JobStore(args.state_dir / "jobs.sqlite3")
    except RuntimeError as exc:
        parser.error(str(exc))
    recovered = jobs.recover()
    command_store = monitor.NotificationStore(args.state_dir / "telegram.sqlite3")
    ledger = spend.Ledger(
        args.state_dir / "spend.sqlite3",
        review_limit_usd=args.review_budget_usd,
        monthly_limit_usd=args.monthly_budget_usd,
    )
    logging.info("review queue recovered_claims=%d workers=%d", recovered, args.workers)
    threading.Thread(
        target=command_worker,
        name="review-commands",
        args=(command_store, jobs, bot_config, forgejo_token, high_engine),
        daemon=True,
    ).start()
    for index in range(args.workers):
        threading.Thread(
            target=worker,
            name=f"review-{index + 1}",
            args=(
                jobs,
                args.state_dir,
                api_key,
                forgejo_token,
                args.bot_login,
                bot_config,
                ledger,
                review_engine,
            ),
            kwargs={
                "github_token": github_token,
                "github_post_major_findings": args.github_post_major_findings,
            },
            daemon=True,
        ).start()
    server = ThreadingHTTPServer(
        (args.listen, args.port),
        make_handler(secret, jobs, bot_config, review_engine.identity),
    )
    server.serve_forever()
