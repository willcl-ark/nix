"""Static public production statistics dashboard."""

import hashlib
import html
import json
from datetime import UTC, datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from . import report


MISSING = object()


def _escape(value):
    return html.escape("" if value is None else str(value), quote=True)


def _field(record, name, default=MISSING):
    if not isinstance(record, dict):
        return default
    if name in record:
        return record[name]
    return default


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value) if "." in value else int(value)
        except ValueError:
            return None
    return None


def _fmt_int(value):
    number = _number(value)
    return "N/A" if number is None else f"{int(number):,}"


def _fmt_decimal(value, digits=1):
    number = _number(value)
    return "N/A" if number is None else f"{number:,.{digits}f}"


def _fmt_percent(numerator, denominator):
    numerator = _number(numerator)
    denominator = _number(denominator)
    if numerator is None or not denominator:
        return "N/A"
    return f"{100 * numerator / denominator:.1f}%"


def _fmt_tokens(record, name):
    tokens = _mapping(record.get("tokens"))
    value = tokens.get(name)
    if value is None:
        return "N/A"
    missing = _number(_mapping(record.get("unknown_token_counts")).get(name)) or 0
    if missing:
        if missing >= (_number(record.get("request_count")) or 0):
            return "N/A"
        return _fmt_int(value) + " (partial)"
    return _fmt_int(value)


def _cache_rate(record):
    missing = _mapping(record.get("unknown_token_counts"))
    if any(missing.get(name) for name in ("input", "cached")):
        return "N/A"
    tokens = _mapping(record.get("tokens"))
    return _fmt_percent(tokens.get("cached"), tokens.get("input"))


def _generated_at(summary):
    value = _field(summary, "generated_at")
    if isinstance(value, str) and value:
        return value
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _valid_http_base(url):
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url.rstrip("/"))
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return None
    return url.rstrip("/")


def _safe_report_name(review):
    name = _field(review, "report_name")
    if (
        isinstance(name, str)
        and report.REPORT_ID.fullmatch(name)
        and name.endswith(".html")
    ):
        return name
    return None


def _valid_sha(value):
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(char in "0123456789abcdefABCDEF" for char in value)
    )


def _pr_number(review):
    value = _field(review, "number")
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def _link(url, text):
    return f'<a href="{_escape(url)}">{_escape(text)}</a>'


def _pr_link(review, repository_url):
    number = _pr_number(review)
    base = _valid_http_base(repository_url)
    if number is None or base is None:
        return _escape(_field(review, "number", default="N/A"))
    return _link(f"{base}/pulls/{number}", f"#{number}")


def _report_link(review, report_base_url):
    name = _safe_report_name(review)
    return _report_name_link(name, report_base_url, "report")


def _report_name_link(name, report_base_url, text):
    base = _valid_http_base(report_base_url)
    if name is None or base is None:
        return "N/A"
    return _link(f"{base}/{quote(name, safe='._-')}", text)


def _compare_link(assessment, repository_url):
    old_head = _field(assessment, "old_head")
    new_head = _field(assessment, "new_head")
    if not _valid_sha(old_head) or not _valid_sha(new_head):
        return "N/A"
    text = f"{old_head[:12]}..{new_head[:12]}"
    base = _valid_http_base(repository_url)
    if base is None:
        return _escape(text)
    return _link(f"{base}/compare/{old_head}..{new_head}", text)


def _record_rows(records):
    if isinstance(records, dict):
        return [
            (
                {"name": key, **value}
                if isinstance(value, dict)
                else {"name": key, "count": value}
            )
            for key, value in sorted(records.items())
        ]
    return [record for record in _list(records) if isinstance(record, dict)]


def _section_note(text):
    return f'<p class="note">{_escape(text)}</p>'


def _internal_link(href, text):
    return f'<a href="{href}">{_escape(text)}</a>'


def _candidate_denominator(counts):
    values = [counts.get("publish"), counts.get("drop"), counts.get("unresolved")]
    if any(_number(value) is None for value in values):
        return None
    return sum(_number(value) for value in values)


def _analysis(summary):
    inventory = _mapping(summary.get("inventory"))
    overview = _mapping(summary.get("overview"))
    usage = _mapping(summary.get("usage"))
    month = _mapping(usage.get("current_month"))
    lifetime = _mapping(usage.get("lifetime"))
    saved_reviews = _field(overview, "saved_reviews")
    archaeology = _mapping(overview.get("archaeology"))
    unique_prs = _field(inventory, "unique_prs")
    jobs = _field(inventory, "jobs_total")
    attempts = _field(lifetime, "request_count")
    failed = _field(inventory, "recorded_failed_attempts")
    if _number(saved_reviews):
        sentence = (
            f"{_fmt_int(saved_reviews)} saved reviews; {_fmt_int(unique_prs)} pull requests have been queued. "
            f"{_fmt_int(_field(archaeology, 'research_completed'))} completed archaeology research. "
            f"Current month review usage is {_fmt_int(month.get('request_count'))} request records."
        )
    else:
        sentence = "No saved reviews are in this summary yet."
    if _number(attempts) is not None and _number(jobs) is not None:
        sentence += (
            f" Lifetime tracking has {_fmt_int(attempts)} request records for "
            f"{_fmt_int(jobs)} review jobs, including {_fmt_int(failed)} recorded failed attempts or retries."
        )
    return sentence


def _kpi(label, value, detail=""):
    return (
        '<article class="card">'
        f'<div class="kpi-label">{_escape(label)}</div>'
        f'<div class="kpi-value">{_escape(value)}</div>'
        f'<div class="kpi-detail">{_escape(detail)}</div>'
        "</article>"
    )


def _top_kpis(summary):
    overview = _mapping(summary.get("overview"))
    inventory = _mapping(summary.get("inventory"))
    month = _mapping(_mapping(summary.get("usage")).get("current_month"))
    counts = _mapping(_mapping(summary.get("addressed_findings")).get("status_counts"))
    return "".join(
        [
            _kpi(
                "Saved reviews",
                _fmt_int(overview.get("saved_reviews")),
                f"{_fmt_int(inventory.get('unique_prs'))} unique PRs",
            ),
            _kpi(
                "Accepted findings",
                _fmt_int(overview.get("published_findings")),
                "Bot verified; not human confirmed",
            ),
            _kpi(
                "Addressed after review",
                _fmt_int(counts.get("addressed")),
                f"{_fmt_int(counts.get('partially_addressed'))} partially addressed",
            ),
            _kpi(
                "Request records this month",
                _fmt_int(month.get("request_count")),
                "Ledger entries, including retries",
            ),
        ]
    )


def _source_status(summary):
    inventory = _mapping(summary.get("inventory"))
    missing = _mapping(inventory.get("missing_sources"))
    if not missing:
        return "Datasets present: N/A."
    present = [name for name, is_missing in missing.items() if not is_missing]
    absent = [name for name, is_missing in missing.items() if is_missing]
    missing_text = "none" if not absent else ", ".join(sorted(absent))
    return f"Datasets present: {len(present)}/{len(missing)}. Missing: {missing_text}."


