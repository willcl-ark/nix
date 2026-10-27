"""Render verified review results with contributor-editable Markdown templates."""

from pathlib import Path
from string import Template
from urllib.parse import urlsplit

_PLACEHOLDERS = {
    "comment": (
        "verification_failed",
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
    "severity": ("heading", "findings"),
    "finding": ("title", "location", "side", "body"),
    "design": ("design",),
    "alternatives": ("alternatives",),
    "history": ("unresolved_notice", "history"),
    "history_failed": ("history",),
    "limitations": ("coverage_notice", "limitations"),
    "github": ("new_findings_notice", "retained_notice", "findings", "report_url"),
}


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
    memory_groups = {}
    for index, entry in enumerate(memory):
        key = entry.get("duplicate_of") or entry.get("key") or index
        memory_groups.setdefault(key, []).append(entry)

    def has_response(entry):
        return entry.get("author_position", "none") != "none" and bool(
            entry.get("author_comments")
        )

    def active_entries(group):
        return [
            entry for entry in group if entry.get("status") not in ("fixed", "invalid")
        ]

    collapsed_ids = {
        finding_id
        for group in memory_groups.values()
        if active_entries(group)
        and all(
            has_response(entry)
            and entry.get("collapsed")
            and entry.get("status") == "still_present"
            for entry in active_entries(group)
        )
        for entry in active_entries(group)
        for finding_id in entry.get("current_finding_ids", [])
    }
    finding_groups = {}
    design_findings = []

    def finding_text(finding):
        return fill(
            "finding",
            title=finding.get("title", ""),
            location=f"{finding.get('path', '')}:{finding.get('line', '')}",
            side=finding.get("side", "head"),
            body=finding.get("body", ""),
        )

    def previous_report(entry):
        status = entry.get("status", "unclear")
        previous = "Previously reported"
        if status == "unclear":
            previous += "; not confirmed against this revision"
        elif status == "still_present":
            previous += "; verified as still present in this revision"
        text = f"{previous}. Status: {status}. Author position: {entry.get('author_position', 'none')}."
        if entry.get("source_head"):
            text += f" Originally reported at `{entry['source_head']}`."
        if entry.get("reason"):
            text += " " + entry["reason"]
        for comment in entry.get("author_comments", []):
            url = comment.get("url", "")
            if urlsplit(url).scheme in ("https", "http"):
                url = url.replace("(", "%28").replace(")", "%29")
                text += f" [Author reply]({url}) {comment.get('updated_at', '')}"
        return text

    severity_order = ("critical", "major", "minor", "suggestion")

    def representative(group):
        return min(
            group,
            key=lambda entry: severity_order.index(entry.get("severity", "suggestion")),
        )

    def memory_text(group, finding):
        text = finding_text(finding)
        if len(group) == 1:
            return text + "\n\n" + previous_report(group[0])
        reports = [
            f"- {entry.get('path', '')}:{entry.get('line', '')}. {previous_report(entry)}"
            for entry in group
        ]
        return (
            text + "\n\nPrevious reports of the same concern:\n\n" + "\n".join(reports)
        )

    remaining = dict(memory_groups)
    for finding in findings:
        if finding["id"] in collapsed_ids:
            continue
        text = finding_text(finding)
        for key, group in list(remaining.items()):
            ids = {
                identifier
                for entry in active_entries(group)
                for identifier in entry.get("current_finding_ids", [])
            }
            if finding["id"] in ids:
                text = memory_text(group, finding)
                del remaining[key]
                break
        if finding["kind"] == "design":
            design_findings.append(text)
        else:
            finding_groups.setdefault(finding["severity"], []).append(text)
    history = []
    unresolved_history = []
    for group in remaining.values():
        active = active_entries(group)
        eligible = active or [entry for entry in group if has_response(entry)]
        if not eligible:
            continue
        entry = representative(eligible)
        if verification_failed:
            history.append(
                f"- **{entry.get('title', '')}** ({entry.get('path', '')}:"
                f"{entry.get('line', '')}, {entry.get('side', 'head')}). "
                f"Status: {entry.get('status', 'unclear')}."
            )
        elif all(has_response(item) for item in eligible):
            history.append(memory_text(group, entry))
            if active:
                unresolved_history.append(entry)
        else:
            finding_groups.setdefault(entry.get("severity", "suggestion"), []).append(
                memory_text(group, entry)
            )
    rendered_findings = []
    for severity, heading in (
        ("critical", "Critical"),
        ("major", "Major"),
        ("minor", "Minor"),
        ("suggestion", "Suggestions"),
    ):
        group = finding_groups.get(severity, [])
        if group:
            rendered_findings.append(
                fill("severity", heading=heading, findings="\n\n".join(group))
            )
    if rendered_findings:
        sections["findings"] = fill("findings", findings="\n\n".join(rendered_findings))
    rendered_design = []
    if design_findings:
        rendered_design.append("\n\n".join(design_findings))
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
            rendered_alternatives.append(text)
        sections["alternatives"] = fill(
            "alternatives", alternatives="\n\n".join(rendered_alternatives)
        )
    if history:
        unresolved_notice = ""
        if unresolved_history:
            severities = {}
            for entry in unresolved_history:
                severity = entry.get("severity", "unknown")
                severities[severity] = severities.get(severity, 0) + 1
            counts = ", ".join(
                f"{count} {severity}" for severity, count in severities.items()
            )
            unresolved_notice = (
                f"Previously reported concerns remain unresolved: {counts}. "
                "Author deferral or disagreement does not resolve a finding.\n\n"
            )
        if verification_failed:
            sections["history"] = fill("history_failed", history="\n\n".join(history))
        else:
            sections["history"] = fill(
                "history",
                unresolved_notice=unresolved_notice,
                history="\n\n".join(history),
            )
    if not any(sections.values()):
        sections["empty"] = (
            "I found no actionable issues in this static review."
            if not limitations
            else "No verified findings are available from this review."
        )
    if limitations:
        sections["limitations"] = fill(
            "limitations",
            coverage_notice="Review coverage was incomplete.",
            limitations="\n".join(
                f"- {limitation}" for limitation in dict.fromkeys(limitations)
            ),
        )
    content = _render_sections(comment_format["comment"], sections)
    if review_tier:
        content += f"\n\nReview tier: {review_tier}"
    return content


def render_github(
    findings,
    *,
    head_sha,
    report_url,
    has_retained=False,
    comment_format=None,
    review_tier=None,
):
    """Format major findings already selected and checked for publication."""
    comment_format = (
        load_format() if comment_format is None else validate_format(comment_format)
    )
    rendered = []
    for finding in findings:
        location = finding.get("path") or ""
        if finding.get("line") is not None:
            location += f":{finding['line']}"
        rendered.append(
            Template(comment_format["finding"]).substitute(
                title=finding.get("presented_title") or finding["title"],
                body=finding.get("presented_body") or finding["body"],
                location=location,
                side=finding.get("side", "head"),
            )
        )
    content = _render_sections(
        comment_format["github"],
        {
            "new_findings_notice": (
                f"Ralph found major issues at {head_sha}." if findings else ""
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
    if review_tier:
        content += f"\n\nReview tier: {review_tier}"
    return content
