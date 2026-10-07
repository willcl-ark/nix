"""Static public production statistics dashboard."""

import html
import json
from datetime import datetime, timezone
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


def _micros(value):
    number = _number(value)
    return int(number) if number is not None else None


def _fmt_int(value):
    number = _number(value)
    return "N/A" if number is None else f"{int(number):,}"


def _fmt_decimal(value, digits=1):
    number = _number(value)
    return "N/A" if number is None else f"{number:,.{digits}f}"


def _fmt_money_micros(value):
    micros = _micros(value)
    if micros is None:
        return "N/A"
    dollars = micros / 1_000_000
    if 0 < abs(dollars) < 0.01:
        return f"${dollars:,.6f}"
    return f"${dollars:,.2f}"


def _fmt_percent(numerator, denominator):
    numerator = _number(numerator)
    denominator = _number(denominator)
    if numerator is None or not denominator:
        return "N/A"
    return f"{100 * numerator / denominator:.1f}%"


def _cost_per_unit(cost_micros, denominator):
    cost = _micros(cost_micros)
    denominator = _number(denominator)
    if cost is None or not denominator:
        return "N/A"
    return _fmt_money_micros(cost / denominator)


def _generated_at(summary):
    value = _field(summary, "generated_at")
    if isinstance(value, str) and value:
        return value
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _valid_http_base(url):
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url.rstrip("/"))
    if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        return None
    return url.rstrip("/")


def _safe_report_name(review):
    name = _field(review, "report_name")
    if isinstance(name, str) and report.REPORT_ID.fullmatch(name) and name.endswith(".html"):
        return name
    return None


def _valid_sha(value):
    return (isinstance(value, str) and len(value) == 40
            and all(char in "0123456789abcdefABCDEF" for char in value))


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
            ({"name": key, **value} if isinstance(value, dict)
             else {"name": key, "count": value})
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
    spend = _mapping(summary.get("spend"))
    month = _mapping(spend.get("current_month"))
    lifetime = _mapping(spend.get("lifetime"))
    saved_reviews = _field(overview, "saved_reviews")
    archaeology = _mapping(overview.get("archaeology"))
    unique_prs = _field(inventory, "unique_prs")
    jobs = _field(inventory, "jobs_total")
    attempts = _field(lifetime, "request_count")
    failed = _field(inventory, "recorded_failed_attempts")
    known = _field(month, "known_cost_micros")
    if _number(saved_reviews):
        sentence = (
            f"{_fmt_int(saved_reviews)} saved reviews; {_fmt_int(unique_prs)} pull requests have been queued. "
            f"{_fmt_int(_field(archaeology, 'research_completed'))} completed archaeology research. "
            f"Current month known spend is {_fmt_money_micros(known)}."
        )
    else:
        sentence = "No saved reviews are in this summary yet."
    if _number(attempts) is not None and _number(jobs) is not None:
        sentence += (
            f" Lifetime ledger accounting has {_fmt_int(attempts)} API request attempts for "
            f"{_fmt_int(jobs)} review jobs, including {_fmt_int(failed)} recorded failed attempts or retries."
        )
    return sentence


def _kpi(label, value, detail=""):
    return (
        '<article class="card">'
        f'<div class="kpi-label">{_escape(label)}</div>'
        f'<div class="kpi-value">{_escape(value)}</div>'
        f'<div class="kpi-detail">{_escape(detail)}</div>'
        '</article>'
    )


