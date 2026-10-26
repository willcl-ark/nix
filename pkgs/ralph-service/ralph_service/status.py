"""Read-only operational summary for the Telegram monitor."""

import subprocess
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import stats, telegram

UNITS = (
    "ralph-telegram.service",
    "ralph.service",
    "ralph-stats.service",
    "ralph-stats.timer",
    "ralph-followup.service",
    "ralph-followup.timer",
)


def _services():
    try:
        result = subprocess.run(
            [
                "systemctl",
                "show",
                *UNITS,
                "--property=Id,LoadState,ActiveState,SubState,Result,MainPID",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ["Services: unavailable"]
    records = {}
    for block in result.stdout.strip().split("\n\n"):
        record = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        records[record.get("Id")] = record
    lines = ["Services:"]
    for unit in UNITS:
        record = records.get(unit, {})
        if record.get("LoadState") != "loaded":
            lines.append(f"{unit}: unavailable")
            continue
        active = record.get("ActiveState", "unknown")
        sub = record.get("SubState", "unknown")
        outcome = record.get("Result")
        detail = f"{unit}: {active}/{sub}"
        if outcome:
            detail += f", result={outcome}"
        if (
            active == "inactive"
            and outcome == "success"
            and unit in ("ralph-stats.service", "ralph-followup.service")
        ):
            detail += " (idle between timer runs)"
        if unit == "ralph.service" and record.get("MainPID", "0") != "0":
            detail += f", PID {record['MainPID']}"
        lines.append(detail)
    return lines


def _jobs(state_dir):
    rows, missing = stats._job_rows(state_dir / "jobs.sqlite3")
    if missing:
        return ["Runs: unavailable (jobs database)"]
    counts = Counter(row["status"] for row in rows)
    lines = [
        "Runs (all time): "
        + ", ".join(
            f"{label} {counts[key]}"
            for key, label in (
                ("complete", "completed"),
                ("failed", "failed"),
                ("superseded", "superseded"),
            )
        )
    ]
    for key, label in (
        ("running", "Running"),
        ("pending", "Queued"),
        ("retry", "Retrying"),
    ):
        numbers = sorted({row["number"] for row in rows if row["status"] == key})
        displayed = ", ".join(f"#{number}" for number in numbers[:20])
        if len(numbers) > 20:
            displayed += f", … ({len(numbers)} total)"
        lines.append(f"{label}: {displayed or 'none'}")
    saved = skipped = malformed = unavailable = exported = 0
    severities = Counter()
    for row in rows:
        result, invalid = stats._review_result(row)
        malformed += invalid
        if result is None:
            continue
        if stats._skipped_result(result):
            skipped += 1
            continue
        saved += 1
        findings = stats._published_findings(result)
        if findings is None:
            unavailable += 1
        else:
            exported += 1
            severities.update(item.get("severity") or "unknown" for item in findings)
    lines.append(f"Saved reviews (all time): {saved}; skipped {skipped}")
    detail = ", ".join(
        f"{severity} {count}" for severity, count in sorted(severities.items())
    )
    lines.append(f"Exported findings (all time, {exported} reviews): {detail or '0'}")
    if unavailable or malformed:
        lines.append(
            f"Unavailable finding exports: {unavailable}; unreadable results: {malformed}"
        )
    return lines


def _spend(state_dir, now):
    start = now - timedelta(hours=24)
    month = now.strftime("%Y-%m")
    lines = [
        "Costs (24h by request start; month by UTC accounting month):",
        "Known cost / reserved / unknown requests:",
    ]
    for label, filename in (
        ("OpenAI", "spend.sqlite3"),
        ("PPQ", "ppq-spend.sqlite3"),
        ("Follow-up", "followup/spend.sqlite3"),
    ):
        requests, missing = stats._provider_ledger_rows(label, state_dir / filename)
        if missing:
            lines.append(f"{label}: unavailable")
            continue
        periods = []
        recent = [
            row
            for row in requests
            if isinstance(row["requested_at"], (int, float))
            and start.timestamp() <= row["requested_at"] < now.timestamp()
        ]
        monthly = [row for row in requests if row["month"] == month]
        for rows in (recent, monthly):
            totals = stats._request_totals(rows)
            periods.append(
                f"${totals['known_cost_micros'] / 1_000_000:.6f} / "
                f"${totals['reserved_micros'] / 1_000_000:.6f} / "
                f"{totals['unknown_cost_request_count']}"
            )
        lines.append(f"{label}: 24h {periods[0]}; month {periods[1]}")
        undated = sum(
            not isinstance(row["requested_at"], (int, float)) for row in requests
        )
        if undated:
            lines.append(f"{label}: {undated} undated requests excluded from 24h")
    return lines


def status_message(state_dir, report_dir, report_base_url, *, now=None):
    """Summarize service health and persisted records without contacting a model."""
    if now is None:
        now = datetime.now(UTC)
    elif isinstance(now, (int, float)):
        now = datetime.fromtimestamp(now, UTC)
    else:
        now = now.astimezone(UTC)
    state_dir = Path(state_dir)
    lines = [f"Ralph status, {now:%Y-%m-%d %H:%M UTC}"]
    lines.extend(_services())
    lines.extend(_jobs(state_dir))
    lines.extend(_spend(state_dir, now))
    if not Path(report_dir).is_dir():
        lines.append("Report directory: unavailable")
    link = f"\nReports and detailed statistics: {report_base_url.rstrip('/')}"
    return telegram.truncate_message(
        telegram.truncate_message(
            "\n".join(lines), 4096 - telegram.message_length(link)
        )
        + link
    )
