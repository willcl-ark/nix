"""Review metrics and pure result accessors."""

from .spend import price_usd
from .config import AUDIT_NAMES


def _turn_usage(turn, fallback_model):
    usage = {
        "input_tokens": turn.get("input_tokens"),
        "output_tokens": turn.get("output_tokens"),
        "cached_tokens": turn.get("cached_tokens", 0),
        "cache_write_tokens": turn.get("cache_write_tokens"),
        "web_search_calls": turn.get("web_search_calls", 0),
    }
    return usage, price_usd(turn.get("model") or fallback_model, usage)


def _stage_turn_metrics(stage):
    priced = []
    unknown_usage = 0
    incomplete_usage = 0
    for turn in stage.get("turns", []):
        usage, cost = _turn_usage(turn, stage.get("model"))
        if cost is None:
            unknown_usage += 1
        else:
            priced.append(cost)
            if turn.get("cache_write_tokens") is None:
                incomplete_usage += 1
    return {
        "known_estimated_cost_usd": float(sum(priced)) if priced else 0,
        "unknown_usage_count": unknown_usage,
        "incomplete_usage_count": incomplete_usage,
    }


def finding_attribution(debug):
    attribution = debug.get("finding_attribution") or []
    return list(attribution) if isinstance(attribution, list) else []


def current_diagnostics(result):
    """Return current-engine diagnostics only when their schema is declared."""
    if not isinstance(result, dict):
        return {}
    engine = result.get("engine")
    if not isinstance(engine, dict) or engine.get("name") != "current":
        return {}
    output = result.get("output")
    if (
        isinstance(output, dict)
        and type(output.get("schema_version")) is int
        and output["schema_version"] == 2
    ):
        data = output.get("data")
        if (
            isinstance(data, dict)
            and type(data.get("schema_version")) is int
            and data["schema_version"] == 2
        ):
            return data
    run = result.get("run")
    attempt = run.get("attempt") if isinstance(run, dict) else None
    debug = attempt
    if (
        isinstance(debug, dict)
        and type(debug.get("schema_version")) is int
        and debug["schema_version"] == 2
    ):
        return debug
    return {}


def findings_export(result):
    """Return exported findings, or None when the engine does not support them."""
    if (
        not isinstance(result, dict)
        or type(result.get("schema_version")) is not int
        or result["schema_version"] != 1
    ):
        return None
    output = result.get("output")
    if (
        not isinstance(output, dict)
        or type(output.get("schema_version")) is not int
        or output["schema_version"] < 1
    ):
        return None
    exports = output.get("exports")
    findings = exports.get("findings") if isinstance(exports, dict) else None
    return findings if isinstance(findings, list) else None


def result_metrics(result):
    run = result.get("run") if isinstance(result, dict) else None
    metrics = run.get("metrics") if isinstance(run, dict) else None
    return metrics if isinstance(metrics, dict) else None


def result_engine(result):
    engine = result.get("engine") if isinstance(result, dict) else None
    return engine if isinstance(engine, dict) else {}


def concept_assessment(debug):
    assessment = debug.get("concept_assessment") if isinstance(debug, dict) else None
    return assessment if isinstance(assessment, dict) else {}


def published_concept_assessment(debug):
    concept = concept_assessment(debug)
    verification = concept.get("verification")
    if (
        concept.get("status") == "verified"
        and isinstance(verification, dict)
        and verification.get("disposition") == "publish"
        and isinstance(verification.get("assessment"), dict)
        and isinstance(concept.get("summary"), str)
        and concept["summary"].strip()
    ):
        return verification["assessment"]
    return None


def published_concept_concern(debug):
    assessment = published_concept_assessment(debug)
    return assessment is not None and assessment.get("assessment") != "worth_pursuing"


def _new_stage_metrics(name, stage):
    record = {
        "model": stage.get("model"),
        "status": stage.get("status", "unknown"),
        "concept_stage": name in {"archaeologist", "alternatives"},
        "conceptual_concerns": 0,
        "calls": len(stage.get("turns", [])),
        "tool_calls": len(stage.get("tools", [])),
        "candidate_counts": {"publish": 0, "drop": 0, "unresolved": 0, "undisposed": 0},
        "accepted_findings": 0,
        "sole_source_findings": 0,
        "shared_findings": 0,
    }
    if "profiles" in stage:
        record["profiles"] = stage["profiles"]
    record.update(_stage_turn_metrics(stage))
    return record


