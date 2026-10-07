"""Assess whether previously published Ralph findings were addressed."""

import hashlib
import json
import re
from datetime import datetime, timezone

from . import model, repository


MAX_CANDIDATES = 12
MAX_CONTENT_BYTES = 20_000
MAX_OUTPUT_TOKENS = 3_000
STATUSES = {"addressed", "partially_addressed", "still_present", "unclear"}

SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source_job_id": {"type": "integer"},
                    "source_generation": {"type": "integer"},
                    "finding_id": {"type": "string"},
                    "status": {"type": "string", "enum": sorted(STATUSES)},
                    "reason": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": [
                    "source_job_id", "source_generation", "finding_id",
                    "status", "reason", "evidence",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["findings"],
    "additionalProperties": False,
}


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z")


def _truncate(text, limit):
    text = str(text or "")
    data = text.encode()
    if len(data) <= limit:
        return text
    return data[:limit].decode(errors="replace") + "\n[Truncated]"


def _fingerprint(finding):
    text = "\n".join(str(finding.get(key) or "")
                     for key in ("path", "title", "body"))
    normalized = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(normalized.encode()).hexdigest()


def _head(job):
    result = job.get("review_result") or {}
    return result.get("head_sha") or job.get("head")


def _base(job):
    result = job.get("review_result") or {}
    return result.get("base_sha") or job.get("base_ref")


def _decision_findings(debug):
    result = {}
    index = 1
    for decision in debug.get("decisions") or []:
        if not isinstance(decision, dict) or decision.get("disposition") != "publish":
            continue
        finding = decision.get("finding")
        if isinstance(finding, dict):
            result[f"finding:{index}"] = finding
            index += 1
    return result


def _published_findings(job):
    result = job.get("review_result") or {}
    debug = result.get("debug") if isinstance(result, dict) else {}
    if not isinstance(debug, dict):
        return []
    decisions = _decision_findings(debug)
    findings = []
    for attribution in debug.get("finding_attribution") or []:
        if not isinstance(attribution, dict):
            continue
        finding_id = attribution.get("finding_id")
        if not isinstance(finding_id, str) or not finding_id:
            continue
        decision = decisions.get(finding_id, {})
        title = attribution.get("title") or decision.get("title")
        path = attribution.get("path") or decision.get("path")
        line = attribution.get("line") or decision.get("line") or 1
        if not isinstance(title, str) or not isinstance(path, str):
            continue
        if type(line) is not int or line < 1:
            line = 1
        finding = {
            "source_job_id": job["id"],
            "source_generation": job["generation"],
            "finding_id": finding_id,
            "title": title,
            "path": path,
            "line": line,
            "body": decision.get("body", ""),
            "source_head": _head(job) or "",
        }
        finding["fingerprint"] = _fingerprint(finding)
        findings.append(finding)
    return findings


def _source_findings(history):
    sources = {}
    for job in history:
        for finding in _published_findings(job):
            sources[(finding["source_job_id"], finding["source_generation"],
                     finding["finding_id"])] = finding
    return sources


def _prior_assessment(prior_assessments, job_id):
    assessment = prior_assessments.get(job_id)
    if isinstance(assessment, str):
        try:
            assessment = json.loads(assessment)
        except json.JSONDecodeError:
            return None
    return assessment if isinstance(assessment, dict) else None


def _candidate_from_assessment(item, sources):
    if not isinstance(item, dict):
        return None, None, None
    try:
        source_job_id = int(item["source_job_id"])
        source_generation = int(item["source_generation"])
    except (KeyError, TypeError, ValueError):
        return None, None, None
    title = item.get("title")
    path = item.get("path")
    finding_id = item.get("finding_id")
    status = item.get("status")
    if (status not in STATUSES or not all(isinstance(value, str) and value
                                           for value in (title, path, finding_id))):
        return None, None, None
    key = (source_job_id, source_generation, finding_id)
    finding = {
        "source_job_id": source_job_id,
        "source_generation": source_generation,
        "finding_id": finding_id,
        "title": title,
        "path": path,
        "line": 1,
        "body": "",
        "source_head": item.get("old_head", ""),
        "deferred": item.get("evidence") == "candidate_limit",
    }
    finding.update(sources.get(key, {}))
    finding["fingerprint"] = _fingerprint(finding)
    return key, finding, status


def _apply_assessment(active, resolved_fingerprints, assessment, sources):
    if not isinstance(assessment, dict):
        return
    for item in assessment.get("findings") or []:
        key, finding, status = _candidate_from_assessment(item, sources)
        if key is None:
            continue
        if status == "addressed":
            active.pop(key, None)
            resolved_fingerprints.add(finding["fingerprint"])
            continue
        if finding["fingerprint"] not in resolved_fingerprints:
            active[key] = finding


