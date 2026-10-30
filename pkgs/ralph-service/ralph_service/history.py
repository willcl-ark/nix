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
        return None, False
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.timestamp(), False
        except ValueError:
            pass
    return path.stat().st_mtime, True


def save_history(report_dir, number=None):
    """Refresh all PR indexes, or just one PR, without changing report artifacts."""
    report_dir = Path(report_dir)
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
            timestamp, estimated = _report_time(record, path)
            groups[pr].append((timestamp, path.stem, estimated, record))
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
    return len(groups)


def _page(number, records):
    rows = []
    for timestamp, name, estimated, record in records:
        head = str(record.get("head") or "unknown")
        escaped_head = html.escape(head, quote=True)
        date = (
            datetime.fromtimestamp(timestamp, timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            )
            if timestamp is not None
            else "Unknown"
        )
        if estimated:
            date += " *"
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
<style>
body {{ font: 16px system-ui, sans-serif; margin: 2rem auto; padding: 0 1rem; max-width: 90rem; color: #202124; background: #fafafa; }}
a {{ color: #185abc; }}
.table-scroll {{ overflow-x: auto; }}
table {{ width: 100%; border-collapse: collapse; background: white; }}
th, td {{ text-align: left; padding: .75rem; border-bottom: 1px solid #ddd; }}
code {{ font-size: .85em; }}
input, button {{ font: inherit; padding: .35rem; }}
form {{ margin: 1rem 0; }}
</style></head><body>
<h1>Ralph reports for PR #{number}</h1>
<p>Saved review runs, newest first. Multiple reports may review the same commit.
A saved report does not necessarily mean its review was posted.</p>
<form><label>Commit <input name="commit" placeholder="Full SHA or prefix" aria-label="Commit hash"></label>
<button type="submit">Filter</button> <a href="./">All reports</a></form>
<p id="filter-status" role="status"></p>
<div class="table-scroll"><table><thead><tr><th>Reviewed</th><th>Commit</th>
<th>Outcome</th><th>Findings</th><th>Report</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div>
<p>* Older reports use the public report file's modification time.</p>
<noscript>Commit filtering requires JavaScript. All reports are listed above.</noscript>
<script>
const commit = (new URLSearchParams(location.search).get('commit') || '').trim().toLowerCase();
const rows = Array.from(document.querySelectorAll('tr[data-commit]'));
document.querySelector('input[name="commit"]').value = commit;
if (commit) {{
    const valid = /^[0-9a-f]{{1,40}}$/.test(commit);
    let shown = 0;
    for (const row of rows) {{
        row.hidden = !valid || !row.dataset.commit.toLowerCase().startsWith(commit);
        if (!row.hidden) shown++;
    }}
    document.getElementById('filter-status').textContent = valid
        ? `${{shown}} report(s) matching ${{commit}}.`
        : 'Enter a hexadecimal commit hash or prefix.';
}}
</script></body></html>
"""
