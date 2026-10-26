"""Read-only operational summary for the Telegram monitor."""

import sqlite3
import subprocess
from collections import Counter
from contextlib import closing
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
                "--property=Id,LoadState,ActiveState,SubState,Result,ExecMainStartTimestampMonotonic",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ["Services", "Service status unavailable"]
    records = {}
    for block in result.stdout.strip().split("\n\n"):
        record = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        records[record.get("Id")] = record
    states = []
    for unit in UNITS:
        record = records.get(unit, {})
        active = record.get("ActiveState", "unknown")
        sub = record.get("SubState", "unknown")
        outcome = record.get("Result")
        if record.get("LoadState") != "loaded":
            state = "unavailable"
        elif active == "active" and sub == "running":
            state = "running"
        elif unit.endswith(".timer") and active == "active" and sub == "waiting":
            state = "active"
        elif active == "inactive" and sub == "dead":
            state = "stopped"
            if (
                unit in ("ralph-stats.service", "ralph-followup.service")
                and outcome == "success"
            ):
                state = (
                    "successful"
                    if record.get("ExecMainStartTimestampMonotonic", "0")
                    not in ("", "0")
                    else "not run yet"
                )
        else:
            state = active if active == sub else f"{active}/{sub}"
        if record.get("LoadState") == "loaded" and outcome not in (None, "", "success"):
            state = (
                f"failed: {outcome}"
                if state in {"failed", "stopped"}
                else f"{state}, last run failed: {outcome}"
            )
        states.append(state)
    telegram_state, reviewer, stats_run, stats_timer, followup_run, followup_timer = (
        states
    )
    lines = ["Services"]
    if reviewer == telegram_state == "running":
        lines.append("Reviewer and Telegram: running")
    else:
        lines.extend([f"Reviewer: {reviewer}", f"Telegram: {telegram_state}"])
    if stats_timer == followup_timer == "active":
        lines.append("Stats and follow-up timers: active")
    else:
        lines.extend(
            [f"Stats timer: {stats_timer}", f"Follow-up timer: {followup_timer}"]
        )
    if stats_run == followup_run == "successful":
        lines.append("Last stats and follow-up runs: successful")
    else:
        lines.extend([f"Stats: {stats_run}", f"Follow-up: {followup_run}"])
    return lines


def _jobs(state_dir):
    rows, missing = stats._job_rows(state_dir / "jobs.sqlite3")
    if missing:
        return ["Reviews", "Run history unavailable"]
    counts = Counter(row["status"] for row in rows)
    lines = ["Reviews"]
    for key, label in (
        ("running", "Running"),
        ("pending", "Queued PRs"),
        ("retry", "Retrying PRs"),
    ):
        numbers = sorted({row["number"] for row in rows if row["status"] == key})
        if key != "running" and not numbers:
            continue
        displayed = ", ".join(f"#{number}" for number in numbers[:20])
        if len(numbers) > 20:
            displayed += f", … ({len(numbers):,} total)"
        lines.append(f"{label}: {displayed or 'none'}")
    lines.append(f"Queued: {counts['pending']:,} · Retrying: {counts['retry']:,}")
    lines.extend(
        [
            "",
            f"All-time runs: {counts['complete']:,} completed · {counts['failed']:,} failed · {counts['superseded']:,} superseded",
        ]
    )
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
    lines.append(f"Saved reviews: {saved:,} · Skipped: {skipped:,}")
    ordered = [
        severity
        for severity in ("critical", "major", "minor", "suggestion", "unknown")
        if severity in severities
    ]
    ordered.extend(sorted(severities.keys() - set(ordered)))
    labels = {"suggestion": "suggestions", "unknown": "unknown severity"}
    detail = " · ".join(
        f"{severities[severity]:,} {labels.get(severity, severity)}"
        for severity in ordered
    )
    lines.append(
        f"Findings: {detail or ('unavailable' if saved and not exported else '0')}"
    )
    lines.append("Finding totals include repeated reviews of the same PR.")
    if unavailable or malformed:
        lines.append(
            f"Unavailable finding exports: {unavailable}; unreadable results: {malformed}"
        )
    return lines


def _spend(state_dir, now):
    start = (now - timedelta(hours=24)).timestamp()
    end = now.timestamp()
    month = now.strftime("%Y-%m")
    lines = ["Estimated costs"]
    reservations = [0, 0]
    unknown = [0, 0]
    undated = False
    missing_sources = False
    available = 0
    for label, filename in (
        ("OpenAI", "spend.sqlite3"),
        ("PPQ", "ppq-spend.sqlite3"),
        ("Follow-up", "followup/spend.sqlite3"),
    ):
        totals = [Counter(), Counter()]
        ledger_undated = False
        try:
            with closing(stats._connect_readonly(state_dir / filename)) as db:
                rows = db.execute(
                    "SELECT requested_at, month, status, cost_micros, reserved_micros "
                    "FROM requests"
                )
                for row in rows:
                    dated = isinstance(row["requested_at"], (int, float))
                    ledger_undated |= not dated
                    periods = (
                        dated and start <= row["requested_at"] < end,
                        row["month"] == month,
                    )
                    for total, included in zip(totals, periods):
                        if not included:
                            continue
                        total["known_cost_micros"] += stats._nonnegative_int(
                            row["cost_micros"]
                        )
                        request_status = row["status"] or "unknown"
                        if request_status in stats.RESERVED_STATUSES:
                            total["reserved_micros"] += stats._nonnegative_int(
                                row["reserved_micros"]
                            )
                        if request_status in stats.UNKNOWN_COST_STATUSES:
                            total["unknown_cost_request_count"] += 1
        except sqlite3.Error:
            lines.append(f"{label}: unavailable")
            missing_sources = True
            continue
        available += 1
        lines.append(
            f"{label}: 24h ${totals[0]['known_cost_micros'] / 1_000_000:,.2f}"
            f" · month ${totals[1]['known_cost_micros'] / 1_000_000:,.2f}"
        )
        for index, period in enumerate(totals):
            reservations[index] += period["reserved_micros"]
            unknown[index] += period["unknown_cost_request_count"]
        undated |= ledger_undated
    if not available:
        return lines + ["", "Reservations and unknown costs: unavailable"]
    lines.extend(
        [
            "",
            "Additional reservations"
            + (" · available ledgers only" if missing_sources else ""),
            f"Last 24h: ${reservations[0] / 1_000_000:,.2f} · This month: ${reservations[1] / 1_000_000:,.2f}",
            f"Unknown-cost requests: {unknown[0]:,} last 24h · {unknown[1]:,} this month",
            "",
            "24h uses request start time; month uses UTC accounting month.",
        ]
    )
    if undated:
        lines.append("24h costs exclude historical requests without timestamps.")
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
    lines = [f"Ralph status · {now.day} {now:%b, %H:%M UTC}", ""]
    lines.extend(_services())
    lines.append("")
    lines.extend(_jobs(state_dir))
    lines.append("")
    lines.extend(_spend(state_dir, now))
    if not Path(report_dir).is_dir():
        lines.append("Report directory: unavailable")
    link = f"\n\nReports: {report_base_url.rstrip('/')}"
    return telegram.truncate_message(
        telegram.truncate_message(
            "\n".join(lines), 4096 - telegram.message_length(link)
        )
        + link
    )
