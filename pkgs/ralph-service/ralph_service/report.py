"""Static public review reports."""

import html
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import nh3
from markdown_it import MarkdownIt

from ralph import trace as core_trace


REPORT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
ATTRIBUTION_FIELDS = (
    "finding_id",
    "title",
    "path",
    "line",
    "side",
    "candidate_ids",
    "raised_by",
    "raised_by_models",
    "verified_by",
    "verified_by_model",
    "edited_by",
    "edited_by_model",
)
FINDING_FIELDS = (
    "kind",
    "severity",
    "path",
    "line",
    "side",
    "title",
    "summary",
    "body",
)
CANDIDATE_FIELDS = (
    "kind",
    "path",
    "line",
    "side",
    "title",
    "claim",
    "consequence",
    "evidence",
    "correction",
    "uncertainty",
)
TOOL_FIELDS = (
    "name",
    "output_bytes",
    "output_sha256",
    "skipped",
    "hosted",
    "action_type",
    "retrieved_at",
    "frozen",
)
COVERAGE_STATUSES = {"complete", "partial", "failed", "unknown", "not_requested"}
COVERAGE_CATEGORIES = {"inspection", "context", "verification", "experiment"}
CONCEPT_FIELDS = (
    "status",
    "stage",
    "model",
    "problem",
    "goal",
    "baseline",
    "assessment",
    "proposed_review",
    "review_reason",
    "delivered_benefit",
    "relevant_history",
    "recommendation",
    "decisive_question",
    "technical_assumptions",
)
CONCEPT_ALTERNATIVE_FIELDS = (
    "name",
    "concept",
    "benefit",
    "cost",
    "unresolved",
    "provenance",
)
CONCEPT_CITATION_FIELDS = ("title", "url", "description")


def _scalar(value):
    return (
        value if value is None or isinstance(value, (str, int, float, bool)) else None
    )


def _filtered(record, fields):
    if not isinstance(record, dict):
        return {}
    return {key: record[key] for key in fields if _scalar(record.get(key)) is not None}


def _strings(value):
    return (
        [item for item in value if isinstance(item, str)]
        if isinstance(value, list)
        else []
    )


def _coverage_status(coverage):
    status = coverage.get("status") if isinstance(coverage, dict) else None
    return status if status in COVERAGE_STATUSES else "unknown"


def _text_or_strings(value):
    if isinstance(value, str):
        return value
    strings = _strings(value)
    return strings if strings else None


def _safe_http_url(url):
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not parsed.hostname
    ):
        return None
    if parsed.username or parsed.password:
        return None
    return url


def _public_coverage(coverage):
    if not isinstance(coverage, dict):
        return None
    record = {
        "status": _coverage_status(coverage),
        "limitations": _strings(coverage.get("limitations")),
    }
    for key in ("context", "verification", "experiments"):
        child = coverage.get(key)
        if isinstance(child, dict):
            record[key] = {
                "status": _coverage_status(child),
                "limitations": _strings(child.get("limitations")),
            }
    issues = []
    for issue in coverage.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        public = {}
        if isinstance(issue.get("stage"), str):
            public["stage"] = issue["stage"]
        category = issue.get("category")
        if category in COVERAGE_CATEGORIES:
            public["category"] = category
        if isinstance(issue.get("reason"), str):
            public["reason"] = issue["reason"]
        if public:
            issues.append(public)
    if issues:
        record["issues"] = issues
    notes = _strings(coverage.get("notes"))
    if notes:
        record["notes"] = notes
    return record


def _unknown_coverage():
    return {"status": "unknown", "limitations": []}


def _public_validation_errors(stage):
    errors = []
    for item in stage.get("validation_errors", []):
        if not isinstance(item, dict):
            continue
        record = {"candidate_ids": _strings(item.get("candidate_ids"))}
        if isinstance(item.get("error"), str):
            record["error"] = item["error"]
        errors.append(record)
    return errors


def _public_tools(stage):
    tools = []
    for tool in stage.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        record = _filtered(tool, TOOL_FIELDS)
        sources = []
        for source in tool.get("sources") or []:
            if isinstance(source, str):
                url = _safe_http_url(source)
                if url is not None:
                    sources.append(url)
            elif isinstance(source, dict):
                item = {
                    key: source[key]
                    for key in ("type", "title")
                    if isinstance(source.get(key), str)
                }
                url = _safe_http_url(source.get("url"))
                if url is not None:
                    item["url"] = url
                if item:
                    sources.append(item)
        if sources:
            record["sources"] = sources
        if record:
            tools.append(record)
    return tools


