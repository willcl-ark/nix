"""Assess whether previously published Ralph findings were addressed."""

import hashlib
import json
import re
import subprocess
import uuid
from datetime import datetime, timezone

from ralph import trace as core_trace
from ralph import repository
from ralph import model


BATCH_SIZE = 12
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
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "source_job_id",
                    "source_generation",
                    "finding_id",
                    "status",
                    "reason",
                    "evidence_ids",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["findings"],
    "additionalProperties": False,
}


def _now():
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _truncate(text, limit):
    text = str(text or "")
    data = text.encode()
    if len(data) <= limit:
        return text
    return data[:limit].decode(errors="replace") + "\n[Truncated]"


def _fingerprint(finding):
    text = "\n".join(str(finding.get(key) or "") for key in ("path", "title", "body"))
    normalized = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(normalized.encode()).hexdigest()


def _head(job):
    result = job.get("review_result") or {}
    return result.get("head_sha") or job.get("head")


def _base(job):
    result = job.get("review_result") or {}
    return result.get("base_sha") or job.get("base_ref")


def _published_findings(job):
    result = job.get("review_result") or {}
    exported = core_trace.findings_export(result)
    if exported is None:
        return None
    findings = []
    for item in exported:
        if not isinstance(item, dict):
            continue
        finding_id = item.get("id")
        if not isinstance(finding_id, str) or not finding_id:
            continue
        title = item.get("presented_title") or item.get("title")
        path = item.get("path")
        line = item.get("line") or 1
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
            "summary": item.get("presented_summary") or item.get("summary", ""),
            "body": item.get("presented_body") or item.get("body", ""),
            "source_head": _head(job) or "",
        }
        finding["fingerprint"] = _fingerprint(finding)
        findings.append(finding)
    return findings


def _source_findings(history):
    sources = {}
    for job in history:
        for finding in _published_findings(job) or []:
            sources[
                (
                    finding["source_job_id"],
                    finding["source_generation"],
                    finding["finding_id"],
                )
            ] = finding
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
    if status not in STATUSES or not all(
        isinstance(value, str) and value for value in (title, path, finding_id)
    ):
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
    for finding in _published_findings(job) or []:
        key = (
            finding["source_job_id"],
            finding["source_generation"],
            finding["finding_id"],
        )
        fingerprint = finding["fingerprint"]
        if (
            key in active
            or fingerprint in active_fingerprints
            or fingerprint in resolved_fingerprints
        ):
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
            active,
            resolved_fingerprints,
            _prior_assessment(prior_assessments, job["id"]),
            sources,
        )
        _add_published(active, resolved_fingerprints, job)
    candidates = list(active.values())
    return sorted(
        candidates,
        key=lambda finding: (
            finding["source_generation"],
            finding["source_job_id"],
            finding["finding_id"],
        ),
    )


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
        index
        for index, job in enumerate(history)
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
        source_ids.update(
            _assessment_source_ids(_prior_assessment(prior_assessments, job["id"]))
        )
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
    return [
        _output_record(
            candidate, assessed_from_head, new_head, "unclear", reason, evidence
        )
        for candidate in candidates
    ]


