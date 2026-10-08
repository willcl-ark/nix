"""Public aggregate statistics for durable review jobs."""

import json
import math
import os
import re
import sqlite3
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import quote

from . import trace
from .spend import SNAPSHOT_MODEL


MICRODOLLARS = Decimal("1000000")
LEDGERS = (("openai", "spend.sqlite3"), ("glm", "ppq-spend.sqlite3"))
PUBLIC_LIMITATION_NOTES = [
    "Monthly trend uses ledger request accounting month.",
    "Model seconds are summed model turn elapsed_seconds, not wall time.",
    "Bot verification is not human validation.",
    "Shared attribution is noncausal.",
    "Unknown costs and reservations are reported separately from known cost.",
]
RESERVED_STATUSES = {"reserved", "uncertain", "unknown"}
UNKNOWN_COST_STATUSES = RESERVED_STATUSES
REVIEW_ID_PREFIX = "pr:"
SHA = re.compile(r"[0-9a-fA-F]{40}")
TRACE_FILENAME = re.compile(r"(\d+)-([0-9a-fA-F]{40})-(\d+)\.json")
ADDRESSED_STATUSES = ("addressed", "partially_addressed", "still_present", "unclear")
COVERAGE_STATUSES = ("complete", "partial", "failed", "unknown")


