"""Public aggregate statistics for durable review jobs."""

import json
import math
import os
import re
import sqlite3
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

from ralph import coverage as core_coverage
from ralph import trace as core_trace

from .report import public_limits


SNAPSHOT_MODEL = re.compile(r"(.+)-\d{4}-\d{2}-\d{2}")
# Include the retired Gemini stage when aggregating historical reviews.
ADVERSARIAL_STAGES = ("adversarial", "adversarial_claude", "adversarial_gemini")
PUBLIC_LIMITATION_NOTES = [
    "Monthly trend uses request month.",
    "Model seconds are summed model turn elapsed_seconds, not wall time.",
    "Bot verification is not human validation.",
    "Shared attribution is noncausal.",
    "Token totals include reported usage only; missing token fields are marked partial.",
    "Subscription quota includes other clients sharing the same account.",
]
REVIEW_ID_PREFIX = "pr:"
SHA = re.compile(r"[0-9a-fA-F]{40}")
TRACE_FILENAME = re.compile(r"(\d+)-([0-9a-fA-F]{40})-(\d+)\.json")
ADDRESSED_STATUSES = ("addressed", "partially_addressed", "still_present", "unclear")
COVERAGE_STATUSES = ("complete", "partial", "failed", "unknown")
TOKEN_FIELDS = ("input", "output", "cached", "cache_write", "reasoning")


