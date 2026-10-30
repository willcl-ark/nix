"""Render verified review results with contributor-editable Markdown templates."""

import re
from collections import Counter
from html import escape
from pathlib import Path
from string import Template
from urllib.parse import quote, urlsplit

_PLACEHOLDERS = {
    "comment": (
        "verification_failed",
        "overview",
        "backport",
        "findings",
        "design",
        "alternatives",
        "history",
        "empty",
        "limitations",
    ),
    "backport": ("verdict", "summary", "details"),
    "findings": ("findings",),
    "finding": ("severity", "title", "summary", "location", "side", "body"),
    "brief_finding": ("severity", "title", "summary", "location", "side"),
    "retained": ("label", "summary", "location", "side", "body", "provenance"),
    "provenance": ("provenance",),
    "resolved": ("history",),
    "design": ("design",),
    "alternatives": ("alternatives",),
    "history": ("label", "history"),
    "history_failed": ("history",),
    "limitations": ("coverage_notice", "limitations"),
    "github": ("new_findings_notice", "retained_notice", "findings", "report_url"),
}


def severity_label(severity):
    symbol = {"critical": "⛔", "major": "🔴", "minor": "🟠", "suggestion": "🔵"}[
        severity
    ]
    return f"{symbol} {severity.capitalize()}"


def summary_label(text):
    # HTML summaries do not parse Markdown. Escape text before adding code tags.
    return re.sub(r"(`+)(.+?)\1", r"<code>\2</code>", escape(text))


def source_location(finding, repository_url=None, base_sha=None, head_sha=None):
    path = finding.get("path", "")
    line = finding.get("line", "")
    label = f"{path}:{line}"
    revision = base_sha if finding.get("side", "head") == "base" else head_sha
    if repository_url and revision and path:
        url = f"{repository_url}/blob/{quote(revision, safe='')}/{quote(path, safe='/')}#L{line}"
        return f"[{label}](<{url}>)"
    return label


def validate_format(comment_format):
    """Validate a saved template snapshot and return a copy."""
    if not isinstance(comment_format, dict) or set(comment_format) != set(
        _PLACEHOLDERS
    ):
        raise ValueError("Comment format must contain: " + ", ".join(_PLACEHOLDERS))
    for name, required in _PLACEHOLDERS.items():
        if not isinstance(comment_format[name], str):
            raise ValueError(f"{name}.md must contain Markdown text")
        template = Template(comment_format[name])
        if not template.is_valid():
            raise ValueError(
                f"{name}.md has an invalid placeholder; use $$ for a literal $."
            )
        identifiers = [
            match.group("named") or match.group("braced")
            for match in template.pattern.finditer(template.template)
            if match.group("named") or match.group("braced")
        ]
        if sorted(identifiers) != sorted(required):
            raise ValueError(
                f"{name}.md must include each placeholder exactly once: "
                + ", ".join("$" + identifier for identifier in required)
            )
    return dict(comment_format)


def load_format(directory=None):
    """Load and validate a JSON-serializable snapshot of the Markdown templates."""
    directory = Path(directory) if directory is not None else Path(__file__).parent
    comment_format = {
        name: (directory / f"{name}.md").read_text().rstrip("\n")
        for name in _PLACEHOLDERS
    }
    return validate_format(comment_format)


def _render_sections(template, sections):
    # Split before substitution so omitted sections leave no empty paragraphs,
    # while whitespace inside finding bodies and other review text stays intact.
    paragraphs = (
        Template(paragraph).substitute(sections) for paragraph in template.split("\n\n")
    )
    return "\n\n".join(paragraph for paragraph in paragraphs if paragraph.strip())


# A display limit only. Full reports and structured exports retain every finding.
COMMENT_FINDING_LIMIT = 6
SEVERITY_ORDER = ("critical", "major", "minor", "suggestion")


def _comment_findings(findings):
    # The verifier orders equally severe findings by impact and evidence.
    return sorted(
        findings, key=lambda finding: SEVERITY_ORDER.index(finding["severity"])
    )[:COMMENT_FINDING_LIMIT]


def _current_findings(findings, memory, head_sha):
    current = list(findings)
    groups = {}
    for entry in memory:
        groups.setdefault(entry.get("duplicate_of") or entry["key"], []).append(entry)
    included = set()
    for key, group in groups.items():
        # A current candidate may have been withheld by a later verifier. Do not
        # resurrect it from memory just because no published finding survived.
        if any(
            entry.get("current_candidate_ids") or entry.get("current_finding_ids")
            for entry in group
        ):
            continue
        confirmed = [
            entry
            for entry in group
            if head_sha
            and entry.get("checked_head") == head_sha
            and entry.get("status") == "still_present"
            and entry.get("reason")
            and any(
                source.get("side") == "head"
                for source in entry.get("source_evidence", [])
            )
        ]
        if not confirmed:
            continue
        entry = next(
            (entry for entry in confirmed if entry["key"] == key), confirmed[0]
        )
        source = next(
            source for source in entry["source_evidence"] if source["side"] == "head"
        )
        current.append(
            {
                "id": entry["key"],
                "severity": entry["severity"],
                "title": entry.get("presented_title") or entry["title"],
                # Reconciliation describes the claim at this head; old wording and
                # old line numbers need not still be accurate.
                "summary": entry["reason"],
                "body": entry["reason"],
                "path": source["path"],
                "line": source["line"],
                "side": "head",
            }
        )
        included.update(entry["key"] for entry in group)
    return current, included


