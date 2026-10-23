"""Public review projection and private service trace storage."""

import json
import os
import time

from ralph.trace import (
    current_diagnostics,
    result_engine,
    result_metrics,
    findings_export,
    finding_attribution,
    stage_metrics,
    review_metrics,
)

MAX_PUBLIC_STAGE_OUTPUT_BYTES = 4_000


def _public_value(value):
    if isinstance(value, dict):
        public = {
            key: _public_value(item)
            for key, item in value.items()
            if key != "raw_output_truncated"
        }
        raw = value.get("raw_output")
        if isinstance(raw, str):
            encoded = raw.encode()
            public["raw_output"] = encoded[:MAX_PUBLIC_STAGE_OUTPUT_BYTES].decode(
                errors="replace"
            )
            public["raw_output_truncated"] = (
                len(encoded) > MAX_PUBLIC_STAGE_OUTPUT_BYTES
            )
        return public
    if isinstance(value, list):
        return [_public_value(item) for item in value]
    return value


def review_trace(result):
    debug = current_diagnostics(result)
    engine = result_engine(result)
    run = result.get("run") if isinstance(result, dict) else {}
    findings = findings_export(result)
    trace = {
        "engine": {
            key: engine.get(key) for key in ("name", "code_sha256", "config_sha256")
        },
        "status": run.get("status") if isinstance(run, dict) else None,
        "metrics": result_metrics(result),
        "findings_export_status": "supported"
        if findings is not None
        else "unsupported",
        "findings": findings,
    }
    if not debug:
        return trace
    trace.update(
        {
            "endpoint": "/v1/responses",
            "store": False,
            "instructions": debug.get("instructions"),
            "input": "PR text, patch, and commits omitted from public debug output",
            "stages": _public_value(debug.get("stages", {})),
            "budget": run.get("budget") if isinstance(run, dict) else None,
        }
    )
    attribution = finding_attribution(debug)
    if attribution:
        trace["finding_attribution"] = attribution
    trace["stage_metrics"] = stage_metrics(debug)
    for key in (
        "routing",
        "coverage",
        "candidate_sources",
        "verification_budget",
        "ppq_budget",
        "concept_assessment",
    ):
        if key in debug:
            trace[key] = debug[key]
    if debug.get("stage_outputs"):
        trace["stage_outputs"] = {
            name: {
                "text": output.encode()[:MAX_PUBLIC_STAGE_OUTPUT_BYTES].decode(
                    errors="replace"
                ),
                "truncated": len(output.encode()) > MAX_PUBLIC_STAGE_OUTPUT_BYTES,
            }
            for name, output in debug["stage_outputs"].items()
        }
        trace["stage_outputs_note"] = (
            "Preliminary agent responses are unverified; only the review above "
            "is intended as a public finding. Full responses are retained in "
            "the bot's private state when trace storage succeeds."
        )
    if "review_input_bytes" in debug:
        trace["review_input_bytes"] = debug["review_input_bytes"]
        trace["review_input_sha256"] = debug["review_input_sha256"]
    if debug.get("skip"):
        trace["skip"] = debug["skip"]
    if debug.get("pipeline_stage"):
        trace["pipeline_stage"] = debug["pipeline_stage"]
    if "decisions" in debug:
        trace["decisions"] = debug["decisions"]
    trace.update(review_metrics(debug))
    trace["pricing_note"] = (
        "Estimated from token usage and hosted web searches at configured rates. "
        "Unknown usage is excluded from this subtotal; the budget "
        "includes uncertain reservations. Missing cache-write "
        "counts use the conservative cache-write rate."
    )
    return trace


def save_review_trace(state_dir, number, result):
    trace_dir = state_dir / "review-traces"
    trace_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    head_sha = result.get("head_sha", "unknown")
    path = trace_dir / f"{number}-{head_sha}-{time.time_ns()}.json"
    record = {**result, "pr": number, "trace": review_trace(result)}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(record, file, ensure_ascii=False, indent=2)
        file.write("\n")
    return path