def _coverage_table(summary):
    coverage = _mapping(_mapping(summary.get("overview")).get("coverage"))
    context = _mapping(_mapping(summary.get("overview")).get("context_coverage"))
    verification = _mapping(
        _mapping(summary.get("overview")).get("verification_status")
    )
    statuses = sorted(set(coverage) | set(context) | set(verification))
    if not statuses:
        return "<p>No coverage counts were recorded.</p>"
    rows = "".join(
        "<tr>"
        f"<td>{_escape(status)}</td>"
        f"<td>{_fmt_int(coverage.get(status))}</td>"
        f"<td>{_fmt_int(context.get(status))}</td>"
        f"<td>{_fmt_int(verification.get(status))}</td>"
        "</tr>"
        for status in statuses
    )
    return (
        "<table><thead><tr><th>Status</th><th>Code review</th>"
        "<th>Historical context</th><th>Verification</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def _concept_section(summary):
    archaeology = _mapping(_mapping(summary.get("overview")).get("archaeology"))
    by_status = _mapping(archaeology.get("assessment_status_counts"))
    distributions = _mapping(summary.get("distributions"))
    citation_counts = _mapping(
        distributions.get("saved_review_conceptual_concern_citations")
    )
    alternative_counts = _mapping(
        distributions.get("saved_review_conceptual_concern_alternatives")
    )
    if not archaeology:
        body = "<p>No archaeology counts were recorded.</p>"
    else:
        rows = "".join(
            f"<tr><td>{_escape(status)}</td><td>{_fmt_int(count)}</td></tr>"
            for status, count in sorted(by_status.items())
        )
        status_table = (
            "<p>No conceptual assessment statuses were recorded.</p>"
            if not rows
            else "<table><thead><tr><th>Status</th><th>Reviews</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
        body = (
            f"<p>{_fmt_int(_field(archaeology, 'research_completed'))} saved reviews "
            "completed archaeology research; "
            f"{_fmt_int(_field(archaeology, 'saved_concerns'))} saved and "
            f"{_fmt_int(_field(archaeology, 'published_concerns'))} published "
            "a conceptual concern.</p>" + status_table + '<p class="note">'
            f"Median citations: {_fmt_decimal(_field(citation_counts, 'median'))}; "
            f"median alternatives: {_fmt_decimal(_field(alternative_counts, 'median'))}."
            "</p>"
        )
    return (
        '<section id="concepts"><h2>Conceptual concerns</h2>'
        + _section_note(
            "The archaeology pass can complete without publishing a concern. "
            "Published conceptual concerns are tracked separately from code findings."
        )
        + body
        + "</section>"
    )


def _addressed_findings(summary):
    addressed = _mapping(summary.get("addressed_findings"))
    return [item for item in _list(addressed.get("findings")) if isinstance(item, dict)]


def _addressed_table(findings, repository_url, report_base_url):
    if not findings:
        return "<p>No addressed-finding assessments were recorded.</p>"
    rows = []
    for finding in findings:
        source_name = _safe_report_name(
            {"report_name": finding.get("source_report_name")}
        )
        title = _escape(finding.get("title") or finding.get("finding_id", "unknown"))
        title += '<span class="subline">' + _escape(finding.get("path", "")) + "</span>"
        status = finding.get("status", "unclear")
        detail = (
            "<details><summary>Assessment</summary><p>"
            + _escape(finding.get("reason", ""))
            + "</p><p>"
            + _escape(finding.get("evidence", ""))
            + "</p><p>"
            + _report_name_link(source_name, report_base_url, "Source report")
            + " · "
            + _compare_link(finding, repository_url)
            + '</p><p class="note">'
            + _escape(finding.get("assessed_at", "N/A"))
            + " · "
            + _escape(finding.get("model", "unknown"))
            + "</p></details>"
        )
        row = _cells(
            [_pr_link(finding, repository_url), title, _escape(status), detail]
        )
        rows.append(row.replace("<tr>", f'<tr data-filter="{_escape(status)}">', 1))
    return _interactive_table(
        ["PR", "Finding", "Outcome", "Evidence"],
        rows,
        "outcomes",
        [
            (v, v.replace("_", " ").capitalize())
            for v in ("addressed", "partially_addressed", "still_present", "unclear")
        ],
    )


def _addressed_section(summary):
    addressed = _mapping(summary.get("addressed_findings"))
    counts = _mapping(addressed.get("status_counts"))
    usage = _mapping(addressed.get("usage"))
    count_rows = "".join(
        f"<tr><td>{_escape(status)}</td><td>{_fmt_int(counts.get(status))}</td></tr>"
        for status in ("addressed", "partially_addressed", "still_present", "unclear")
    )
    coverage = (
        f"{_fmt_int(_field(addressed, 'evaluated_findings'))} assessed findings from "
        f"{_fmt_int(_field(addressed, 'source_published_findings'))} published findings "
        f"across {_fmt_int(_field(addressed, 'assessed_source_reviews'))} source reviews."
    )
    unsupported = _field(addressed, "unsupported_exports", 0)
    if unsupported:
        coverage += (
            f" Follow-up was unavailable for {_fmt_int(unsupported)} reviews "
            "because their engine did not export findings."
        )
    return (
        '<section id="addressed"><h2>Addressed after review</h2>'
        + _section_note(
            _field(
                addressed,
                "note",
                default=(
                    "Best-effort automated assessment. This is not a causal "
                    "claim or human validation."
                ),
            )
        )
        + f"<p>{coverage}</p>"
        + '<div class="split"><div>'
        + "<table><thead><tr><th>Status</th><th>Findings</th></tr></thead>"
        + f"<tbody>{count_rows}</tbody></table></div>"
        + "<div>"
        + f"<p>Assessment jobs: {_fmt_int(_field(addressed, 'assessment_jobs'))}</p>"
        + f"<p>Assessment usage: {_fmt_int(_field(usage, 'request_count'))} request records; "
        + f"{_fmt_tokens(usage, 'input')} input tokens; "
        + f"{_fmt_tokens(usage, 'output')} output tokens.</p>"
        + "</div></div>"
        + "</section>"
    )


def _failed_requests(record):
    statuses = _mapping(record.get("status_counts"))
    return sum(statuses.get(status, 0) for status in ("failed", "uncertain"))


def _usage_rows(records):
    rows = []
    for record in _record_rows(records):
        name = record.get("name", record.get("provider", "unknown"))
        rows.append(
            _cells(
                [
                    _escape(name),
                    _fmt_int(record.get("request_count")),
                    _fmt_tokens(record, "input"),
                    _fmt_tokens(record, "cached"),
                    _fmt_tokens(record, "output"),
                    _fmt_tokens(record, "reasoning"),
                    _fmt_int(_failed_requests(record)),
                    _fmt_int(record.get("rate_limit_count")),
                    _fmt_int(record.get("unknown_usage_count")),
                ]
            )
        )
    if not rows:
        return "<p>No provider usage was recorded.</p>"
    return _data_table(
        [
            "Provider",
            "Records",
            "Input tokens",
            "Cached tokens",
            "Output tokens",
            "Reasoning tokens",
            "Failed requests",
            "Rate limits (429)",
            "Unknown usage records",
        ],
        rows,
    )


def _usage_trends_section(summary):
    trends = _mapping(summary.get("usage_trends"))
    cards = []
    for key, label in (
        ("current_day", "Today"),
        ("current_week", "This week"),
        ("current_month", "This month"),
    ):
        period = _mapping(trends.get(key))
        cards.append(
            _kpi(label, _fmt_int(period.get("request_count")), "Request records")
        )
    charts = []
    for key, title, description in (
        ("days", "Daily request records · 30 days", "Request records per UTC day."),
        (
            "weeks",
            "Weekly request records · 12 weeks",
            "Request records per UTC week. Weeks start Monday.",
        ),
        (
            "months",
            "Monthly request records · 12 months",
            "Request records per UTC month, including undated requests.",
        ),
    ):
        records = _list(trends.get(key))
        if records:
            charts.append(
                _chart_svg(f"usage-{key}", title, description, records, "request_count")
            )
    undated = _mapping(trends.get("undated"))
    note = ""
    if _number(undated.get("request_count")):
        note = _section_note(
            f"{_fmt_int(undated.get('request_count'))} request records have no timestamp and appear only in monthly totals."
        )
    return (
        '<section id="usage-trends"><h2>Request activity</h2>'
        + _section_note(
            "Ralph request records include review and follow-up retries and failed requests; duplicate response usage is counted once. All periods use UTC."
        )
        + '<div class="cards usage-cards">'
        + "".join(cards)
        + "</div>"
        + note
        + '<div class="charts">'
        + "".join(charts)
        + "</div></section>"
    )


def _usage_section(summary):
    usage = _mapping(summary.get("usage"))
    current = _mapping(usage.get("current_month"))
    lifetime = _mapping(usage.get("lifetime"))
    return (
        '<section id="requests"><h2>Review usage</h2>'
        + _section_note(
            "Durable Ralph ledgers include retries and failed requests; duplicate response usage is counted once. "
            "Follow-up assessments appear separately. "
            "Token totals sum reported fields; partial means some requests omitted that field. "
            "Reasoning tokens are part of output tokens, not an additional total. "
            "Failed requests include timeouts and other uncertain outcomes; 429 rate limits are a subset. "
            "Model family identifies the provider, not the account or authentication method."
        )
        + '<div class="split"><div><h3>Current month</h3>'
        + _usage_rows(current.get("providers"))
        + "</div><div><h3>Lifetime</h3>"
        + _usage_rows(lifetime.get("providers"))
        + "</div></div></section>"
    )


def _stage_filename(stage):
    identity = json.dumps(
        [stage.get("stage"), stage.get("configured_model")], ensure_ascii=True
    )
    return "agent-" + hashlib.sha256(identity.encode()).hexdigest()[:20] + ".html"


def _stage_label(stage, prefix=""):
    return (
        _link(prefix + _stage_filename(stage), stage.get("stage", "unknown"))
        + '<span class="subline">'
        + _escape(stage.get("configured_model", "unknown"))
        + "</span>"
    )


def _data_table(headers, rows):
    numeric = {
        "Accepted",
        "Unique",
        "Shared",
        "Completion",
        "Executed",
        "Failed",
        "Findings",
        "Sole accepted",
        "Shared accepted",
    }
    return (
        "<table><thead><tr>"
        + "".join(
            '<th scope="col"'
            + (' class="numeric"' if h in numeric else "")
            + f">{_escape(h)}</th>"
            for h in headers
        )
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _cells(values):
    return (
        "<tr>"
        + "".join(
            (
                '<td class="numeric">'
                if value
                and "<" not in value
                and (value[0].isdigit() or value[0] == "$" or value == "N/A")
                else "<td>"
            )
            + value
            + "</td>"
            for value in values
        )
        + "</tr>"
    )


def _interactive_table(headers, rows, label, filters=()):
    options = "".join(
        f'<option value="{_escape(value)}">{_escape(text)}</option>'
        for value, text in filters
    )
    select = (
        (
            '<label>Show <select data-filter><option value="">All</option>'
            + options
            + "</select></label>"
        )
        if filters
        else ""
    )
    return (
        '<div class="data-list"><div class="list-controls" hidden>'
        f'<label>Search {_escape(label)} <input type="search" data-search></label>'
        + select
        + "</div>"
        + _data_table(headers, rows)
        + '<div class="pagination" hidden><button type="button" data-prev>Previous</button>'
        + '<span data-page role="status" aria-live="polite"></span>'
        + '<button type="button" data-next>Next</button></div></div>'
    )


def _stage_findings_details(stage, repository_url, report_base_url):
    findings = [item for item in _list(stage.get("findings")) if isinstance(item, dict)]
    if not findings:
        return "<p>No candidate findings were recorded for this agent.</p>"
    rows = []
    for finding in sorted(findings, key=_review_sort_key, reverse=True):
        decision = finding.get("disposition", "undisposed")
        accepted = finding.get("accepted")
        group = "accepted" if accepted else decision
        attribution = (
            "Accepted, " + str(finding.get("attribution") or "unknown")
            if accepted
            else "Not accepted"
        )
        title = _escape(finding.get("title", "unknown"))
        location = ":".join(
            str(finding[k]) for k in ("path", "line") if finding.get(k) is not None
        )
        title += '<span class="subline">' + _escape(location) + "</span>"
        title += (
            '<span class="subline">'
            + _escape(finding.get("candidate_id", "unknown"))
            + "</span>"
        )
        row = _cells(
            [
                _pr_link(finding, repository_url),
                title,
                _escape(decision),
                _escape(attribution),
                _report_link(finding, report_base_url),
            ]
        )
        rows.append(row.replace("<tr>", f'<tr data-filter="{_escape(group)}">', 1))
    return (
        '<p class="note">Candidate rows can merge into one accepted finding. Publish is the verifier '
        "decision; accepted attribution is recorded separately.</p>"
        + _interactive_table(
            ["PR", "Finding", "Verifier decision", "Attribution", "Report"],
            rows,
            "findings",
            [
                ("accepted", "Accepted"),
                ("drop", "Dropped"),
                ("unresolved", "Unresolved"),
                ("undisposed", "Unassessed"),
                ("publish", "Published without accepted attribution"),
            ],
        )
    )


def _stage_section(summary, repository_url, report_base_url):
    stages = [
        stage for stage in _list(summary.get("stages")) if isinstance(stage, dict)
    ]
    rows = []
    for stage in stages:
        statuses = _mapping(stage.get("status_counts"))
        ledger = _mapping(stage.get("ledger"))
        rows.append(
            _cells(
                [
                    _stage_label(stage),
                    _fmt_int(stage.get("executed_runs")),
                    _fmt_percent(
                        statuses.get("completed", 0), stage.get("executed_runs")
                    ),
                    _fmt_int(statuses.get("failed", 0)),
                    _fmt_int(stage.get("accepted_findings")),
                    _fmt_int(ledger.get("request_count")),
                ]
            )
        )
    return (
        '<section><h2>All agents</h2><p class="note">Choose an agent to inspect its findings, '
        "coverage and usage. Completion is completed stages divided by executed runs; "
        "skipped stages are excluded.</p>"
        + (
            _interactive_table(
                [
                    "Agent / model",
                    "Executed",
                    "Completion",
                    "Failed",
                    "Accepted",
                    "Records",
                ],
                rows,
                "agents",
            )
            if rows
            else "<p>No agent stage statistics were recorded.</p>"
        )
        + "</section>"
    )


def _leaderboard_section(summary):
    leaders = [
        item
        for item in _list(summary.get("stage_model_leaderboard"))
        if isinstance(item, dict)
    ]
    stages = {
        (stage.get("stage"), stage.get("configured_model")): stage
        for stage in _list(summary.get("stages"))
        if isinstance(stage, dict)
    }
    max_accepted = max(
        (_number(item.get("accepted_findings")) or 0 for item in leaders), default=0
    )
    rows = []
    for leader in leaders:
        stage = stages.get((leader.get("stage"), leader.get("configured_model")))
        statuses = _mapping(stage.get("status_counts")) if stage else {}
        label = (
            _stage_label(leader, "agents/")
            if stage
            else _escape(leader.get("stage", "unknown"))
        )
        accepted = _number(leader.get("accepted_findings"))
        meter = ""
        if max_accepted and accepted is not None:
            meter = (
                f'<meter min="0" max="{max_accepted}" value="{accepted}" '
                'aria-label="Accepted findings"></meter>'
            )
        rows.append(
            _cells(
                [
                    label,
                    f'<span class="finding-score">{meter}{_fmt_int(accepted)}</span>',
                    _fmt_int(leader.get("sole_source_findings")),
                    _fmt_int(leader.get("shared_findings")),
                    _fmt_percent(
                        statuses.get("completed", 0) if stage else None,
                        _field(stage, "executed_runs"),
                    ),
                ]
            )
        )
    return (
        '<section id="leaderboard"><h2>Agent leaderboard</h2>'
        '<p class="note">Ranked by verifier-accepted findings across saved reviews. Unique means '
        "one contributing agent; shared findings count for each contributor. Different agents "
        "may review different PRs.</p>"
        + (
            _data_table(
                [
                    "Agent / model",
                    "Accepted",
                    "Unique",
                    "Shared",
                    "Completion",
                ],
                rows,
            )
            if rows
            else "<p>No accepted finding leaderboard was recorded.</p>"
        )
        + "<p>"
        + _internal_link("agents/", "Explore all agents →")
        + "</p></section>"
    )


def _paired_section(summary):
    paired = _mapping(summary.get("adversarial_comparison"))
    names = sorted(
        set(_mapping(paired.get("sole_accepted")))
        | set(_mapping(paired.get("shared_accepted")))
    )
    if not names:
        body = "<p>No adversarial model comparison was recorded.</p>"
    else:
        rows = []
        sole = _mapping(paired.get("sole_accepted"))
        shared = _mapping(paired.get("shared_accepted"))
        for name in names:
            rows.append(
                _cells(
                    [
                        _escape(name),
                        _fmt_int(sole.get(name)),
                        _fmt_int(shared.get(name)),
                    ]
                )
            )
        body = _data_table(
            [
                "Model group",
                "Sole accepted",
                "Shared accepted",
            ],
            rows,
        )
    note = _field(
        paired,
        "note",
        default="Observed reviews with completed adversarial pairs only; this is not recall or causality.",
    )
    return (
        '<section id="paired"><h2>Adversarial model comparison</h2>'
        + _section_note(note)
        + f"<p>Completed reviews: {_fmt_int(_field(paired, 'completed_reviews'))}</p>"
        + body
        + "</section>"
    )


def _model_section(summary):
    models = _list(_mapping(summary.get("usage")).get("models"))
    if not models:
        body = "<p>No model usage was recorded.</p>"
    else:
        rows = []
        for model in models:
            rows.append(
                "<tr>"
                f"<td>{_escape(_field(model, 'provider', default='unknown'))}</td>"
                f"<td>{_escape(_field(model, 'model', default='unknown'))}</td>"
                f"<td>{_fmt_int(_field(model, 'request_count'))}</td>"
                f"<td>{_fmt_tokens(model, 'input')}</td>"
                f"<td>{_fmt_tokens(model, 'cached')}</td>"
                f"<td>{_cache_rate(model)}</td>"
                f"<td>{_fmt_tokens(model, 'output')}</td>"
                f"<td>{_fmt_tokens(model, 'reasoning')}</td>"
                f"<td>{_fmt_int(_failed_requests(model))}</td>"
                f"<td>{_fmt_int(model.get('rate_limit_count'))}</td>"
                "</tr>"
            )
        body = (
            "<table><thead><tr><th>Provider</th><th>Model</th><th>Attempts</th>"
            "<th>Input tokens</th><th>Cached tokens</th>"
            "<th>Cache rate</th><th>Output tokens</th><th>Reasoning tokens</th>"
            "<th>Failed requests</th><th>Rate limits (429)</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )
    return '<section id="models"><h2>Model usage</h2>' + body + "</section>"


def _routing_section(summary):
    tiers = summary.get("routing_tiers")
    rows = []
    for tier in _record_rows(tiers):
        rows.append(
            "<tr>"
            f"<td>{_escape(_field(tier, 'name', default='unknown'))}</td>"
            f"<td>{_fmt_int(_field(tier, 'count'))}</td>"
            "</tr>"
        )
    body = (
        "<p>No routing tier statistics were recorded.</p>"
        if not rows
        else "<table><thead><tr><th>Tier</th><th>Reviews</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )
    return '<section id="routing"><h2>Routing outcomes</h2>' + body + "</section>"


def _distribution_section(summary):
    distributions = _mapping(summary.get("distributions"))
    if not distributions:
        return '<section id="distributions"><h2>Review distributions</h2><p>No distributions were recorded.</p></section>'
    labels = {
        "saved_review_tools": "Tool calls",
        "saved_review_model_seconds": "Model seconds",
        "saved_review_conceptual_concern_citations": "Conceptual concern citations",
        "saved_review_conceptual_concern_alternatives": "Conceptual concern alternatives",
    }
    rows = []
    for key, label in labels.items():
        record = _mapping(distributions.get(key))
        formatter = _fmt_decimal
        rows.append(
            "<tr>"
            f"<td>{_escape(label)}</td>"
            f"<td>{_fmt_int(_field(record, 'count'))}</td>"
            f"<td>{formatter(_field(record, 'median'))}</td>"
            f"<td>{formatter(_field(record, 'p90'))}</td>"
            f"<td>{formatter(_field(record, 'max'))}</td>"
            "</tr>"
        )
    return (
        '<section id="distributions"><h2>Review distributions</h2>'
        "<table><thead><tr><th>Metric</th><th>Count</th><th>Median</th><th>P90</th><th>Max</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></section>"
    )


def _review_sort_key(review):
    number = _number(_field(review, "job_id"))
    return number if number is not None else -1


def _sorted_reviews(summary):
    reviews = [
        review for review in _list(summary.get("reviews")) if isinstance(review, dict)
    ]
    return sorted(reviews, key=_review_sort_key, reverse=True)


def _reviews_table(reviews, repository_url, report_base_url):
    if not reviews:
        return "<p>No saved reviews were recorded.</p>"
    rows = []
    for review in reviews:
        findings = _mapping(review.get("findings"))
        ledger = _mapping(review.get("ledger"))
        archaeology = _mapping(review.get("archaeology"))
        detail = _key_value_table(
            {
                "Generation": review.get("generation", "N/A"),
                "Historical context": review.get("context_coverage", "unknown"),
                "Verification": review.get("verification_status", "unknown"),
                "Saved findings": findings.get("saved", "N/A"),
                "Concept": archaeology.get("status", "N/A"),
                "Concept published": archaeology.get("published_concern", False),
                "Research complete": archaeology.get("research_completed", False),
                "Citations": archaeology.get("citation_count", "N/A"),
                "Alternatives": archaeology.get("alternative_count", "N/A"),
                "Request records": ledger.get("request_count", "N/A"),
                "Failed/retries": review.get("recorded_failed_attempts", "N/A"),
                "Input tokens": _fmt_tokens(ledger, "input"),
                "Cached tokens": _fmt_tokens(ledger, "cached"),
                "Output tokens": _fmt_tokens(ledger, "output"),
            }
        )
        rows.append(
            _cells(
                [
                    _pr_link(review, repository_url)
                    + '<span class="subline">Job '
                    + _fmt_int(review.get("job_id"))
                    + "</span>",
                    _escape(review.get("review_status", "unknown")),
                    _escape(review.get("status", "saved")),
                    _escape(review.get("coverage", "unknown")),
                    "unsupported"
                    if findings.get("export_status") == "unsupported"
                    else _fmt_int(findings.get("published")),
                    _fmt_int(ledger.get("request_count")),
                    _report_link(review, report_base_url),
                    "<details><summary>Details</summary>" + detail + "</details>",
                ]
            )
        )
    return _interactive_table(
        [
            "Review",
            "Review status",
            "Delivery",
            "Coverage",
            "Findings",
            "Records",
            "Report",
            "Details",
        ],
        rows,
        "reviews",
    )


def _valid_period(value):
    if not isinstance(value, str) or len(value) != 10:
        return False
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") == value
    except ValueError:
        return False


def _time_series(summary):
    records = []
    for record in _list(summary.get("time_series")):
        if not isinstance(record, dict):
            continue
        period = _field(record, "period")
        if not _valid_period(period):
            continue
        records.append(
            {
                "period": period,
                "reviews": max(0, int(_number(_field(record, "reviews")) or 0)),
                "verified_reviews": (
                    max(0, int(_number(_field(record, "verified_reviews"))))
                    if _number(_field(record, "verified_reviews")) is not None
                    else None
                ),
                "findings": (
                    max(0, int(_number(_field(record, "findings"))))
                    if _number(_field(record, "findings")) is not None
                    else None
                ),
                "addressed": max(0, int(_number(_field(record, "addressed")) or 0)),
            }
        )
    return sorted(records, key=lambda item: item["period"])


def _chart_svg(chart_id, title, description, records, field, *, bars=False):
    width = 280
    height = 132
    right = 12
    top = 12
    bottom = 30
    formatter = _fmt_int
    values = [_number(record.get(field)) for record in records]
    max_value = max((value for value in values if value is not None), default=0) or 1
    left = max(34, len(formatter(max_value)) * 6 + 12)
    x_span = width - left - right
    y_span = height - top - bottom
    points = []
    for index, value in enumerate(values):
        if value is None:
            points.append(None)
            continue
        x = (
            left + x_span / 2
            if len(records) == 1
            else left + x_span * index / (len(records) - 1)
        )
        points.append((x, top + y_span * (1 - value / max_value)))
    segments = []
    segment = []
    for point in points + [None]:
        if point is None:
            if len(segment) > 1:
                segments.append(" ".join(f"{x:.1f},{y:.1f}" for x, y in segment))
            segment = []
        else:
            segment.append(point)
    lines = "".join(
        f'<polyline class="chart-line" points="{segment}"></polyline>'
        for segment in segments
    )
    marks = []
    bar_width = min(12, x_span / max(len(records), 1) * 0.7)
    for index, point in enumerate(points):
        if point is None:
            continue
        record = records[index]
        tooltip = f"{record['period']}: {formatter(record.get(field))}"
        x, y = point
        if bars:
            marks.append(
                f'<rect class="chart-bar" x="{x - bar_width / 2:.1f}" y="{y:.1f}" '
                f'width="{bar_width:.1f}" height="{top + y_span - y:.1f}">'
                f"<title>{_escape(tooltip)}</title></rect>"
            )
        else:
            marks.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{2 if len(records) > 30 else 3}">'
                f"<title>{_escape(tooltip)}</title></circle>"
            )
    first = records[0]["period"]
    last = records[-1]["period"]
    title_id = f"{chart_id}-title"
    desc_id = f"{chart_id}-desc"
    return (
        '<figure class="chart">'
        f"<figcaption>{_escape(title)}</figcaption>"
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-labelledby="{title_id} {desc_id}">'
        f'<title id="{title_id}">{_escape(title)}</title>'
        f'<desc id="{desc_id}">{_escape(description)}</desc>'
        f'<line class="chart-axis" x1="{left}" y1="{top + y_span}" x2="{width - right}" y2="{top + y_span}"></line>'
        f'<line class="chart-axis" x1="{left}" y1="{top}" x2="{left}" y2="{top + y_span}"></line>'
        + ("" if bars else lines)
        + "".join(marks)
        + f'<text x="{left}" y="{height - 10}">{_escape(first)}</text>'
        + f'<text x="{width - right}" y="{height - 10}" text-anchor="end">{_escape(last)}</text>'
        + f'<text x="{left - 6}" y="{top + 4}" text-anchor="end">{formatter(max_value)}</text>'
        + f'<text x="{left - 6}" y="105" text-anchor="end">{formatter(0)}</text>'
        "</svg>"
        "</figure>"
    )


def _time_series_notes(summary):
    notes = [
        note
        for note in _list(summary.get("time_series_notes"))
        if isinstance(note, str) and note
    ]
    undated = _mapping(summary.get("time_series_undated"))
    excluded = []
    if _number(undated.get("reviews")):
        excluded.append(f"{_fmt_int(undated.get('reviews'))} reviews")
    if _number(undated.get("addressed")):
        excluded.append(f"{_fmt_int(undated.get('addressed'))} addressed findings")
    if excluded:
        notes.append(f"Undated records are excluded: {', '.join(excluded)}.")
    return "".join(f'<p class="note">{_escape(note)}</p>' for note in notes)


def _cumulative_series(records):
    totals = dict.fromkeys(("reviews", "verified_reviews", "findings", "addressed"), 0)
    cumulative = []
    for record in records:
        for field in totals:
            value = record[field]
            totals[field] = (
                totals[field] + value
                if totals[field] is not None and value is not None
                else None
            )
        cumulative.append({"period": record["period"], **totals})
    return cumulative


def _charts_section(summary):
    records = _time_series(summary)
    if not records:
        return (
            '<section id="trends"><h2>Production trends</h2>'
            "<p>No daily review history is available yet.</p>"
            + _time_series_notes(summary)
            + "</section>"
        )
    charts = [
        ("reviews", "Reviews", "Saved reviews", "reviews"),
        (
            "verified-reviews",
            "Current-engine verified reviews",
            "Current-engine reviews whose verifier pass completed",
            "verified_reviews",
        ),
        (
            "findings",
            "Exported findings",
            "Findings from supported engine exports",
            "findings",
        ),
        (
            "addressed",
            "Addressed findings",
            "Addressed findings, dated by the assessed PR head commit",
            "addressed",
        ),
    ]
    panels = []
    for mode, values in (
        ("daily", records),
        ("cumulative", _cumulative_series(records)),
    ):
        description_suffix = (
            " per UTC day." if mode == "daily" else " accumulated through each UTC day."
        )
        body = "".join(
            _chart_svg(
                f"chart-{mode}-{chart_id}",
                title,
                description + description_suffix,
                values,
                field,
                bars=mode == "daily",
            )
            for chart_id, title, description, field in charts
        )
        note = (
            "Daily counts, UTC. Days without recorded activity are zero."
            if mode == "daily"
            else "Running totals of dated records through each day, starting at the beginning of the displayed history."
        )
        panels.append(
            f'<div id="trends-{mode}" data-trend-panel="{mode}"'
            + (" hidden" if mode == "cumulative" else "")
            + f'>{_section_note(note)}<div class="charts">{body}</div></div>'
        )
    return (
        '<section id="trends"><div class="section-heading"><h2>Production trends</h2>'
        '<div class="chart-controls" role="group" aria-label="Production trend view" hidden>'
        '<button type="button" data-trend-mode="daily" aria-controls="trends-daily" aria-pressed="true">Daily</button>'
        '<button type="button" data-trend-mode="cumulative" aria-controls="trends-cumulative" aria-pressed="false">Cumulative</button>'
        "</div></div>"
        + "".join(panels)
        + '<details class="trend-notes"><summary>Dates and missing data</summary>'
        + _section_note(
            "Undated records are excluded. Addressed findings use the PR head commit date. "
            "Missing daily counts leave gaps and prevent subsequent cumulative totals for that metric."
        )
        + _time_series_notes(summary)
        + "</details></section>"
    )


def _style():
    return """
:root { color-scheme: dark; --bg: #111313; --surface: #191c1c; --raised: #222727; --text: #e8ece8; --muted: #9ca9a3; --border: #303836; --accent: #75dbb1; }
:root[data-theme="light"] { color-scheme: light; --bg: #f3f5f2; --surface: #ffffff; --raised: #eaf0eb; --text: #23392f; --muted: #596d60; --border: #d6e0d8; --accent: #187454; }
* { box-sizing: border-box; }
body { margin: 0; color: var(--text); background: var(--bg); font: 14px/1.55 system-ui, sans-serif; }
a { color: var(--accent); text-underline-offset: .18em; }
a:hover { color: var(--text); }
header { position: fixed; inset: 0 auto 0 0; width: 14rem; background: var(--surface); border-right: 1px solid var(--border); padding: 2rem 1.1rem; display: flex; flex-direction: column; z-index: 5; overflow-y: auto; }
.brand { display: flex; align-items: center; gap: .75rem; font-size: 1.3rem; font-weight: 750; text-decoration: none; color: var(--text); letter-spacing: -.04em; }
.brand-mark { display: grid; place-items: center; width: 2.3rem; height: 2.3rem; border-radius: .65rem; background: var(--accent); color: var(--bg); font: 700 1.25rem ui-monospace, monospace; }
.brand small { display: block; color: var(--muted); font-size: .7rem; font-weight: 450; letter-spacing: .03em; }
.header-top { padding: 0 .5rem 2.5rem; }
.nav-label { color: var(--muted); text-transform: uppercase; font-size: .65rem; letter-spacing: .15em; padding: 0 .8rem; }
nav { display: grid; gap: .3rem; margin-top: .7rem; }
nav a { display: flex; align-items: center; gap: .8rem; text-decoration: none; color: var(--muted); padding: .7rem .8rem; border: 1px solid transparent; border-radius: .5rem; }
nav svg { width: 1.1rem; height: 1.1rem; fill: none; stroke: currentColor; stroke-width: 1.5; stroke-linecap: round; stroke-linejoin: round; }
nav a:hover { background: var(--raised); color: var(--text); }
nav a[aria-current] { background: var(--raised); color: var(--accent); border-color: var(--border); }
.sidebar-tools { margin-top: auto; padding: 2rem .5rem 0; display: grid; gap: .8rem; font-size: .8rem; }
.sidebar-tools a { color: var(--muted); text-decoration: none; }
main { max-width: 112rem; margin-left: 14rem; padding: 2.5rem clamp(1rem, 3vw, 3.5rem) 3rem; }
h1 { font-size: 2rem; margin: 0; letter-spacing: -.035em; }
h2 { font-size: 1.2rem; margin: 0 0 .7rem; }
h3 { font-size: 1rem; }
p { margin: .6rem 0 1rem; }
.subtitle, .note, .subline, .kpi-detail { color: var(--muted); }
.subline { display: block; font-size: .8rem; margin-top: .25rem; overflow-wrap: anywhere; }
.subtitle { margin-bottom: 1.6rem; }
.cards { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 1rem; margin: 1.5rem 0; }
.cards.usage-cards { grid-template-columns: repeat(auto-fit, minmax(min(100%, 15rem), 1fr)); }
.card, section { background: var(--surface); border: 1px solid var(--border); border-radius: .65rem; padding: 1.35rem; }
section { margin-top: 1.25rem; }
.card { position: relative; overflow: hidden; border-top: 1px solid var(--border); }
.kpi-label { color: var(--muted); font-size: .8rem; }
.kpi-value, .big { font-size: 2.4rem; letter-spacing: -.045em; font-weight: 750; font-variant-numeric: tabular-nums; }
.kpi-detail { font-size: .85rem; }
.split { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 22rem), 1fr)); gap: 1.25rem; }
.split > section { min-width: 0; }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: .9rem; }
th, td { padding: .7rem .8rem; border-bottom: 1px solid var(--border); text-align: left; vertical-align: top; }
th { font-size: .75rem; font-weight: 650; color: var(--muted); background: var(--raised); white-space: nowrap; }
tbody tr:last-child > td { border-bottom: 0; }
tbody tr:hover { background: var(--raised); }
td { font-variant-numeric: tabular-nums; }
.numeric { text-align: right; white-space: nowrap; }
td:first-child { min-width: 5rem; }
.key-values { display: grid; grid-template-columns: minmax(8rem, 1fr) 1fr; gap: .6rem 1rem; margin: .5rem 0; min-width: 15rem; }
.key-values dt { color: var(--muted); }
.key-values dd { margin: 0; overflow-wrap: anywhere; }
details { margin: .2rem 0; }
summary { cursor: pointer; color: var(--accent); font-weight: 600; }
details[open] > summary { margin-bottom: .75rem; }
.list-controls, .pagination { display: flex; gap: 1rem; align-items: end; flex-wrap: wrap; margin: 1rem 0; }
.list-controls label { display: flex; flex-direction: column; gap: .35rem; font-size: .8rem; color: var(--muted); }
input, select, button { font: inherit; padding: .5rem .7rem; border: 1px solid var(--border); background: var(--surface); color: var(--text); border-radius: .35rem; }
input { width: min(23rem, 100%); }
button { cursor: pointer; }
button:disabled { opacity: .45; cursor: default; }
a:focus-visible, summary:focus-visible, button:focus-visible, input:focus-visible, select:focus-visible { outline: 3px solid var(--accent); outline-offset: 3px; }
.pagination { justify-content: flex-end; font-size: .85rem; }
[hidden] { display: none !important; }
footer { margin-left: 14rem; padding: 0 1.2rem 2rem; color: var(--muted); font-size: .8rem; }
.preview { background: #fef3c7; color: #624500; padding: .5rem; }
.charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 15rem), 1fr)); gap: 1rem; }
.chart { margin: 0; padding: 1rem; background: var(--bg); border: 1px solid var(--border); border-radius: .6rem; }
.chart figcaption { font-weight: 600; }
.chart svg { display: block; width: 100%; height: auto; }
.chart text { fill: var(--muted); font-size: 10px; }
.chart-axis { stroke: var(--border); }
.chart-line { fill: none; stroke: var(--accent); stroke-width: 3; }
.chart circle, .chart-bar { fill: var(--accent); }
.section-heading { display: flex; justify-content: space-between; align-items: center; gap: 1rem; flex-wrap: wrap; }
.section-heading h2 { margin: 0; }
.chart-controls { display: flex; gap: .2rem; padding: .2rem; background: var(--bg); border: 1px solid var(--border); border-radius: .5rem; }
.chart-controls button { border-color: transparent; background: transparent; padding: .3rem .7rem; font-size: .8rem; color: var(--muted); }
.chart-controls button[aria-pressed="true"] { background: var(--raised); color: var(--accent); }
.trend-notes { margin-top: 1rem; font-size: .8rem; }
#trends figcaption { min-height: 3.1em; }
.card::before { content: ""; position: absolute; top: 1.4rem; left: 0; width: 3px; height: 2rem; background: var(--accent); }
.card:nth-child(2)::before { background: #b4a2ee; }
.card:nth-child(3)::before { background: #e7bb7c; }
.card:nth-child(4)::before { background: #83bce4; }
.kpi-label { text-transform: uppercase; letter-spacing: .08em; font-size: .67rem; }
h2 { letter-spacing: -.02em; }
.note { font-size: .85rem; max-width: 85rem; }
#leaderboard td:first-child { width: 38%; }
.finding-score { display: flex; align-items: center; justify-content: flex-end; gap: .8rem; font-variant-numeric: tabular-nums; }
.finding-score meter { width: 5rem; height: .45rem; }
meter { appearance: none; border: 0; border-radius: 1rem; background: var(--raised); }
meter::-webkit-meter-bar { background: var(--raised); border: 0; }
meter::-webkit-meter-optimum-value { background: var(--accent); border-radius: 1rem; }
meter::-moz-meter-bar { background: var(--accent); border-radius: 1rem; }
.quota-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 19rem), 1fr)); gap: 1rem; margin: 1.2rem 0; }
.quota-card { padding: 1.2rem; border: 1px solid var(--border); background: var(--bg); border-radius: .65rem; }
.quota-heading { display: flex; justify-content: space-between; align-items: center; gap: 1rem; margin-bottom: 1rem; }
.quota-heading h3 { margin: 0; text-transform: capitalize; font-size: 1.05rem; }
.plan { border: 1px solid var(--border); border-radius: 2rem; padding: .15rem .6rem; font-size: .75rem; color: var(--muted); }
.quota-window { margin: 1rem 0; }
.quota-label { display: flex; justify-content: space-between; gap: .8rem; font-size: .85rem; }
.quota-window meter { display: block; width: 100%; height: .4rem; margin: .5rem 0; }
.quota-window small { color: var(--muted); font: .7rem ui-monospace, monospace; }
.quota-window.low meter::-webkit-meter-optimum-value { background: #e7bb7c; }
.quota-window.low meter::-moz-meter-bar { background: #e7bb7c; }
.sort-button { padding: 0; border: 0; border-radius: 0; background: transparent; color: inherit; font: inherit; }
.sort-button::after { content: " ↕"; opacity: .55; }
[aria-sort="ascending"] .sort-button::after { content: " ↑"; opacity: 1; }
[aria-sort="descending"] .sort-button::after { content: " ↓"; opacity: 1; }
.skip-link { position: fixed; top: -5rem; left: 1rem; z-index: 10; background: var(--surface); padding: .8rem; }
.skip-link:focus { top: .5rem; }
@media (min-width: 1800px) { main { margin-right: auto; } }
@media (max-width: 1000px) { .cards { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 750px) {
  header { position: static; width: auto; padding: 1rem; border-right: 0; border-bottom: 1px solid var(--border); }
  .header-top { padding: 0 0 .75rem; }
  .nav-label { display: none; }
  nav { display: flex; overflow-x: auto; gap: .2rem; }
  nav a { padding: .5rem .65rem; flex-shrink: 0; }
  nav svg { display: none; }
  .sidebar-tools { padding: .8rem 0 0; display: flex; align-items: center; justify-content: space-between; }
  main { margin: 0; padding: 1.5rem 1rem; }
  footer { margin-left: 0; }
  section, .card { padding: 1rem; }
  .cards { gap: .65rem; }
  .kpi-value { font-size: 2rem; }
  .finding-score meter { display: none; }
}
"""


def _script():
    return """
const trendControls = document.querySelector('.chart-controls');
if (trendControls) {
  trendControls.hidden = false;
  trendControls.querySelectorAll('[data-trend-mode]').forEach(button => {
    button.addEventListener('click', () => {
      trendControls.querySelectorAll('button').forEach(other => {
        other.setAttribute('aria-pressed', String(other === button));
      });
      document.querySelectorAll('[data-trend-panel]').forEach(panel => {
        panel.hidden = panel.dataset.trendPanel !== button.dataset.trendMode;
      });
    });
  });
}
const themeToggle = document.querySelector('[data-theme-toggle]');
if (themeToggle) {
  function setTheme(theme) {
    document.documentElement.dataset.theme = theme;
    themeToggle.textContent = theme === 'dark' ? 'Light theme' : 'Dark theme';
  }
  let theme = 'dark';
  try { theme = localStorage.getItem('ralph-theme') === 'light' ? 'light' : 'dark'; } catch {}
  setTheme(theme);
  themeToggle.hidden = false;
  themeToggle.addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    setTheme(next);
    try { localStorage.setItem('ralph-theme', next); } catch {}
  });
}
const leaderboard = document.querySelector('#leaderboard table');
if (leaderboard) {
  const headers = [...leaderboard.querySelectorAll('th')];
  headers.forEach((header, column) => {
    const button = document.createElement('button');
    button.className = 'sort-button';
    button.type = 'button';
    button.textContent = header.textContent;
    header.textContent = '';
    header.append(button);
    button.addEventListener('click', () => {
      const ascending = header.getAttribute('aria-sort') === 'descending';
      headers.forEach(h => h.removeAttribute('aria-sort'));
      header.setAttribute('aria-sort', ascending ? 'ascending' : 'descending');
      const rows = [...leaderboard.tBodies[0].rows];
      rows.sort((a, b) => {
        const x = a.cells[column].textContent.trim();
        const y = b.cells[column].textContent.trim();
        const nx = Number.parseFloat(x.replaceAll(',', ''));
        const ny = Number.parseFloat(y.replaceAll(',', ''));
        if (column && (Number.isNaN(nx) || Number.isNaN(ny))) {
          return Number.isNaN(nx) - Number.isNaN(ny);
        }
        return (ascending ? 1 : -1) * (column ? nx - ny : x.localeCompare(y));
      });
      leaderboard.tBodies[0].append(...rows);
    });
  });
}
document.querySelectorAll('.data-list').forEach(list => {
  const rows = [...list.querySelector('tbody').children];
  const search = list.querySelector('[data-search]');
  const filter = list.querySelector('select[data-filter]');
  const previous = list.querySelector('[data-prev]');
  const next = list.querySelector('[data-next]');
  const status = list.querySelector('[data-page]');
  const searchable = rows.map(row => row.textContent.toLocaleLowerCase());
  const size = 25;
  let page = 0;
  function render() {
    const query = search.value.trim().toLocaleLowerCase();
    const matches = rows.filter((row, index) => searchable[index].includes(query)
      && (!filter || !filter.value || row.dataset.filter === filter.value));
    page = Math.min(page, Math.max(0, Math.ceil(matches.length / size) - 1));
    rows.forEach(row => { row.hidden = true; });
    matches.slice(page * size, (page + 1) * size).forEach(row => { row.hidden = false; });
    status.textContent = matches.length
      ? `${page * size + 1}–${Math.min((page + 1) * size, matches.length)} of ${matches.length}`
      : 'No matching results';
    previous.disabled = page === 0;
    next.disabled = (page + 1) * size >= matches.length;
  }
  search.addEventListener('input', () => { page = 0; render(); });
  if (filter) filter.addEventListener('change', () => { page = 0; render(); });
  previous.addEventListener('click', () => { page--; render(); });
  next.addEventListener('click', () => { page++; render(); });
  list.querySelector('.list-controls').hidden = false;
  list.querySelector('.pagination').hidden = false;
  render();
});
"""


def _wrap_tables(html_text):
    return html_text.replace(
        "<table>",
        '<div class="table-wrap" tabindex="0" role="region" aria-label="Scrollable data table"><table>',
    ).replace("</table>", "</table></div>")


def _header(summary, page_prefix="", active="Overview"):
    links = [
        ("index.html", "Overview"),
        ("agents/", "Agents"),
        ("reviews/", "Reviews"),
        ("addressed/", "Outcomes"),
        ("usage/", "Usage"),
        ("limits/", "Limits"),
    ]
    icons = {
        "Overview": "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
        "Agents": "M8 21v-3a4 4 0 0 1 8 0v3 M12 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M3 18v-2a4 4 0 0 1 3-4 M21 18v-2a4 4 0 0 0-3-4",
        "Reviews": "M5 3h14v18H5z M8 7h8 M8 11h8 M8 15h5",
        "Outcomes": "M4 12l5 5L20 6",
        "Usage": "M3 20h18 M6 16v-5 M12 16V4 M18 16V8",
        "Limits": "M4 7h16 M4 17h16 M8 4v6 M16 14v6",
    }
    nav = "".join(
        f'<a href="{page_prefix}{path}"'
        + (' aria-current="page"' if label == active else "")
        + f'><svg viewBox="0 0 24 24" aria-hidden="true"><path d="{icons[label]}"/></svg>{label}</a>'
        for path, label in links
    )
    preview = _field(summary, "preview_notice", "")
    return (
        '<header><div class="header-top">'
        + f'<a class="brand" href="{page_prefix}index.html"><span class="brand-mark" aria-hidden="true">r.</span>'
        + "<span>ralph<small>Review statistics</small></span></a></div>"
        + (f'<p class="preview">{_escape(preview)}</p>' if preview else "")
        + '<div class="nav-label">Workspace</div><nav aria-label="Statistics">'
        + nav
        + '</nav><div class="sidebar-tools">'
        + '<button type="button" data-theme-toggle hidden>Switch theme</button>'
        + f'<a href="{page_prefix}stats.json" download>Download JSON ↓</a></div></header>'
    )


def _page(title, summary, body, page_prefix="", active="Overview"):
    return _wrap_tables(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_escape(title)} · ralph stats</title><style>{_style()}</style><script src="{page_prefix}stats.js" defer></script></head>
<body><a class="skip-link" href="#main">Skip to content</a>{_header(summary, page_prefix, active)}<main id="main">{body}</main>
<footer>Generated {_escape(_generated_at(summary))}. {_escape(_source_status(summary))}</footer>
</body></html>
""")


def _key_value_table(values):
    return (
        '<dl class="key-values">'
        + "".join(
            f"<dt>{_escape(key)}</dt><dd>{_escape(value)}</dd>"
            for key, value in values.items()
        )
        + "</dl>"
    )


def _html(summary, repository_url, report_base_url):
    body = (
        '<h1>Overview</h1><p class="subtitle">Review activity and agent performance at a glance.</p>'
        + f'<div class="cards">{_top_kpis(summary)}</div>'
        + _leaderboard_section(summary)
        + _paired_section(summary)
    )
    return _page("Overview", summary, body)


def _agents_html(summary, repository_url, report_base_url):
    body = (
        '<h1>Agents</h1><p class="subtitle">Findings and reliability by stage and configured model.</p>'
        + _stage_section(summary, repository_url, report_base_url)
        + "<section><h2>Coverage snapshot</h2>"
        + _section_note(
            "Code review, historical context and verification are separate dimensions. "
            "Complete is a reported coverage status, not a guarantee of exhaustive review."
        )
        + _coverage_table(summary)
        + "</section>"
    )
    return _page("Agents", summary, body, "../", "Agents")


def _stage_html(summary, stage, repository_url, report_base_url):
    ledger = _mapping(stage.get("ledger"))
    candidates = _mapping(stage.get("candidate_counts"))
    statuses = _mapping(stage.get("status_counts"))
    body = (
        "<p>"
        + _internal_link("index.html", "← All agents")
        + "</p><h1>"
        + _escape(stage.get("stage", "unknown"))
        + '</h1><p class="subtitle">'
        + _escape(stage.get("configured_model", "unknown"))
        + "</p>"
        + '<div class="cards">'
        + _kpi(
            "Accepted findings",
            _fmt_int(stage.get("accepted_findings")),
            f"{_fmt_int(stage.get('sole_source_findings'))} unique; {_fmt_int(stage.get('shared_findings'))} shared",
        )
        + _kpi(
            "Completion",
            _fmt_percent(statuses.get("completed", 0), stage.get("executed_runs")),
            f"{_fmt_int(stage.get('executed_runs'))} executed runs; skipped excluded",
        )
        + _kpi(
            "Request records",
            _fmt_int(ledger.get("request_count")),
            "Includes reruns and retries",
        )
        + _kpi(
            "Input tokens",
            _fmt_tokens(ledger, "input"),
        )
        + "</div><section><h2>Candidate findings</h2>"
        + _stage_findings_details(stage, repository_url, report_base_url)
        + "</section>"
        + '<div class="split"><section><h2>Run status</h2>'
        + _key_value_table({k: _fmt_int(v) for k, v in statuses.items()})
        + _key_value_table(
            {
                "Saved runs": _fmt_int(stage.get("saved_result_runs")),
                "Executed runs": _fmt_int(stage.get("executed_runs")),
            }
        )
        + "</section>"
        + "<section><h2>Reported coverage</h2>"
        + _key_value_table(
            {k: _fmt_int(v) for k, v in _mapping(stage.get("coverage_counts")).items()}
        )
        + "</section><section><h2>Candidate decisions</h2>"
        + _key_value_table({k: _fmt_int(v) for k, v in candidates.items()})
        + _key_value_table(
            {
                "Selected among assessed": _fmt_percent(
                    candidates.get("publish"), _candidate_denominator(candidates)
                ),
                "Accepted / executed run": _fmt_decimal(
                    stage.get("accepted_per_executed_run"), 3
                ),
                "Conceptual concerns": _fmt_int(stage.get("conceptual_concerns")),
            }
        )
        + '<p class="note">Selection uses publish, drop and unresolved candidates; unassessed candidates are excluded.</p></section>'
        + "<section><h2>Usage</h2>"
        + _key_value_table(
            {
                "Request records": _fmt_int(ledger.get("request_count")),
                "Input tokens": _fmt_tokens(ledger, "input"),
                "Cached tokens": _fmt_tokens(ledger, "cached"),
                "Output tokens": _fmt_tokens(ledger, "output"),
                "Unknown usage records": _fmt_int(ledger.get("unknown_usage_count")),
            }
        )
        + "</section></div>"
    )
    return _page(str(stage.get("stage", "Agent")), summary, body, "../", "Agents")


def _reviews_html(summary, repository_url, report_base_url):
    body = (
        '<h1>Reviews</h1><p class="subtitle">Search saved reviews by PR, job or review status. Newest first.</p>'
        + "<section><h2>Saved reviews</h2>"
        + _reviews_table(_sorted_reviews(summary), repository_url, report_base_url)
        + "</section>"
    )
    return _page("Reviews", summary, body, "../", "Reviews")


def _addressed_html(summary, repository_url, report_base_url):
    body = (
        '<h1>Outcomes</h1><p class="subtitle">What happened to findings after review.</p>'
        + _addressed_section(summary)
        + "<section><h2>Finding assessments</h2>"
        + _addressed_table(
            _addressed_findings(summary), repository_url, report_base_url
        )
        + "</section>"
        + _concept_section(summary)
    )
    return _page("Outcomes", summary, body, "../", "Outcomes")


def _subscription_time(value):
    if not isinstance(value, str):
        return "Unknown"
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError:
        return "Unknown"
    if timestamp.tzinfo is None:
        return "Unknown"
    return timestamp.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _subscriptions_section(summary):
    subscriptions = _mapping(summary.get("subscriptions"))
    cards = []
    for account in _list(subscriptions.get("accounts")):
        if not isinstance(account, dict):
            continue
        provider = _escape(account.get("provider", "unknown"))
        plan = _escape(account.get("plan") or "Unknown plan")
        windows = _list(account.get("windows")) if account.get("status") == "ok" else []
        credits = (
            _mapping(account.get("credits")) if account.get("status") == "ok" else {}
        )
        credit_note = ""
        if credits:
            if credits.get("unlimited") is True:
                balance = "Unlimited"
            elif credits.get("has_credits") is False:
                balance = "None"
            else:
                balance = credits.get("balance")
                balance = "Unknown" if balance is None else str(balance)
            credit_note = f'<p class="note">{provider} credits: {_escape(balance)}.</p>'
        quotas = []
        for window in windows:
            if not isinstance(window, dict):
                continue
            used = _number(window.get("used_percent"))
            remaining = (
                f"{100 - used:.1f}%"
                if used is not None and 0 <= used <= 100
                else "Unknown"
            )
            meter = ""
            if remaining != "Unknown":
                meter = f'<meter min="0" max="100" value="{100 - used}" aria-label="Remaining quota"></meter>'
            low = " low" if used is not None and 80 <= used <= 100 else ""
            quotas.append(
                f'<div class="quota-window{low}"><div class="quota-label">'
                f"<span>{_escape(window.get('name', 'Unknown'))}</span><strong>{remaining} remaining</strong></div>"
                + meter
                + f"<small>Resets {_escape(_subscription_time(window.get('resets_at')))}</small></div>"
            )
        cards.append(
            '<article class="quota-card"><div class="quota-heading">'
            f'<h3>{provider}</h3><span class="plan">{plan}</span></div>'
            + (
                "".join(quotas)
                if quotas
                else '<p class="note">Quota unavailable. Remaining capacity is unknown.</p>'
            )
            + credit_note
            + "</article>"
        )
    status = subscriptions.get("status", "not_configured")
    freshness = _subscription_time(subscriptions.get("checked_at"))
    body = (
        '<div class="quota-grid">' + "".join(cards) + "</div>"
        if cards
        else "<p>Subscription quota is unknown.</p>"
    )
    return (
        '<section id="subscriptions"><h2>Subscription capacity</h2>'
        + f"<p>Status: {_escape(status.replace('_', ' '))}. Checked: {_escape(freshness)}.</p>"
        + body
        + _section_note(
            "Provider quota is a snapshot and includes all clients using the account. "
            "Unknown capacity is not unused capacity. Token totals below measure Ralph usage, not subscription capacity."
        )
        + "</section>"
    )


def _usage_html(summary):
    limitations = _mapping(summary.get("limitations"))
    notes = "".join(
        f"<li>{_escape(note)}</li>" for note in _list(limitations.get("notes"))
    )
    overview = _mapping(summary.get("overview"))
    body = (
        '<h1>Usage</h1><p class="subtitle">Subscription capacity, request usage and operational diagnostics.</p>'
        + _subscriptions_section(summary)
        + _usage_trends_section(summary)
        + _usage_section(summary)
        + _model_section(summary)
        + "<details><summary>Routing and review distributions</summary>"
        + _routing_section(summary)
        + _distribution_section(summary)
        + "</details>"
        + _charts_section(summary)
        + "<section><h2>Accounting and limitations</h2>"
        + "<p>"
        + _escape(_analysis(summary))
        + "</p>"
        + _key_value_table(
            {
                "Reviews without findings export": _fmt_int(
                    overview.get("findings_unavailable_reviews")
                ),
                "Reviews without stage diagnostics": _fmt_int(
                    overview.get("diagnostics_unavailable_reviews")
                ),
            }
        )
        + "<ul>"
        + (notes or "<li>No limitations were recorded.</li>")
        + "</ul></section>"
    )
    return _page("Usage", summary, body, "../", "Usage")


def _limits_html(summary, repository_url, report_base_url):
    limits = _mapping(summary.get("limits"))
    labels = {
        "output_tokens": "Output tokens per response",
        "tool_calls": "Tool calls / turns",
        "context_calls": "Discussion lookups",
        "history_calls": "History lookups",
        "web_search_calls_per_response": "Web searches per response",
    }
    rows = []
    for group in limits.get("groups", []):
        rows.append(
            _cells(
                [
                    _escape(group["profile"]),
                    _escape(group["stage"]),
                    _escape(group["model"]),
                    _escape(labels.get(group["name"], group["name"])),
                    _fmt_int(group["cap"]),
                    _fmt_int(group["runs"]),
                    f"{group['hit_runs']} / {group['hit_known_runs']}"
                    if group["hit_known_runs"]
                    else "Unknown",
                    f"{group['at_cap_runs']} / {group['usage_known_runs']}"
                    if group["usage_known_runs"]
                    else "Unknown",
                    _fmt_int(group["max_used"]),
                ]
            )
        )
    affected_rows = []
    for run in limits.get("recent_affected", []):
        reasons = []
        for name, value in run["limits"].items():
            label = labels.get(name, name)
            if value["hit"] is True:
                reasons.append(label + ": work limited")
            elif value["used"] is not None and value["used"] >= value["limit"]:
                reasons.append(label + ": reached cap")
        affected_rows.append(
            _cells(
                [
                    _pr_link(run, repository_url) + f" / job {run['job_id']}",
                    _escape(run["profile"]),
                    _escape(run["stage"]),
                    _escape(", ".join(reasons)),
                    _report_link(run, report_base_url),
                ]
            )
        )
    body = (
        '<h1>Limits</h1><p class="subtitle">Where review allowances may be worth increasing.</p>'
        + _section_note(
            "Saved stage runs only; live work and overwritten retry attempts are excluded. "
            "Reaching a cap does not prove that review quality suffered. Compare affected reports before changing allowances. "
            "Skipped stages are excluded."
        )
        + f"<p>{_fmt_int(limits.get('uninstrumented_stage_runs'))} historical stage runs "
        + "without limit telemetry excluded.</p>"
    )
    if not rows:
        body += (
            "<section><h2>Awaiting first instrumented review</h2>"
            "<p>Measurements will appear after a new review saves its results "
            "and stats refresh.</p></section>"
        )
        return _page("Limits", summary, body, "../", "Limits")
    body += (
        "<section><h2>Token and tool limits</h2>"
        + _section_note(
            "Work limited counts observed output truncations, forced final answers or refused tool calls. "
            "The tool allowance also bounds model turns, so a forced final answer can occur below the tool call cap. "
            "Reached cap counts measured usage at or above the cap. Fractions use runs with known observations only. "
            "Output tokens and web searches show the maximum in one response, not the stage total. "
            "Web searches also consume the total tool allowance; reaching that smaller remaining allowance is counted under tool calls. "
            "Providers do not report whether the web search cap stopped additional searches, so work limited is unknown."
        )
        + _interactive_table(
            [
                "Profile",
                "Stage",
                "Model",
                "Limit",
                "Configured cap",
                "Saved runs",
                "Work limited / observed",
                "Reached cap / measured",
                "Maximum used",
            ],
            rows,
            "limits",
        )
        + "</section><section><h2>Recent affected stage runs</h2>"
        + _section_note(
            "Newest job IDs first. Reports show the recorded limits for each stage."
        )
        + _data_table(
            ["Review", "Profile", "Stage", "Observation", "Report"], affected_rows
        )
        + "</section>"
    )
    return _page("Limits", summary, body, "../", "Limits")


def save_stats(
    output_dir: Path, summary: dict, repository_url: str, report_base_url: str
):
    """Write the public statistics views, agent details and matching JSON."""
    output_dir = Path(output_dir)
    output_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    json_text = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)
    report._write_public_file(output_dir / "stats.json", json_text + "\n")
    report._write_public_file(output_dir / "stats.js", _script())
    pages = {
        "index.html": _html(summary, repository_url, report_base_url),
        "agents/index.html": _agents_html(summary, repository_url, report_base_url),
        "reviews/index.html": _reviews_html(summary, repository_url, report_base_url),
        "addressed/index.html": _addressed_html(
            summary, repository_url, report_base_url
        ),
        "usage/index.html": _usage_html(summary),
        "limits/index.html": _limits_html(summary, repository_url, report_base_url),
    }
    for stage in _list(summary.get("stages")):
        if isinstance(stage, dict):
            pages["agents/" + _stage_filename(stage)] = _stage_html(
                summary, stage, repository_url, report_base_url
            )
    for name, content in pages.items():
        target = output_dir / name
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        report._write_public_file(target, content)