def _brief_finding(finding, comment_format, location=None):
    return Template(comment_format["brief_finding"]).substitute(
        severity=severity_label(finding["severity"]),
        title=finding.get("presented_title") or finding["title"],
        summary=finding.get("presented_summary") or finding["summary"],
        location=location or f"{finding.get('path', '')}:{finding.get('line', '')}",
        side=finding.get("side", "head"),
    )


def render(
    findings,
    limitations=(),
    concept_summary=None,
    alternatives=(),
    backport=None,
    review_memory=None,
    verification_failed=False,
    *,
    comment_format=None,
    review_tier=None,
    report_url=None,
    head_sha=None,
):
    """Render a bounded current review; history and explanations live in the report."""
    comment_format = (
        load_format() if comment_format is None else validate_format(comment_format)
    )
    sections = dict.fromkeys(_PLACEHOLDERS["comment"], "")
    findings, included_history = _current_findings(
        findings, review_memory or [], head_sha
    )
    selected = [] if verification_failed else _comment_findings(findings)
    if verification_failed:
        sections["verification_failed"] = (
            "Review failed because required verification did not complete."
        )
    if selected:
        sections["findings"] = Template(comment_format["findings"]).substitute(
            findings="\n\n".join(
                _brief_finding(finding, comment_format) for finding in selected
            )
        )
    if concept_summary and not verification_failed:
        sections["design"] = Template(comment_format["design"]).substitute(
            design=concept_summary
        )
    if backport is not None:
        sections["backport"] = Template(comment_format["backport"]).substitute(
            verdict=backport["verdict"], summary=backport["summary"], details=""
        )
    notices = []
    if len(findings) > len(selected) and not verification_failed:
        count = len(findings) - len(selected)
        label = f"{count} additional verified finding{'s' if count != 1 else ''} in the full report"
        notices.append(f"[{label}](<{report_url}>)." if report_url else label + ".")
    # Never replay unverified claims or infer that omitted history was resolved.
    unlinked = [
        entry
        for entry in review_memory or []
        if entry.get("status") not in {"fixed", "invalid"}
        and not entry.get("current_finding_ids")
        and (verification_failed or entry["key"] not in included_history)
    ]
    if unlinked:
        notices.append(
            "Earlier concerns remain unresolved; see the full report for their current assessments."
        )
    if limitations or any(
        entry.get("status") == "unclear" for entry in review_memory or []
    ):
        sections["limitations"] = Template(comment_format["limitations"]).substitute(
            coverage_notice="Review coverage was incomplete.", limitations=""
        )
    if (
        not selected
        and not concept_summary
        and not backport
        and not verification_failed
    ):
        sections["empty"] = (
            "No verified findings are available from this review."
            if limitations or unlinked
            else "I found no actionable issues in this static review."
        )
    sections["overview"] = "\n\n".join(notices)
    content = _render_sections(comment_format["comment"], sections)
    if review_tier:
        content += f"\n\n<!-- Review tier: {escape(review_tier)} -->"
    return content