def collect_stats(state_dir: Path, report_dir: Path) -> dict:
    """Return public JSON-compatible aggregate review statistics."""
    state_dir = Path(state_dir)
    report_dir = Path(report_dir)
    now = datetime.now(timezone.utc)
    current_month = now.strftime("%Y-%m")
    jobs, jobs_missing = _job_rows(state_dir / "jobs.sqlite3")
    followup_dir = state_dir / "followup"
    followups, followups_missing, malformed_followups = _followup_rows(
        followup_dir / "assessments.sqlite3"
    )
    requests, ledger_missing = _ledger_rows(state_dir)
    followup_requests, followup_ledger_missing = _provider_ledger_rows(
        "followup", followup_dir / "usage.sqlite3"
    )
    usage = _usage_summary(requests, current_month)

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
        "recorded_failed_attempts": sum(
            max(0, int(row["attempts"] or 0)) for row in jobs
        ),
        "missing_sources": {
            "jobs_db": jobs_missing,
            "followup_assessments": followups_missing,
            "followup_ledger": followup_ledger_missing,
            "review_ledger": ledger_missing["openai"],
            "report_dir": report_dir_missing,
        },
    }
    overview = {
        "saved_reviews": 0,
        "published_reviews": 0,
        "review_status": {},
        "saved_findings": 0,
        "published_findings": 0,
        "findings_unavailable_reviews": 0,
        "current_diagnostics_reviews": 0,
        "diagnostics_unavailable_reviews": 0,
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
    limit_runs = []
    routing_tiers = Counter()
    review_records = []
    review_time_records = []
    completed_reviews = {}
    completed_reviews_by_job = {}
    tool_values = []
    second_values = []
    concept_citation_values = []
    concept_alternative_values = []
    paired = _new_adversarial_comparison()
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
        debug = core_trace.current_diagnostics(result)
        exported_findings = core_trace.findings_export(result)
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
            ledger_totals, bool(review_requests)
        )
        findings = _published_findings(result) or []
        concept = _concept_summary(debug)
        research_completed = _archaeology_research_completed(debug)
        coverage = _coverage_summary(debug)
        routing = _routing_summary(debug)
        metrics = _saved_review_metrics(debug, result)
        report_name = _report_name(report_dir, row, result)
        _collect_limit_runs(limit_runs, debug, result, row, report_name)

        if not skipped:
            overview["saved_reviews"] += 1
            review_status = (result.get("run") or {}).get("status", "unknown")
            counts = overview["review_status"]
            counts[review_status] = counts.get(review_status, 0) + 1
            overview[
                "current_diagnostics_reviews"
                if debug
                else "diagnostics_unavailable_reviews"
            ] += 1
            if exported_findings is None:
                overview["findings_unavailable_reviews"] += 1
            else:
                overview["saved_findings"] += len(findings)
            overview["coverage"][coverage["status"]] += 1
            overview["context_coverage"][coverage["context_status"]] += 1
            overview["verification_status"][coverage["verification_status"]] += 1
            if exported_findings is not None:
                overview["attribution_coverage"][
                    "complete" if _has_attribution(result, findings) else "missing"
                ] += 1
            if debug:
                _add_archaeology_counts(
                    overview, research_completed, concept, row["status"] == "complete"
                )
            if row["status"] == "complete":
                overview["published_reviews"] += 1
                if exported_findings is not None:
                    overview["published_findings"] += len(findings)
            if exported_findings is not None:
                _add_finding_counts(overview, findings)
            if debug:
                _add_stage_results(stages, debug, row, report_name)
            if routing.get("selected_tier"):
                routing_tiers[routing["selected_tier"]] += 1
            if debug:
                _add_adversarial_comparison(paired, debug)
            if metrics["tool_calls"] is not None:
                tool_values.append(metrics["tool_calls"])
            if metrics["model_seconds"] is not None:
                second_values.append(metrics["model_seconds"])
            if debug and concept["published_concern"]:
                concept_citation_values.append(concept["citation_count"])
                concept_alternative_values.append(concept["alternative_count"])

        record = {
            "job_id": row["id"],
            "number": row["number"],
            "generation": row["generation"],
            "head": result.get("head_sha") or row["head"],
            "pr_title": _optional_string((result.get("run") or {}).get("pr_title")),
            "status": row["status"],
            "review_status": (result.get("run") or {}).get("status", "unknown"),
            "job_attempts": row["attempts"],
            "recorded_failed_attempts": max(0, int(row["attempts"] or 0)),
            "skipped": skipped,
            "routing": routing,
            "coverage": coverage["status"],
            "context_coverage": coverage["context_status"],
            "verification_status": coverage["verification_status"],
            "findings": {
                "export_status": "supported"
                if exported_findings is not None
                else "unsupported",
                "severities": dict(
                    Counter(
                        (
                            finding.get("severity")
                            if isinstance(finding.get("severity"), str)
                            else "unknown"
                        )
                        for finding in findings
                    )
                )
                if exported_findings is not None and row["status"] == "complete"
                else None,
                "saved": len(findings) if exported_findings is not None else None,
                "published": (len(findings) if row["status"] == "complete" else 0)
                if exported_findings is not None
                else None,
            },
            "archaeology": (
                {"research_completed": research_completed, **concept} if debug else None
            ),
            "ledger": observed_ledger_totals,
            "tokens": observed_ledger_totals["tokens"],
            "runtime_metrics": core_trace.result_metrics(result) or {},
            "tool_calls": metrics["tool_calls"],
            "model_seconds": metrics["model_seconds"],
        }
        if report_name is not None:
            record["report_name"] = report_name
        review_records.append(record)
        review_time_records.append(
            {
                "number": row["number"],
                "job_id": row["id"],
                "generation": row["generation"],
                "head": record["head"],
                "verified": _verifier_completed(debug) if debug else None,
                "published_findings": (
                    (len(findings) if row["status"] == "complete" else 0)
                    if exported_findings is not None
                    else None
                ),
            }
        )
        if row["status"] == "complete":
            completed_reviews[(row["number"], row["id"], row["generation"])] = {
                "number": row["number"],
                "job_id": row["id"],
                "generation": row["generation"],
                "head": record["head"],
                "published_findings": (len(findings) if not skipped else 0)
                if exported_findings is not None
                else None,
                "finding_ids": _published_finding_ids(result),
                "report_name": report_name,
            }
            completed_reviews_by_job[row["id"]] = completed_reviews[
                (row["number"], row["id"], row["generation"])
            ]

    addressed_records = []
    for followup in followups:
        addressed_records.extend(_addressed_records(followup, completed_reviews_by_job))

    for request in requests:
        _add_stage_ledger(stages, request, configured_stage_models)

    addressed_summary = _addressed_summary(
        addressed_records,
        completed_reviews,
        followup_requests,
        followups,
        completed_reviews_by_job,
    )
    time_series, time_series_undated = _time_series_summary(
        state_dir, review_time_records, addressed_summary["findings"]
    )

    return {
        "generated_at": _iso_z(now),
        "limitations": {
            "missing_timestamps": True,
            "notes": list(PUBLIC_LIMITATION_NOTES),
        },
        "inventory": inventory,
        "overview": overview,
        "usage": usage,
        "usage_trends": _usage_trends(requests + followup_requests, now),
        "stage_model_leaderboard": _stage_leaderboard(stages),
        "stages": _stage_records(stages),
        "limits": _limit_summary(limit_runs),
        "routing_tiers": dict(sorted(routing_tiers.items())),
        "distributions": {
            "saved_review_tools": _distribution(tool_values),
            "saved_review_model_seconds": _distribution(second_values),
            "saved_review_conceptual_concern_citations": _distribution(
                concept_citation_values
            ),
            "saved_review_conceptual_concern_alternatives": _distribution(
                concept_alternative_values
            ),
        },
        "adversarial_comparison": paired,
        "time_series": time_series,
        "time_series_undated": time_series_undated,
        "time_series_notes": [
            "Review days use UTC timestamps from private trace filenames and are present only when trace files can be unambiguously matched to saved result rows by PR and head.",
            "Addressed days use the UTC new_head commit committer timestamp in the follow-up checkout; assessed_at is not used for trend bucketing.",
        ],
        "addressed_findings": addressed_summary,
        "recent_reviews": sorted(
            review_records, key=lambda item: item["job_id"], reverse=True
        )[:50],
        "reviews": sorted(
            review_records, key=lambda item: item["job_id"], reverse=True
        ),
    }


