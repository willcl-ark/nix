"""Render verified review results with contributor-editable Markdown templates."""

from collections import Counter
from html import escape
from pathlib import Path
from string import Template
from urllib.parse import quote

_PLACEHOLDERS = {
    "comment": (
        "verification_failed",
        "overview",
        "backport",
        "findings",
        "design",
        "empty",
        "limitations",
    ),
    "backport": ("verdict", "summary", "details"),
    "findings": ("findings",),
    "finding": ("severity", "title", "summary", "location", "side", "body"),
    "brief_finding": ("severity", "title", "summary", "location", "side"),
    "design": ("design",),
    "limitations": ("coverage_notice",),
    "github": ("new_findings_notice", "retained_notice", "findings", "report_url"),
}


def severity_label(severity):
    symbol = {"critical": "⛔", "major": "🔴", "minor": "🟠", "suggestion": "🔵"}[
        severity
    ]
    return f"{symbol} {severity.capitalize()}"


def source_location(finding, repository_url=None, base_sha=None, head_sha=None):
    path = finding.get("path", "")
    line = finding.get("line", "")
    label = f"{path}:{line}"
    revision = base_sha if finding.get("side", "head") == "base" else head_sha
    if finding.get("side") == "commit":
        revision = finding.get("parent") or finding.get("commit")
        context = (
            f"parent of {finding['commit'][:12]}" if finding.get("parent") else "commit"
        )
        label += f" at {revision[:12]} ({context})"
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


# A display limit only. Review reports and structured exports retain every finding.
COMMENT_FINDING_LIMIT = 6
SEVERITY_ORDER = ("critical", "major", "minor", "suggestion")


def _ordered_findings(findings):
    # The verifier orders equally severe findings by impact and evidence.
    return sorted(
        findings, key=lambda finding: SEVERITY_ORDER.index(finding["severity"])
    )


def report_findings(
    findings, review_memory=None, *, head_sha=None, verification_failed=False
):
    """Return only findings approved by the publication pipeline."""
    if verification_failed:
        return []
    return list(findings)