def render_report(
    findings,
    limitations=(),
    concept_summary=None,
    alternatives=(),
    backport=None,
    review_memory=None,
    verification_failed=False,
    *,
    comment_format=None,
    review_tier=None,
    head_sha=None,
):
    if comment_format is None:
        comment_format = load_format()
    else:
        comment_format = validate_format(comment_format)

    def fill(name, **values):
        return Template(comment_format[name]).substitute(values)

    sections = dict.fromkeys(_PLACEHOLDERS["comment"], "")
    if verification_failed:
        sections["verification_failed"] = (
            "Review failed because required verification did not complete."
        )
    if backport is not None:
        details = []
        for key, heading in (
            ("evidence", "Evidence"),
            ("required_fixes", "Required fixes"),
            ("limitations", "Limitations"),
        ):
            if backport[key]:
                details.append(
                    heading + ":\n\n" + "\n".join(f"- {item}" for item in backport[key])
                )
        sections["backport"] = fill(
            "backport",
            verdict=backport["verdict"],
            summary=backport["summary"],
            details="\n\n" + "\n\n".join(details) if details else "",
        )
    memory = review_memory or []
    findings, included_history = _current_findings(
        findings, memory, None if verification_failed else head_sha
    )
    memory_groups = {}
    for index, entry in enumerate(memory):
        key = entry.get("duplicate_of") or entry.get("key") or index
        memory_groups.setdefault(key, []).append(entry)

    def active_entries(group):
        return [
            entry for entry in group if entry.get("status") not in ("fixed", "invalid")
        ]

    finding_groups = {}
    current_counts = Counter()

    def finding_text(finding):
        return fill(
            "finding",
            severity=severity_label(finding["severity"]),
            title=finding.get("title", ""),
            summary=finding["summary"],
            location=f"{finding.get('path', '')}:{finding.get('line', '')}",
            side=finding.get("side", "head"),
            body=finding.get("body", ""),
        )

    def previous_report(entry):
        status = entry.get("status", "unclear")
        parts = []
        if status == "unclear":
            parts.append("Not confirmed against this revision.")
        elif status == "fixed":
            parts.append("Fixed.")
        elif status == "invalid":
            parts.append("No longer considered actionable.")
        if entry.get("source_head"):
            parts.append(f"First reported at `{entry['source_head']}`.")
        report_url = entry.get("source_report_url", "")
        if urlsplit(report_url).scheme in ("https", "http"):
            report_url = report_url.replace("(", "%28").replace(")", "%29")
            parts.append(f"[Original review]({report_url}).")
        if entry.get("checked_head"):
            parts.append(f"Last checked at `{entry['checked_head']}`.")
        if entry.get("reason") and status != "still_present":
            parts.append(entry["reason"])
        position = {
            "address_elsewhere": "The author plans to address this separately.",
            "wontfix": "The author does not plan to fix this.",
            "rejected": "The author disagrees with this finding.",
        }.get(entry.get("author_position"))
        if position:
            parts.append(position)
        for comment in entry.get("author_comments", []):
            url = comment.get("url", "")
            if urlsplit(url).scheme in ("https", "http"):
                url = url.replace("(", "%28").replace(")", "%29")
                parts.append(
                    f"[Author reply]({url}) {comment.get('updated_at', '')}".rstrip()
                )
        return " ".join(parts)

    severity_order = ("critical", "major", "minor", "suggestion")

    def published_findings(entry):
        if entry.get("last_presented_findings"):
            return entry["last_presented_findings"]
        finding = dict(entry)
        for field in ("title", "summary", "body"):
            if entry.get("presented_" + field):
                finding[field] = entry["presented_" + field]
        return [finding]

    def representative(group):
        return min(
            group,
            key=lambda entry: min(
                severity_order.index(finding.get("severity", "suggestion"))
                for finding in published_findings(entry)
            ),
        )

    def provenance(group):
        if len(group) == 1:
            return previous_report(group[0])
        reports = [
            f"- {entry.get('path', '')}:{entry.get('line', '')}. {previous_report(entry)}"
            for entry in group
        ]
        return "Previous reports of the same concern:\n\n" + "\n".join(reports)

    def memory_text(groups, finding, status=None):
        active = [entry for group in groups for entry in active_entries(group)]
        status = status or ("not confirmed" if active else "resolved")
        return fill(
            "retained",
            label=summary_label(
                f"[{finding['severity'].capitalize()} · {status}] {finding['title']}"
            ),
            summary=finding.get("summary", ""),
            location=f"{finding.get('path', '')}:{finding.get('line', '')}",
            side=finding.get("side", "head"),
            body=finding.get("body", ""),
            provenance="\n\n".join(provenance(group) for group in groups),
        )

    matched_groups = set()
    history = []
    resolved_history = []
    unresolved_history = []
    for finding in findings:
        text = finding_text(finding)
        linked = {
            key: group
            for key, group in memory_groups.items()
            if any(
                finding["id"] in entry.get("current_finding_ids", [])
                or (entry["key"] in included_history and finding["id"] == entry["key"])
                for entry in active_entries(group)
            )
        }
        if linked:
            matched_groups.update(linked)
            reports = []
            for key, group in linked.items():
                if key in included_history:
                    for old in published_findings(
                        representative(active_entries(group))
                    ):
                        reports.append(memory_text([group], old, "previously reported"))
                else:
                    reports.append(provenance(group))
            text += "\n\n" + fill("provenance", provenance="\n\n".join(reports))
        current_counts[finding["severity"]] += 1
        finding_groups.setdefault(finding["severity"], []).append(text)
    for key, group in memory_groups.items():
        if key in matched_groups:
            continue
        active = active_entries(group)
        eligible = active or group
        entry = representative(eligible)
        for finding in published_findings(entry):
            if verification_failed:
                history.append(
                    f"- **{finding.get('title', '')}** ({finding.get('path', '')}:"
                    f"{finding.get('line', '')}, {finding.get('side', 'head')}). "
                    f"Status: {entry.get('status', 'unclear')}."
                )
            elif active:
                history.append(memory_text([group], finding))
                unresolved_history.append(finding)
            else:
                resolved_history.append(memory_text([group], finding))
    rendered_findings = []
    for severity in severity_order:
        group = finding_groups.get(severity, [])
        if group:
            rendered_findings.extend(group)
    if rendered_findings:
        visible = rendered_findings[:COMMENT_FINDING_LIMIT]
        extra = rendered_findings[COMMENT_FINDING_LIMIT:]
        if extra:
            visible.append(
                fill(
                    "history",
                    label=f"Additional verified findings ({len(extra)})",
                    history="\n\n".join(extra),
                )
            )
        sections["findings"] = fill("findings", findings="\n\n".join(visible))
    rendered_design = []
    if concept_summary:
        rendered_design.append(f"**Concept and approach.** {concept_summary}")
    if rendered_design:
        sections["design"] = fill("design", design="\n\n".join(rendered_design))
    if alternatives:
        rendered_alternatives = []
        for alternative in alternatives:
            text = (
                f"**{alternative['name']}.** {alternative['concept']} "
                f"Benefit: {alternative['benefit']} "
                f"Trade-off: {alternative['cost']}"
            )
            if alternative["unresolved"]:
                text += f" Unresolved: {alternative['unresolved']}"
            if alternative["citations"]:
                text += " " + " ".join(
                    f"[Source {index}]({citation})"
                    for index, citation in enumerate(alternative["citations"], 1)
                )
            rendered_alternatives.append(
                "<details>\n<summary>Alternative approach: "
                + summary_label(alternative["name"])
                + "</summary>\n\n"
                + text
                + "\n\n</details>"
            )
        sections["alternatives"] = fill(
            "alternatives", alternatives="\n\n".join(rendered_alternatives)
        )
    if history or resolved_history:
        history_label = f"Unconfirmed previous findings ({len(unresolved_history)})"
        if not unresolved_history:
            history_label = "Previous findings"
        if resolved_history:
            history.append(fill("resolved", history="\n\n".join(resolved_history)))
        if verification_failed:
            sections["history"] = fill("history_failed", history="\n\n".join(history))
        else:
            sections["history"] = fill(
                "history",
                label=history_label,
                history="\n\n".join(history),
            )
    if not unresolved_history and not any(
        value for name, value in sections.items() if name != "history"
    ):
        sections["empty"] = (
            "I found no actionable issues in this static review."
            if not limitations
            else "No verified findings are available from this review."
        )
    if limitations:
        sections["limitations"] = fill(
            "limitations",
            coverage_notice="Review coverage was incomplete.",
            limitations="",
        )
    counts = [
        f"{current_counts[severity]} {severity}{' finding' if severity != 'suggestion' else ''}{'s' if current_counts[severity] != 1 else ''}"
        for severity in severity_order
        if current_counts[severity]
    ]
    if unresolved_history:
        count = len(unresolved_history)
        counts.append(
            f"{count} unconfirmed previous finding{'s' if count != 1 else ''}"
        )
    sections["overview"] = " · ".join(counts)
    content = _render_sections(comment_format["comment"], sections)
    if review_tier:
        content += f"\n\n<!-- Review tier: {escape(review_tier)} -->"
    return content


