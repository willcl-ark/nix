"""Durable Telegram notifications and UTC monitoring summaries."""

import argparse
import json
import logging
import sqlite3
import threading
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from . import commands, report, telegram

LOGGER = logging.getLogger(__name__)


def _readonly(path):
    return sqlite3.connect(f"file:{quote(str(path), safe='/:')}?mode=ro", uri=True)


class NotificationStore:
    def __init__(self, path):
        self.path = Path(path)
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS outbox (
                    key TEXT PRIMARY KEY, text TEXT NOT NULL,
                    sent INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS observed_jobs (
                    id INTEGER NOT NULL, generation INTEGER NOT NULL,
                    status TEXT NOT NULL, PRIMARY KEY (id, generation)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER NOT NULL, generation INTEGER NOT NULL,
                    status TEXT NOT NULL, day TEXT NOT NULL,
                    PRIMARY KEY (id, generation, status)
                );
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
            """)

    def _connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def observe(
        self, state_dir, report_base_url, repository_url, *, report_dir=None, now=None
    ):
        """Observe jobs and enqueue alerts in one transaction, without changing jobs."""
        now = time.time() if now is None else now
        period_day = (datetime.fromtimestamp(now, UTC) - timedelta(hours=21)).date()
        with closing(_readonly(Path(state_dir) / "jobs.sqlite3")) as jobs:
            rows = jobs.execute(
                "SELECT id, generation, number, status, review_result, head FROM jobs ORDER BY id"
            ).fetchall()
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT value FROM metadata WHERE key = 'period_day'"
            ).fetchone()
            failed = []
            for job_id, generation, number, status, raw_result, head in rows:
                old = db.execute(
                    "SELECT status FROM observed_jobs WHERE id = ? AND generation = ?",
                    (job_id, generation),
                ).fetchone()
                if (
                    previous
                    and (old is None or old[0] != status)
                    and (
                        status == "failed"
                        or (status == "complete" and raw_result is not None)
                    )
                ):
                    db.execute(
                        "INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?)",
                        (job_id, generation, status, str(period_day)),
                    )
                    if status == "failed":
                        failed.append((job_id, generation, number))
                    elif raw_result is not None:
                        result = json.loads(raw_result)
                        if not result.get("run", {}).get("report_url"):
                            try:
                                filename = report.save_report(
                                    report_dir,
                                    number,
                                    result,
                                    f"{number}-{head}-{job_id}-{generation}",
                                )
                            except (OSError, ValueError, TypeError):
                                LOGGER.warning(
                                    "Telegram review report unavailable; will retry"
                                )
                                continue
                            result.setdefault("run", {})["report_url"] = (
                                report_base_url.rstrip("/") + "/" + filename
                            )
                        db.execute(
                            "INSERT OR IGNORE INTO outbox (key, text) VALUES (?, ?)",
                            (
                                f"review:{job_id}:{generation}",
                                telegram.review_message(repository_url, number, result),
                            ),
                        )
                db.execute(
                    "INSERT OR REPLACE INTO observed_jobs VALUES (?, ?, ?)",
                    (job_id, generation, status),
                )
            if failed:
                key = "failures:" + ",".join(f"{j}:{g}" for j, g, _ in failed)
                db.execute(
                    "INSERT OR IGNORE INTO outbox (key, text) VALUES (?, ?)",
                    (key, _failure_message(failed, repository_url)),
                )
            if previous and previous[0] < str(period_day):
                completed_day = period_day - timedelta(days=1)
                counts = dict(
                    db.execute(
                        "SELECT status, count(*) FROM events WHERE day = ? GROUP BY status",
                        (str(completed_day),),
                    )
                )
                pending = sum(row[3] in {"pending", "retry"} for row in rows)
                running = sum(row[3] == "running" for row in rows)
                text = (
                    f"Ralph daily digest: {completed_day} 21:00 to {period_day} 21:00 UTC\n"
                    f"Reviews completed: {counts.get('complete', 0)}\n"
                    f"Terminal failures: {counts.get('failed', 0)}\n"
                    f"Queue now: {pending} pending, {running} running\n"
                    "Counts use the period when this monitor observed completion.\n"
                    + _daily_spend(Path(state_dir), completed_day)
                )
                if report_base_url:
                    text += "\nReports: " + report_base_url.rstrip("/")
                db.execute(
                    "INSERT OR IGNORE INTO outbox (key, text) VALUES (?, ?)",
                    (f"digest:{completed_day}", telegram.truncate_message(text)),
                )
            db.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('period_day', ?)",
                (str(period_day),),
            )

    def deliver(self, token, chat_id, *, now=None):
        """Send one due message per tick. Network failures never affect review jobs."""
        now = time.time() if now is None else now
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT key, text, next_attempt FROM outbox WHERE sent = 0 "
                "ORDER BY rowid LIMIT 1",
            ).fetchone()
        if row is None or row[2] > now:
            return
        try:
            telegram.send_message(token, chat_id, row[1])
        except telegram.TelegramError as exc:
            # Exception strings from HTTP clients may contain the bot token URL.
            LOGGER.warning("Telegram delivery failed; notification remains queued")
            delay = getattr(exc, "retry_after", 60)
            if not isinstance(delay, (int, float)) or delay < 1:
                delay = 60
            with closing(self._connect()) as db, db:
                db.execute(
                    "UPDATE outbox SET next_attempt = ? WHERE key = ?",
                    (now + max(60, delay), row[0]),
                )
        else:
            with closing(self._connect()) as db, db:
                db.execute("UPDATE outbox SET sent = 1 WHERE key = ?", (row[0],))


def _failure_message(failed, repository_url):
    text = f"Ralph: {len(failed)} terminal review failure(s)\n"
    for index, (_, _, number) in enumerate(failed):
        line = f"\nPR #{number}: {repository_url.rstrip('/')}/pulls/{number}"
        remainder = f"\n…and {len(failed) - index} more."
        if telegram.message_length(text + line + remainder) > 4096:
            return text + remainder
        text += line
    return text


def _daily_spend(state_dir, day):
    start = (
        datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=21)
    ).timestamp()
    lines = ["Spend for requests started in this period:"]
    for label, filename in (
        ("OpenAI", "spend.sqlite3"),
        ("PPQ", "ppq-spend.sqlite3"),
        ("Follow-up", "followup/spend.sqlite3"),
    ):
        try:
            with closing(_readonly(state_dir / filename)) as db:
                rows = db.execute(
                    "SELECT cost_micros, reserved_micros, status FROM requests "
                    "WHERE requested_at >= ? AND requested_at < ?",
                    (start, start + 86400),
                ).fetchall()
        except sqlite3.Error:
            lines.append(f"{label}: unavailable")
            continue
        known = sum(cost for cost, _, _ in rows if cost is not None)
        outstanding = [
            value
            for _, value, status in rows
            if status in {"reserved", "uncertain", "unknown"}
        ]
        unknown = len(outstanding)
        reserved = sum(value for value in outstanding if value is not None)
        detail = f"{label}: ${known / 1_000_000:.6f} known"
        if unknown:
            detail += (
                f", {unknown} unknown cost(s), ${reserved / 1_000_000:.6f} reserved"
            )
            unknown_reservations = sum(value is None for value in outstanding)
            if unknown_reservations:
                detail += f", {unknown_reservations} unknown reservation(s)"
        lines.append(detail)
    return "\n".join(lines)


def run_monitor(
    state_dir,
    report_dir,
    report_base_url,
    repository_url,
    token,
    chat_id,
    stop_event=None,
):
    """Observe reviews and poll commands independently of the review daemon."""
    stop_event = stop_event or threading.Event()
    store = None
    bot = None
    next_scan = 0
    while not stop_event.is_set():
        if store is None:
            try:
                store = NotificationStore(Path(state_dir) / "telegram.sqlite3")
            except (OSError, sqlite3.Error):
                LOGGER.warning("Telegram notification state unavailable; will retry")
                stop_event.wait(30)
                continue
        if time.monotonic() >= next_scan:
            try:
                store.observe(
                    state_dir, report_base_url, repository_url, report_dir=report_dir
                )
            except (OSError, sqlite3.Error):
                LOGGER.warning("Telegram monitor could not read state; will retry")
            try:
                store.deliver(token, chat_id)
            except (OSError, sqlite3.Error):
                LOGGER.warning("Telegram notification state unavailable; will retry")
            next_scan = time.monotonic() + 30
        try:
            if bot is None:
                bot = telegram.api_request(token, "getMe", {})
            commands.poll_commands(
                store,
                token,
                chat_id,
                bot,
                state_dir,
                report_dir,
                report_base_url,
                timeout=max(1, min(20, int(next_scan - time.monotonic()))),
            )
        except telegram.TelegramError as exc:
            LOGGER.warning("Telegram command polling failed; will retry")
            delay = exc.retry_after
            stop_event.wait(
                delay if isinstance(delay, (int, float)) and delay > 0 else 60
            )
        except (OSError, sqlite3.Error):
            LOGGER.warning("Telegram command state unavailable; will retry")
            stop_event.wait(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--report-base-url", required=True)
    parser.add_argument("--repository-url", required=True)
    parser.add_argument("--telegram-token-file", type=Path, required=True)
    parser.add_argument("--telegram-chat-id", required=True)
    args = parser.parse_args()
    try:
        token = args.telegram_token_file.read_text().strip()
    except (OSError, UnicodeError):
        parser.error("could not read Telegram token file")
    if not token or not args.telegram_chat_id.strip():
        parser.error("Telegram token and chat ID must not be empty")
    args.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run_monitor(
        args.state_dir,
        args.report_dir,
        args.report_base_url.rstrip("/"),
        args.repository_url.rstrip("/"),
        token,
        args.telegram_chat_id,
    )
