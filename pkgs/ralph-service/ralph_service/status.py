"""Read-only operational summary for the Telegram monitor."""

import json
import math
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


def recorded_usage(state_dir, now, *, include_month=True):
    """Summarize persisted requests for the preceding day and optional UTC month."""
    rows, missing = stats._ledger_rows(state_dir)
    followups, followup_missing = stats._provider_ledger_rows(
        "followup", state_dir / "followup/spend.sqlite3"
    )
    missing["followup"] = followup_missing
    unavailable = [
        label
        for key, label in (
            ("openai", "review proxy"),
            ("ppq", "PPQ"),
            ("followup", "follow-up"),
        )
        if missing.get(key)
    ]
    lines = ["Recorded model usage"]
    if all(missing.values()):
        return lines + ["Usage unavailable: no readable request ledgers"]
    rows += followups
    start = (now - timedelta(hours=24)).timestamp()
    end = now.timestamp()
    month = now.strftime("%Y-%m")
    periods = [
        (
            "Last 24h",
            [
                row
                for row in rows
                if isinstance(row["requested_at"], (int, float))
                and start <= row["requested_at"] < end
            ],
        ),
    ]
    if include_month:
        periods.append(("UTC month", [row for row in rows if row["month"] == month]))
    for label, requests in periods:
        tokens = Counter()
        unknown = partial = 0
        for row in requests:
            tokens.update(row["tokens"])
            unknown += row["usage_unknown"]
            partial += row["usage_incomplete"] and not row["usage_unknown"]
        lines.extend(
            [
                f"{label}: {len(requests):,} requests",
                f"Reported tokens: input {tokens['input']:,} · output {tokens['output']:,} · cached {tokens['cached']:,}",
                f"Missing usage: {unknown:,} requests · Partial: {partial:,}",
            ]
        )
    lines.append("Reported tokens only; missing usage is unknown.")
    if unavailable:
        lines.append("Unavailable ledgers: " + ", ".join(unavailable))
    if any(not isinstance(row["requested_at"], (int, float)) for row in rows):
        lines.append("24h excludes historical requests without timestamps.")
    return lines


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return timestamp.astimezone(UTC) if timestamp.tzinfo is not None else None


def _subscriptions(report_dir, now):
    lines = ["Subscription capacity"]
    try:
        summary = json.loads((report_dir / "stats/stats.json").read_text())
        snapshot = summary.get("subscriptions") if isinstance(summary, dict) else None
    except (OSError, ValueError):
        snapshot = None
    if not isinstance(snapshot, dict):
        return lines + ["Quota snapshot unavailable"]
    checked = _timestamp(snapshot.get("checked_at"))
    if checked is None:
        lines.append("Checked: unknown; freshness unknown")
    else:
        stale = " · stale" if now - checked > timedelta(minutes=15) else ""
        lines.append(f"Checked: {checked:%d %b, %H:%M UTC}{stale}")
    if snapshot.get("status") != "ok":
        lines.append("Some or all subscription quota unavailable")
    accounts = snapshot.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        return lines + ["Subscription quota unknown"]
    for account in accounts[:4]:
        if not isinstance(account, dict):
            continue
        provider = account.get("provider") or "Unknown provider"
        windows = account.get("windows")
        if (
            account.get("status") != "ok"
            or not isinstance(windows, list)
            or not windows
        ):
            lines.append(f"{provider}: quota unknown")
            continue
        for window in windows[:4]:
            if not isinstance(window, dict):
                continue
            used = window.get("used_percent")
            remaining = (
                f"{100 - used:.1f}% remaining"
                if isinstance(used, (int, float))
                and not isinstance(used, bool)
                and math.isfinite(used)
                and 0 <= used <= 100
                else "remaining unknown"
            )
            reset = _timestamp(window.get("resets_at"))
            resets = reset.strftime("%d %b, %H:%M UTC") if reset else "unknown"
            lines.append(
                f"{provider} {window.get('name', 'window')}: {remaining}; resets {resets}"
            )
    lines.append("Quota includes other clients sharing each account.")
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
    lines.extend(recorded_usage(state_dir, now))
    lines.append("")
    lines.extend(_subscriptions(Path(report_dir), now))
    if not Path(report_dir).is_dir():
        lines.append("Report directory: unavailable")
    base_url = report_base_url.rstrip("/")
    link = f"\n\nReports: {base_url}\nStats: {base_url}/stats/"
    return telegram.truncate_message(
        telegram.truncate_message(
            "\n".join(lines), 4096 - telegram.message_length(link)
        )
        + link
    )