def _add_published(active, resolved_fingerprints, job):
    active_fingerprints = {finding["fingerprint"] for finding in active.values()}
    for finding in _published_findings(job):
        key = (finding["source_job_id"], finding["source_generation"],
               finding["finding_id"])
        fingerprint = finding["fingerprint"]
        if (key in active or fingerprint in active_fingerprints
                or fingerprint in resolved_fingerprints):
            continue
        active[key] = finding
        active_fingerprints.add(fingerprint)


def _candidate_findings(history, prior_assessments):
    history = _relevant_history(history, prior_assessments)
    sources = _source_findings(history)
    active = {}
    resolved_fingerprints = set()
    for job in reversed(history):
        _apply_assessment(
            active, resolved_fingerprints,
            _prior_assessment(prior_assessments, job["id"]), sources)
        _add_published(active, resolved_fingerprints, job)
    candidates = list(active.values())
    return sorted(candidates, key=lambda finding: (
        not finding.get("deferred"), finding["source_generation"],
        finding["source_job_id"], finding["finding_id"]))


def _assessment_source_ids(assessment):
    if not isinstance(assessment, dict):
        return set()
    source_ids = set()
    for item in assessment.get("findings") or []:
        if not isinstance(item, dict):
            continue
        try:
            source_ids.add(int(item["source_job_id"]))
        except (KeyError, TypeError, ValueError):
            continue
    return source_ids


def _relevant_history(history, prior_assessments):
    assessed = [
        index for index, job in enumerate(history)
        if _prior_assessment(prior_assessments, job["id"]) is not None
    ]
    if not assessed:
        return history[:1]
    oldest_assessed = max(assessed)
    end = min(len(history), oldest_assessed + 2)
    relevant = list(history[:end])
    included = {job["id"] for job in relevant}
    source_ids = set()
    for job in relevant:
        source_ids.update(_assessment_source_ids(
            _prior_assessment(prior_assessments, job["id"])))
    for job in history[end:]:
        if job["id"] in source_ids and job["id"] not in included:
            relevant.append(job)
            included.add(job["id"])
    return relevant


def _output_record(candidate, assessed_from_head, new_head, status, reason, evidence):
    return {
        "source_job_id": candidate["source_job_id"],
        "source_generation": candidate["source_generation"],
        "finding_id": candidate["finding_id"],
        "title": candidate["title"],
        "path": candidate["path"],
        "old_head": candidate.get("source_head") or assessed_from_head,
        "new_head": new_head,
        "assessed_from_head": assessed_from_head,
        "status": status,
        "reason": str(reason or "")[:1000],
        "evidence": str(evidence or "")[:1000],
    }


def _unclear(candidates, assessed_from_head, new_head, reason, evidence):
    return [_output_record(candidate, assessed_from_head, new_head, "unclear",
                           reason, evidence)
            for candidate in candidates]


def _model_input(checkout, previous_job, current_job, candidates,
                 assessed_from_head, new_head):
    diff = repository.diff_between_heads(checkout, assessed_from_head, new_head)
    finding_inputs = []
    for index, candidate in enumerate(candidates, 1):
        source_head = candidate.get("source_head") or assessed_from_head
        if repository.has_commit(checkout, source_head):
            source_to_new_diff = repository.diff_path_between_heads(
                checkout, source_head, new_head, candidate["path"])
            old_file_excerpt = repository.file_excerpt_at(
                checkout, source_head, candidate["path"], candidate["line"])
        else:
            source_to_new_diff = "Source head is unavailable in the local checkout."
            old_file_excerpt = source_to_new_diff
        finding_inputs.append({
            "index": index,
            "source_job_id": candidate["source_job_id"],
            "source_generation": candidate["source_generation"],
            "finding_id": candidate["finding_id"],
            "title": candidate["title"],
            "path": candidate["path"],
            "source_head": source_head,
            "body": candidate.get("body", ""),
            "source_to_new_path_diff": source_to_new_diff,
            "old_file_excerpt": old_file_excerpt,
            "new_file_excerpt": repository.file_excerpt_at(
                checkout, new_head, candidate["path"], candidate["line"]),
        })
    previous_result = previous_job.get("review_result") or {}
    return json.dumps({
        "previous_job_id": previous_job["id"],
        "previous_generation": previous_job["generation"],
        "old_head": assessed_from_head,
        "new_head": new_head,
        "old_base": _base(previous_job),
        "new_base": _base(current_job),
        "previous_review_content": _truncate(
            previous_result.get("content", ""), MAX_CONTENT_BYTES),
        "old_to_new_diff": diff,
        "findings": finding_inputs,
    }, sort_keys=True)