def _collect_limit_runs(runs, debug, result, row, report_name):
    config = (result.get("engine") or {}).get("config") or {}
    profile = config.get("profile") or {}
    for name, stage in (debug.get("stages") or {}).items():
        if not isinstance(stage, dict) or stage.get("status") == "skipped":
            continue
        runs.append(
            {
                "profile": profile.get("name") or "unknown",
                "stage": name,
                "model": stage.get("model") or "unknown",
                "limits": public_limits(stage),
                "job_id": row["id"],
                "number": row["number"],
                "report_name": report_name,
            }
        )


def _limit_summary(runs):
    groups = {}
    for run in runs:
        identity = tuple(run[key] for key in ("profile", "stage", "model"))
        group_identity = {key: run[key] for key in ("profile", "stage", "model")}
        for name, limit in run["limits"].items():
            group = groups.setdefault(
                (*identity, name, limit["limit"]),
                {
                    **group_identity,
                    "name": name,
                    "cap": limit["limit"],
                    "runs": 0,
                    "usage_known_runs": 0,
                    "hit_known_runs": 0,
                    "hit_runs": 0,
                    "at_cap_runs": 0,
                    "max_used": None,
                },
            )
            group["runs"] += 1
            if limit["hit"] is not None:
                group["hit_known_runs"] += 1
                group["hit_runs"] += limit["hit"]
            if limit["used"] is not None:
                group["usage_known_runs"] += 1
                group["at_cap_runs"] += limit["used"] >= limit["limit"]
                group["max_used"] = max(group["max_used"] or 0, limit["used"])
    affected = [
        run
        for run in runs
        if any(
            value["hit"] is True
            or (value["used"] is not None and value["used"] >= value["limit"])
            for value in run["limits"].values()
        )
    ]
    return {
        "uninstrumented_stage_runs": sum(not run["limits"] for run in runs),
        "stage_runs": len(runs),
        "groups": [groups[key] for key in sorted(groups)],
        "recent_affected": sorted(
            affected, key=lambda run: run["job_id"], reverse=True
        )[:20],
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
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id, number, generation, base_ref, head, action, force, "
                    "status, attempts, review_result FROM jobs ORDER BY id"
                )
            ], False
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
                "SELECT job_id, result FROM assessments ORDER BY job_id"
            )
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
    rows, missing = _provider_ledger_rows("openai", state_dir / "usage.sqlite3")
    return rows, {"openai": missing}


def _provider_ledger_rows(provider, path):
    if not path.exists():
        return [], True
    try:
        with _connect_readonly(path) as db:
            rows = db.execute(
                "SELECT review_id, stage, model, month, status, usage_json, "
                "requested_at, completed_at, http_status FROM requests ORDER BY rowid"
            )
            return [_ledger_record(provider, row) for row in rows], False
    except sqlite3.Error:
        return [], True


def _ledger_record(provider, row):
    usage = _parse_json(row["usage_json"])
    tokens, usage_unknown, usage_incomplete, missing = _usage_metrics(usage)
    parsed = _parse_review_id(row["review_id"])
    model = _normalize_model(row["model"] or "unknown")
    if provider == "openai" and model.startswith("claude-"):
        provider = "claude"
    return {
        "provider": provider,
        "review_id": row["review_id"],
        "number": parsed[0],
        "job_id": parsed[1],
        "generation": parsed[2],
        "stage": row["stage"] or "unknown",
        "model": model,
        "month": row["month"] or "unknown",
        "requested_at": row["requested_at"],
        "status": row["status"] or "unknown",
        "completed_at": row["completed_at"],
        "http_status": row["http_status"],
        "web_search_calls": (
            usage.get("web_search_calls") if isinstance(usage, dict) else None
        ),
        "tokens": tokens,
        "usage_unknown": usage_unknown,
        "usage_incomplete": usage_incomplete,
        "missing_token_fields": missing,
    }