def public_limits(stage):
    """Export only numeric limit telemetry, never arbitrary debug fields."""
    public = {}
    limits = stage.get("limits")
    if not isinstance(limits, dict):
        return public
    for name in (
        "output_tokens",
        "tool_calls",
        "context_calls",
        "history_calls",
        "web_search_calls_per_response",
    ):
        value = limits.get(name)
        if not isinstance(value, dict):
            continue
        cap = value.get("limit")
        if type(cap) is not int or cap < 0:
            continue
        used = value.get("used")
        public[name] = {
            "limit": cap,
            "used": used if type(used) is int and used >= 0 else None,
            "hit": value.get("hit") if type(value.get("hit")) is bool else None,
        }
    return public


def _public_stages(debug):
    public = {}
    for name, stage in sorted((debug.get("stages") or {}).items()):
        if not isinstance(stage, dict):
            continue
        record = {}
        for key in (
            "model",
            "status",
            "reason",
            "error",
            "error_type",
            "error_message",
            "validation_error",
            "incomplete_reason",
        ):
            if isinstance(stage.get(key), str):
                record[key] = stage[key]
        if type(stage.get("http_status")) is int:
            record["http_status"] = stage["http_status"]
        if isinstance(stage.get("used_verified_wording"), bool):
            record["used_verified_wording"] = stage["used_verified_wording"]
        if "limits" in stage:
            record["limits"] = public_limits(stage)
        profiles = _strings(stage.get("profiles"))
        if profiles:
            record["profiles"] = profiles
        coverage = _public_coverage(stage.get("coverage"))
        if coverage:
            record["coverage"] = coverage
        validation_errors = _public_validation_errors(stage)
        if validation_errors:
            record["validation_errors"] = validation_errors
        tools = _public_tools(stage)
        if tools:
            record["tools"] = tools
        raw_output = stage.get("raw_output")
        if isinstance(raw_output, str):
            record["raw_output"] = raw_output
        if name == "write_verifier":
            record["results"] = [
                {
                    **_filtered(item, ("finding_id", "outcome", "reason")),
                    "command_ids": _strings(item.get("command_ids")),
                }
                for item in stage.get("results", [])
                if isinstance(item, dict)
            ]
            execution = stage.get("execution")
            if isinstance(execution, dict):
                record["execution"] = {
                    **_filtered(
                        execution,
                        (
                            "head_sha",
                            "diff_base_sha",
                            "environment",
                            "fresh_workspace_per_command",
                        ),
                    ),
                    "errors": _strings(execution.get("errors")),
                    "commands": [
                        _filtered(
                            command,
                            (
                                "id",
                                "finding_id",
                                "side",
                                "revision",
                                "command",
                                "status",
                                "exit_code",
                                "output",
                                "patch",
                                "output_truncated",
                                "patch_truncated",
                                "capture_complete",
                                "error",
                            ),
                        )
                        for command in execution.get("commands", [])
                        if isinstance(command, dict)
                    ],
                }
        public[str(name)] = record
    return public


def _public_stage_outputs(debug):
    outputs = debug.get("stage_outputs") or {}
    return {
        str(name): output
        for name, output in sorted(outputs.items())
        if isinstance(output, str)
    }


def _public_attribution(debug):
    attribution = []
    for finding in core_trace.finding_attribution(debug):
        if not isinstance(finding, dict):
            continue
        record = _filtered(finding, ATTRIBUTION_FIELDS)
        for key in ("candidate_ids", "raised_by", "raised_by_models"):
            if key in finding:
                record[key] = _strings(finding[key])
        attribution.append(record)
    return attribution


def _public_finding(finding):
    if not isinstance(finding, dict):
        return None
    record = _filtered(finding, FINDING_FIELDS)
    if isinstance(finding.get("security"), dict):
        record["security"] = _filtered(
            finding["security"], ("classification", "reason")
        )
    return record


def _public_decisions(debug):
    decisions = []
    for decision in debug.get("decisions", []):
        if not isinstance(decision, dict):
            continue
        record = {}
        record["candidate_ids"] = _strings(decision.get("candidate_ids"))
        for key in ("disposition", "reason"):
            if isinstance(decision.get(key), str):
                record[key] = decision[key]
        record["finding"] = _public_finding(decision.get("finding"))
        decisions.append(record)
    return decisions