def collect_stats(state_dir: Path, report_dir: Path) -> dict:
    """Return public JSON-compatible aggregate review statistics."""
    state_dir = Path(state_dir)
    report_dir = Path(report_dir)
    now = datetime.now(timezone.utc)
    current_month = now.strftime("%Y-%m")
    jobs, jobs_missing = _job_rows(state_dir / "jobs.sqlite3")
    followup_dir = state_dir / "followup"
    followups, followups_missing, malformed_followups = _followup_rows(
        followup_dir / "assessments.sqlite3")
    requests, ledger_missing = _ledger_rows(state_dir)
    followup_requests, followup_ledger_missing = _provider_ledger_rows(
        "followup", followup_dir / "spend.sqlite3")
    spend = _spend_summary(requests, current_month)

    report_dir_missing = not report_dir.exists()
    inventory = {
        "jobs_total": len(jobs),
        "jobs_by_status": dict(sorted(Counter(row["status"] for row in jobs).items())),
        "saved_results": 0,
        "analyzed_reviews": 0,
        "unique_prs": len({row["number"] for row in jobs}),
        "skipped_results": 0,
        "malformed_results": 0,
        "malformed_followups": malformed_followups,
        "recorded_failed_attempts": sum(max(0, int(row["attempts"] or 0))
                                        for row in jobs),
        "missing_sources": {
            "jobs_db": jobs_missing,
            "followup_assessments": followups_missing,
            "followup_ledger": followup_ledger_missing,
            "openai_ledger": ledger_missing["openai"],
            "glm_ledger": ledger_missing["glm"],
            "report_dir": report_dir_missing,
        },
    }
    overview = {
        "saved_reviews": 0,
        "published_reviews": 0,
        "saved_findings": 0,
        "published_findings": 0,
        "coverage": _new_coverage_counts(),
        "context_coverage": _new_coverage_counts(),
        "verification_status": _new_coverage_counts(),
        "attribution_coverage": {"complete": 0, "missing": 0},
        "archaeology": {
            "research_completed": 0,
            "saved_concerns": 0,
            "published_concerns": 0,
            "assessment_status_counts": {},
        },
        "finding_kinds": {},
        "finding_severities": {},
    }
    stages = defaultdict(_new_stage)
    routing_tiers = Counter()
    review_records = []
    review_time_records = []
    completed_reviews = {}
    completed_reviews_by_job = {}
    cost_values = []
    reserved_values = []
    cost_missing_ledger = 0
    tool_values = []
    second_values = []
    concept_citation_values = []
    concept_alternative_values = []
    paired = _new_paired_summary()
    requests_by_review = defaultdict(list)
    configured_stage_models = {}
    for request in requests:
        if request.get("review_id"):
            requests_by_review[request["review_id"]].append(request)

    for row in jobs:
        result, malformed = _review_result(row)
        if malformed:
            inventory["malformed_results"] += 1
            continue
        if result is None:
            continue
        inventory["saved_results"] += 1
        debug = result.get("debug") if isinstance(result.get("debug"), dict) else {}
        skipped = _skipped_result(result)
        if skipped:
            inventory["skipped_results"] += 1
        else:
            inventory["analyzed_reviews"] += 1

        review_id = _review_id(row)
        configured_stage_models.update(_configured_stage_models(review_id, debug))
        review_requests = requests_by_review.get(review_id, [])
        ledger_totals = _request_totals(review_requests)
        observed_ledger_totals = _observed_review_totals(
            ledger_totals, bool(review_requests))
        findings = _published_findings(debug)
        concept = _concept_summary(debug)
        research_completed = _archaeology_research_completed(debug)
        coverage = _coverage_summary(debug)
        routing = _routing_summary(debug)
        metrics = _saved_review_metrics(debug)
        report_name = _report_name(report_dir, row, result)

        if not skipped:
            overview["saved_reviews"] += 1
            overview["saved_findings"] += len(findings)
            overview["coverage"][coverage["status"]] += 1
            overview["context_coverage"][coverage["context_status"]] += 1
            overview["verification_status"][coverage["verification_status"]] += 1
            overview["attribution_coverage"][
                "complete" if _has_attribution(debug, findings) else "missing"] += 1
            _add_archaeology_counts(overview, research_completed, concept,
                                    row["status"] == "complete")
            if row["status"] == "complete":
                overview["published_reviews"] += 1
                overview["published_findings"] += len(findings)
            _add_finding_counts(overview, findings)
            _add_stage_results(stages, debug)
            if routing.get("selected_tier"):
                routing_tiers[routing["selected_tier"]] += 1
            _add_paired_summary(paired, debug, review_requests)
            if review_requests:
                cost_values.append(ledger_totals["known_cost_micros"])
                reserved_values.append(ledger_totals["reserved_micros"])
            else:
                cost_missing_ledger += 1
            tool_values.append(metrics["tool_calls"])
            second_values.append(metrics["model_seconds"])
            if concept["published_concern"]:
                concept_citation_values.append(concept["citation_count"])
                concept_alternative_values.append(concept["alternative_count"])

        record = {
            "job_id": row["id"],
            "number": row["number"],
            "generation": row["generation"],
            "head": result.get("head_sha") or row["head"],
            "status": row["status"],
            "job_attempts": row["attempts"],
            "recorded_failed_attempts": max(0, int(row["attempts"] or 0)),
            "skipped": skipped,
            "routing": routing,
            "coverage": coverage["status"],
            "context_coverage": coverage["context_status"],
            "verification_status": coverage["verification_status"],
            "findings": {
                "saved": len(findings),
                "published": len(findings) if row["status"] == "complete" else 0,
            },
            "archaeology": {"research_completed": research_completed, **concept},
            "ledger": observed_ledger_totals,
            "tokens": observed_ledger_totals["tokens"],
            "tool_calls": metrics["tool_calls"],
            "model_seconds": metrics["model_seconds"],
            "saved_trace_known_estimated_cost_micros": (
                metrics["saved_trace_known_estimated_cost_micros"]),
        }
        if report_name is not None:
            record["report_name"] = report_name
        review_records.append(record)
        review_time_records.append({
            "number": row["number"],
            "job_id": row["id"],
            "generation": row["generation"],
            "head": record["head"],
            "verified": _verifier_completed(debug),
            "published_findings": (
                len(findings) if row["status"] == "complete" else 0),
        })
        if row["status"] == "complete":
            completed_reviews[(row["number"], row["id"], row["generation"])] = {
                "number": row["number"],
                "job_id": row["id"],
                "generation": row["generation"],
                "head": record["head"],
                "published_findings": len(findings) if not skipped else 0,
                "finding_ids": _published_finding_ids(debug),
                "report_name": report_name,
            }
            completed_reviews_by_job[row["id"]] = completed_reviews[
                (row["number"], row["id"], row["generation"])]

    addressed_records = []
    for followup in followups:
        addressed_records.extend(
            _addressed_records(followup, completed_reviews_by_job))

    for request in requests:
        _add_stage_ledger(stages, request, configured_stage_models)

    addressed_summary = _addressed_summary(
        addressed_records, completed_reviews, followup_requests)
    time_series, time_series_undated = _time_series_summary(
        state_dir, review_time_records, addressed_summary["findings"])

    return {
        "generated_at": _iso_z(now),
        "limitations": {
            "missing_timestamps": True,
            "notes": list(PUBLIC_LIMITATION_NOTES),
        },
        "inventory": inventory,
        "overview": overview,
        "spend": spend,
        "stage_model_leaderboard": _stage_leaderboard(stages),
        "stages": _stage_records(stages),
        "routing_tiers": dict(sorted(routing_tiers.items())),
        "distributions": {
            "saved_review_known_cost_micros": _distribution(cost_values),
            "saved_review_reserved_micros": _distribution(reserved_values),
            "saved_review_cost_missing_ledger_count": cost_missing_ledger,
            "saved_review_tools": _distribution(tool_values),
            "saved_review_model_seconds": _distribution(second_values),
            "saved_review_conceptual_concern_citations": _distribution(
                concept_citation_values),
            "saved_review_conceptual_concern_alternatives": _distribution(
                concept_alternative_values),
        },
        "paired_sol_glm": paired,
        "time_series": time_series,
        "time_series_undated": time_series_undated,
        "time_series_notes": [
            "Review months come from private trace filenames and are present only when trace files can be unambiguously matched to saved result rows by PR and head.",
            "Addressed months come from the new_head commit committer timestamp in the follow-up checkout; assessed_at is not used for trend bucketing.",
        ],
        "addressed_findings": addressed_summary,
        "recent_reviews": sorted(review_records, key=lambda item: item["job_id"],
                                 reverse=True)[:50],
        "reviews": sorted(review_records, key=lambda item: item["job_id"],
                          reverse=True),
    }


def _iso_z(value):
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _connect_readonly(path):
    uri = f"file:{quote(str(path), safe='/:')}?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    return db


def _job_rows(path):
    if not path.exists():
        return [], True
    try:
        with _connect_readonly(path) as db:
            return [dict(row) for row in db.execute(
                "SELECT id, number, generation, base_ref, head, action, force, "
                "status, attempts, review_result FROM jobs ORDER BY id")], False
    except sqlite3.Error:
        return [], True