def _parse_json(text):
    if not isinstance(text, str):
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _usage_metrics(usage):
    fields = {
        name: usage.get(name + "_tokens") if isinstance(usage, dict) else None
        for name in TOKEN_FIELDS
    }
    tokens = {
        name: value if _is_nonnegative_int(value) else None
        for name, value in fields.items()
    }
    missing = [name for name, value in tokens.items() if value is None]
    unknown = tokens["input"] is None or tokens["output"] is None
    return tokens, unknown, bool(missing), missing


def _parse_review_id(review_id):
    if not isinstance(review_id, str) or not review_id.startswith(REVIEW_ID_PREFIX):
        return None, None, None
    parts = review_id[len(REVIEW_ID_PREFIX) :].split(":")
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
    if (
        not isinstance(result, dict)
        or type(result.get("schema_version")) is not int
        or result["schema_version"] != 1
    ):
        return None, True
    return result, False


def _skipped_result(result):
    run = result.get("run") if isinstance(result, dict) else None
    return isinstance(run, dict) and run.get("skipped") is True


def _review_id(row):
    return f"pr:{row['number']}:{row['id']}:{row['generation']}"


def _verifier_completed(debug):
    stages = debug.get("stages") if isinstance(debug, dict) else None
    verifier = stages.get("verifier") if isinstance(stages, dict) else None
    if not isinstance(verifier, dict) or verifier.get("status") != "completed":
        return False
    major_verifier = stages.get("major_verifier")
    if major_verifier is None:
        return True
    if not isinstance(major_verifier, dict):
        return False
    if major_verifier.get("status") == "skipped":
        return True
    coverage = core_coverage.summarize({"stages": {"major_verifier": major_verifier}})
    return coverage["verification"]["status"] == "complete"


def _time_series_summary(state_dir, review_records, addressed_findings):
    series = defaultdict(
        lambda: {
            "reviews": 0,
            "verified_reviews": 0,
            "findings": 0,
            "addressed": 0,
            "verified_unknown": False,
            "findings_unknown": False,
        }
    )
    review_days, review_undated = _review_days(
        state_dir / "review-traces", review_records
    )
    for record in review_records:
        period = review_days.get((record["job_id"], record["generation"]))
        if period is None:
            continue
        series[period]["reviews"] += 1
        if record["verified"] is None:
            series[period]["verified_unknown"] = True
        elif record["verified"]:
            series[period]["verified_reviews"] += 1
        if record["published_findings"] is None:
            series[period]["findings_unknown"] = True
        else:
            series[period]["findings"] += record["published_findings"]

    addressed_counts, addressed_undated = _addressed_day_counts(
        state_dir, addressed_findings
    )
    for period, count in addressed_counts.items():
        series[period]["addressed"] += count

    return [
        {
            "period": period,
            **{
                key: value
                for key, value in series[period].items()
                if not key.endswith("_unknown")
            },
            **(
                {"verified_reviews": None} if series[period]["verified_unknown"] else {}
            ),
            **({"findings": None} if series[period]["findings_unknown"] else {}),
        }
        for period in _day_range(series)
    ], {**review_undated, **addressed_undated}


def _day_range(series):
    if not series:
        return []
    start = datetime.fromisoformat(min(series)).date()
    end = datetime.fromisoformat(max(series)).date()
    return [
        (start + timedelta(days=offset)).isoformat()
        for offset in range((end - start).days + 1)
    ]


def _review_days(trace_dir, records):
    traces, malformed_traces = _trace_days(trace_dir)
    grouped = defaultdict(list)
    invalid_heads = 0
    for record in records:
        head = record.get("head")
        if not isinstance(head, str):
            invalid_heads += 1
            continue
        grouped[(record["number"], head.lower())].append(record)

    days = {}
    undated = invalid_heads
    missing_groups = 0
    ambiguous_groups = 0
    for key, rows in grouped.items():
        trace_times = sorted(traces.get(key, []))
        if len(rows) == 1 and len(trace_times) == 1:
            row = rows[0]
            days[(row["job_id"], row["generation"])] = trace_times[0][1]
            continue
        undated += len(rows)
        if trace_times:
            ambiguous_groups += 1
        else:
            missing_groups += 1

    return days, {
        "reviews": undated,
        "review_missing_trace_groups": missing_groups,
        "review_ambiguous_trace_groups": ambiguous_groups,
        "review_unmatched_trace_groups": len(set(traces) - set(grouped)),
        "review_invalid_head_rows": invalid_heads,
        "review_malformed_trace_filenames": malformed_traces,
    }