def _public_concept_citation(citation):
    if isinstance(citation, str):
        record = {"title": citation}
        url = _safe_http_url(citation)
        if url is not None:
            record["url"] = url
        return record
    if not isinstance(citation, dict):
        return None
    record = _filtered(citation, CONCEPT_CITATION_FIELDS)
    url = _safe_http_url(record.get("url"))
    if url is None:
        record.pop("url", None)
    else:
        record["url"] = url
    return record if record else None


def _public_concept_assessment(debug):
    assessment = core_trace.concept_assessment(debug)
    if not assessment:
        return None
    record = {}
    for key in ("status", "stage", "summary", "edited_by", "reason", "error_type"):
        if isinstance(assessment.get(key), str):
            record[key] = assessment[key]
    if isinstance(assessment.get("used_verified_wording"), bool):
        record["used_verified_wording"] = assessment["used_verified_wording"]
    coverage = _public_coverage(assessment.get("coverage"))
    if coverage:
        record["coverage"] = coverage
    candidate = _public_concept_brief(assessment.get("candidate"))
    if candidate:
        record["candidate"] = candidate
    verification = assessment.get("verification")
    verified = None
    if isinstance(verification, dict):
        public_verification = {}
        for key in ("disposition", "reason", "proposed_review", "review_reason"):
            if isinstance(verification.get(key), str):
                public_verification[key] = verification[key]
        verified = _public_concept_brief(verification.get("assessment"))
        if verified:
            public_verification["assessment"] = verified
        if public_verification:
            record["verification"] = public_verification
    if verified:
        record.update(verified)
    elif any(key in assessment for key in ("problem", "alternatives", "citations")):
        brief = _public_concept_brief(assessment)
        if brief:
            record.update(brief)
    return record or None


def _public_concept_brief(assessment):
    if not isinstance(assessment, dict):
        return None
    record = {}
    for key in CONCEPT_FIELDS:
        value = _text_or_strings(assessment.get(key))
        if value is not None:
            record[key] = value
    alternatives = []
    for alternative in assessment.get("alternatives") or []:
        if not isinstance(alternative, dict):
            continue
        public = {}
        for key in CONCEPT_ALTERNATIVE_FIELDS:
            value = _text_or_strings(alternative.get(key))
            if value is not None:
                public[key] = value
        citations = []
        for citation in alternative.get("citations") or []:
            public_citation = _public_concept_citation(citation)
            if public_citation:
                citations.append(public_citation)
        if citations:
            public["citations"] = citations
        if public:
            alternatives.append(public)
    if alternatives:
        record["alternatives"] = alternatives
    citations = []
    for citation in assessment.get("citations") or []:
        public = _public_concept_citation(citation)
        if public:
            citations.append(public)
    if citations:
        record["citations"] = citations
    return record or None


def _public_record(number, result, report_id):
    debug = core_trace.current_diagnostics(result)
    engine = core_trace.result_engine(result)
    findings = core_trace.findings_export(result)
    metrics = core_trace.result_metrics(result)
    record = {
        "report_id": report_id,
        "status": (result.get("run") or {}).get("status", "unknown"),
        "pr": number,
        "head": result.get("head_sha"),
        "review": result.get("content", ""),
        "engine": {
            key: engine.get(key) for key in ("name", "code_sha256", "config_sha256")
        },
        "findings_export_status": "supported"
        if findings is not None
        else "unsupported",
        "findings": findings,
        "review_memory": core_trace.review_memory_export(result),
        "actionable": (
            (result.get("output", {}).get("exports", {}) or {}).get("actionable")
            if isinstance(result.get("output"), dict)
            else None
        ),
        "metrics": metrics,
        "usage": (
            result.get("run", {}).get("usage")
            if isinstance(result.get("run"), dict)
            else None
        ),
        "notice": (
            "The review text is the engine's published assessment. Diagnostic "
            "records describe model activity and do not establish correctness."
        ),
    }
    exports = (result.get("output") or {}).get("exports") or {}
    assessment = exports.get("backport_assessment")
    if isinstance(assessment, dict):
        record["backport_assessment"] = _filtered(
            assessment,
            ("verdict", "summary", "evidence", "required_fixes", "limitations"),
        )
    if debug:
        config = engine.get("config")
        profile = config.get("profile") if isinstance(config, dict) else None
        stages = profile.get("stages") if isinstance(profile, dict) else None
        record["profile"] = profile.get("name") if isinstance(profile, dict) else None
        record["models"] = (
            {
                name: settings.get("model")
                for name, settings in stages.items()
                if isinstance(settings, dict)
            }
            if isinstance(stages, dict)
            else None
        )
        record["metrics"] = core_trace.review_metrics(debug)
        record["metrics"].update(metrics or {})
        record["coverage"] = (
            _public_coverage(debug.get("coverage")) or _unknown_coverage()
        )
        record["stages"] = _public_stages(debug)
        record["stage_outputs"] = _public_stage_outputs(debug)
        record["stage_metrics"] = core_trace.stage_metrics(debug)
        record["concept_assessment"] = _public_concept_assessment(debug)
        record["finding_attribution"] = _public_attribution(debug)
        record["decisions"] = _public_decisions(debug)
    return record


