"""Static PR history indexes built exclusively from public report pairs."""

import html
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from . import report


def _report_time(record, path):
    value = record.get("review_completed_at")
    if value == "unknown":
        return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.timestamp()
        except ValueError:
            pass
    return path.stat().st_mtime


def save_history(report_dir, number=None):
    """Refresh all PR indexes, or just one PR, without changing report artifacts."""
    report_dir = Path(report_dir)
    report_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    report._write_public_file(report_dir / "history.js", _script())
    groups = defaultdict(list)
    for path in report_dir.glob("*.json"):
        if not path.with_suffix(".html").is_file():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                continue
            pr = record.get("pr")
            if (
                type(pr) is not int
                or pr <= 0
                or (number is not None and pr != number)
                or record.get("report_id") != path.stem
                or not report.REPORT_ID.fullmatch(path.stem)
            ):
                continue
            timestamp = _report_time(record, path)
            groups[pr].append((timestamp, path.stem, record))
        except (OSError, ValueError) as exc:
            logging.warning("Could not index public report %s: %s", path.name, exc)
    for pr, records in groups.items():
        records.sort(
            key=lambda item: (item[0] is not None, item[0] or 0, item[1]),
            reverse=True,
        )
        target = report_dir / str(pr) / "index.html"
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        report._write_public_file(target, _page(pr, records))
    if number is None:
        catalog = [
            {
                "pr": pr,
                "head": str(record.get("head") or "unknown"),
                "report_id": name,
                "reviewed": _date(timestamp),
                "timestamp": timestamp,
                "findings": len(record["findings"])
                if isinstance(record.get("findings"), list)
                else "Unknown",
                "status": str(record.get("status") or "unknown"),
                "latest": _latest_label(records) if index == 0 else "",
            }
            for pr, records in groups.items()
            for index, (timestamp, name, record) in enumerate(records)
        ]
        report._write_public_file(
            report_dir / "history.json", json.dumps(catalog, ensure_ascii=False) + "\n"
        )
        report._write_public_file(report_dir / "index.html", _search_page())
    return len(groups)