def _trace_days(trace_dir):
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
            (int(match.group(3)), period)
        )
    return traces, malformed


def _time_ns_period(value):
    try:
        seconds = int(value) // 1_000_000_000
        return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return None


def _addressed_day_counts(state_dir, findings):
    eligible = [
        item
        for item in findings
        if item["status"] in {"addressed", "partially_addressed"}
    ]
    days = _commit_days(
        state_dir / "followup" / "checkout" / ".git",
        {item.get("new_head") for item in eligible},
    )
    counts = Counter()
    undated = 0
    for item in eligible:
        period = days.get(item.get("new_head"))
        if period is None:
            undated += 1
        else:
            counts[period] += 1
    return counts, {"addressed": undated, "addressed_missing_commit": undated}


def _commit_days(git_dir, heads):
    heads = sorted(
        {
            head.lower()
            for head in heads
            if isinstance(head, str) and SHA.fullmatch(head)
        }
    )
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
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}

    days = {}
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
        content = output[offset : offset + size]
        offset += size
        if offset < len(output) and output[offset : offset + 1] == b"\n":
            offset += 1
        timestamp = _committer_timestamp(content)
        if timestamp is not None:
            days[head] = datetime.fromtimestamp(timestamp, timezone.utc).strftime(
                "%Y-%m-%d"
            )
    return days


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
        records.append(
            {
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
            }
        )
    return records


def _optional_string(value):
    return value if isinstance(value, str) and value else None