def _escape(value):
    return html.escape("" if value is None else str(value))


def _summary_list(items):
    return "".join(f"<li>{_escape(item)}</li>" for item in items)


def _coverage_html(coverage, detailed=False):
    if not isinstance(coverage, dict):
        return "<p>No coverage summary was recorded.</p>"
    limitations = coverage.get("limitations") or []
    status = coverage.get("status", "unknown")
    if not detailed and not any(
        key in coverage for key in ("context", "verification", "issues", "notes")
    ):
        if limitations:
            return (
                f"<p>Status: <strong>{_escape(status)}</strong></p>"
                f"<ul>{_summary_list(limitations)}</ul>"
            )
        return f"<p>Status: <strong>{_escape(status)}</strong>. No limitations recorded.</p>"
    parts = [f"<p>Code review status: <strong>{_escape(status)}</strong></p>"]
    if limitations:
        parts.append(f"<ul>{_summary_list(limitations)}</ul>")
    else:
        parts.append("<p>No code review limitations recorded.</p>")
    for key, label in (
        ("context", "Historical context"),
        ("verification", "Verification"),
        ("experiments", "Optional experiments"),
    ):
        if key == "experiments" and key not in coverage:
            continue
        child = coverage.get(key) if isinstance(coverage.get(key), dict) else {}
        child_status = child.get("status", "unknown")
        child_limits = child.get("limitations") or []
        parts.append(f"<p>{label} status: <strong>{_escape(child_status)}</strong></p>")
        if child_limits:
            parts.append(f"<ul>{_summary_list(child_limits)}</ul>")
        else:
            parts.append(f"<p>No {label.lower()} limitations recorded.</p>")
    notes = coverage.get("notes") or []
    if notes:
        parts.append("<p>Scope notes:</p>")
        parts.append(f"<ul>{_summary_list(notes)}</ul>")
    return "".join(parts)