def _followup_rows(path):
    if not path.exists():
        return [], True, 0
    rows = []
    malformed = 0
    try:
        with _connect_readonly(path) as db:
            records = db.execute(
                "SELECT job_id, result FROM assessments ORDER BY job_id")
            for row in records:
                job_id = _positive_int(row["job_id"])
                if row["result"] is None:
                    continue
                result = _parse_json(row["result"])
                if job_id is None or not isinstance(result, dict):
                    malformed += 1
                    continue
                rows.append({"job_id": job_id, "result": result})
            return rows, False, malformed
    except sqlite3.Error:
        return [], True, 0


def _ledger_rows(state_dir):
    rows = []
    missing = {}
    for provider, filename in LEDGERS:
        path = state_dir / filename
        provider_rows, provider_missing = _provider_ledger_rows(provider, path)
        rows.extend(provider_rows)
        missing[provider] = provider_missing
    return rows, missing


def _provider_ledger_rows(provider, path):
    if not path.exists():
        return [], True
    try:
        with _connect_readonly(path) as db:
            rows = db.execute(
                "SELECT review_id, stage, model, month, status, usage_json, "
                "cost_micros, reserved_micros FROM requests ORDER BY rowid")
            return [_ledger_record(provider, row) for row in rows], False
    except sqlite3.Error:
        return [], True


def _ledger_record(provider, row):
    usage = _parse_json(row["usage_json"])
    tokens, usage_unknown, usage_incomplete = _usage_metrics(usage, row["status"])
    parsed = _parse_review_id(row["review_id"])
    return {
        "provider": provider,
        "review_id": row["review_id"],
        "number": parsed[0],
        "job_id": parsed[1],
        "generation": parsed[2],
        "stage": row["stage"] or "unknown",
        "model": _normalize_model(row["model"] or "unknown"),
        "month": row["month"] or "unknown",
        "status": row["status"] or "unknown",
        "cost_micros": _nonnegative_int(row["cost_micros"]),
        "reserved_micros": _nonnegative_int(row["reserved_micros"]),
        "tokens": tokens,
        "usage_unknown": usage_unknown,
        "usage_incomplete": usage_incomplete,
    }


def _parse_json(text):
    if not isinstance(text, str):
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _usage_metrics(usage, status):
    tokens = {"input": 0, "output": 0, "cached": 0, "cache_write": 0}
    if status == "duplicate":
        return tokens, False, False
    if not isinstance(usage, dict):
        return tokens, True, False
    fields = {
        "input": usage.get("input_tokens"),
        "output": usage.get("output_tokens"),
        "cached": usage.get("cached_tokens"),
        "cache_write": usage.get("cache_write_tokens"),
    }
    known_core = _is_nonnegative_int(fields["input"]) and _is_nonnegative_int(fields["output"])
    if not known_core:
        return tokens, True, False
    for key, value in fields.items():
        if _is_nonnegative_int(value):
            tokens[key] = int(value)
    incomplete = usage.get("complete") is not True or not _is_nonnegative_int(
        fields["cache_write"])
    return tokens, False, incomplete


def _parse_review_id(review_id):
    if not isinstance(review_id, str) or not review_id.startswith(REVIEW_ID_PREFIX):
        return None, None, None
    parts = review_id[len(REVIEW_ID_PREFIX):].split(":")
    if len(parts) != 3:
        return None, None, None
    try:
        return tuple(int(part) for part in parts)
    except ValueError:
        return None, None, None


def _nonnegative_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _normalize_model(model):
    return "glm-5.3" if model == "z-ai/glm-5.3" else model


def _is_nonnegative_int(value):
    return isinstance(value, int) and value >= 0


def _review_result(row):
    if row.get("review_result") is None:
        return None, False
    result = _parse_json(row["review_result"])
    if not isinstance(result, dict):
        return None, True
    if not isinstance(result.get("debug"), dict):
        result["debug"] = {}
    return result, False


def _skipped_result(result):
    debug = result.get("debug") if isinstance(result.get("debug"), dict) else {}
    return bool(debug.get("skip")) or str(result.get("content", "")).startswith("Skipped:")


def _review_id(row):
    return f'pr:{row["number"]}:{row["id"]}:{row["generation"]}'


def _verifier_completed(debug):
    stages = debug.get("stages") if isinstance(debug, dict) else None
    verifier = stages.get("verifier") if isinstance(stages, dict) else None
    return isinstance(verifier, dict) and verifier.get("status") == "completed"


def _time_series_summary(state_dir, review_records, addressed_findings):
    series = defaultdict(lambda: {"reviews": 0, "verified_reviews": 0,
                                  "findings": 0, "addressed": 0})
    review_months, review_undated = _review_months(
        state_dir / "review-traces", review_records)
    for record in review_records:
        period = review_months.get((record["job_id"], record["generation"]))
        if period is None:
            continue
        series[period]["reviews"] += 1
        if record["verified"]:
            series[period]["verified_reviews"] += 1
        series[period]["findings"] += record["published_findings"]

    addressed_counts, addressed_undated = _addressed_month_counts(
        state_dir, addressed_findings)
    for period, count in addressed_counts.items():
        series[period]["addressed"] += count

    return [
        {"period": period, **series[period]}
        for period in _month_range(series)
    ], {**review_undated, **addressed_undated}


