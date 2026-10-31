import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from ralph.usage import Ledger
from ralph_service import followup_runner, jobs, stats, stats_cache


class StatsCacheTests(unittest.TestCase):
    def test_incremental_matches_full_after_mutations_and_publication_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = root / "reports"
            reports.mkdir()
            store = jobs.JobStore(root / "jobs.sqlite3")
            ledger = Ledger(root / "usage.sqlite3")
            followups = root / "followup" / "assessments.sqlite3"
            followups.parent.mkdir()
            followup_runner.init_assessments(followups, [])
            cache_dir = root / "stats"
            now = datetime(2026, 10, 31, tzinfo=timezone.utc)

            def collect(*, full=False):
                with patch.object(stats, "datetime") as clock:
                    clock.now.return_value = now
                    # Other datetime helpers still need real parsing.
                    clock.fromtimestamp.side_effect = datetime.fromtimestamp
                    clock.fromisoformat.side_effect = datetime.fromisoformat
                    with stats_cache.Cache(cache_dir, full=full) as cache:
                        return stats.collect_stats(root, reports, cache=cache)

            def equivalent():
                incremental = collect()
                self.assertEqual(incremental, collect(full=True))
                return incremental

            equivalent()
            store.enqueue(
                42, "master", "a" * 40, "opened", engine_identity={"name": "current"}
            )
            job = store.claim()
            token = ledger.start(
                "tests", "test-model", f"pr:42:{job['id']}:{job['generation']}"
            )
            equivalent()
            result = {
                "schema_version": 1,
                "head_sha": "a" * 40,
                "run": {"status": "completed", "pr_title": "A review"},
                "output": {
                    "schema_version": 2,
                    "data": {
                        "schema_version": 2,
                        "stages": {
                            "tests": {
                                "model": "test-model",
                                "status": "completed",
                                "raw_output": '{"findings": []}',
                            }
                        },
                    },
                    "exports": {
                        "findings": [
                            {
                                "id": "finding:1",
                                "title": "A finding",
                                "severity": "major",
                            }
                        ]
                    },
                },
            }
            store.save_result(job, result)
            store.complete(job)
            ledger.complete(
                token,
                {"id": "response", "usage": {"input_tokens": 10, "output_tokens": 2}},
            )
            followup_runner.save_assessment(
                followups, job["id"], {"status": "completed", "findings": []}
            )
            equivalent()

            # No-change cycles never parse historical result payloads.
            with patch.object(
                stats, "_review_summary", side_effect=AssertionError("historical read")
            ):
                collect()
            with closing(sqlite3.connect(cache_dir / "cache.sqlite3")) as db, db:
                summary = db.execute(
                    "SELECT summary FROM records WHERE source LIKE '%:jobs'"
                ).fetchone()[0]
                self.assertNotIn("raw_output", summary)

            # Report appearance/removal changes links even without a job mutation.
            report = reports / f"42-{'a' * 40}-{job['id']}-{job['generation']}.html"
            report.write_text("report")
            self.assertIn("report_name", equivalent()["reviews"][0])
            report.unlink()
            self.assertNotIn("report_name", equivalent()["reviews"][0])

            with closing(sqlite3.connect(root / "jobs.sqlite3")) as db, db:
                result["run"]["pr_title"] = "Corrected title"
                db.execute(
                    "UPDATE jobs SET attempts=2, review_result=? WHERE id=?",
                    (json.dumps(result), job["id"]),
                )
            # A failed publication must not consume this mutation.
            with self.assertRaisesRegex(RuntimeError, "publish"):
                with stats_cache.Cache(cache_dir) as cache:
                    stats.collect_stats(root, reports, cache=cache)
                    raise RuntimeError("publish")
            self.assertEqual(equivalent()["reviews"][0]["pr_title"], "Corrected title")

            with closing(sqlite3.connect(root / "jobs.sqlite3")) as db, db:
                db.execute("DELETE FROM jobs")
            self.assertEqual(equivalent()["inventory"]["jobs_total"], 0)

    def test_overlapping_build_waits_for_publication_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            script = (
                "from ralph_service.stats_cache import Cache\n"
                "import sys\n"
                "with Cache(sys.argv[1]):\n"
                "    print('acquired')\n"
            )
            with stats_cache.Cache(directory):
                child = subprocess.Popen(
                    [sys.executable, "-c", script, directory],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        child.communicate(timeout=0.5)
                except BaseException:
                    child.kill()
                    child.communicate()
                    raise
            output, error = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, error)
            self.assertEqual(output.strip(), "acquired")

    def test_source_replacement_version_change_and_untracked_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "jobs.sqlite3"
            jobs.JobStore(path)
            calls = []

            def summary(row):
                calls.append(row["id"])
                return {"id": row["id"], "status": row["status"]}

            def read():
                with stats_cache.Cache(root / "stats") as cache:
                    return cache.rows(path, "jobs", "id", summary)

            with closing(sqlite3.connect(path)) as db, db:
                db.execute(
                    "INSERT INTO jobs (number,generation,base_ref,head,action,force,engine_identity,status) VALUES (1,0,'main','a','open',0,'{}','pending')"
                )
            self.assertEqual(read(), [{"id": 1, "status": "pending"}])
            read()
            self.assertEqual(calls, [1])
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("UPDATE stats_sources SET identity='replaced'")
            read()
            self.assertEqual(calls, [1, 1])
            with closing(sqlite3.connect(root / "stats" / "cache.sqlite3")) as db, db:
                db.execute("PRAGMA user_version=999")
            read()
            self.assertEqual(calls, [1, 1, 1])
            # SQLite REPLACE deletes UNIQUE conflicts without DELETE triggers.
            with closing(sqlite3.connect(path)) as db, db:
                db.execute(
                    "INSERT OR REPLACE INTO jobs (id,number,generation,base_ref,head,action,force,engine_identity,status) VALUES (2,1,0,'main','b','open',0,'{}','complete')"
                )
            self.assertEqual(read(), [{"id": 2, "status": "complete"}])
            # Pre-initialization stores must be fully reread, never stale-cached.
            with closing(sqlite3.connect(path)) as db, db:
                for operation in ("insert", "update", "delete"):
                    db.execute(f"DROP TRIGGER stats_jobs_{operation}")
                db.execute("DROP TABLE stats_sources")
            read()
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("UPDATE jobs SET status='complete'")
            self.assertEqual(read(), [{"id": 2, "status": "complete"}])


if __name__ == "__main__":
    unittest.main()