def _top_kpis(summary):
    inventory = _mapping(summary.get("inventory"))
    overview = _mapping(summary.get("overview"))
    spend = _mapping(summary.get("spend"))
    month = _mapping(spend.get("current_month"))
    lifetime = _mapping(spend.get("lifetime"))
    saved_reviews = _field(overview, "saved_reviews")
    unique_prs = _field(inventory, "unique_prs")
    findings = _field(overview, "published_findings")
    archaeology = _mapping(overview.get("archaeology"))
    addressed = _mapping(summary.get("addressed_findings"))
    addressed_counts = _mapping(addressed.get("status_counts"))
    known = _field(month, "known_cost_micros")
    reserved = _field(month, "reserved_micros")
    review_costs = _mapping(_mapping(summary.get("distributions")).get(
        "saved_review_known_cost_micros"))
    median_review_cost = _field(review_costs, "median")
    review_cost_count = _field(review_costs, "count")
    provider_count = len(_mapping(month.get("providers")))
    request_count = _field(lifetime, "request_count")
    jobs = _field(inventory, "jobs_total")
    failed = _field(inventory, "recorded_failed_attempts")
    return "".join([
        _kpi("Saved reviews", _fmt_int(saved_reviews), f"{_fmt_int(unique_prs)} unique PRs"),
        _kpi("Verified findings", _fmt_int(findings), "Bot verified, not human confirmed"),
        _kpi("Conceptual concerns", _fmt_int(_field(archaeology, "published_concerns")),
             f"{_fmt_int(_field(archaeology, 'research_completed'))} research runs"),
        _kpi("Addressed after review", _fmt_int(addressed_counts.get("addressed")),
             f"{_fmt_int(addressed_counts.get('partially_addressed'))} partial; "
             f"{_fmt_int(_field(addressed, 'evaluated_findings'))} assessed"),
        _kpi("Current month spend", _fmt_money_micros(known),
             f"{_fmt_money_micros(reserved)} reserved, {provider_count} providers"),
        _kpi("Median review cost", _fmt_money_micros(median_review_cost),
             f"matched saved-review cohort: {_fmt_int(review_cost_count)}"),
        _kpi("API attempts", _fmt_int(request_count),
             f"{_fmt_int(jobs)} review jobs; {_fmt_int(failed)} failed/retry attempts"),
    ])


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
    verification = _mapping(_mapping(summary.get("overview")).get("verification_status"))
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
    citation_counts = _mapping(distributions.get(
        "saved_review_conceptual_concern_citations"))
    alternative_counts = _mapping(distributions.get(
        "saved_review_conceptual_concern_alternatives"))
    if not archaeology:
        body = "<p>No archaeology counts were recorded.</p>"
    else:
        rows = "".join(
            f"<tr><td>{_escape(status)}</td><td>{_fmt_int(count)}</td></tr>"
            for status, count in sorted(by_status.items())
        )
        status_table = (
            "<p>No conceptual assessment statuses were recorded.</p>" if not rows else
            "<table><thead><tr><th>Status</th><th>Reviews</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
        body = (
            f"<p>{_fmt_int(_field(archaeology, 'research_completed'))} saved reviews "
            "completed archaeology research; "
            f"{_fmt_int(_field(archaeology, 'saved_concerns'))} saved and "
            f"{_fmt_int(_field(archaeology, 'published_concerns'))} published "
            "a conceptual concern.</p>"
            + status_table
            + "<p class=\"note\">"
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
        + '</section>'
    )


def _addressed_findings(summary):
    addressed = _mapping(summary.get("addressed_findings"))
    return [item for item in _list(addressed.get("findings")) if isinstance(item, dict)]


def _addressed_table(findings, repository_url, report_base_url):
    if not findings:
        return "<p>No addressed-finding assessments were recorded.</p>"
    rows = []
    for finding in findings:
        source_name = _field(finding, "source_report_name")
        if not (isinstance(source_name, str)
                and report.REPORT_ID.fullmatch(source_name)
                and source_name.endswith(".html")):
            source_name = None
        finding_label = _field(finding, "title", default=None) or _field(
            finding, "finding_id", default="unknown")
        path = _field(finding, "path", default=None)
        location = "" if path is None else f"<br><span class=\"note\">{_escape(path)}</span>"
        rows.append(
            "<tr>"
            f"<td>{_pr_link(finding, repository_url)}</td>"
            f"<td>{_report_name_link(source_name, report_base_url, 'source report')}</td>"
            f"<td>{_compare_link(finding, repository_url)}</td>"
            f"<td>{_escape(finding_label)}{location}</td>"
            f"<td>{_escape(_field(finding, 'status', default='unclear'))}</td>"
            f"<td>{_escape(_field(finding, 'reason', default=''))}</td>"
            f"<td>{_escape(_field(finding, 'evidence', default=''))}</td>"
            f"<td>{_escape(_field(finding, 'assessed_at', default='N/A'))}<br>"
            f"<span class=\"note\">{_escape(_field(finding, 'model', default='unknown'))}</span></td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>PR</th><th>Original review</th><th>Comparison</th>"
        "<th>Finding</th><th>Status</th><th>Rationale</th><th>Evidence</th>"
        "<th>Checked</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _addressed_section(summary):
    addressed = _mapping(summary.get("addressed_findings"))
    findings = _addressed_findings(summary)
    counts = _mapping(addressed.get("status_counts"))
    spend = _mapping(addressed.get("spend"))
    count_rows = "".join(
        f"<tr><td>{_escape(status)}</td><td>{_fmt_int(counts.get(status))}</td></tr>"
        for status in ("addressed", "partially_addressed", "still_present", "unclear")
    )
    coverage = (
        f"{_fmt_int(_field(addressed, 'evaluated_findings'))} assessed findings from "
        f"{_fmt_int(_field(addressed, 'source_published_findings'))} published findings "
        f"across {_fmt_int(_field(addressed, 'assessed_source_reviews'))} source reviews."
    )
    table_link = (
        "<p>No addressed-finding assessments were recorded.</p>" if not findings else
        f'<p>{_internal_link("addressed/", f"View all {len(findings):,} addressed assessments")}.</p>'
    )
    return (
        '<section id="addressed"><h2>Addressed after review</h2>'
        + _section_note(_field(
            addressed, "note",
            default=("Best-effort automated assessment. This is not a causal "
                     "claim or human validation.")))
        + f"<p>{coverage}</p>"
        + "<div class=\"split\"><div>"
        + "<table><thead><tr><th>Status</th><th>Findings</th></tr></thead>"
        + f"<tbody>{count_rows}</tbody></table></div>"
        + "<div>"
        + f"<p>Assessment jobs: {_fmt_int(_field(addressed, 'assessment_jobs'))}</p>"
        + f"<p>Assessment spend: {_fmt_money_micros(_field(spend, 'known_cost_micros'))} "
        + f"known, {_fmt_money_micros(_field(spend, 'reserved_micros'))} reserved, "
        + f"{_fmt_int(_field(spend, 'request_count'))} attempts.</p>"
        + "</div></div>"
        + table_link
        + '</section>'
    )


def _spend_rows(records):
    rows = []
    for record in _record_rows(records):
        name = record["name"] if "name" in record else _field(record, "provider", default="unknown")
        rows.append(
            "<tr>"
            f"<td>{_escape(name)}</td>"
            f"<td>{_fmt_int(_field(record, 'request_count'))}</td>"
            f"<td>{_fmt_money_micros(_field(record, 'known_cost_micros'))}</td>"
            f"<td>{_fmt_money_micros(_field(record, 'reserved_micros'))}</td>"
            f"<td>{_fmt_int(_field(record, 'unknown_cost_request_count'))}</td>"
            "</tr>"
        )
    if not rows:
        return "<p>No provider spend was recorded.</p>"
    return (
        "<table><thead><tr><th>Provider</th><th>API attempts</th><th>Known cost</th>"
        "<th>Reserved</th><th>Unknown cost attempts</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _monthly_trend(summary):
    months = _list(_mapping(summary.get("spend")).get("months"))
    if not months:
        return "<p>No monthly request accounting was recorded.</p>"
    max_requests = max((_number(_field(month, "request_count")) or 0) for month in months) or 1
    rows = []
    for month in months:
        requests = _number(_field(month, "request_count")) or 0
        width = max(2, min(100, int(100 * requests / max_requests))) if requests else 0
        rows.append(
            '<div class="bar-row">'
            f'<div class="bar-label">{_escape(_field(month, "month", default="unknown"))}</div>'
            '<div class="bar-track">'
            f'<div class="bar-fill" style="width:{width}%"></div>'
            '</div>'
            '<div class="bar-value">'
            f'{_fmt_int(requests)} attempts, {_fmt_money_micros(_field(month, "known_cost_micros"))}'
            '</div>'
            '</div>'
        )
    return "".join(rows)


def _spend_section(summary):
    spend = _mapping(summary.get("spend"))
    current = _mapping(spend.get("current_month"))
    lifetime = _mapping(spend.get("lifetime"))
    current_providers = current.get("providers")
    lifetime_providers = lifetime.get("providers")
    return (
        '<section id="spend"><h2>Spend and request attempts</h2>'
        + _section_note(
            "Spend includes recorded ledger attempts, including failed or superseded requests. "
            "Unknown costs and reservations are kept separate from known settled cost."
        )
        + '<div class="split">'
        + '<div><h3>Current month</h3>'
        + f'<p class="big">{_fmt_money_micros(_field(current, "known_cost_micros"))}</p>'
        + _spend_rows(current_providers)
        + '</div><div><h3>Lifetime</h3>'
        + f'<p class="big">{_fmt_money_micros(_field(lifetime, "known_cost_micros"))}</p>'
        + _spend_rows(lifetime_providers)
        + '</div></div>'
        + '<h3>Monthly request accounting</h3>'
        + _monthly_trend(summary)
        + '</section>'
    )


def _stage_section(summary):
    stages = _list(summary.get("stages"))
    if not stages:
        body = "<p>No agent stage statistics were recorded.</p>"
    else:
        rows = []
        for stage in stages:
            candidates = _mapping(stage.get("candidate_counts"))
            ledger = _mapping(stage.get("ledger"))
            candidate_denominator = _candidate_denominator(candidates)
            rows.append(
                "<tr>"
                f"<td>{_escape(_field(stage, 'stage', default='unknown'))}</td>"
                f"<td>{_escape(_field(stage, 'configured_model', default='unknown'))}</td>"
                f"<td>{_fmt_int(_field(stage, 'executed_runs'))}</td>"
                f"<td>{_fmt_int(_field(stage, 'saved_result_runs'))}</td>"
                f"<td>{_fmt_int(_field(stage, 'conceptual_concerns'))}</td>"
                f"<td>{_fmt_int(_field(stage, 'accepted_findings'))}</td>"
                f"<td>{_fmt_int(_field(stage, 'sole_source_findings'))}</td>"
                f"<td>{_fmt_int(_field(stage, 'shared_findings'))}</td>"
                f"<td>{_fmt_int(candidates.get('publish'))} / {_fmt_int(candidates.get('drop'))} / "
                f"{_fmt_int(candidates.get('unresolved'))} / {_fmt_int(candidates.get('undisposed'))}</td>"
                f"<td>{_fmt_percent(candidates.get('publish'), candidate_denominator)}</td>"
                f"<td>{_fmt_decimal(_field(stage, 'accepted_per_executed_run'), 3)}</td>"
                f"<td>{_fmt_int(_field(ledger, 'request_count'))}</td>"
                f"<td>{_fmt_money_micros(_field(ledger, 'known_cost_micros'))}</td>"
                f"<td>{_fmt_money_micros(_field(stage, 'known_cost_per_accepted_finding_micros'))}</td>"
                "</tr>"
            )
        body = (
            "<table><thead><tr><th>Stage</th><th>Configured model</th><th>Executed runs</th>"
            "<th>Saved runs</th><th>Conceptual concerns</th><th>Accepted</th><th>Sole</th><th>Shared</th>"
            "<th>Publish / drop / unresolved / undisposed</th><th>Selected among assessed</th>"
            "<th>Accepted findings/run</th><th>Attempts</th>"
            "<th>Known cost</th><th>Cost/accepted</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )
    return (
        '<section id="stages"><h2>Agent stages</h2>'
        + _section_note(
            "Opportunities and accepted findings describe observed attribution in saved review results. "
            "Conceptual concerns are counted separately from findings. Cost comes from ledger stage and model attempts, including reruns. Selected rates use publish, drop and unresolved candidates; unassessed candidates are excluded."
        )
        + body
        + '</section>'
    )


def _leaderboard_section(summary):
    leaders = [item for item in _list(summary.get("stage_model_leaderboard"))
               if isinstance(item, dict)]
    stage_details = {
        (_field(stage, "stage"), _field(stage, "configured_model")): stage
        for stage in _list(summary.get("stages")) if isinstance(stage, dict)
    }
    if not leaders:
        body = "<p>No accepted finding leaderboard was recorded.</p>"
    else:
        rows = []
        for leader in leaders:
            key = (_field(leader, "stage"), _field(leader, "configured_model"))
            details = _mapping(stage_details.get(key))
            candidates = _mapping(details.get("candidate_counts"))
            executed = _field(leader, "executed_runs")
            accepted = _field(leader, "accepted_findings")
            candidate_denominator = _candidate_denominator(candidates)
            rows.append(
                "<tr>"
                f"<td>{_escape(_field(leader, 'stage', default='unknown'))}</td>"
                f"<td>{_escape(_field(leader, 'configured_model', default='unknown'))}</td>"
                f"<td>{_fmt_int(accepted)}</td>"
                f"<td>{_fmt_int(_field(leader, 'sole_source_findings'))}</td>"
                f"<td>{_fmt_int(_field(leader, 'shared_findings'))}</td>"
                f"<td>{_fmt_int(executed)}</td>"
                f"<td>{_fmt_int(_field(leader, 'selected_candidate_findings'))} / "
                f"{_fmt_int(candidates.get('drop'))} / {_fmt_int(candidates.get('unresolved'))} / "
                f"{_fmt_int(candidates.get('undisposed'))}</td>"
                f"<td>{_fmt_percent(_field(leader, 'selected_candidate_findings'), candidate_denominator)}</td>"
                f"<td>{_fmt_decimal((_number(accepted) / _number(executed)) if _number(accepted) is not None and _number(executed) else None, 3)}</td>"
                f"<td>{_fmt_int(_field(leader, 'request_count'))}</td>"
                f"<td>{_fmt_money_micros(_field(leader, 'known_cost_micros'))}</td>"
                f"<td>{_fmt_money_micros(_field(leader, 'reserved_micros'))}</td>"
                f"<td>{_fmt_money_micros(_field(leader, 'known_cost_per_accepted_finding_micros'))}</td>"
                "</tr>"
            )
        body = (
            "<table><thead><tr><th>Stage</th><th>Configured model</th>"
            "<th>Accepted findings</th><th>Sole</th><th>Shared</th><th>Executed runs</th>"
            "<th>Selected / dropped / unresolved / undisposed</th><th>Selected among assessed</th>"
            "<th>Accepted findings/run</th><th>Attempts</th><th>Ledger cost</th>"
            "<th>Reserved</th><th>Cost/accepted</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )
    return (
        '<section id="leaderboard"><h2>Accepted finding leaderboard</h2>'
        + _section_note(
            "Ranked by verifier-accepted final findings. Shared findings are credited to each "
            "contributing agent, so row totals can overlap. Cost includes ledger retries and "
            "is grouped by stage and configured model. Selected rates exclude unassessed candidates."
        )
        + body
        + '</section>'
    )


def _paired_section(summary):
    paired = _mapping(summary.get("paired_sol_glm"))
    names = sorted(set(_mapping(paired.get("sole_accepted")))
                   | set(_mapping(paired.get("shared_accepted")))
                   | set(_mapping(paired.get("known_cost_micros"))))
    if not names:
        body = "<p>No paired Sol/GLM comparison was recorded.</p>"
    else:
        rows = []
        sole = _mapping(paired.get("sole_accepted"))
        shared = _mapping(paired.get("shared_accepted"))
        cost = _mapping(paired.get("known_cost_micros"))
        for name in names:
            rows.append(
                "<tr>"
                f"<td>{_escape(name)}</td>"
                f"<td>{_fmt_int(sole.get(name))}</td>"
                f"<td>{_fmt_int(shared.get(name))}</td>"
                f"<td>{_fmt_money_micros(cost.get(name))}</td>"
                f"<td>{_fmt_money_micros(_mapping(paired.get('reserved_micros')).get(name))}</td>"
                "</tr>"
            )
        body = (
            "<table><thead><tr><th>Model group</th><th>Sole accepted</th>"
            "<th>Shared accepted</th><th>Known cost</th><th>Reserved</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )
    note = _field(paired, "note", default="Observed completed pairs only; this is not recall or causality.")
    return (
        '<section id="paired"><h2>Paired model reviews</h2>'
        + _section_note(note)
        + f'<p>Completed pairs: {_fmt_int(_field(paired, "completed_pairs"))}</p>'
        + body
        + '</section>'
    )


def _model_section(summary):
    models = _list(_mapping(summary.get("spend")).get("models"))
    if not models:
        body = "<p>No model usage was recorded.</p>"
    else:
        rows = []
        for model in models:
            tokens = _mapping(model.get("tokens"))
            input_tokens = tokens.get("input")
            cached_tokens = tokens.get("cached")
            rows.append(
                "<tr>"
                f"<td>{_escape(_field(model, 'provider', default='unknown'))}</td>"
                f"<td>{_escape(_field(model, 'model', default='unknown'))}</td>"
                f"<td>{_fmt_int(_field(model, 'request_count'))}</td>"
                f"<td>{_fmt_money_micros(_field(model, 'known_cost_micros'))}</td>"
                f"<td>{_fmt_int(input_tokens)}</td>"
                f"<td>{_fmt_int(cached_tokens)}</td>"
                f"<td>{_fmt_percent(cached_tokens, input_tokens)}</td>"
                f"<td>{_fmt_int(tokens.get('output'))}</td>"
                "</tr>"
            )
        body = (
            "<table><thead><tr><th>Provider</th><th>Model</th><th>Attempts</th>"
            "<th>Known cost</th><th>Input tokens</th><th>Cached tokens</th>"
            "<th>Cache rate</th><th>Output tokens</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )
    return '<section id="models"><h2>Model usage</h2>' + body + '</section>'


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
        "<p>No routing tier statistics were recorded.</p>" if not rows else
        "<table><thead><tr><th>Tier</th><th>Reviews</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )
    return '<section id="routing"><h2>Routing outcomes</h2>' + body + '</section>'


def _distribution_section(summary):
    distributions = _mapping(summary.get("distributions"))
    if not distributions:
        return '<section id="distributions"><h2>Review distributions</h2><p>No distributions were recorded.</p></section>'
    labels = {
        "saved_review_known_cost_micros": "Saved review known cost",
        "saved_review_reserved_micros": "Saved review reserved",
        "saved_review_tools": "Tool calls",
        "saved_review_model_seconds": "Model seconds",
        "saved_review_conceptual_concern_citations": "Conceptual concern citations",
        "saved_review_conceptual_concern_alternatives": "Conceptual concern alternatives",
    }
    rows = []
    for key, label in labels.items():
        record = _mapping(distributions.get(key))
        formatter = _fmt_money_micros if key.endswith("_micros") else _fmt_decimal
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
        '<table><thead><tr><th>Metric</th><th>Count</th><th>Median</th><th>P90</th><th>Max</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></section>"
    )


def _review_sort_key(review):
    number = _number(_field(review, "job_id"))
    return number if number is not None else -1


def _sorted_reviews(summary):
    reviews = [review for review in _list(summary.get("reviews")) if isinstance(review, dict)]
    return sorted(reviews, key=_review_sort_key, reverse=True)


def _reviews_table(reviews, repository_url, report_base_url):
    if not reviews:
        return "<p>No saved reviews were recorded.</p>"
    rows = []
    for review in reviews:
        findings = _mapping(review.get("findings"))
        archaeology = _mapping(review.get("archaeology"))
        ledger = _mapping(review.get("ledger"))
        if archaeology.get("published_concern"):
            concept_status = (
                f"{_escape(_field(archaeology, 'status', default='unknown'))} "
                f"({_fmt_int(_field(archaeology, 'citation_count'))} citations, "
                f"{_fmt_int(_field(archaeology, 'alternative_count'))} alternatives)"
            )
        elif archaeology.get("research_completed"):
            concept_status = "research complete; no concern"
        else:
            concept_status = "N/A"
        rows.append(
            "<tr>"
            f"<td>{_fmt_int(_field(review, 'job_id'))}</td>"
            f"<td>{_pr_link(review, repository_url)}</td>"
            f"<td>{_fmt_int(_field(review, 'generation'))}</td>"
            f"<td>{_report_link(review, report_base_url)}</td>"
            f"<td>{_escape(_field(review, 'status', default='saved'))}</td>"
            f"<td>{_escape(_field(review, 'coverage', default='unknown'))}</td>"
            f"<td>{_escape(_field(review, 'context_coverage', default='unknown'))}</td>"
            f"<td>{_escape(_field(review, 'verification_status', default='unknown'))}</td>"
            f"<td>{_fmt_int(findings.get('saved'))} / {_fmt_int(findings.get('published'))}</td>"
            f"<td>{concept_status}</td>"
            f"<td>{_fmt_int(_field(ledger, 'request_count'))}</td>"
            f"<td>{_fmt_int(_field(review, 'recorded_failed_attempts'))}</td>"
            f"<td>{_fmt_money_micros(_field(ledger, 'known_cost_micros'))}</td>"
            f"<td>{_fmt_money_micros(_field(ledger, 'reserved_micros'))}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Job</th><th>PR</th><th>Generation</th><th>Report</th>"
        "<th>Status</th><th>Code review</th><th>Historical context</th>"
        "<th>Verification</th><th>Findings saved/published</th>"
        "<th>Concept</th><th>Attempts</th><th>Failed/retries</th><th>Known cost</th><th>Reserved</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _reviews_section(summary, repository_url, report_base_url):
    reviews = _sorted_reviews(summary)
    preview = reviews[:20]
    if reviews:
        shown = len(preview)
        note = (
            f"<p>Showing the latest {shown:,} of {len(reviews):,} saved reviews. "
            f'{_internal_link("reviews/", "View all reviews")}.</p>'
        )
    else:
        note = ""
    return (
        '<section id="reviews"><h2>Latest reviews</h2>'
        + note
        + _reviews_table(preview, repository_url, report_base_url)
        + '</section>'
    )


def _valid_period(value):
    return (isinstance(value, str) and len(value) == 7 and value[4] == "-"
            and value[:4].isdigit() and value[5:].isdigit())


def _time_series(summary):
    records = []
    for record in _list(summary.get("time_series")):
        if not isinstance(record, dict):
            continue
        period = _field(record, "period")
        if not _valid_period(period):
            continue
        records.append({
            "period": period,
            "reviews": max(0, int(_number(_field(record, "reviews")) or 0)),
            "verified_reviews": max(0, int(_number(_field(record, "verified_reviews")) or 0)),
            "findings": max(0, int(_number(_field(record, "findings")) or 0)),
            "addressed": max(0, int(_number(_field(record, "addressed")) or 0)),
        })
    return sorted(records, key=lambda item: item["period"])


def _chart_svg(chart_id, title, description, records, field):
    width = 280
    height = 132
    left = 34
    right = 12
    top = 12
    bottom = 30
    values = [_number(record.get(field)) or 0 for record in records]
    max_value = max(values) or 1
    x_span = width - left - right
    y_span = height - top - bottom
    if len(records) == 1:
        points = [(left + x_span / 2, top + y_span * (1 - values[0] / max_value))]
    else:
        points = [
            (left + x_span * index / (len(records) - 1),
             top + y_span * (1 - value / max_value))
            for index, value in enumerate(values)
        ]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    circles = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3"><title>{_escape(record["period"])}: '
        f'{_fmt_int(record.get(field))}</title></circle>'
        for (x, y), record in zip(points, records)
    )
    first = records[0]["period"]
    last = records[-1]["period"]
    title_id = f"{chart_id}-title"
    desc_id = f"{chart_id}-desc"
    return (
        '<figure class="chart">'
        f'<figcaption>{_escape(title)}</figcaption>'
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-labelledby="{title_id} {desc_id}">'
        f'<title id="{title_id}">{_escape(title)}</title>'
        f'<desc id="{desc_id}">{_escape(description)}</desc>'
        f'<line class="chart-axis" x1="{left}" y1="{top + y_span}" x2="{width - right}" y2="{top + y_span}"></line>'
        f'<line class="chart-axis" x1="{left}" y1="{top}" x2="{left}" y2="{top + y_span}"></line>'
        f'<polyline class="chart-line" points="{line}"></polyline>'
        f'{circles}'
        f'<text x="{left}" y="{height - 10}">{_escape(first)}</text>'
        f'<text x="{width - right}" y="{height - 10}" text-anchor="end">{_escape(last)}</text>'
        f'<text x="{left - 6}" y="{top + 4}" text-anchor="end">{_fmt_int(max_value)}</text>'
        '<text x="28" y="105" text-anchor="end">0</text>'
        '</svg>'
        '</figure>'
    )


def _time_series_notes(summary):
    notes = [
        note for note in _list(summary.get("time_series_notes"))
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


def _charts_section(summary):
    records = _time_series(summary)
    if not records:
        return (
            '<section id="trends"><h2>Production trends</h2>'
            '<p>No review timestamp history is available yet. Older summaries do not include '
            'enough event timestamps to chart production over time.</p>'
            + _time_series_notes(summary)
            + '</section>'
        )
    charts = [
        ("reviews", "Reviews", "Saved reviews by month.", "reviews"),
        ("verified-reviews", "Verifier-completed reviews",
         "Reviews whose verifier pass completed by month.", "verified_reviews"),
        ("findings", "Verified findings", "Published verified findings by month.", "findings"),
        ("addressed", "Addressed findings by PR head commit date",
         "Addressed findings by month, based on the assessed PR head commit date.", "addressed"),
    ]
    body = "".join(
        _chart_svg(f"chart-{chart_id}", title, description, records, field)
        for chart_id, title, description, field in charts
    )
    return (
        '<section id="trends"><h2>Production trends</h2>'
        + _section_note(
            "Addressed findings follow the commit date of the PR head used for assessment; "
            "undated records are excluded."
        )
        + _time_series_notes(summary)
        + f'<div class="charts">{body}</div></section>'
    )


def _style():
    return """
body { margin: 0; color: #1d2733; background: #f7f8fb; font: 16px/1.5 system-ui, sans-serif; }
a { color: #0f5f8c; }
header { background: #16202c; color: #fff; padding: 2rem max(1rem, calc((100vw - 76rem) / 2)); }
main { max-width: 76rem; margin: 0 auto; padding: 1.25rem 1rem 3rem; }
h1 { margin: 0 0 .4rem; font-size: clamp(2rem, 4vw, 3.4rem); line-height: 1; }
h2 { margin-top: 2rem; font-size: 1.45rem; }
h3 { margin: 1rem 0 .5rem; font-size: 1rem; }
nav { display: flex; flex-wrap: wrap; gap: .5rem; margin-top: 1rem; }
nav a, .download { color: #fff; border: 1px solid rgb(255 255 255 / 35%); border-radius: 6px; padding: .35rem .55rem; text-decoration: none; }
.summary { max-width: 58rem; font-size: 1.1rem; color: #dbeafe; }
.meta { color: #aebecd; }
.preview { display: inline-block; color: #111827; background: #fef3c7; border-radius: 6px; padding: .35rem .55rem; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr)); gap: .75rem; margin-top: -1.2rem; }
.card { background: #fff; border: 1px solid #d8dee8; border-radius: 8px; padding: 1rem; box-shadow: 0 2px 12px rgb(22 32 44 / 7%); }
.kpi-label { color: #526170; font-size: .84rem; text-transform: uppercase; letter-spacing: .04em; }
.kpi-value { font-size: 1.75rem; font-weight: 750; margin-top: .2rem; }
.kpi-detail, .note { color: #5b6876; }
.big { font-size: 1.7rem; font-weight: 750; margin: .25rem 0 .75rem; }
section { background: #fff; border: 1px solid #d8dee8; border-radius: 8px; padding: 1rem; margin-top: 1rem; }
.split { display: grid; grid-template-columns: repeat(auto-fit, minmax(20rem, 1fr)); gap: 1rem; }
.charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(15rem, 1fr)); gap: 1rem; }
.chart { margin: 0; border: 1px solid #e3e7ee; border-radius: 8px; padding: .75rem; }
.chart figcaption { font-weight: 700; margin-bottom: .4rem; }
.chart svg { display: block; width: 100%; height: auto; overflow: visible; }
.chart text { fill: #5b6876; font-size: 10px; }
.chart-axis { stroke: #cad2dd; stroke-width: 1; }
.chart-line { fill: none; stroke: #0f766e; stroke-width: 3; stroke-linecap: round; stroke-linejoin: round; }
.chart circle { fill: #0f5f8c; }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; min-width: 44rem; width: 100%; }
th, td { border-bottom: 1px solid #e3e7ee; padding: .5rem .55rem; text-align: left; vertical-align: top; }
th { color: #425060; background: #f0f3f7; font-size: .85rem; }
.bar-row { display: grid; grid-template-columns: 6rem minmax(8rem, 1fr) 16rem; gap: .65rem; align-items: center; margin: .45rem 0; }
.bar-track { height: .75rem; background: #e7ebf0; border-radius: 999px; overflow: hidden; }
.bar-fill { height: 100%; background: linear-gradient(90deg, #0f766e, #2f80ed); }
details { border-top: 1px solid #e3e7ee; padding: .8rem 0; }
summary { cursor: pointer; font-weight: 700; }
@media (max-width: 700px) {
  .bar-row { grid-template-columns: 1fr; gap: .2rem; }
  header { padding-top: 1.4rem; }
}
"""


def _wrap_tables(html_text):
    return html_text.replace("<table>", '<div class="table-wrap"><table>').replace("</table>", "</table></div>")


def _header(summary, page_prefix=""):
    generated_at = _generated_at(summary)
    preview_notice = _field(summary, "preview_notice", default="")
    preview_html = f'<p class="preview">{_escape(preview_notice)}</p>' if preview_notice else ""
    overview_link = f'<a href="{page_prefix}index.html">Overview</a>' if page_prefix else ""
    def anchor(fragment):
        return f"{page_prefix}index.html{fragment}" if page_prefix else fragment
    return f"""<header>
<h1>ralph stats</h1>
{preview_html}
<p class="summary">{_escape(_analysis(summary))}</p>
<p class="meta">Generated {_escape(generated_at)}. {_escape(_source_status(summary))}</p>
<nav>
{overview_link}
<a href="{anchor("#leaderboard")}">Leaderboard</a>
<a href="{anchor("#concepts")}">Concepts</a>
<a href="{anchor("#addressed")}">Addressed</a>
<a href="{anchor("#spend")}">Spend</a>
<a href="{anchor("#stages")}">Agent stages</a>
<a href="{anchor("#paired")}">Paired models</a>
<a href="{anchor("#models")}">Models</a>
<a href="{anchor("#routing")}">Routing</a>
<a href="{page_prefix}reviews/">Reviews</a>
<a href="{page_prefix}addressed/">Addressed details</a>
<a class="download" href="{page_prefix}stats.json" download>Download JSON</a>
</nav>
</header>"""


def _page(title, summary, body, page_prefix=""):
    return _wrap_tables(f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_escape(title)}</title>
<style>{_style()}</style>
</head>
<body>
{_header(summary, page_prefix)}
<main>
{body}
</main>
</body>
</html>
""")


def _html(summary, repository_url, report_base_url):
    limitations = _mapping(summary.get("limitations"))
    notes = "".join(f"<li>{_escape(note)}</li>" for note in _list(limitations.get("notes")))
    body = f"""
<div class="cards">{_top_kpis(summary)}</div>
{_leaderboard_section(summary)}
{_concept_section(summary)}
{_addressed_section(summary)}
{_paired_section(summary)}
<section id="coverage"><h2>Coverage snapshot</h2>
{_section_note("Code review coverage excludes advisory historical context. Historical context and verification are counted as separate dimensions; bot verification is not human confirmation.")}
{_coverage_table(summary)}
</section>
{_spend_section(summary)}
{_stage_section(summary)}
{_model_section(summary)}
{_routing_section(summary)}
{_distribution_section(summary)}
{_reviews_section(summary, repository_url, report_base_url)}
{_charts_section(summary)}
<section id="limits"><h2>Limitations</h2>
<ul>{notes or '<li>No limitations were recorded.</li>'}</ul>
</section>
"""
    return _page("ralph stats", summary, body)


def _reviews_html(summary, repository_url, report_base_url):
    reviews = _sorted_reviews(summary)
    body = (
        '<section id="reviews"><h2>All reviews</h2>'
        f'<p>{_internal_link("../index.html#reviews", "Back to latest reviews")}.</p>'
        + _reviews_table(reviews, repository_url, report_base_url)
        + '</section>'
    )
    return _page("ralph review stats", summary, body, "../")


def _addressed_html(summary, repository_url, report_base_url):
    findings = _addressed_findings(summary)
    body = (
        '<section id="addressed-details"><h2>Addressed assessment details</h2>'
        f'<p>{_internal_link("../index.html#addressed", "Back to addressed summary")}.</p>'
        + _addressed_table(findings, repository_url, report_base_url)
        + '</section>'
    )
    return _page("ralph addressed stats", summary, body, "../")


def save_stats(output_dir: Path, summary: dict, repository_url: str, report_base_url: str):
    """Write a static HTML dashboard and matching public JSON."""
    output_dir = Path(output_dir)
    output_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    (output_dir / "reviews").mkdir(mode=0o755, exist_ok=True)
    (output_dir / "addressed").mkdir(mode=0o755, exist_ok=True)
    json_text = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)
    report._write_public_file(output_dir / "stats.json", json_text + "\n")
    report._write_public_file(output_dir / "index.html", _html(summary, repository_url, report_base_url))
    report._write_public_file(
        output_dir / "reviews" / "index.html",
        _reviews_html(summary, repository_url, report_base_url))
    report._write_public_file(
        output_dir / "addressed" / "index.html",
        _addressed_html(summary, repository_url, report_base_url))