def _month_range(series):
    if not series:
        return []
    start = min(series)
    end = max(series)
    year, month = (int(part) for part in start.split("-"))
    end_year, end_month = (int(part) for part in end.split("-"))
    periods = []
    while (year, month) <= (end_year, end_month):
        periods.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
    return periods


def _review_months(trace_dir, records):
    traces, malformed_traces = _trace_months(trace_dir)
    grouped = defaultdict(list)
    invalid_heads = 0
    for record in records:
        head = record.get("head")
        if not isinstance(head, str):
            invalid_heads += 1
            continue
        grouped[(record["number"], head.lower())].append(record)

    months = {}
    undated = invalid_heads
    missing_groups = 0
    ambiguous_groups = 0
    for key, rows in grouped.items():
        trace_times = sorted(traces.get(key, []))
        if len(rows) == 1 and len(trace_times) == 1:
            row = rows[0]
            months[(row["job_id"], row["generation"])] = trace_times[0][1]
            continue
        undated += len(rows)
        if trace_times:
            ambiguous_groups += 1
        else:
            missing_groups += 1

    return months, {
        "reviews": undated,
        "review_missing_trace_groups": missing_groups,
        "review_ambiguous_trace_groups": ambiguous_groups,
        "review_unmatched_trace_groups": len(set(traces) - set(grouped)),
        "review_invalid_head_rows": invalid_heads,
        "review_malformed_trace_filenames": malformed_traces,
    }


def _trace_months(trace_dir):
    traces = defaultdict(list)
    malformed = 0
    if not trace_dir.exists():
        return traces, malformed
    try:
        paths = sorted(trace_dir.iterdir())
    except OSError:
        return traces, malformed
    for path in paths:
        if not path.is_file():
            continue
        match = TRACE_FILENAME.fullmatch(path.name)
        if match is None:
            if path.suffix == ".json":
                malformed += 1
            continue
        period = _time_ns_period(match.group(3))
        if period is None:
            malformed += 1
            continue
        traces[(int(match.group(1)), match.group(2).lower())].append(
            (int(match.group(3)), period))
    return traces, malformed


def _time_ns_period(value):
    try:
        seconds = int(value) / 1_000_000_000
        return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m")
    except (OverflowError, OSError, ValueError):
        return None


def _addressed_month_counts(state_dir, findings):
    eligible = [
        item for item in findings
        if item["status"] in {"addressed", "partially_addressed"}
    ]
    months = _commit_months(
        state_dir / "followup" / "checkout" / ".git",
        {item.get("new_head") for item in eligible})
    counts = Counter()
    undated = 0
    for item in eligible:
        period = months.get(item.get("new_head"))
        if period is None:
            undated += 1
        else:
            counts[period] += 1
    return counts, {"addressed": undated, "addressed_missing_commit": undated}


def _commit_months(git_dir, heads):
    heads = sorted({head.lower() for head in heads
                    if isinstance(head, str) and SHA.fullmatch(head)})
    if not heads or not git_dir.exists():
        return {}
    env = os.environ.copy()
    env["GIT_NO_LAZY_FETCH"] = "1"
    try:
        completed = subprocess.run(
            ["git", f"--git-dir={git_dir}", "cat-file", "--batch"],
            input="".join(f"{head}^{{commit}}\n" for head in heads).encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            timeout=15,
            check=False)
    except (OSError, subprocess.SubprocessError):
        return {}

    months = {}
    offset = 0
    output = completed.stdout
    for head in heads:
        line_end = output.find(b"\n", offset)
        if line_end < 0:
            break
        header = output[offset:line_end].decode(errors="replace").split()
        offset = line_end + 1
        if len(header) == 2 and header[1] == "missing":
            continue
        if len(header) != 3 or header[1] != "commit":
            break
        try:
            size = int(header[2])
        except ValueError:
            break
        content = output[offset:offset + size]
        offset += size
        if offset < len(output) and output[offset:offset + 1] == b"\n":
            offset += 1
        timestamp = _committer_timestamp(content)
        if timestamp is not None:
            months[head] = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m")
    return months


def _committer_timestamp(commit):
    for line in commit.splitlines():
        if not line.startswith(b"committer "):
            continue
        parts = line.rsplit(b" ", 2)
        if len(parts) != 3:
            return None
        try:
            return int(parts[1])
        except ValueError:
            return None
    return None


def _addressed_records(followup, completed_reviews_by_job):
    assessment = completed_reviews_by_job.get(followup["job_id"])
    if assessment is None:
        return []
    addressed = followup["result"]
    if not isinstance(addressed, dict):
        return []
    assessed_at = addressed.get("assessed_at")
    if not isinstance(assessed_at, str) or not assessed_at:
        assessed_at = None
    model = addressed.get("model")
    if not isinstance(model, str) or not model:
        model = None
    findings = addressed.get("findings")
    if not isinstance(findings, list):
        return []
    records = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        source_job_id = _positive_int(finding.get("source_job_id"))
        source_generation = _positive_int(finding.get("source_generation"))
        finding_id = finding.get("finding_id")
        if source_job_id is None or source_generation is None:
            continue
        if not isinstance(finding_id, str) or not finding_id:
            continue
        status = finding.get("status")
        if status not in ADDRESSED_STATUSES:
            status = "unclear"
        records.append({
            "number": assessment["number"],
            "assessment_job_id": assessment["job_id"],
            "assessment_generation": assessment["generation"],
            "source_job_id": source_job_id,
            "source_generation": source_generation,
            "finding_id": finding_id,
            "title": _optional_string(finding.get("title")),
            "path": _optional_string(finding.get("path")),
            "old_head": _optional_string(finding.get("old_head")),
            "new_head": _optional_string(finding.get("new_head"))
                        or assessment["head"],
            "assessment_head": assessment["head"],
            "status": status,
            "reason": _optional_string(finding.get("reason")),
            "evidence": _optional_string(finding.get("evidence")),
            "assessed_at": assessed_at,
            "model": model,
        })
    return records