def stage_metrics(debug):
    stages = debug.get("stages") or {}
    candidate_sources = debug.get("candidate_sources") or {}
    metrics = {
        name: _new_stage_metrics(name, stage)
        for name, stage in stages.items()
        if isinstance(stage, dict)
    }
    assessment = concept_assessment(debug)
    if published_concept_concern(debug):
        assessment_stage = assessment.get("stage")
        if not isinstance(assessment_stage, str) or assessment_stage not in metrics:
            concept_stages = [
                name for name, record in metrics.items() if record.get("concept_stage")
            ]
            assessment_stage = concept_stages[0] if len(concept_stages) == 1 else None
        if assessment_stage in metrics:
            metrics[assessment_stage]["conceptual_concerns"] = 1

    candidates_by_stage = {}
    disposed_candidate_ids = set()
    for decision in debug.get("decisions", []):
        if not isinstance(decision, dict):
            continue
        disposition = decision.get("disposition")
        if disposition not in {"publish", "drop", "unresolved"}:
            continue
        for candidate_id in decision.get("candidate_ids") or []:
            if not isinstance(candidate_id, str):
                continue
            disposed_candidate_ids.add(candidate_id)
            stage = candidate_sources.get(candidate_id)
            if stage in metrics:
                candidates_by_stage.setdefault(stage, {}).setdefault(
                    disposition, set()
                ).add(candidate_id)

    for stage, disposition_ids in candidates_by_stage.items():
        for disposition, candidate_ids in disposition_ids.items():
            metrics[stage]["candidate_counts"][disposition] = len(candidate_ids)
    for candidate_id, stage in candidate_sources.items():
        if stage in metrics and candidate_id not in disposed_candidate_ids:
            metrics[stage]["candidate_counts"]["undisposed"] += 1

    findings_by_stage = {}
    for finding in finding_attribution(debug):
        if not isinstance(finding, dict):
            continue
        finding_id = finding.get("finding_id")
        if not finding_id:
            continue
        raised_by = finding.get("raised_by") or []
        shared = len(set(raised_by)) > 1
        for stage in set(raised_by):
            if stage in metrics:
                findings_by_stage.setdefault(
                    stage, {"accepted": set(), "sole": set(), "shared": set()}
                )
                findings_by_stage[stage]["accepted"].add(finding_id)
                findings_by_stage[stage]["shared" if shared else "sole"].add(finding_id)

    for stage, counts in findings_by_stage.items():
        record = metrics[stage]
        record["accepted_findings"] = len(counts["accepted"])
        record["sole_source_findings"] = len(counts["sole"])
        record["shared_findings"] = len(counts["shared"])

    return {name: metrics[name] for name in sorted(metrics)}


def request_metrics(records):
    """Summarize model and tool records without relying on engine stage names."""
    if isinstance(records, dict):
        records = records.values()
    records = list(records or [])
    turns = [
        (turn, record.get("model"))
        for record in records
        if isinstance(record, dict)
        for turn in record.get("turns", [])
    ]
    tools = [
        tool
        for record in records
        if isinstance(record, dict)
        for tool in record.get("tools", [])
    ]
    priced = []
    unknown_usage = 0
    incomplete_usage = 0
    input_tokens = output_tokens = 0
    total_seconds = 0.0
    for turn, fallback_model in turns:
        usage, cost = _turn_usage(turn, fallback_model)
        if cost is None:
            unknown_usage += 1
        else:
            priced.append(cost)
            input_tokens += usage["input_tokens"]
            output_tokens += usage["output_tokens"]
            if turn.get("cache_write_tokens") is None:
                incomplete_usage += 1
        if isinstance(turn.get("elapsed_seconds"), (int, float)):
            total_seconds += turn["elapsed_seconds"]
    model_turns = [turn for turn, _ in turns if "request_bytes" in turn]
    return {
        "model_turns": len(turns),
        "tool_calls": len(tools),
        "estimated_cost_usd": (float(sum(priced)) if priced else 0),
        "known_usage_count": len(priced),
        "unknown_usage_count": unknown_usage,
        "incomplete_usage_count": incomplete_usage,
        "usage_complete": not (unknown_usage or incomplete_usage),
        "total_input_tokens": input_tokens,
        "total_output_tokens": output_tokens,
        "total_model_seconds": round(total_seconds, 2),
        "model_responses": len(model_turns),
    }


def review_metrics(debug):
    stages = debug.get("stages", {})
    metrics = request_metrics(stages)
    metrics["audit_calls"] = sum(
        name in AUDIT_NAMES and stage.get("status") != "skipped"
        for name, stage in stages.items()
    )
    return metrics