def _model_input(
    checkout,
    previous_job,
    current_job,
    candidates,
    assessed_from_head,
    new_head,
    *,
    submodules=None,
):
    diff = repository.diff_between_heads(checkout, assessed_from_head, new_head)
    registry = {}
    snapshots = {}
    finding_inputs = []
    for index, candidate in enumerate(candidates, 1):
        source_head = candidate.get("source_head") or assessed_from_head
        identity = {
            field: candidate[field]
            for field in ("source_job_id", "source_generation", "finding_id")
        }
        sources = []
        gaps = []

        source_available = repository.has_commit(checkout, source_head)
        if source_available:
            path_diff = repository.diff_path_between_heads(
                checkout, source_head, new_head, candidate["path"]
            )
            if path_diff.startswith("diff --git "):
                sources.append(
                    {
                        "kind": "path_diff",
                        "text": path_diff,
                        "truncated": path_diff.endswith("[Path diff truncated]"),
                    }
                )
        else:
            gaps.append(
                "Original finding commit is unavailable; the change cannot be compared."
            )
        for side, revision in (("old", source_head), ("new", new_head)):
            if side == "old" and not source_available:
                continue
            try:
                if revision not in snapshots:
                    snapshots[revision] = repository.snapshot_repository(
                        checkout, revision, revision, submodules=submodules
                    )
                snapshot = snapshots[revision]
                output = repository.cite_file(
                    snapshot, "head", candidate["path"], max(1, candidate["line"] - 30)
                )
                source = snapshot.citations[
                    output.splitlines()[0].removeprefix("Citation: ")
                ]
            except (
                repository.ToolFailure,
                ValueError,
                OSError,
                subprocess.SubprocessError,
            ):
                gaps.append(f"{side} source is unavailable at the finding location.")
                continue
            sources.append(
                {
                    "kind": f"{side}_source",
                    "text": source["excerpt"],
                    "revision": revision,
                    "line": source["line"],
                    "end_line": source["end_line"],
                    "bounded": True,
                }
            )
        for source in sources:
            source.update(
                evidence_id=uuid.uuid4().hex,
                **identity,
                path=candidate["path"],
                old_head=source_head,
                new_head=new_head,
            )
            registry[source["evidence_id"]] = source
        finding_inputs.append(
            {
                "index": index,
                **identity,
                "title": candidate["title"],
                "path": candidate["path"],
                "source_head": source_head,
                "summary": candidate.get("summary", ""),
                "body": candidate.get("body", ""),
                "evidence": sources,
                "gaps": gaps,
            }
        )
    previous_result = previous_job.get("review_result") or {}
    return json.dumps(
        {
            "previous_job_id": previous_job["id"],
            "previous_generation": previous_job["generation"],
            "old_head": assessed_from_head,
            "new_head": new_head,
            "old_base": _base(previous_job),
            "new_base": _base(current_job),
            "previous_review_content": _truncate(
                previous_result.get("content", ""), MAX_CONTENT_BYTES
            ),
            "old_to_new_diff": diff,
            "findings": finding_inputs,
        },
        sort_keys=True,
    ), registry


def _assessment_record(row, candidate, assessed_from_head, new_head, registry):
    if set(row) != set(SCHEMA["properties"]["findings"]["items"]["properties"]):
        raise ValueError("invalid addressed finding row")
    key = _identity(candidate)
    status = row["status"]
    reason = row["reason"]
    evidence_ids = row["evidence_ids"]
    if status not in STATUSES:
        raise ValueError("unexpected addressed finding status")
    if not isinstance(reason, str) or not isinstance(evidence_ids, list):
        raise ValueError("addressed finding requires reason and evidence IDs")
    sources = []
    for identifier in evidence_ids:
        if not isinstance(identifier, str) or identifier not in registry:
            raise ValueError("unknown follow-up evidence ID")
        source = registry[identifier]
        if (
            _identity(source) != key
            or source["new_head"] != new_head
            or source["old_head"]
            != (candidate.get("source_head") or assessed_from_head)
        ):
            raise ValueError(
                "follow-up evidence belongs to another finding or comparison"
            )
        sources.append(dict(source))
    if status != "unclear" and (
        not reason.strip()
        or not any(source["kind"] in {"new_source", "path_diff"} for source in sources)
    ):
        raise ValueError("definitive assessments require current evidence and a reason")
    if status in {"addressed", "partially_addressed"} and not any(
        source["kind"] in {"old_source", "path_diff"} for source in sources
    ):
        raise ValueError("addressed findings require original comparison evidence")
    record = _output_record(
        candidate,
        assessed_from_head,
        new_head,
        status,
        reason,
        "; ".join(f"{source['kind']}: {source['path']}" for source in sources),
    )
    record["source_evidence"] = sources
    return record


def _identity(row):
    return tuple(
        row.get(field) for field in ("source_job_id", "source_generation", "finding_id")
    )