def _findings_html(attribution):
    if not attribution:
        return "<p>No verified finding attribution was recorded.</p>"
    rows = []
    for finding in attribution:
        location = ""
        if finding.get("path") and finding.get("line") and finding.get("side"):
            location = f"{finding['path']}:{finding['line']} ({finding['side']})"
        rows.append(
            "<tr>"
            f"<td>{_escape(finding.get('finding_id'))}</td>"
            f"<td>{_escape(finding.get('title'))}</td>"
            f"<td>{_escape(location)}</td>"
            f"<td>{_escape(', '.join(finding.get('raised_by') or []))}</td>"
            f"<td>{_escape(', '.join(finding.get('candidate_ids') or []))}</td>"
            f"<td>{_escape(finding.get('verified_by'))}</td>"
            f"<td>{_escape(finding.get('edited_by'))}</td>"
            "</tr>"
        )
    return (
        "<table>"
        "<thead><tr><th>ID</th><th>Finding</th><th>Location</th>"
        "<th>Raised by</th><th>Candidates</th><th>Verified</th><th>Edited</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def _json_html(value):
    return f"<pre>{_escape(json.dumps(value, ensure_ascii=False, indent=2))}</pre>"


def _paragraphs(record, keys):
    paragraphs = []
    for key, label in keys:
        value = record.get(key)
        if isinstance(value, str) and value:
            paragraphs.append(
                f"<p><strong>{_escape(label)}:</strong> {_escape(value)}</p>"
            )
    return "".join(paragraphs)


def _concept_value_html(value):
    if isinstance(value, list):
        return "<ul>" + "".join(f"<li>{_escape(item)}</li>" for item in value) + "</ul>"
    return _escape(value)


def _concept_assessment_html(assessment):
    if not isinstance(assessment, dict) or not assessment:
        return "<p>No conceptual concern was recorded.</p>"
    verification = (
        assessment.get("verification")
        if isinstance(assessment.get("verification"), dict)
        else {}
    )
    brief = verification.get("assessment")
    if not isinstance(brief, dict):
        brief = (
            assessment.get("candidate")
            if isinstance(assessment.get("candidate"), dict)
            else {}
        )
    body = _paragraphs(
        assessment,
        (
            ("status", "Status"),
            ("summary", "Published summary"),
        ),
    )
    candidate = assessment.get("candidate") or {}
    if candidate.get("proposed_review") or verification.get("proposed_review"):
        body += (
            "<p>Review recommendations are advisory and do not stop code review.</p>"
        )
        body += _paragraphs(
            candidate,
            (
                ("assessment", "Initial concept assessment"),
                ("proposed_review", "Initial review recommendation"),
                ("review_reason", "Initial review reason"),
            ),
        )
    body += _paragraphs(
        verification,
        (
            ("disposition", "Verifier disposition"),
            ("reason", "Verifier reason"),
            ("proposed_review", "Verified review recommendation"),
            ("review_reason", "Verified review reason"),
        ),
    )
    body += _paragraphs(
        brief,
        (
            ("recommendation", "Recommendation"),
            ("problem", "Problem"),
            ("goal", "Higher-level goal"),
            ("baseline", "Baseline"),
            ("delivered_benefit", "Delivered benefit"),
            ("relevant_history", "Relevant history"),
            ("decisive_question", "Question that could change this"),
            ("technical_assumptions", "Technical assumptions"),
        ),
    )
    alternatives = (
        brief.get("alternatives") if isinstance(brief.get("alternatives"), list) else []
    )
    if alternatives:
        rows = []
        for alternative in alternatives:
            if not isinstance(alternative, dict):
                continue
            details = []
            for key, label in (
                ("concept", "Concept"),
                ("benefit", "Benefit"),
                ("cost", "Cost"),
                ("unresolved", "Unresolved"),
            ):
                if alternative.get(key):
                    details.append(
                        f"<p><strong>{_escape(label)}:</strong> "
                        f"{_concept_value_html(alternative[key])}</p>"
                    )
            rows.append(
                "<tr>"
                f"<td>{_escape(alternative.get('name'))}</td>"
                f"<td>{_escape(alternative.get('provenance'))}</td>"
                f"<td>{''.join(details)}</td>"
                "</tr>"
            )
        if rows:
            body += (
                "<h3>Alternatives</h3><table><thead><tr><th>Concept</th>"
                "<th>Provenance</th><th>Assessment</th></tr></thead>"
                f"<tbody>{''.join(rows)}</tbody></table>"
            )
    sources = brief.get("citations") if isinstance(brief.get("citations"), list) else []
    links = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        title = source.get("title") or source.get("url") or source.get("description")
        if not title:
            continue
        if source.get("url"):
            label = f'<a href="{_escape(source["url"])}">{_escape(title)}</a>'
        else:
            label = _escape(title)
        description = source.get("description")
        links.append(
            f"<li>{label}{': ' + _escape(description) if description else ''}</li>"
        )
    if links:
        body += f"<h3>Sources</h3><ul>{''.join(links)}</ul>"
    return body or "<p>No conceptual concern was recorded.</p>"


def _reply_json(text):
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None


def _finding_detail(finding, index):
    if not isinstance(finding, dict):
        return ""
    title = finding.get("title") or f"Finding {index}"
    location = ""
    if finding.get("path") and finding.get("line") and finding.get("side"):
        location = f" {finding['path']}:{finding['line']} ({finding['side']})"
    rows = []
    for key in (
        "claim",
        "consequence",
        "evidence",
        "correction",
        "uncertainty",
        "summary",
        "body",
    ):
        if finding.get(key):
            rows.append(
                f"<p><strong>{_escape(key.title())}:</strong> {_escape(finding[key])}</p>"
            )
    return (
        f"<details><summary>{_escape(title)}{_escape(location)}</summary>"
        f"{''.join(rows)}</details>"
    )


def _parsed_findings_html(parsed):
    if not isinstance(parsed, dict) or not isinstance(parsed.get("findings"), list):
        return ""
    findings = [
        _filtered(finding, CANDIDATE_FIELDS + FINDING_FIELDS)
        for finding in parsed["findings"]
    ]
    details = "".join(
        _finding_detail(finding, index)
        for index, finding in enumerate(findings, 1)
        if finding
    )
    return f"<h4>Parsed Findings</h4>{details}" if details else ""


def _reply_html(text):
    parsed = _reply_json(text)
    if parsed is None:
        return f"<pre>{_escape(text)}</pre>"
    return (
        f"{_parsed_findings_html(parsed)}"
        f"<h4>Formatted JSON</h4>{_json_html(parsed)}"
        f"<details><summary>Raw full reply</summary><pre>{_escape(text)}</pre></details>"
    )


def _decisions_html(decisions):
    if not decisions:
        return "<p>No verifier decisions were recorded.</p>"
    sections = []
    for index, decision in enumerate(decisions, 1):
        finding = decision.get("finding") or {}
        finding_html = ""
        if finding:
            location = ""
            if finding.get("path") and finding.get("line") and finding.get("side"):
                location = f"{finding['path']}:{finding['line']} ({finding['side']})"
            finding_html = (
                f"<p><strong>{_escape(finding.get('title'))}</strong>"
                f" {_escape(location)}</p>"
                f"<p>{_escape(finding.get('summary'))}</p>"
                f"<pre>{_escape(finding.get('body'))}</pre>"
            )
        sections.append(
            f"<details><summary>Decision {index}: "
            f"{_escape(decision.get('disposition'))}</summary>"
            f"<p>Candidates: {_escape(', '.join(decision.get('candidate_ids') or []))}</p>"
            f"<p>{_escape(decision.get('reason'))}</p>"
            f"{finding_html}</details>"
        )
    return "".join(sections)


def _stage_html(name, stage, stage_output):
    title = f"{name}: {stage.get('status', 'unknown')}"
    if stage.get("model"):
        title += f" ({stage['model']})"
    raw_output = stage.get("raw_output")
    output_blocks = []
    if raw_output is not None:
        output_blocks.append(("<h4>Raw Output</h4>", raw_output))
    if stage_output is not None and stage_output != raw_output:
        output_blocks.append(("<h4>Stage Output</h4>", stage_output))
    outputs = "".join(
        f"{heading}{_reply_html(text)}" for heading, text in output_blocks
    )
    coverage = (
        _coverage_html(stage["coverage"])
        if isinstance(stage.get("coverage"), dict)
        else ""
    )
    error = _paragraphs(stage, (("error_message", "Error"),))
    if "http_status" in stage:
        error = f"<p>HTTP status: {_escape(stage['http_status'])}</p>" + error
    experiments = ""
    if name == "write_verifier":
        experiments = (
            "<h4>Experiment evidence</h4>"
            "<p>Experiments inform the final Major check. Inconclusive results "
            "do not prevent publication based on source evidence.</p>"
            + _json_html(
                {
                    "results": stage.get("results", []),
                    "execution": stage.get("execution", {}),
                }
            )
        )
    return (
        f"<details><summary>{_escape(title)}</summary>"
        f"{error}"
        f"{coverage}"
        f"{outputs}"
        f"{experiments}"
        f"<details><summary>Stage metadata</summary>{_json_html({key: value for key, value in stage.items() if key not in {'raw_output', 'execution', 'results'}})}</details>"
        "</details>"
    )


def _html_report(record, json_name):
    status = record.get("status", "unknown")
    outcome = (
        '<p class="notice"><strong>Review failed.</strong> '
        "Verified findings and review history may still be present.</p>"
        if status == "failed"
        else f"<p>Review status: <strong>{_escape(status)}</strong></p>"
    )
    profile = (
        f"<p>Review profile: {_escape(record['profile'])}</p>"
        if record.get("profile")
        else ""
    )
    stages = record.get("stages", {})
    stage_outputs = record.get("stage_outputs", {})
    stage_sections = "".join(
        _stage_html(name, stage, stage_outputs.get(name))
        for name, stage in stages.items()
    )
    orphan_outputs = "".join(
        f"<details><summary>{_escape(name)}: stage output</summary>"
        f"<pre>{_escape(output)}</pre></details>"
        for name, output in stage_outputs.items()
        if name not in stages
    )
    if "stages" in record:
        engine_sections = f"""
<section><h2>Concept and Approach</h2>
{_concept_assessment_html(record.get("concept_assessment"))}</section>
<section><h2>Coverage</h2>
{_coverage_html(record.get("coverage"), detailed=True)}</section>
<section><h2>Findings</h2>
{_findings_html(record.get("finding_attribution") or [])}</section>
<section><h2>Verifier Decisions</h2>
{_decisions_html(record.get("decisions") or [])}</section>
<section><h2>Agent Outputs</h2>{stage_sections}{orphan_outputs}</section>
"""
    else:
        engine_sections = f"<section><h2>Findings export</h2>{_export_findings_html(record)}</section>"
    if record.get("backport_assessment"):
        engine_sections = (
            "<section><h2>Backport assessment</h2>"
            + _json_html(record["backport_assessment"])
            + "</section>"
            + engine_sections
        )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Review report PR #{_escape(record["pr"])}</title>
<style>
body {{ margin: 2rem auto; max-width: 76rem; padding: 0 1rem; color: #1f2933; font: 16px/1.5 system-ui, sans-serif; overflow-wrap: anywhere; }}
a {{ color: #075985; }}
.notice {{ border-left: 4px solid #d97706; background: #fff7ed; padding: 0.75rem 1rem; }}
pre {{ background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px; overflow-x: auto; padding: 1rem; white-space: pre-wrap; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #d0d7de; padding: 0.4rem 0.5rem; text-align: left; vertical-align: top; }}
th {{ background: #f6f8fa; }}
details {{ border-top: 1px solid #d0d7de; padding: 0.75rem 0; }}
summary {{ cursor: pointer; font-weight: 600; }}
</style>
</head>
<body>
<header>
<h1>Forgejo Review Report</h1>
{outcome}
<p>PR #{_escape(record["pr"])} at <code>{_escape(record["head"])}</code></p>
{profile}
<p><a href="{_escape(json_name)}" download>Download JSON report</a></p>
<p><a href="stats/">Review statistics</a></p>
</header>
<p class="notice">{_escape(record["notice"])}</p>
<section>
<h2>Review</h2>
{
        nh3.clean(
            MarkdownIt("commonmark")
            .enable(["table", "strikethrough"])
            .render(record["review"])
        )
    }
</section>
{engine_sections}
<section>
<h2>Usage</h2>
{
        _json_html(
            {
                "metrics": record["metrics"],
                "stage_metrics": record.get("stage_metrics"),
                "usage": record.get("usage"),
            }
        )
    }
</section>
</body>
</html>
"""


def _export_findings_html(record):
    findings = record.get("findings")
    if findings is None:
        return "<p>This engine does not provide a findings export.</p>"
    if not findings:
        return "<p>The supported findings export is empty.</p>"
    rows = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        location = finding.get("path", "")
        if finding.get("line") is not None:
            location = f"{location}:{finding['line']}"
        rows.append(
            "<tr>"
            f"<td>{_escape(finding.get('id', ''))}</td>"
            f"<td>{_escape(finding.get('severity', 'unknown'))}</td>"
            f"<td>{_escape(finding.get('kind', 'unknown'))}</td>"
            f"<td>{_escape(finding.get('presented_title') or finding.get('title', ''))}</td>"
            f"<td><code>{_escape(location)}</code></td>"
            f"<td>{_escape(finding.get('presented_summary') or finding.get('summary', ''))}</td>"
            f"<td>{_escape(finding.get('presented_body') or finding.get('body', ''))}</td>"
            "</tr>"
        )
    if not rows:
        return "<p>The supported findings export contained no readable items.</p>"
    return (
        "<table><thead><tr><th>ID</th><th>Severity</th><th>Kind</th>"
        "<th>Title</th><th>Location</th><th>Summary</th><th>Details</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _write_public_file(path, content):
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    data = content.encode("utf-8")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fchmod(file.fileno(), 0o644)
            os.fsync(file.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def save_report(report_dir, number, result, report_id):
    """Write a static HTML report and matching public JSON, returning the HTML name."""
    if not isinstance(report_id, str) or not REPORT_ID.fullmatch(report_id):
        raise ValueError("report_id must be a safe filename stem")
    report_dir = Path(report_dir)
    report_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    html_name = f"{report_id}.html"
    json_name = f"{report_id}.json"
    record = _public_record(number, result, report_id)
    json_text = json.dumps(record, ensure_ascii=False, indent=2)
    _write_public_file(report_dir / json_name, json_text + "\n")
    _write_public_file(report_dir / html_name, _html_report(record, json_name))
    return html_name
