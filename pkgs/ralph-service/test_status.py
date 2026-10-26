import json
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ralph_service import status, telegram


class StatusTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.now = datetime(2026, 10, 26, 12, tzinfo=UTC)

    def message(self):
        return status.status_message(
            self.root, self.root / "reports", "https://reviews.example", now=self.now
        )

    def test_missing_sources_and_services(self):
        for error in (FileNotFoundError(), subprocess.TimeoutExpired("systemctl", 5)):
            with (
                self.subTest(error=error),
                patch.object(status.subprocess, "run", side_effect=error),
            ):
                text = self.message()
                self.assertIn("Service status unavailable", text)
                self.assertIn("Run history unavailable", text)
                self.assertIn("OpenAI: unavailable", text)
                self.assertIn("PPQ: unavailable", text)
                self.assertIn("Follow-up: unavailable", text)
                self.assertIn("Reservations and unknown costs: unavailable", text)
                self.assertTrue(text.endswith("https://reviews.example"))
                self.assertLessEqual(telegram.message_length(text), 4096)
        self.assertFalse((self.root / "jobs.sqlite3").exists())

    def test_service_states(self):
        output = "\n\n".join(
            f"Id={unit}\nLoadState=loaded\nActiveState={active}\nSubState={sub}\nResult={result}\nExecMainStartTimestampMonotonic=123"
            for unit, active, sub, result, _pid in (
                ("ralph-telegram.service", "active", "running", "success", 10),
                ("ralph.service", "failed", "failed", "exit-code", 0),
                ("ralph-stats.service", "inactive", "dead", "success", 0),
                ("ralph-stats.timer", "active", "waiting", "", 0),
                ("ralph-followup.service", "inactive", "dead", "exit-code", 0),
                ("ralph-followup.timer", "active", "waiting", "", 0),
            )
        )
        with patch.object(
            status.subprocess, "run", return_value=SimpleNamespace(stdout=output)
        ) as run:
            text = self.message()
        self.assertIn("Reviewer: failed: exit-code", text)
        self.assertIn("Stats: successful", text)
        self.assertIn("Follow-up: failed: exit-code", text)
        self.assertIn("Stats and follow-up timers: active", text)
        self.assertEqual(run.call_args.kwargs["timeout"], 5)
        self.assertNotIn("shell", run.call_args.kwargs)
        healthy = output.replace(
            "ActiveState=failed\nSubState=failed",
            "ActiveState=active\nSubState=running",
        ).replace("Result=exit-code", "Result=success")
        with patch.object(
            status.subprocess, "run", return_value=SimpleNamespace(stdout=healthy)
        ):
            text = self.message()
        self.assertIn("Reviewer and Telegram: running", text)
        self.assertIn("Last stats and follow-up runs: successful", text)
        self.assertNotIn(".service:", text)
        never_run = healthy.replace(
            "ExecMainStartTimestampMonotonic=123", "ExecMainStartTimestampMonotonic=0"
        )
        with patch.object(
            status.subprocess, "run", return_value=SimpleNamespace(stdout=never_run)
        ):
            self.assertIn("Stats: not run yet", self.message())

    def test_queue_and_exported_findings(self):
        with sqlite3.connect(self.root / "jobs.sqlite3") as db:
            db.execute(
                "CREATE TABLE jobs (id, number, generation, base_ref, head, action, force, status, attempts, review_result)"
            )
            for index, (state, result) in enumerate(
                (
                    ("pending", None),
                    ("running", None),
                    (
                        "complete",
                        {
                            "schema_version": 1,
                            "output": {
                                "schema_version": 1,
                                "exports": {
                                    "findings": [
                                        {"severity": "major"},
                                        {"severity": "minor"},
                                    ]
                                },
                            },
                        },
                    ),
                    ("failed", {"schema_version": 1}),
                    ("superseded", {"schema_version": 1, "run": {"skipped": True}}),
                    ("retry", None),
                ),
                1,
            ):
                db.execute(
                    "INSERT INTO jobs VALUES (?, ?, 1, 'master', 'abc', 'sync', 0, ?, 0, ?)",
                    (index, index + 100, state, json.dumps(result) if result else None),
                )
        with patch.object(status.subprocess, "run", side_effect=FileNotFoundError):
            text = self.message()
        self.assertIn("1 completed · 1 failed · 1 superseded", text)
        self.assertIn("Running: #102", text)
        self.assertIn("Queued PRs: #101", text)
        self.assertIn("Retrying PRs: #106", text)
        self.assertIn("Queued: 1 · Retrying: 1", text)
        self.assertIn("Saved reviews: 2 · Skipped: 1", text)
        self.assertIn("Findings: 1 major · 1 minor", text)
        self.assertIn("Finding totals include repeated reviews of the same PR.", text)
        self.assertIn("Unavailable finding exports: 1", text)

    def test_cost_windows_include_all_ledgers_and_unknowns(self):
        start = self.now - timedelta(hours=24)
        month = self.now.replace(day=1, hour=0)
        for filename in (
            "spend.sqlite3",
            "ppq-spend.sqlite3",
            "followup/spend.sqlite3",
        ):
            path = self.root / filename
            path.parent.mkdir(exist_ok=True)
            with sqlite3.connect(path) as db:
                db.execute(
                    "CREATE TABLE requests (review_id, stage, model, month, status, usage_json, cost_micros, reserved_micros, requested_at)"
                )
                for when, cost, reserved, state in (
                    (month - timedelta(seconds=1), 8000000, 0, "complete"),
                    (month, 2000000, 0, "complete"),
                    (start - timedelta(seconds=1), 4000000, 0, "complete"),
                    (start, 1000000, 0, "complete"),
                    (self.now - timedelta(seconds=1), None, 3000000, "uncertain"),
                    (self.now, 16000000, 0, "complete"),
                    (None, 32000000, 0, "complete"),
                ):
                    db.execute(
                        "INSERT INTO requests VALUES ('pr:1:1:1', 'review', 'model', ?, ?, '{}', ?, ?, ?)",
                        (
                            when.strftime("%Y-%m") if when else "2026-10",
                            state,
                            cost,
                            reserved,
                            when.timestamp() if when else None,
                        ),
                    )
        with patch.object(status.subprocess, "run", side_effect=FileNotFoundError):
            text = self.message()
        for label in ("OpenAI", "PPQ", "Follow-up"):
            self.assertIn(f"{label}: 24h $1.00 · month $55.00", text)
        self.assertIn("Last 24h: $9.00 · This month: $9.00", text)
        self.assertIn("Unknown-cost requests: 3 last 24h · 3 this month", text)
        self.assertEqual(
            text.count("24h costs exclude historical requests without timestamps."), 1
        )
        (self.root / "ppq-spend.sqlite3").unlink()
        with patch.object(status.subprocess, "run", side_effect=FileNotFoundError):
            partial = self.message()
        self.assertIn("available ledgers only", partial)
        self.assertIn("Last 24h: $6.00 · This month: $6.00", partial)
        self.assertIn("Unknown-cost requests: 2 last 24h · 2 this month", partial)


if __name__ == "__main__":
    unittest.main()
