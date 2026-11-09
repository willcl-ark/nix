"""Webhook server and review worker."""

from contextlib import closing, contextmanager, ExitStack
import fcntl
import hashlib
import hmac
import json
import logging
import re
import signal
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler

from ralph import trace as core_trace
from . import (
    badge,
    comments,
    delivery,
    forgejo,
    github,
    history,
    report,
    repository,
    trace,
)
from ralph import repository as local_repository
from ralph import engine, model, runner
from ralph import usage as usage_tracking
from .research import discussion_lookup
from .review_feedback import collect_review_feedback
from ralph.repository import BRANCH, SHA

MAX_BODY = 1024 * 1024


@contextmanager
def checkout_lock(state_dir):
    # Every review process uses the same repository checkout.
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state_dir / "checkout.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


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


def reconcile_stale_review(jobs, number, base_ref, action, engine_identity):
    # Refresh existing intent without repeating a forced same-head review.
    jobs.reconcile_hint(number, base_ref, action, engine_identity=engine_identity)


def short_sha(sha):
    return sha[:12]


def _usage_summary(usage, attempt, name):
    if usage is None:
        return None
    try:
        return usage.summary()
    except sqlite3.Error:
        return attempt.get(name)


def metric_value(value):
    return "unknown" if value is None else value


