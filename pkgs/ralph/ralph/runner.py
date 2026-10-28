"""Execute one review without repository, queue, or publication policy."""

import json
import uuid


def validate_json(value):
    """Reject values which cannot be written as interoperable JSON."""
    json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return value


def _usage_summary(usage, attempt, name, *, failure=False):
    if usage is None:
        return None
    try:
        return usage.summary()
    except Exception:
        if not failure:
            raise
        return attempt.get(name)


def _result(case, runtime, selected_engine, status, *, skipped=False, failure=False):
    if not hasattr(runtime, "_review_id"):
        runtime._review_id = (
            runtime.usage.review_id if runtime.usage is not None else str(uuid.uuid4())
        )
    snapshot = case.snapshot
    return {
        "schema_version": 1,
        "engine": selected_engine.identity,
        "base_sha": snapshot.base_sha,
        "head_sha": snapshot.head_sha,
        "merge_base": snapshot.merge_base,
        "diff_base_sha": snapshot.diff_base_sha,
        "content": "",
        "output": None,
        "run": {
            "review_id": runtime._review_id,
            "status": status,
            "metrics": runtime.metrics(),
            "skipped": skipped,
            "usage": _usage_summary(
                runtime.usage, runtime.attempt, "usage", failure=failure
            ),
            "attempt": runtime.attempt,
        },
    }


def run_review(case, runtime, selected_engine):
    """Return a validated result, preserving original exceptions on failure."""
    runtime.check_current()
    output = selected_engine.run(case, runtime)
    runtime.check_current()
    result = _result(case, runtime, selected_engine, output.status)
    result["content"] = output.content
    result["output"] = {
        "schema_version": output.schema_version,
        "data": output.data,
        "exports": output.exports,
    }
    return validate_json(result)


def failure_result(case, runtime, selected_engine, error, *, status="failed"):
    """Build diagnostics after the caller has handled an execution exception."""
    result = _result(case, runtime, selected_engine, status, failure=True)
    result["error"] = {"type": type(error).__name__, "message": str(error)}
    return validate_json(result)


def skipped_result(case, runtime, selected_engine, reason, *, no_changes=False):
    """Record inputs that require no model execution."""
    result = _result(
        case,
        runtime,
        selected_engine,
        "no_changes" if no_changes else "skipped",
        skipped=True,
    )
    result["content"] = (
        "No committed changes to review." if no_changes else f"Skipped: {reason}"
    )
    result["run"]["reason"] = reason
    return validate_json(result)