def _positive_int(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _addressed_summary(
    records, completed_reviews, followup_requests, followups, completed_reviews_by_job
):
    latest = {}
    for record in records:
        source_key = (
            record["number"],
            record["source_job_id"],
            record["source_generation"],
        )
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
            key: value for key, value in record.items() if key != "assessment_head"
        }
        enriched["source_report_name"] = source.get("report_name")
        key = (*source_key, record["finding_id"])
        previous = latest.get(key)
        if previous is None or _assessment_order(enriched) > _assessment_order(
            previous
        ):
            latest[key] = enriched
    findings = sorted(
        latest.values(),
        key=lambda item: (
            item["source_job_id"],
            item["source_generation"],
            item["finding_id"],
            item["assessment_job_id"],
            item["assessment_generation"],
        ),
        reverse=True,
    )
    status_counts = Counter(item["status"] for item in findings)
    source_keys = {
        (item["number"], item["source_job_id"], item["source_generation"])
        for item in findings
    }
    source_finding_count = sum(
        completed_reviews[key]["published_findings"] for key in source_keys
    )
    unsupported_exports = sum(
        1
        for followup in followups
        if (
            followup["result"].get("reason") == "findings_export_unsupported"
            and followup["job_id"] in completed_reviews_by_job
        )
    )
    return {
        "note": (
            "Best-effort automated assessment of whether later PR updates addressed "
            "findings from previous reviews. This is not a causal claim or human validation."
        ),
        "evaluated_findings": len(findings),
        "source_published_findings": source_finding_count,
        "assessed_source_reviews": len(source_keys),
        "assessment_jobs": len(
            {
                (
                    item["number"],
                    item["assessment_job_id"],
                    item["assessment_generation"],
                )
                for item in findings
            }
        ),
        "unsupported_exports": unsupported_exports,
        "usage": _request_totals(followup_requests),
        "status_counts": {
            status: status_counts.get(status, 0) for status in ADDRESSED_STATUSES
        },
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
    selected = (
        routing.get("selected") if isinstance(routing.get("selected"), dict) else {}
    )
    proposed = (
        routing.get("proposed") if isinstance(routing.get("proposed"), dict) else {}
    )
    return {
        "mode": routing.get("mode") if isinstance(routing.get("mode"), str) else None,
        "minimum_tier": (
            routing.get("minimum_tier")
            if isinstance(routing.get("minimum_tier"), str)
            else None
        ),
        "proposed_tier": proposed.get("tier")
        if isinstance(proposed.get("tier"), str)
        else None,
        "selected_tier": selected.get("tier")
        if isinstance(selected.get("tier"), str)
        else None,
        "selected_audits": _strings(selected.get("audits")),
        "selected_profiles": _strings(selected.get("profiles")),
    }


def _strings(value):
    return (
        [item for item in value if isinstance(item, str)]
        if isinstance(value, list)
        else []
    )


def _published_findings(result):
    findings = core_trace.findings_export(result)
    if findings is None:
        return None
    return [item for item in findings if isinstance(item, dict)]


def _published_finding_ids(result):
    findings = core_trace.findings_export(result)
    if findings is None:
        return set()
    return {
        item["id"]
        for item in findings
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def _has_attribution(result, findings):
    if not findings:
        return True
    return all(
        isinstance(item.get("raised_by"), list)
        and isinstance(item.get("verified_by"), str)
        for item in findings
    )


def _concept_summary(debug):
    concept = core_trace.concept_assessment(debug)
    if not concept:
        return {"published_concern": False}
    verification = (
        concept.get("verification")
        if isinstance(concept.get("verification"), dict)
        else {}
    )
    assessment = (
        verification.get("assessment")
        if isinstance(verification.get("assessment"), dict)
        else None
    )
    alternatives = (
        assessment.get("alternatives") if isinstance(assessment, dict) else []
    )
    citations = list(
        assessment.get("citations") if isinstance(assessment, dict) else []
    )
    for alternative in alternatives if isinstance(alternatives, list) else []:
        if isinstance(alternative, dict) and isinstance(
            alternative.get("citations"), list
        ):
            citations.extend(alternative["citations"])
    status = (
        concept.get("status") if isinstance(concept.get("status"), str) else "unknown"
    )
    stage = concept.get("stage") if isinstance(concept.get("stage"), str) else None
    published_concern = core_trace.published_concept_concern(debug)
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
        kind = (
            finding.get("kind") if isinstance(finding.get("kind"), str) else "unknown"
        )
        severity = (
            finding.get("severity")
            if isinstance(finding.get("severity"), str)
            else "unknown"
        )
        kinds[kind] += 1
        severities[severity] += 1
    overview["finding_kinds"] = dict(sorted(kinds.items()))
    overview["finding_severities"] = dict(sorted(severities.items()))


def _saved_review_metrics(debug, result):
    if not debug:
        common = core_trace.result_metrics(result)
        if common is None:
            return {
                "tool_calls": None,
                "model_seconds": None,
            }
        return {
            "tool_calls": common.get("tool_calls"),
            "model_seconds": common.get("total_model_seconds"),
        }
    model_turns = 0
    tool_calls = 0
    model_seconds = Decimal("0")
    for stage in (debug.get("stages") or {}).values():
        if not isinstance(stage, dict):
            continue
        turns = stage.get("turns") if isinstance(stage.get("turns"), list) else []
        tools = stage.get("tools") if isinstance(stage.get("tools"), list) else []
        model_turns += len(turns)
        tool_calls += len(tools)
        for turn in turns:
            if isinstance(turn, dict) and isinstance(
                turn.get("elapsed_seconds"), (int, float)
            ):
                model_seconds += Decimal(str(turn["elapsed_seconds"]))
    return {
        "model_turns": model_turns,
        "tool_calls": tool_calls,
        "model_seconds": float(model_seconds),
    }


def _report_name(report_dir, row, result):
    head = result.get("head_sha")
    if not isinstance(head, str):
        return None
    name = f"{row['number']}-{head}-{row['id']}-{row['generation']}.html"
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
    return {**_new_provider_totals(), "providers": defaultdict(_new_provider_totals)}


def _new_provider_totals():
    return {
        "request_count": 0,
        "unknown_usage_count": 0,
        "incomplete_usage_count": 0,
        "status_counts": Counter(),
        "http_status_counts": Counter(),
        "rate_limit_count": 0,
        "web_search_calls": None,
        "unknown_web_search_count": 0,
        "tokens": dict.fromkeys(TOKEN_FIELDS),
        "unknown_token_counts": dict.fromkeys(TOKEN_FIELDS, 0),
    }


def _add_request(record, request):
    record["request_count"] += 1
    record["status_counts"][request["status"]] += 1
    if "providers" in record and request.get("provider") is not None:
        _add_request(
            record["providers"][request["provider"]], {**request, "provider": None}
        )
    if request["status"] == "duplicate":
        return
    if request["usage_unknown"]:
        record["unknown_usage_count"] += 1
    if request["usage_incomplete"]:
        record["incomplete_usage_count"] += 1
    if request.get("http_status") is not None:
        record["http_status_counts"][str(request["http_status"])] += 1
    record["rate_limit_count"] += request.get("http_status") == 429
    searches = request.get("web_search_calls")
    if _is_nonnegative_int(searches):
        record["web_search_calls"] = (record["web_search_calls"] or 0) + searches
    else:
        record["unknown_web_search_count"] += 1
    for key, value in request["tokens"].items():
        if value is not None:
            record["tokens"][key] = (record["tokens"][key] or 0) + value
    for key in request.get("missing_token_fields", []):
        record["unknown_token_counts"][key] += 1


def _final_request_totals(record):
    return {
        "request_count": record["request_count"],
        "unknown_usage_count": record["unknown_usage_count"],
        "incomplete_usage_count": record["incomplete_usage_count"],
        "status_counts": dict(sorted(record["status_counts"].items())),
        "http_status_counts": dict(sorted(record["http_status_counts"].items())),
        "rate_limit_count": record["rate_limit_count"],
        "web_search_calls": record["web_search_calls"],
        "unknown_web_search_count": record["unknown_web_search_count"],
        "tokens": dict(record["tokens"]),
        "unknown_token_counts": dict(record["unknown_token_counts"]),
    }


def _observed_review_totals(totals, observed):
    if observed:
        return {"usage_observed": True, **totals}
    return {
        "usage_observed": False,
        **totals,
        "tokens": dict.fromkeys(totals["tokens"]),
    }


def _usage_summary(requests, current_month):
    lifetime = _request_totals(requests)
    current = _request_totals(
        [request for request in requests if request["month"] == current_month]
    )
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
        "usage_note": (
            "Requests include retries, failed attempts and duplicate responses. "
            "Duplicate response usage is counted once. "
            "Token totals include only reported usage."
        ),
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


def _activity_totals(record):
    record = record or _new_provider_totals()
    return {
        key: record[key]
        for key in (
            "request_count",
            "unknown_usage_count",
        )
    }


def _activity_periods(records, periods):
    if not records:
        return []
    first = min(records)
    return [
        {"period": period, **_activity_totals(records.get(period))}
        for period in periods
        if period >= first
    ]


def _usage_trends(requests, now):
    by_day = defaultdict(_new_provider_totals)
    by_week = defaultdict(_new_provider_totals)
    by_month = defaultdict(_new_provider_totals)
    undated = _new_provider_totals()
    for request in requests:
        _add_request(by_month[request["month"]], request)
        try:
            day = datetime.fromtimestamp(request["requested_at"], timezone.utc).date()
        except (OverflowError, OSError, TypeError, ValueError):
            _add_request(undated, request)
            continue
        monday = day - timedelta(days=day.weekday())
        _add_request(by_day[day.isoformat()], request)
        _add_request(by_week[monday.isoformat()], request)

    today = now.date()
    monday = today - timedelta(days=today.weekday())
    days = _activity_periods(
        by_day,
        [(today - timedelta(days=offset)).isoformat() for offset in range(29, -1, -1)],
    )
    weeks = _activity_periods(
        by_week,
        [
            (monday - timedelta(weeks=offset)).isoformat()
            for offset in range(11, -1, -1)
        ],
    )
    month_periods = []
    for offset in range(11, -1, -1):
        year, month = divmod(now.year * 12 + now.month - 1 - offset, 12)
        month_periods.append(f"{year:04d}-{month + 1:02d}")
    current_month = now.strftime("%Y-%m")
    return {
        "timezone": "UTC",
        "week_start": "Monday",
        "current_day": days[-1] if days else None,
        "current_week": weeks[-1] if weeks else None,
        "current_month": {
            "period": current_month,
            **_activity_totals(by_month.get(current_month)),
        },
        "days": days,
        "weeks": weeks,
        "months": _activity_periods(by_month, month_periods),
        "undated": _activity_totals(undated),
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
            {"publish": 0, "drop": 0, "unresolved": 0, "undisposed": 0}
        ),
        "accepted_findings": 0,
        "findings": [],
        "concept_stage": False,
        "conceptual_concerns": 0,
        "sole_source_findings": 0,
        "shared_findings": 0,
        "attribution_unknown_reviews": 0,
        "tool_calls": 0,
        "model_turns": 0,
        "model_seconds": 0.0,
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


def _stage_findings(debug, name, stage, row, report_name):
    """Export candidate metadata only; model output and evidence stay in traces."""
    sources = debug.get("candidate_sources") or {}
    try:
        output = json.loads(stage.get("raw_output") or "{}")
    except (TypeError, ValueError):
        output = {}
    originals = output.get("findings", []) if isinstance(output, dict) else []
    originals = originals if isinstance(originals, list) else []
    attribution = core_trace.finding_attribution(debug)
    records = []
    for identifier, source in sorted(sources.items()):
        if source != name:
            continue
        decision = next(
            (
                item
                for item in debug.get("decisions", [])
                if isinstance(item, dict)
                and identifier in (item.get("candidate_ids") or [])
            ),
            {},
        )
        accepted = next(
            (
                item
                for item in attribution
                if isinstance(item, dict)
                and identifier in (item.get("candidate_ids") or [])
            ),
            {},
        )
        original = next(
            (
                item
                for index, item in enumerate(originals, 1)
                if f"{name}:{index}" == identifier and isinstance(item, dict)
            ),
            {},
        )
        finding = accepted or decision.get("finding") or original
        disposition = decision.get("disposition", "undisposed")
        records.append(
            {
                "number": row["number"],
                "job_id": row["id"],
                "candidate_id": identifier,
                "title": finding.get("title") or identifier,
                "path": finding.get("path"),
                "line": finding.get("line"),
                "disposition": disposition
                if disposition in {"publish", "drop", "unresolved"}
                else "undisposed",
                "accepted": bool(accepted),
                "attribution": (
                    "shared"
                    if len(set(accepted.get("raised_by") or [])) > 1
                    else "sole"
                )
                if accepted
                else None,
                "finding_id": accepted.get("finding_id"),
                "report_name": report_name,
            }
        )
    return records


def _add_stage_results(stages, debug, row, report_name):
    metrics = core_trace.stage_metrics(debug)
    attribution_known = bool(core_trace.finding_attribution(debug))
    has_publish = any(
        isinstance(decision, dict) and decision.get("disposition") == "publish"
        for decision in debug.get("decisions", [])
    )
    for name, stage in sorted((debug.get("stages") or {}).items()):
        if not isinstance(stage, dict):
            continue
        model = _normalize_model(
            stage.get("model") if isinstance(stage.get("model"), str) else "unknown"
        )
        record = stages[_stage_key(name, model)]
        record["stage"] = name
        record["configured_model"] = model
        record["findings"].extend(_stage_findings(debug, name, stage, row, report_name))
        stage_metrics = metrics.get(name, {})
        record["concept_stage"] = bool(stage_metrics.get("concept_stage"))
        record["conceptual_concerns"] += stage_metrics.get("conceptual_concerns", 0)
        record["saved_result_runs"] += 1
        status = (
            stage.get("status") if isinstance(stage.get("status"), str) else "unknown"
        )
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
            if isinstance(turn, dict) and isinstance(
                turn.get("elapsed_seconds"), (int, float)
            ):
                record["model_seconds"] += float(turn["elapsed_seconds"])
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
            "findings": record["findings"],
            "concept_stage": record["concept_stage"],
            "conceptual_concerns": record["conceptual_concerns"],
            "selected_candidate_findings": (record["candidate_counts"]["publish"]),
            "sole_source_findings": record["sole_source_findings"],
            "shared_findings": record["shared_findings"],
            "attribution_unknown_reviews": record["attribution_unknown_reviews"],
            "acceptance_denominator_runs": executed,
            "accepted_per_executed_run": (
                round(accepted / executed, 4) if executed else None
            ),
            "tool_calls": record["tool_calls"],
            "model_turns": record["model_turns"],
            "model_seconds": round(record["model_seconds"], 2),
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
        records.append(
            {
                "stage": record["stage"],
                "configured_model": record["configured_model"],
                "accepted_findings": accepted,
                "selected_candidate_findings": selected_candidates,
                "sole_source_findings": record["sole_source_findings"],
                "shared_findings": record["shared_findings"],
                "executed_runs": record["executed_runs"],
                "request_count": record["ledger"]["request_count"],
                "note": "Shared findings overlap across stages and do not sum to total unique findings.",
            }
        )
    return sorted(
        records,
        key=lambda item: (
            item["accepted_findings"],
            item["sole_source_findings"],
            item["shared_findings"],
            -item["request_count"],
        ),
        reverse=True,
    )


def _new_adversarial_comparison():
    return {
        "completed_reviews": 0,
        "sole_accepted": dict.fromkeys(ADVERSARIAL_STAGES, 0),
        "shared_accepted": dict.fromkeys(ADVERSARIAL_STAGES, 0),
        "note": "Observed reviews with completed Codex and Claude (historically Gemini) adversarial stages only; this is not causal recall.",
    }


def _add_adversarial_comparison(paired, debug):
    stages = debug.get("stages") or {}
    completed = {
        name
        for name in ADVERSARIAL_STAGES
        if isinstance(stages.get(name), dict)
        and stages[name].get("status") == "completed"
    }
    if "adversarial" not in completed or len(completed) < 2:
        return
    metrics = core_trace.stage_metrics(debug)
    paired["completed_reviews"] += 1
    for name in completed:
        paired["sole_accepted"][name] += metrics.get(name, {}).get(
            "sole_source_findings", 0
        )
        paired["shared_accepted"][name] += metrics.get(name, {}).get(
            "shared_findings", 0
        )


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