def log_review_outcome(
    repository,
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
        "review outcome repo=%s pr=%d action=%s head=%s outcome=%s "
        "review_status=%s elapsed_seconds=%.2f model_turns=%s tool_calls=%s "
        "input_tokens=%s output_tokens=%s"
    )
    values = [
        repository,
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
        exc,
        (
            urllib.error.URLError,
            TimeoutError,
            subprocess.TimeoutExpired,
            repository.FetchError,
        ),
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
    github_publication="off",
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
    resumed_failure = False
    runtime = None
    run_status = "failed"
    base_sha = None
    head_sha = expected_head
    merge_base = None
    expected_context = None
    review_fingerprint = None
    backport_evidence = None
    usage = usage_tracking.RequestUsage(
        ledger,
        review_id=f"pr:{bot_config.repo_id}:{number}:{job['id']}:{job['generation']}",
    )

    def is_current():
        return (
            jobs.is_current(job)
            and repository.current_head(bot_config, number) == expected_head
            and (
                expected_context is None
                or forgejo.pull_request_context(bot_config, forgejo_token, number)[
                    "base_ref"
                ]
                == expected_context["base_ref"]
            )
        )

    try:
        try:
            logging.info(
                "review start repo=%s pr=%d action=%s head=%s force=%s",
                bot_config.repository,
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
                    if force or github_publication == "full"
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
                    and expected_context.get("base_ref") != base_ref
                ):
                    raise model.StaleReview("PR target branch changed")
                title, description = expected_context["title"], expected_context["body"]
                metadata = {
                    **expected_context,
                    "base_ref": expected_context.get("base_ref") or base_ref,
                    "repository": bot_config.github_repository or bot_config.repository,
                }
                stage = "collect"
                with checkout_lock(state_dir):
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
                            reconcile_stale_review(
                                jobs, number, base_ref, action, engine_identity
                            )
                        raise model.StaleReview("PR head changed during fetch")
                    snapshot = (
                        None
                        if skip
                        else local_repository.snapshot_repository(
                            checkout,
                            base_sha,
                            head_sha,
                            submodules=bot_config.submodules,
                            experiment_shell=bot_config.experiment_shell,
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
                            "url": forgejo.pull_request_url(bot_config, number),
                        },
                    )
                    runtime = engine.Runtime(
                        case=case,
                        api_key=api_key,
                        usage=usage,
                        is_current=lambda: jobs.is_current(job),
                        is_cancelled=lambda: not jobs.is_current(job),
                        discussion_lookup=(
                            discussion_lookup(bot_config, number, github_token)
                            if allow_discussions
                            else None
                        ),
                        allow_web_search=allow_discussions,
                        attempt=debug,
                        log_context={
                            "repo": bot_config.repository,
                            "repo_id": job["repo_id"],
                            "pr": number,
                            "job": job["id"],
                            "generation": job["generation"],
                            "attempt": job["attempts"] + 1,
                        },
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
                            "review_id": usage.review_id,
                            "status": "completed",
                            "metrics": {},
                            "skipped": True,
                            "usage": usage.summary(),
                            "attempt": debug,
                        },
                    }
                content = saved["content"]
                saved["run"]["repository_id"] = bot_config.repo_id
                saved["run"]["repository"] = bot_config.repository
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
                if not expected_context or not expected_context.get("base_ref"):
                    raise ValueError(
                        "saved review lacks target identity; explicit recapture required"
                    )
            result_record = saved
            forgejo_delivery = delivery.Session(
                jobs, job, "forgejo:" + bot_config.forgejo_api
            )
            github_delivery = delivery.Session(
                jobs, job, "github:" + (bot_config.github_repository or "")
            )
            pending = delivery.held(jobs)
            destinations = {
                row["destination"] for row in pending if row["number"] == number
            }
            if forgejo_delivery.destination in destinations:
                forgejo.reconcile_delivery(
                    bot_config, forgejo_token, number, bot_login, forgejo_delivery
                )
            if github_delivery.destination in destinations and github_token:
                github.reconcile_delivery(
                    bot_config.github_repository, github_token, number, github_delivery
                )
            forgejo_delivery.check_hold()
            if not is_current():
                raise model.StaleReview("PR head changed before publication")
            if bot_config.report_dir is not None:
                stage = "report"
                report_id = f"{number}-{head_sha}-{job['id']}-{job['generation']}"
                try:
                    filename = report.save_report(
                        bot_config.report_dir,
                        number,
                        result_record,
                        report_id,
                        repository_id=bot_config.repo_id,
                        repository=bot_config.repository,
                        repository_url=bot_config.repository_url,
                        report_base_url=bot_config.report_base_url,
                        archive_base_url=bot_config.archive_base_url,
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
                if result_record["run"].get("report_url"):
                    try:
                        history.save_history(
                            bot_config.report_dir,
                            number,
                            report_base_url=bot_config.report_base_url,
                            archive_base_url=bot_config.archive_base_url,
                        )
                    except OSError as exc:
                        logging.warning(
                            "Could not refresh review history for PR %d: %s",
                            number,
                            type(exc).__name__,
                        )
                if not jobs.save_result(job, result_record):
                    outcome = "superseded"
                    return outcome
            if (
                github_publication != "full"
                and not forgejo_delivery.has_intent()
                and engine_identity["name"] == "current"
                and not saved["run"].get("skipped")
            ):
                content = comments.review_comment(saved)
            result = "unchanged"
            if github_publication != "full":
                stage = "publish"
                result = forgejo.publish_review(
                    bot_config,
                    forgejo_token,
                    number,
                    bot_login,
                    base_sha,
                    head_sha,
                    content,
                    source_base_sha=result_record["merge_base"],
                    engine_identity=engine_identity,
                    report_url=result_record["run"].get("report_url"),
                    review_fingerprint=review_fingerprint,
                    expected_context=expected_context,
                    is_current=lambda: jobs.is_current(job),
                    delivery=forgejo_delivery,
                )
            if result == "stale":
                if jobs.is_current(job):
                    reconcile_stale_review(
                        jobs, number, base_ref, action, engine_identity
                    )
                jobs.supersede(job)
            else:
                if github_publication != "off":
                    if not is_current():
                        raise model.StaleReview(
                            "PR head changed before GitHub publication"
                        )
                    stage = "github"
                    github_result = github.publish_review(
                        bot_config.github_repository,
                        github_token,
                        number,
                        result_record,
                        publication=github_publication,
                        expected_context=expected_context,
                        is_current=lambda: jobs.is_current(job),
                        delivery=github_delivery,
                    )
                    if github_publication == "full":
                        result = github_result
                    if github_result == "stale":
                        raise model.StaleReview("PR changed before GitHub publication")
                jobs.complete(job)
            outcome = result
            return outcome
        except model.StaleReview:
            stage = "stale"
            if jobs.is_current(job):
                reconcile_stale_review(jobs, number, base_ref, action, engine_identity)
            jobs.supersede(job)
            outcome = "stale"
            run_status = "cancelled"
            return outcome
    except Exception as exc:
        failure = exc
        if isinstance(exc, repository.FetchError):
            logging.warning(
                "review fetch failed repo=%s pr=%d job=%d error=%s",
                bot_config.repository,
                number,
                job["id"],
                exc,
            )
        if (
            github_publication == "full"
            and result_record is None
            and job["review_result"] is None
            and (expected_context is not None or not base_ref.startswith("sha:"))
            and (not retryable_error(exc) or job["attempts"] + 1 >= jobs.max_attempts)
            and jobs.is_current(job)
        ):
            # Persist a safe terminal result before releasing the claim. The saved
            # result path below handles reports and publication retries without
            # repeating the failed review or exposing the exception text.
            result_record = {
                "schema_version": 1,
                "engine": engine_identity,
                "base_sha": base_sha,
                "head_sha": expected_head,
                "merge_base": merge_base,
                "content": "Review failed before completion.",
                "output": {
                    "schema_version": 1,
                    "data": {},
                    "exports": {"findings": []},
                },
                "run": {
                    "review_id": usage.review_id,
                    "usage": _usage_summary(usage, debug, "usage"),
                    "status": "failed",
                    "skipped": False,
                    "attempt": {},
                    "metrics": runtime.metrics() if runtime else {},
                    "review_context": expected_context or {"base_ref": base_ref},
                    "review_completed_at": forgejo._retrieved_at(),
                    "repository_id": bot_config.repo_id,
                    "repository": bot_config.repository,
                },
            }
            if jobs.save_result(job, result_record):
                logging.warning(
                    "terminal review failure repo=%s pr=%d stage=%s error_type=%s",
                    bot_config.repository,
                    number,
                    stage,
                    type(exc).__name__,
                )
                resumed_failure = True
                return process_job(
                    {**job, "review_result": result_record},
                    jobs,
                    state_dir,
                    api_key,
                    forgejo_token,
                    bot_login,
                    bot_config,
                    ledger,
                    review_engine,
                    allow_discussions=allow_discussions,
                    github_token=github_token,
                    github_publication=github_publication,
                )
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
                with checkout_lock(state_dir):
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
                            "review_id": usage.review_id,
                            "status": run_status,
                            "metrics": runtime.metrics() if runtime else {},
                            "skipped": False,
                            "usage": _usage_summary(usage, debug, "usage"),
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
        if not resumed_failure:
            log_review_outcome(
                bot_config.repository,
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
                "monthly usage month=%s requests=%d "
                "unknown_requests=%d usage_complete=%s",
                summary["month"],
                summary["request_count"],
                summary["unknown_request_count"],
                summary["usage_complete"],
            )
        except sqlite3.Error as exc:
            logging.error(
                "Could not read monthly usage summary: %s", type(exc).__name__
            )


def consume_review_requests(
    store, jobs, bot_config, token, default_engine, high_engine
):
    """Replay durable Telegram requests safely after either process restarts."""
    with closing(store._connect()) as db:
        requests = db.execute(
            "SELECT id, number, profile FROM review_requests WHERE processed = 0 AND repo_id = ? ORDER BY id",
            (bot_config.repo_id,),
        ).fetchall()
    for request_id, number, profile in requests:
        label = "high" if profile == "high" else "normal"
        try:
            selected = {"default": default_engine, "high": high_engine}.get(profile)
            if selected is None:
                raise ValueError(f"{label} review engine is unavailable")
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
            jobs.reconcile_hint(
                number,
                base_ref,
                action,
                force,
                engine_identity=selected.identity,
                request_id=f"telegram:{request_id}",
            )
            message = f"Queued {label} review of {bot_config.repo_id}#{number}."
        except ValueError as exc:
            message = f"Could not queue {label} review of {bot_config.repo_id}#{number}: {exc}."
        except urllib.error.HTTPError as exc:
            if exc.code not in {404, 422}:
                logging.warning(
                    "Could not fetch PR %d for Telegram review: HTTP %d; will retry",
                    number,
                    exc.code,
                )
                continue
            message = f"Could not find {bot_config.repo_id}#{number}."
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


def reconcile_next(jobs, bot_config, token):
    """Resolve one durable hint outside the queue transaction."""
    hint = jobs.claim_reconciliation()
    if hint is None:
        return False
    try:
        number = hint["number"]
        pr = forgejo.forgejo_request(bot_config, token, f"/pulls/{number}")
        if pr.get("number") != number or pr.get("state") not in {"open", "closed"}:
            raise ValueError("invalid pull request state")
        if pr["state"] == "closed":
            jobs.finish_reconciliation(hint)
        else:
            resolved = parse_event(
                bot_config,
                "pull_request",
                {
                    "action": "opened",
                    "repository": {
                        "full_name": bot_config.repository,
                        "html_url": bot_config.repository_url or "",
                    },
                    "pull_request": pr,
                },
            )
            _, base_ref, head, _, _ = resolved
            jobs.finish_reconciliation(hint, base_ref, head)
        return True
    except Exception:
        jobs.retry_reconciliation(hint)
        raise


def run_job(job, jobs, api_key, ledger, contexts, allow_discussions=True):
    """Run one already-admitted claim, including durable publication."""
    try:
        job_store = jobs.for_repository(job["repo_id"])
        context = contexts[job["repo_id"]]
        state_dir = context["state_dir"]
        forgejo_token = context["forgejo_token"]
        bot_login = context["entry"]["bot_login"]
        bot_config = context["bot"]
        review_engine = context["engine"]
        github_token = context["github_token"]
        github_publication = context["entry"].get("github_publication", "off")
        if job["review_result"] is None:
            identity = job["engine_identity"]
            job_engine = (
                review_engine
                if identity is None
                or identity.get("config") is None
                or identity == review_engine.identity
                else engine.load(identity["name"], identity["config"], configured=True)
            )
            if not job_store.bind_engine(job, job_engine.identity):
                return
        else:
            job_engine = None
        process_job(
            job,
            job_store,
            state_dir,
            api_key,
            forgejo_token,
            bot_login,
            bot_config,
            ledger,
            job_engine,
            allow_discussions,
            github_token,
            github_publication,
        )
    except Exception as exc:
        logging.exception("Review worker failed unexpectedly for PR %d", job["number"])
        if retryable_error(exc):
            jobs.for_repository(job["repo_id"]).retry(job, exc)
        else:
            jobs.for_repository(job["repo_id"]).fail(job, exc)


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
                    accepted = jobs.reconcile_hint(
                        number,
                        base_ref,
                        action,
                        force,
                        engine_identity=engine_identity,
                    )
                except Exception:
                    logging.exception("Could not persist webhook job for PR %d", number)
                    self.send_error(503)
                    return
                logging.debug(
                    "review hint repo=%s pr=%d action=%s head=%s force=%s accepted=%s",
                    bot_config.repository,
                    number,
                    action,
                    short_sha(head_sha),
                    force,
                    accepted,
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


def serve_until_stopped(
    server, workers, command_thread, drain_event, command_stop_event
):
    shutdown_requested = False

    def request_shutdown(signum, frame):
        nonlocal shutdown_requested
        # Signal handlers must not acquire locks, including Event.set()'s lock.
        shutdown_requested = True

    previous_handlers = {}
    http_thread = threading.Thread(
        target=server.serve_forever, name="review-webhooks", daemon=True
    )
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.signal(signum, request_shutdown)
        http_thread.start()
        command_thread.start()
        for thread in workers:
            thread.start()
        while not shutdown_requested:
            http_thread.join(timeout=0.5)
            if not http_thread.is_alive():
                raise RuntimeError("Webhook server stopped unexpectedly")
    finally:
        try:
            drain_event.set()
            logging.info("Stopping admission; independent review jobs remain running")
            # These are short-lived coordinator loops, never review workers.
            for thread in workers:
                if thread.ident is not None:
                    thread.join(timeout=2)
            command_stop_event.set()
            if command_thread.ident is not None:
                command_thread.join(timeout=2)
            if http_thread.is_alive():
                server.shutdown()
            if http_thread.ident is not None:
                http_thread.join(timeout=2)
        finally:
            server.server_close()
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)


@contextmanager
def coordinator_lock(state_dir):
    """Keep recovery and coordination exclusive for this state directory."""
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state_dir / "coordinator.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                "another coordinator already owns this state directory"
            ) from None
        yield


def main():
    with ExitStack() as resources:
        return _main(resources)


def _main(resources):
    from .coordinator import main as coordinator_main

    return coordinator_main(resources)