def _optional_string(value):
    return value if isinstance(value, str) and value else None


def _positive_int(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _addressed_summary(records, completed_reviews, followup_requests):
    latest = {}
    for record in records:
        source_key = (record["number"], record["source_job_id"],
                      record["source_generation"])
        source = completed_reviews.get(source_key)
        if source is None:
            continue
        if source["generation"] >= record["assessment_generation"]:
            continue
        if record["old_head"] != source["head"]:
            continue
        if record["new_head"] != record["assessment_head"]:
            continue
        if record["finding_id"] not in source["finding_ids"]:
            continue
        enriched = {
            key: value for key, value in record.items()
            if key != "assessment_head"
        }
        enriched["source_report_name"] = source.get("report_name")
        key = (*source_key, record["finding_id"])
        previous = latest.get(key)
        if previous is None or _assessment_order(enriched) > _assessment_order(previous):
            latest[key] = enriched
    findings = sorted(latest.values(), key=lambda item: (
        item["source_job_id"], item["source_generation"], item["finding_id"],
        item["assessment_job_id"], item["assessment_generation"]),
        reverse=True)
    status_counts = Counter(item["status"] for item in findings)
    source_keys = {
        (item["number"], item["source_job_id"], item["source_generation"])
        for item in findings
    }
    source_finding_count = sum(
        completed_reviews[key]["published_findings"] for key in source_keys)
    return {
        "note": (
            "Best-effort automated assessment of whether later PR updates addressed "
            "previous Ralph findings. This is not a causal claim or human validation."
        ),
        "evaluated_findings": len(findings),
        "source_published_findings": source_finding_count,
        "assessed_source_reviews": len(source_keys),
        "assessment_jobs": len({
            (item["number"], item["assessment_job_id"], item["assessment_generation"])
            for item in findings
        }),
        "spend": _request_totals(followup_requests),
        "status_counts": {status: status_counts.get(status, 0)
                          for status in ADDRESSED_STATUSES},
        "findings": findings,
    }


def _assessment_order(record):
    return record["assessment_generation"], record["assessment_job_id"]


def _new_coverage_counts():
    return {status: 0 for status in COVERAGE_STATUSES}


def _coverage_summary(debug):
    coverage = debug.get("coverage") if isinstance(debug, dict) else None
    context = coverage.get("context") if isinstance(coverage, dict) else None
    verification = coverage.get("verification") if isinstance(coverage, dict) else None
    return {
        "status": _coverage_status(coverage),
        "context_status": _coverage_status(context),
        "verification_status": _coverage_status(verification),
    }


def _coverage_status(coverage):
    status = coverage.get("status") if isinstance(coverage, dict) else None
    return status if status in COVERAGE_STATUSES else "unknown"


def _routing_summary(debug):
    routing = debug.get("routing") if isinstance(debug, dict) else None
    if not isinstance(routing, dict):
        return {}
    selected = routing.get("selected") if isinstance(routing.get("selected"), dict) else {}
    proposed = routing.get("proposed") if isinstance(routing.get("proposed"), dict) else {}
    return {
        "mode": routing.get("mode") if isinstance(routing.get("mode"), str) else None,
        "minimum_tier": (routing.get("minimum_tier")
                         if isinstance(routing.get("minimum_tier"), str) else None),
        "proposed_tier": proposed.get("tier") if isinstance(proposed.get("tier"), str) else None,
        "selected_tier": selected.get("tier") if isinstance(selected.get("tier"), str) else None,
        "selected_audits": _strings(selected.get("audits")),
        "selected_profiles": _strings(selected.get("profiles")),
    }


def _strings(value):
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _published_findings(debug):
    findings = []
    seen = set()
    for index, decision in enumerate(debug.get("decisions", [])):
        if not isinstance(decision, dict) or decision.get("disposition") != "publish":
            continue
        finding = decision.get("finding")
        if not isinstance(finding, dict):
            continue
        key = json.dumps(finding, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        findings.append(finding)
    if findings:
        return findings
    attribution = trace.finding_attribution(debug)
    return [{"kind": "unknown", "severity": "unknown"}
            for item in attribution if isinstance(item, dict)]


def _published_finding_ids(debug):
    ids = set()
    for item in trace.finding_attribution(debug):
        if isinstance(item, dict) and isinstance(item.get("finding_id"), str):
            ids.add(item["finding_id"])
    return ids


def _has_attribution(debug, findings):
    if not findings:
        return True
    return bool(trace.finding_attribution(debug))


def _concept_summary(debug):
    concept = trace.concept_assessment(debug)
    if not concept:
        return {"published_concern": False}
    verification = concept.get("verification") if isinstance(concept.get("verification"), dict) else {}
    assessment = verification.get("assessment") if isinstance(
        verification.get("assessment"), dict) else None
    alternatives = assessment.get("alternatives") if isinstance(assessment, dict) else []
    citations = list(assessment.get("citations") if isinstance(assessment, dict) else [])
    for alternative in alternatives if isinstance(alternatives, list) else []:
        if isinstance(alternative, dict) and isinstance(alternative.get("citations"), list):
            citations.extend(alternative["citations"])
    status = concept.get("status") if isinstance(concept.get("status"), str) else "unknown"
    stage = concept.get("stage") if isinstance(concept.get("stage"), str) else None
    published_concern = trace.published_concept_concern(debug)
    return {
        "published_concern": published_concern,
        "status": status,
        "stage": stage,
        "citation_count": len(set(item for item in citations if isinstance(item, str))),
        "alternative_count": len(alternatives) if isinstance(alternatives, list) else 0,
    }


def _archaeology_research_completed(debug):
    for name, stage in (debug.get("stages") or {}).items():
        if not isinstance(stage, dict) or name != "archaeologist":
            continue
        if stage.get("status") == "completed":
            return True
    return False


def _add_archaeology_counts(overview, research_completed, concept, published):
    counts = overview["archaeology"]
    if research_completed:
        counts["research_completed"] += 1
    if "status" in concept:
        by_status = Counter(counts["assessment_status_counts"])
        by_status[concept["status"]] += 1
        counts["assessment_status_counts"] = dict(sorted(by_status.items()))
    if concept.get("published_concern"):
        counts["saved_concerns"] += 1
        if published:
            counts["published_concerns"] += 1


def _add_finding_counts(overview, findings):
    kinds = Counter(overview["finding_kinds"])
    severities = Counter(overview["finding_severities"])
    for finding in findings:
        kind = finding.get("kind") if isinstance(finding.get("kind"), str) else "unknown"
        severity = (finding.get("severity")
                    if isinstance(finding.get("severity"), str) else "unknown")
        kinds[kind] += 1
        severities[severity] += 1
    overview["finding_kinds"] = dict(sorted(kinds.items()))
    overview["finding_severities"] = dict(sorted(severities.items()))


def _saved_review_metrics(debug):
    model_turns = 0
    tool_calls = 0
    model_seconds = Decimal("0")
    saved_cost = 0
    for stage in (debug.get("stages") or {}).values():
        if not isinstance(stage, dict):
            continue
        turns = stage.get("turns") if isinstance(stage.get("turns"), list) else []
        tools = stage.get("tools") if isinstance(stage.get("tools"), list) else []
        model_turns += len(turns)
        tool_calls += len(tools)
        for turn in turns:
            if isinstance(turn, dict) and isinstance(turn.get("elapsed_seconds"), (int, float)):
                model_seconds += Decimal(str(turn["elapsed_seconds"]))
    for metric in trace.stage_metrics(debug).values():
        saved_cost += _usd_to_micros(metric.get("known_estimated_cost_usd", 0))
    return {
        "model_turns": model_turns,
        "tool_calls": tool_calls,
        "model_seconds": float(model_seconds),
        "saved_trace_known_estimated_cost_micros": saved_cost,
    }


def _usd_to_micros(value):
    return int((Decimal(str(value)) * MICRODOLLARS).to_integral_value(
        rounding=ROUND_HALF_UP))


def _report_name(report_dir, row, result):
    head = result.get("head_sha")
    if not isinstance(head, str):
        return None
    name = f'{row["number"]}-{head}-{row["id"]}-{row["generation"]}.html'
    return name if (report_dir / name).is_file() else None


def _request_totals(requests):
    totals = _new_request_totals()
    for request in requests:
        _add_request(totals, request)
    totals["status_counts"] = dict(sorted(totals["status_counts"].items()))
    totals["providers"] = {
        provider: _final_request_totals(record)
        for provider, record in sorted(totals["providers"].items())
    }
    return totals


def _new_request_totals():
    return {
        "request_count": 0,
        "known_cost_micros": 0,
        "reserved_micros": 0,
        "unknown_cost_request_count": 0,
        "unknown_usage_count": 0,
        "incomplete_usage_count": 0,
        "status_counts": Counter(),
        "tokens": {"input": 0, "output": 0, "cached": 0, "cache_write": 0},
        "providers": defaultdict(_new_provider_totals),
    }


def _new_provider_totals():
    return {
        "request_count": 0,
        "known_cost_micros": 0,
        "reserved_micros": 0,
        "unknown_cost_request_count": 0,
        "unknown_usage_count": 0,
        "incomplete_usage_count": 0,
        "status_counts": Counter(),
        "tokens": {"input": 0, "output": 0, "cached": 0, "cache_write": 0},
    }


def _add_request(record, request):
    record["request_count"] += 1
    record["known_cost_micros"] += request["cost_micros"]
    if request["status"] in RESERVED_STATUSES:
        record["reserved_micros"] += request["reserved_micros"]
    if request["status"] in UNKNOWN_COST_STATUSES:
        record["unknown_cost_request_count"] += 1
    if request["usage_unknown"]:
        record["unknown_usage_count"] += 1
    if request["usage_incomplete"]:
        record["incomplete_usage_count"] += 1
    record["status_counts"][request["status"]] += 1
    for key, value in request["tokens"].items():
        record["tokens"][key] += value
    if "providers" in record and request.get("provider") is not None:
        _add_request(record["providers"][request["provider"]],
                     {**request, "provider": None})


def _final_request_totals(record):
    return {
        "request_count": record["request_count"],
        "known_cost_micros": record["known_cost_micros"],
        "reserved_micros": record["reserved_micros"],
        "unknown_cost_request_count": record["unknown_cost_request_count"],
        "unknown_usage_count": record["unknown_usage_count"],
        "incomplete_usage_count": record["incomplete_usage_count"],
        "status_counts": dict(sorted(record["status_counts"].items())),
        "tokens": dict(record["tokens"]),
    }


def _observed_review_totals(totals, observed):
    if observed:
        return {"cost_observed": True, **totals}
    return {
        "cost_observed": False,
        **totals,
        "known_cost_micros": None,
        "reserved_micros": None,
    }


def _spend_summary(requests, current_month):
    lifetime = _request_totals(requests)
    current = _request_totals([request for request in requests
                               if request["month"] == current_month])
    by_provider = defaultdict(_new_request_totals)
    by_model = defaultdict(_new_request_totals)
    by_month = defaultdict(_new_request_totals)
    for request in requests:
        _add_request(by_provider[request["provider"]], request)
        _add_request(by_model[(request["provider"], request["model"])], request)
        _add_request(by_month[request["month"]], request)
    current_totals = _final_request_totals(current)
    current_totals["providers"] = current["providers"]
    lifetime_totals = _final_request_totals(lifetime)
    lifetime_totals["providers"] = lifetime["providers"]
    return {
        "accounting_note": (
            "request_count is every ledger request row, including retries, "
            "duplicates, released reservations, and failed attempts. Known "
            "cost and reserved micros are the observed accounting amounts."),
        "current_month": {"month": current_month, **current_totals},
        "lifetime": lifetime_totals,
        "providers": [
            {"provider": provider, **_final_request_totals(record)}
            for provider, record in sorted(by_provider.items())
        ],
        "models": [
            {"provider": provider, "model": model, **_final_request_totals(record)}
            for (provider, model), record in sorted(by_model.items())
        ],
        "months": [
            {"month": month, **_final_request_totals(record)}
            for month, record in sorted(by_month.items())
        ],
    }


def _new_stage():
    return {
        "stage": None,
        "configured_model": None,
        "saved_result_runs": 0,
        "executed_runs": 0,
        "skipped_runs": 0,
        "status_counts": Counter(),
        "coverage_counts": Counter(_new_coverage_counts()),
        "candidate_counts": Counter(
            {"publish": 0, "drop": 0, "unresolved": 0, "undisposed": 0}),
        "accepted_findings": 0,
        "concept_stage": False,
        "conceptual_concerns": 0,
        "sole_source_findings": 0,
        "shared_findings": 0,
        "attribution_unknown_reviews": 0,
        "tool_calls": 0,
        "model_turns": 0,
        "model_seconds": 0.0,
        "saved_trace_known_estimated_cost_micros": 0,
        "ledger": _new_request_totals(),
    }


def _stage_key(stage, model):
    return stage or "unknown", model or "unknown"


def _configured_stage_models(review_id, debug):
    models = {}
    for name, stage in sorted((debug.get("stages") or {}).items()):
        if not isinstance(stage, dict):
            continue
        model = stage.get("model")
        if isinstance(model, str):
            models[(review_id, name)] = _normalize_model(model)
    return models


def _add_stage_results(stages, debug):
    metrics = trace.stage_metrics(debug)
    attribution_known = bool(trace.finding_attribution(debug))
    has_publish = bool(_published_findings(debug))
    for name, stage in sorted((debug.get("stages") or {}).items()):
        if not isinstance(stage, dict):
            continue
        model = _normalize_model(stage.get("model") if isinstance(stage.get("model"), str)
                                 else "unknown")
        record = stages[_stage_key(name, model)]
        record["stage"] = name
        record["configured_model"] = model
        stage_metrics = metrics.get(name, {})
        record["concept_stage"] = bool(stage_metrics.get("concept_stage"))
        record["conceptual_concerns"] += stage_metrics.get("conceptual_concerns", 0)
        record["saved_result_runs"] += 1
        status = stage.get("status") if isinstance(stage.get("status"), str) else "unknown"
        record["status_counts"][status] += 1
        record["coverage_counts"][_coverage_status(stage.get("coverage"))] += 1
        if status == "skipped":
            record["skipped_runs"] += 1
        else:
            record["executed_runs"] += 1
        for disposition, count in stage_metrics.get("candidate_counts", {}).items():
            record["candidate_counts"][disposition] += count
        record["accepted_findings"] += stage_metrics.get("accepted_findings", 0)
        record["sole_source_findings"] += stage_metrics.get("sole_source_findings", 0)
        record["shared_findings"] += stage_metrics.get("shared_findings", 0)
        record["tool_calls"] += stage_metrics.get("tool_calls", 0)
        record["model_turns"] += stage_metrics.get("calls", 0)
        for turn in stage.get("turns", []):
            if isinstance(turn, dict) and isinstance(turn.get("elapsed_seconds"), (int, float)):
                record["model_seconds"] += float(turn["elapsed_seconds"])
        record["saved_trace_known_estimated_cost_micros"] += _usd_to_micros(
            stage_metrics.get("known_estimated_cost_usd", 0))
        if has_publish and not attribution_known:
            record["attribution_unknown_reviews"] += 1


def _add_stage_ledger(stages, request, configured_stage_models):
    model = _stage_ledger_model(request, configured_stage_models)
    record = stages[_stage_key(request["stage"], model)]
    record["stage"] = request["stage"]
    record["configured_model"] = model
    if request["stage"] == "archaeologist":
        record["concept_stage"] = True
    _add_request(record["ledger"], request)


def _stage_ledger_model(request, configured_stage_models):
    configured = configured_stage_models.get((request["review_id"], request["stage"]))
    if configured is None:
        return request["model"]
    match = SNAPSHOT_MODEL.fullmatch(request["model"])
    if match and configured == match.group(1):
        return configured
    return request["model"]


def _stage_records(stages):
    records = []
    for (_, _), record in sorted(stages.items()):
        executed = record["executed_runs"]
        accepted = record["accepted_findings"]
        stage_record = {
            "stage": record["stage"],
            "configured_model": record["configured_model"],
            "saved_result_runs": record["saved_result_runs"],
            "executed_runs": executed,
            "skipped_runs": record["skipped_runs"],
            "status_counts": dict(sorted(record["status_counts"].items())),
            "coverage_counts": dict(sorted(record["coverage_counts"].items())),
            "candidate_counts": dict(sorted(record["candidate_counts"].items())),
            "accepted_findings": accepted,
            "concept_stage": record["concept_stage"],
            "conceptual_concerns": record["conceptual_concerns"],
            "selected_candidate_findings": (
                record["candidate_counts"]["publish"]),
            "sole_source_findings": record["sole_source_findings"],
            "shared_findings": record["shared_findings"],
            "known_cost_per_accepted_finding_micros": (
                round(record["ledger"]["known_cost_micros"] / accepted, 2)
                if accepted else None),
            "attribution_unknown_reviews": record["attribution_unknown_reviews"],
            "acceptance_denominator_runs": executed,
            "accepted_per_executed_run": (
                round(accepted / executed, 4) if executed else None),
            "tool_calls": record["tool_calls"],
            "model_turns": record["model_turns"],
            "model_seconds": round(record["model_seconds"], 2),
            "saved_trace_known_estimated_cost_micros": (
                record["saved_trace_known_estimated_cost_micros"]),
            "ledger": _final_request_totals(record["ledger"]),
        }
        records.append(stage_record)
    return records


def _stage_leaderboard(stages):
    records = []
    for record in stages.values():
        if record["concept_stage"]:
            continue
        accepted = record["accepted_findings"]
        selected_candidates = record["candidate_counts"]["publish"]
        cost = record["ledger"]["known_cost_micros"]
        records.append({
            "stage": record["stage"],
            "configured_model": record["configured_model"],
            "accepted_findings": accepted,
            "selected_candidate_findings": selected_candidates,
            "sole_source_findings": record["sole_source_findings"],
            "shared_findings": record["shared_findings"],
            "executed_runs": record["executed_runs"],
            "known_cost_micros": cost,
            "reserved_micros": record["ledger"]["reserved_micros"],
            "request_count": record["ledger"]["request_count"],
            "known_cost_per_accepted_finding_micros": (
                round(cost / accepted, 2) if accepted else None),
            "note": "Shared findings overlap across stages and do not sum to total unique findings.",
        })
    return sorted(records, key=lambda item: (
        item["accepted_findings"],
        item["sole_source_findings"],
        item["shared_findings"],
        -item["request_count"],
    ), reverse=True)


def _new_paired_summary():
    return {
        "completed_pairs": 0,
        "sole_accepted": {"adversarial": 0, "adversarial_glm": 0},
        "shared_accepted": {"adversarial": 0, "adversarial_glm": 0},
        "known_cost_micros": {"adversarial": 0, "adversarial_glm": 0},
        "reserved_micros": {"adversarial": 0, "adversarial_glm": 0},
        "note": "Observed paired completed reviews only; this is not causal recall.",
    }


def _add_paired_summary(paired, debug, requests):
    stages = debug.get("stages") or {}
    if not all(isinstance(stages.get(name), dict)
               and stages[name].get("status") == "completed"
               for name in ("adversarial", "adversarial_glm")):
        return
    metrics = trace.stage_metrics(debug)
    paired["completed_pairs"] += 1
    for name in ("adversarial", "adversarial_glm"):
        paired["sole_accepted"][name] += metrics.get(name, {}).get(
            "sole_source_findings", 0)
        paired["shared_accepted"][name] += metrics.get(name, {}).get(
            "shared_findings", 0)
    for request in requests:
        if request["stage"] in ("adversarial", "adversarial_glm"):
            paired["known_cost_micros"][request["stage"]] += request["cost_micros"]
            if request["status"] in RESERVED_STATUSES:
                paired["reserved_micros"][request["stage"]] += request["reserved_micros"]


def _distribution(values):
    if not values:
        return {"count": 0, "median": None, "p90": None, "max": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "median": _median(ordered),
        "p90": ordered[max(0, math.ceil(len(ordered) * 0.9) - 1)],
        "max": ordered[-1],
    }


def _median(ordered):
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2