def _parse_assessments(text, candidates, assessed_from_head, new_head, registry):
    result = json.loads(text)
    if (
        not isinstance(result, dict)
        or set(result) != {"findings"}
        or not isinstance(result["findings"], list)
    ):
        raise ValueError("invalid addressed findings response")
    rows = [row for row in result["findings"] if isinstance(row, dict)]
    output = []
    for candidate in candidates:
        key = _identity(candidate)
        matches = [row for row in rows if _identity(row) == key]
        # One invalid row must not erase separately valid assessments.
        try:
            if len(matches) != 1:
                raise ValueError(f"expected one assessment, received {len(matches)}")
            record = _assessment_record(
                matches[0], candidate, assessed_from_head, new_head, registry
            )
        except ValueError as exc:
            [record] = _unclear(
                [candidate],
                assessed_from_head,
                new_head,
                f"The follow-up assessment was invalid: {exc}.",
                "invalid_assessment",
            )
        output.append(record)
    return output


def assess_completed_review(
    api_key,
    checkout,
    current_job,
    history,
    prior_assessments,
    prompt,
    stage_model,
    usage,
    is_current=None,
    *,
    api_base="https://api.openai.com/v1",
    submodules=None,
):
    record = {"assessed_at": _now(), "model": stage_model, "findings": []}
    if not history:
        record.update(status="skipped", reason="no_previous_review")
        return record
    previous = history[0]
    assessed_from_head = _head(previous)
    new_head = _head(current_job)
    if assessed_from_head == new_head:
        record.update(
            status="skipped",
            reason="same_head",
            old_head=assessed_from_head,
            new_head=new_head,
        )
        return record
    if _published_findings(history[0]) is None:
        record.update(
            status="skipped",
            reason="findings_export_unsupported",
            old_head=assessed_from_head,
            new_head=new_head,
        )
        return record
    candidates = _candidate_findings(history, prior_assessments)
    if not candidates:
        record.update(
            status="skipped",
            reason="no_previous_findings",
            old_head=assessed_from_head,
            new_head=new_head,
        )
        return record
    if not repository.has_commit(
        checkout, assessed_from_head
    ) or not repository.has_commit(checkout, new_head):
        record.update(
            status="complete",
            old_head=assessed_from_head,
            new_head=new_head,
            reason="missing_git_object",
        )
        record["findings"] = _unclear(
            candidates,
            assessed_from_head,
            new_head,
            "Old or new head object is unavailable.",
            "Ralph cannot compare the old and new heads in the local checkout.",
        )
        return record
    record.update(
        status="complete", old_head=assessed_from_head, new_head=new_head, batches=[]
    )
    for start in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[start : start + BATCH_SIZE]
        batch_record = {}
        record["batches"].append(batch_record)
        try:
            input_text, evidence_registry = _model_input(
                checkout,
                previous,
                current_job,
                batch,
                assessed_from_head,
                new_head,
                submodules=submodules,
            )
            data = {
                "model": stage_model,
                "store": False,
                "reasoning": {"effort": "low"},
                "instructions": prompt,
                "input": [{"role": "user", "content": input_text}],
                "max_output_tokens": MAX_OUTPUT_TOKENS,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "addressed_findings",
                        "strict": True,
                        "schema": SCHEMA,
                    }
                },
            }
            result, payload, elapsed = model.request_response(
                api_key,
                data,
                "addressed_findings",
                usage=usage,
                is_current=is_current,
                api_base=api_base,
            )
            text = model.output_text(result.get("output", []))
            batch_record["turn"] = model.response_record(result, payload, elapsed)
            batch_record["raw_output"] = text
            if result.get("status") != "completed" or not text.strip():
                raise ValueError("addressed findings response did not complete")
            record["findings"].extend(
                _parse_assessments(
                    text, batch, assessed_from_head, new_head, evidence_registry
                )
            )
        except model.StaleReview:
            raise
        except Exception as exc:
            batch_record["error_type"] = type(exc).__name__
            if isinstance(exc, ValueError):
                batch_record["error"] = str(exc)
            record["findings"].extend(
                _unclear(
                    batch,
                    assessed_from_head,
                    new_head,
                    "Ralph could not make a reliable follow-up assessment.",
                    type(exc).__name__,
                )
            )
    return record