def render_github(
    findings,
    *,
    head_sha,
    report_url,
    has_retained=False,
    repository_url=None,
    base_sha=None,
    comment_format=None,
    review_tier=None,
):
    """Format major findings already selected and checked for publication."""
    comment_format = (
        load_format() if comment_format is None else validate_format(comment_format)
    )
    selected = _comment_findings(findings)
    rendered = [
        _brief_finding(
            finding,
            comment_format,
            source_location(finding, repository_url, base_sha, head_sha),
        )
        for finding in selected
    ]
    content = _render_sections(
        comment_format["github"],
        {
            "new_findings_notice": (
                f"{len(selected)} major finding{'s' if len(selected) != 1 else ''}."
                + (
                    f" {len(findings) - len(selected)} additional verified findings in the full report."
                    if len(findings) > len(selected)
                    else ""
                )
                if findings
                else ""
            ),
            "retained_notice": (
                "Previously discussed major concerns remain unresolved. "
                "Their original findings and current assessments are retained in the full report."
                if has_retained
                else ""
            ),
            "findings": "\n\n".join(rendered),
            "report_url": report_url,
        },
    )
    revision = f"`{head_sha[:12]}`"
    if repository_url:
        revision = f"[{revision}](<{repository_url}/commit/{quote(head_sha, safe='')}>)"
    content += f"\n\nReviewed head: {revision}"
    if review_tier:
        content += f"\n\n<!-- Review tier: {escape(review_tier)} -->"
    return content