def _page(number, records):
    rows = []
    for index, (timestamp, name, record) in enumerate(records):
        head = str(record.get("head") or "unknown")
        escaped_head = html.escape(head, quote=True)
        date = _date(timestamp)
        if index == 0 and _latest_label(records):
            date += f" {_latest_label(records)}"
        findings = record.get("findings")
        count = str(len(findings)) if isinstance(findings, list) else "Unknown"
        rows.append(
            f'<tr data-commit="{escaped_head}"><td>{date}</td>'
            f'<td><a href="?commit={quote(head, safe="")}"><code>'
            f"{escaped_head}</code></a></td>"
            f"<td>{html.escape(str(record.get('status') or 'unknown'))}</td>"
            f'<td>{count}</td><td><a href="../{quote(name, safe="")}.html">'
            "Full report</a></td></tr>"
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ralph reports for PR #{number}</title>
<style>{_style()}</style><script src="../history.js" defer></script></head><body>
<p><a href="../">Search all reports</a></p>
<h1>Ralph reports for PR #{number}</h1>
<p>Saved review runs, newest first. Multiple reports may review the same commit.
A saved report does not necessarily mean its review was posted.</p>
<form><label>Commit <input name="commit" placeholder="Full SHA or prefix" aria-label="Commit hash"></label>
<button type="submit">Filter</button> <a href="./">All reports</a></form>
<p id="filter-status" role="status"></p>
<div class="table-scroll"><table><thead><tr><th>Reviewed</th><th>Commit</th>
<th>Outcome</th><th>Findings</th><th>Report</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div>
<noscript>Commit filtering requires JavaScript. All reports are listed above.</noscript>
</body></html>
"""


def _latest_label(records):
    if not records or records[0][0] is None:
        return ""
    return "🟠"


def _date(timestamp):
    if timestamp is None:
        return "Unknown"
    date = datetime.fromtimestamp(timestamp, timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    return date


def _style():
    return """body { font: 16px system-ui, sans-serif; margin: 2rem auto; padding: 0 1rem; max-width: 90rem; color: #202124; background: #fafafa; }
a { color: #185abc; }
.table-scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; background: white; }
th, td { text-align: left; padding: .75rem; border-bottom: 1px solid #ddd; }
code { font-size: .85em; }
input, button { font: inherit; padding: .35rem; }
form { margin: 1rem 0; }
"""


def _search_page():
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Search Ralph reports</title>
<style>{_style()}</style><script src="history.js" defer></script></head><body>
<h1>Search Ralph reports</h1>
<form><label>PR number <input name="q" placeholder="PR number, e.g. 36688" aria-label="PR number or commit"></label>
<button type="submit">Search</button></form>
<p>You can also search by a commit hash or prefix.</p>
<p id="search-status" role="status"></p>
<div id="search-results"></div>
<noscript>Report search requires JavaScript. Open a PR history by appending its number and a slash to this URL.</noscript>
</body></html>
"""


def _script():
    return """'use strict';
const params = new URLSearchParams(location.search);
const commitInput = document.querySelector('input[name="commit"]');
if (commitInput) {
    const commit = (params.get('commit') || '').trim().toLowerCase();
    commitInput.value = commit;
    if (commit) {
        const valid = /^[0-9a-f]{1,40}$/.test(commit);
        let shown = 0;
        for (const row of document.querySelectorAll('tr[data-commit]')) {
            row.hidden = !valid || !row.dataset.commit.toLowerCase().startsWith(commit);
            if (!row.hidden) shown++;
        }
        document.getElementById('filter-status').textContent = valid
            ? `${shown} report(s) matching ${commit}.`
            : 'Enter a hexadecimal commit hash or prefix.';
    }
}

const searchInput = document.querySelector('input[name="q"]');
if (searchInput) {
    const query = (params.get('q') || '').trim().toLowerCase();
    searchInput.value = query;
    if (query) searchReports(query);
}

async function searchReports(query) {
    const status = document.getElementById('search-status');
    const pr = /^#?[0-9]+$/.test(query) ? Number(query.replace('#', '')) : null;
    const commit = /^[0-9a-f]{1,40}$/.test(query) ? query : null;
    if (pr === null && commit === null) {
        status.textContent = 'Enter a PR number or hexadecimal commit hash or prefix.';
        return;
    }
    status.textContent = 'Searching reports…';
    try {
        const response = await fetch('history.json');
        if (!response.ok) throw new Error('Report index unavailable');
        const reports = await response.json();
        const matches = reports.filter(report => report.pr === pr
            || (commit !== null && report.head.toLowerCase().startsWith(commit)));
        status.textContent = `${matches.length} report(s) matching ${query}.`;
        const results = document.getElementById('search-results');
        matches.sort((a, b) => (b.timestamp ?? -Infinity) - (a.timestamp ?? -Infinity)
            || b.report_id.localeCompare(a.report_id));
        if (!matches.length) return;
        const scroll = document.createElement('div');
        scroll.className = 'table-scroll';
        const table = document.createElement('table');
        const header = table.createTHead().insertRow();
        for (const title of ['PR', 'Reviewed', 'Commit', 'Outcome', 'Findings', 'Report']) {
            const cell = document.createElement('th');
            cell.textContent = title;
            header.append(cell);
        }
        const body = table.createTBody();
        function link(cell, href, text) {
            const anchor = document.createElement('a');
            anchor.href = href;
            anchor.textContent = text;
            cell.append(anchor);
        }
        for (const report of matches) {
            const row = body.insertRow();
            link(row.insertCell(), `${report.pr}/`, `#${report.pr}`);
            row.insertCell().textContent = `${report.reviewed}${report.latest ? " " + report.latest : ""}`;
            const commitCell = row.insertCell();
            const code = document.createElement('code');
            link(code, `${report.pr}/?commit=${encodeURIComponent(report.head)}`, report.head);
            commitCell.append(code);
            row.insertCell().textContent = report.status;
            row.insertCell().textContent = report.findings;
            link(row.insertCell(), `${encodeURIComponent(report.report_id)}.html`, 'Full report');
        }
        scroll.append(table);
        results.append(scroll);
    } catch (error) {
        status.textContent = 'Could not load the report index. Please try again later.';
    }
}
"""
