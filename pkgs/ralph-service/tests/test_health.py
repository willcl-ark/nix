import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from contextlib import closing

from ralph_service import health, monitor, stats_page


class HealthTests(unittest.TestCase):
    def test_evidence_repair_and_eligible_saved_results(self):
        stage = {
            "status": "completed",
            "model": "test",
            "rejected_test_coverage": [{"assessment": {"paths": ["test/a.py"]}}],
            "test_coverage": [{"paths": ["test/a.py"], "status": "checked"}],
        }
        row = {"id": 1, "generation": 1, "number": 42, "status": "complete"}
        debug = {"stages": {"tests": stage, "skipped": {"status": "skipped"}}}
        self.assertEqual(health.stage_runs(debug, {}, row, None)[0]["reasons"], [])
        stage["test_coverage"] = []
        self.assertEqual(
            health.stage_runs(debug, {}, row, None)[0]["unrepaired_assessments"], 1
        )
        stage["rejected_test_coverage"].append({"assessment": None})
        self.assertEqual(
            health.stage_runs(debug, {}, row, None)[0]["unrepaired_assessments"], 2
        )
        stage["status"] = "invalid"
        self.assertIn(
            "Invalid stage output",
            health.stage_runs(debug, {}, row, None)[0]["reasons"],
        )
        row["status"] = "cancelled"
        self.assertEqual(health.stage_runs(debug, {}, row, None), [])

    def test_stats_to_notifications_threshold_restart_recovery_and_staleness(self):
        now = 1800000000
        runs, requests = [], []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "stats").mkdir()
            store = monitor.NotificationStore(root / "telegram.sqlite3")

            def add(job, failed):
                stage = {"model": "test", "status": "failed" if failed else "completed"}
                runs.extend(
                    health.stage_runs(
                        {"stages": {"state": stage}},
                        {},
                        {
                            "id": job,
                            "generation": 1,
                            "number": job,
                            "status": "complete",
                        },
                        f"{job}.html",
                    )
                )
                requests.append(
                    {
                        "repository_id": "core",
                        "job_id": job,
                        "generation": 1,
                        "stage": "state",
                        "completed_at": now + job,
                    }
                )

            def observe(at):
                summary = {
                    "generated_at": datetime.fromtimestamp(at, UTC).isoformat(),
                    "health": health.summarize(runs, requests, at),
                }
                (root / "stats/stats.json").write_text(json.dumps(summary))
                store.observe_health(root, "https://reviews.example/traces", now=at)
                return summary

            def messages():
                with closing(store._connect()) as db:
                    return list(db.execute("SELECT text FROM outbox ORDER BY rowid"))

            for job in range(1, 5):
                add(job, job <= 3)
            observe(now + 5)
            self.assertEqual(messages(), [])
            add(5, False)
            summary = observe(now + 6)
            self.assertEqual(len(messages()), 1)
            self.assertIn("3/5", messages()[0][0])
            self.assertIn("https://reviews.example/stats/health/", messages()[0][0])
            store = monitor.NotificationStore(store.path)
            observe(now + 7)
            self.assertEqual(len(messages()), 1)
            for job in range(6, 9):
                add(job, False)
                observe(now + job)
            self.assertEqual(len(messages()), 2)
            self.assertIn("recovered", messages()[-1][0])
            for job in range(9, 12):
                add(job, True)
            observe(now + 12)
            self.assertEqual(len(messages()), 3)
            store.observe_health(root, "https://reviews.example/traces", now=now + 900)
            self.assertEqual(len(messages()), 3)
            self.assertEqual(
                health.summarize(runs, requests, now + 90000)["groups"], []
            )
            page = stats_page._health_html(summary, "https://reviews.example/traces")
            self.assertIn("3/5", page)
            self.assertIn("overwritten retry attempts", page)