def _parse_assessments(text, candidates, assessed_from_head, new_head):
    result = json.loads(text)
    if not isinstance(result, dict) or set(result) != {"findings"}:
        raise ValueError("invalid addressed findings response")
    rows = result["findings"]
    if not isinstance(rows, list) or len(rows) != len(candidates):
        raise ValueError("addressed findings response omitted candidates")
    expected = {(candidate["source_job_id"], candidate["source_generation"],
                 candidate["finding_id"]): candidate for candidate in candidates}
    seen = set()
    output = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid addressed finding row")
        key = (row.get("source_job_id"), row.get("source_generation"),
               row.get("finding_id"))
        if key not in expected or key in seen or row.get("status") not in STATUSES:
            raise ValueError("unexpected addressed finding identity or status")
        status = row["status"]
        reason = row.get("reason")
        evidence = row.get("evidence")
        if not isinstance(reason, str) or not isinstance(evidence, str):
            raise ValueError("addressed finding reason and evidence must be strings")
        if status in {"addressed", "partially_addressed"} and (
                not reason.strip() or not evidence.strip()):
            raise ValueError("addressed findings require reason and evidence")
        seen.add(key)
        output.append(_output_record(
            expected[key], assessed_from_head, new_head, status, reason, evidence))
    if seen != set(expected):
        raise ValueError("addressed findings response repeated candidates")
    return output


def assess_completed_review(api_key, checkout, current_job, history,
                            prior_assessments, prompt_config, budget,
                            is_current=None):
    stage_model = prompt_config.models["addressed_findings"]
    record = {"assessed_at": _now(), "model": stage_model, "findings": []}
    if not history:
        record.update(status="skipped", reason="no_previous_review")
        return record
    previous = history[0]
    assessed_from_head = _head(previous)
    new_head = _head(current_job)
    if assessed_from_head == new_head:
        record.update(status="skipped", reason="same_head",
                      old_head=assessed_from_head, new_head=new_head)
        return record
    candidates = _candidate_findings(history, prior_assessments)
    if not candidates:
        record.update(status="skipped", reason="no_previous_findings",
                      old_head=assessed_from_head, new_head=new_head)
        return record
    assessed = candidates[:MAX_CANDIDATES]
    overflow = candidates[MAX_CANDIDATES:]
    if (not repository.has_commit(checkout, assessed_from_head)
            or not repository.has_commit(checkout, new_head)):
        record.update(status="complete", old_head=assessed_from_head,
                      new_head=new_head, reason="missing_git_object")
        record["findings"] = _unclear(
            candidates, assessed_from_head, new_head,
            "Old or new head object is unavailable.",
            "Ralph cannot compare the old and new heads in the local checkout.")
        return record
    try:
        input_text = _model_input(
            checkout, previous, current_job, assessed, assessed_from_head, new_head)
        data = {"model": stage_model, "store": False,
                "reasoning": {"effort": "low"},
                "instructions": prompt_config.audit_prompts["addressed_findings"],
                "input": [{"role": "user", "content": input_text}],
                "max_output_tokens": MAX_OUTPUT_TOKENS,
                "text": {"format": {"type": "json_schema",
                                    "name": "addressed_findings",
                                    "strict": True, "schema": SCHEMA}}}
        result, payload, elapsed = model.request_response(
            api_key, data, "addressed_findings", budget=budget,
            is_current=is_current)
        text = model.output_text(result.get("output", []))
        record["turn"] = model.response_record(result, payload, elapsed)
        record["raw_output"] = text
        if result.get("status") != "completed" or not text.strip():
            raise ValueError("addressed findings response did not complete")
        record["findings"] = _parse_assessments(
            text, assessed, assessed_from_head, new_head)
        record.update(status="complete", old_head=assessed_from_head,
                      new_head=new_head)
    except model.StaleReview:
        record.update(status="stale", old_head=assessed_from_head,
                      new_head=new_head)
        raise
    except Exception as exc:
        record.update(status="complete", old_head=assessed_from_head,
                      new_head=new_head, error_type=type(exc).__name__)
        record["findings"] = _unclear(
            assessed, assessed_from_head, new_head,
            "Ralph could not make a reliable follow-up assessment.",
            type(exc).__name__)
    record["findings"].extend(
        _output_record(candidate, assessed_from_head, new_head, "unclear",
                       "Deferred because the follow-up candidate limit was reached.",
                       "candidate_limit")
        for candidate in overflow)
    return record