def _unconfirmed_history(memory):
    return [
        entry
        for entry in memory
        if entry.get("status") not in {"fixed", "invalid"}
        and not entry.get("current_finding_ids")
    ]


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
    backport=None,
    review_memory=None,
    verification_failed=False,
    *,
    comment_format=None,
    review_tier=None,
    report_url=None,
    head_sha=None,
    repository_url=None,
    base_sha=None,
):
    """Render a bounded PR comment using the approved finding summaries."""
    comment_format = (
        load_format() if comment_format is None else validate_format(comment_format)
    )
    sections = dict.fromkeys(_PLACEHOLDERS["comment"], "")
    selected = (
        []
        if verification_failed
        else _ordered_findings(findings)[:COMMENT_FINDING_LIMIT]
    )
    if verification_failed:
        sections["verification_failed"] = (
            "Review failed because required verification did not complete."
        )
    if selected:
        sections["findings"] = Template(comment_format["findings"]).substitute(
            findings="\n\n".join(
                _brief_finding(
                    finding,
                    comment_format,
                    source_location(finding, repository_url, base_sha, head_sha)
                    if repository_url
                    else None,
                )
                for finding in selected
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
        label = f"{count} additional verified finding{'s' if count != 1 else ''} in the Review report"
        notices.append(f"[{label}](<{report_url}>)." if report_url else label + ".")
    # Never replay unverified claims or infer that omitted history was resolved.
    unlinked = _unconfirmed_history(review_memory or [])
    if unlinked:
        notices.append(
            "Earlier concerns remain unresolved; see the Run archive linked from the Review report."
        )
    if limitations or any(
        entry.get("status") == "unclear" for entry in review_memory or []
    ):
        sections["limitations"] = Template(comment_format["limitations"]).substitute(
            coverage_notice="Review coverage was incomplete."
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
    backport=None,
    review_memory=None,
    verification_failed=False,
    *,
    comment_format=None,
    review_tier=None,
    head_sha=None,
    coverage_url=None,
    repository_url=None,
    base_sha=None,
    failure_reason=None,
    finding_suffix=None,
):
    """Render the approved review; diagnostic detail belongs in the run archive."""
    comment_format = (
        load_format() if comment_format is None else validate_format(comment_format)
    )

    def fill(name, **values):
        return Template(comment_format[name]).substitute(values)

    sections = dict.fromkeys(_PLACEHOLDERS["comment"], "")
    memory = review_memory or []
    findings = [] if verification_failed else _ordered_findings(findings)
    if verification_failed:
        sections["verification_failed"] = (
            failure_reason
            or "Review failed because required verification did not complete."
        )

    def finding_content(finding):
        summary = finding.get("presented_summary") or finding["summary"]
        body = finding.get("presented_body") or finding.get("body", "")
        location = source_location(finding, repository_url, base_sha, head_sha)
        if not body or body == summary:
            return _brief_finding(finding, comment_format, location)
        return fill(
            "finding",
            severity=severity_label(finding["severity"]),
            title=finding.get("presented_title") or finding["title"],
            summary=summary,
            location=location,
            side=finding.get("side", "head"),
            body=body,
        )

    def finding_text(finding):
        content = finding_content(finding)
        if finding_suffix is not None:
            content += finding_suffix(finding)
        return content

    if findings:
        groups = []
        for heading, suggestions in (
            ("Findings", False),
            ("Optional improvements", True),
        ):
            selected = [
                finding
                for finding in findings
                if (finding["severity"] == "suggestion") == suggestions
            ]
            if selected:
                groups.append(
                    f"## {heading}\n\n"
                    + "\n\n".join(finding_text(finding) for finding in selected)
                )
        sections["findings"] = fill("findings", findings="\n\n".join(groups))
    if concept_summary and not verification_failed:
        sections["design"] = fill("design", design=concept_summary)
    if backport is not None:
        details = []
        for key, heading in (
            ("evidence", "Evidence"),
            ("required_fixes", "Required fixes"),
            ("limitations", "Limitations"),
        ):
            if backport.get(key):
                details.append(
                    heading + ":\n\n" + "\n".join(f"- {item}" for item in backport[key])
                )
        sections["backport"] = fill(
            "backport",
            verdict=backport["verdict"],
            summary=backport["summary"],
            details="\n\n" + "\n\n".join(details) if details else "",
        )
    unconfirmed = _unconfirmed_history(memory)
    counts = Counter(finding["severity"] for finding in findings)
    overview = [
        f"{counts[severity]} {severity}{' finding' if severity != 'suggestion' else ''}{'s' if counts[severity] != 1 else ''}"
        for severity in SEVERITY_ORDER
        if counts[severity]
    ]
    sections["overview"] = " · ".join(overview)
    if unconfirmed:
        sections["overview"] = "\n\n".join(
            part
            for part in (
                sections["overview"],
                "Earlier concerns remain unresolved; see the Run archive for their assessments.",
            )
            if part
        )
    if (
        coverage_url
        or limitations
        or any(entry.get("status") == "unclear" for entry in memory)
    ):
        sections["limitations"] = fill(
            "limitations",
            coverage_notice=f"[Coverage details](<{coverage_url}>)"
            if coverage_url
            else "Review coverage was incomplete.",
        )
    if (
        not findings
        and not concept_summary
        and not backport
        and not verification_failed
    ):
        sections["empty"] = (
            "The review is inconclusive; no findings were verified."
            if limitations or unconfirmed
            else "Review completed with no actionable findings."
        )
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
    """Format critical and major findings already checked for publication."""
    comment_format = (
        load_format() if comment_format is None else validate_format(comment_format)
    )
    selected = _ordered_findings(findings)[:COMMENT_FINDING_LIMIT]
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
                f"{len(selected)} major or critical finding{'s' if len(selected) != 1 else ''}."
                + (
                    f" {len(findings) - len(selected)} additional verified findings in the Review report."
                    if len(findings) > len(selected)
                    else ""
                )
                if findings
                else ""
            ),
            "retained_notice": (
                "Previously discussed major concerns remain unresolved. "
                "Their assessments are in the Run archive linked from the Review report."
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
