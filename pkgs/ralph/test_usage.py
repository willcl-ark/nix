import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ralph.usage import Ledger, RequestUsage


class UsageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "usage.sqlite3"
        self.ledger = Ledger(self.path)
        self.requests = RequestUsage(self.ledger, "pr-1")

    @staticmethod
    def response():
        return {
            "id": "response-1",
            "model": "provider-model",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "input_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 10},
                "output_tokens_details": {"reasoning_tokens": 12},
            },
            "output": [
                {"type": "web_search_call", "action": {"type": "search"}},
                {"type": "web_search_call", "action": {"type": "open_page"}},
            ],
        }

    def start(self):
        return self.requests.start("reviewer", {"model": "any-new-model"})

    def test_retry_history_and_usage_survive_reopen(self):
        rejected = self.start()
        self.requests.fail(rejected, usage_unknown=False, http_status=429)
        uncertain = self.start()
        self.requests.fail(uncertain)
        self.start()
        completed = self.start()
        self.assertTrue(self.requests.complete(completed, self.response()))
        self.assertFalse(self.requests.fail(completed))
        self.assertFalse(self.requests.complete(uncertain, self.response()))
        summary = Ledger(self.path).summary(review_id="pr-1")
        self.assertEqual(
            summary["status_counts"],
            {
                "failed": 1,
                "uncertain": 1,
                "pending": 1,
                "completed": 1,
            },
        )
        self.assertEqual(summary["request_count"], 4)
        self.assertEqual(summary["unknown_request_count"], 2)
        self.assertFalse(summary["usage_complete"])
        self.assertEqual(summary["tokens"]["input_tokens"], 100)
        self.assertEqual(summary["tokens"]["reasoning_tokens"], 12)
        self.assertEqual(summary["web_search_calls"], 1)
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT http_status,completed_at FROM requests WHERE token=?",
                (rejected,),
            ).fetchone()
            self.assertEqual(row[0], 429)
            self.assertIsInstance(row[1], int)
            row = db.execute(
                "SELECT model,requested_at,completed_at FROM requests WHERE token=?",
                (completed,),
            ).fetchone()
            self.assertEqual(row[0], "provider-model")
            self.assertLessEqual(row[1], row[2])

    def test_statistics_cursor_tracks_completion_and_deleted_requests(self):
        with sqlite3.connect(self.path) as db:
            identity, sequence = db.execute(
                "SELECT identity,sequence FROM stats_sources WHERE table_name='requests'"
            ).fetchone()
        token = self.start()
        self.requests.complete(token, self.response())
        with sqlite3.connect(self.path) as db:
            changed = db.execute(
                "SELECT record_key,sequence FROM stats_changes "
                "WHERE table_name='requests' AND sequence>?",
                (sequence,),
            ).fetchall()
            self.assertEqual(len(changed), 1)
            self.assertEqual(changed[0][0], token)
            sequence = changed[0][1]
            # An interrupted maintenance correction must not advance the cursor.
            db.execute(
                "UPDATE requests SET model='replacement' WHERE token=?", (token,)
            )
            db.rollback()
            self.assertEqual(
                db.execute(
                    "SELECT sequence FROM stats_sources WHERE table_name='requests'"
                ).fetchone()[0],
                sequence,
            )
            db.execute("DELETE FROM requests WHERE token=?", (token,))
        Ledger(self.path)
        with sqlite3.connect(self.path) as db:
            reopened = db.execute(
                "SELECT identity,sequence FROM stats_sources WHERE table_name='requests'"
            ).fetchone()
            self.assertEqual(reopened[0], identity)
            self.assertGreater(reopened[1], sequence)
            self.assertEqual(
                db.execute(
                    "SELECT record_key FROM stats_changes "
                    "WHERE table_name='requests' AND sequence>?",
                    (sequence,),
                ).fetchall(),
                [(token,)],
            )
            self.assertIsNone(
                db.execute(
                    "SELECT token FROM requests WHERE token=?", (token,)
                ).fetchone()
            )

    def test_missing_usage_stays_unknown_and_partial_totals_are_reported(self):
        self.requests.complete(self.start(), {"id": "empty"})
        self.requests.complete(
            self.start(),
            {"id": "partial", "usage": {"input_tokens": 8, "output_tokens": 0}},
        )
        summary = self.requests.summary()
        self.assertEqual(summary["tokens"]["input_tokens"], 8)
        self.assertEqual(summary["tokens"]["output_tokens"], 0)
        self.assertIsNone(summary["tokens"]["cached_tokens"])
        self.assertIsNone(summary["web_search_calls"])
        self.assertEqual(summary["unknown_request_count"], 0)
        self.assertEqual(summary["incomplete_usage_count"], 2)
        self.assertFalse(summary["usage_complete"])
        with sqlite3.connect(self.path) as db:
            raw = db.execute(
                "SELECT usage_json FROM requests WHERE response_id='empty'"
            ).fetchone()[0]
        self.assertTrue(all(value is None for value in json.loads(raw).values()))

    def test_concurrent_duplicate_completion_counts_response_once(self):
        def complete_attempt(_):
            ledger = Ledger(self.path)
            token = ledger.start("reviewer", "model", "pr-1")
            return ledger.complete(token, self.response())

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(complete_attempt, range(16)))
        self.assertEqual(sum(results), 1)
        summary = self.requests.summary()
        self.assertEqual(summary["status_counts"], {"completed": 1, "duplicate": 15})
        self.assertEqual(summary["tokens"]["input_tokens"], 100)
        self.assertTrue(summary["usage_complete"])

    def test_summary_filters_reviews_and_request_months(self):
        self.requests.complete(self.start(), self.response())
        other = self.ledger.start("router", "other-model", "pr-2")
        self.ledger.fail(other, usage_unknown=False)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE requests SET month='2000-01' WHERE review_id='pr-1'")
        self.assertEqual(self.ledger.summary(month="2000-01")["request_count"], 1)
        self.assertEqual(self.requests.summary()["request_count"], 1)
        self.assertEqual(self.ledger.summary()["request_count"], 1)
        self.assertEqual(
            self.ledger.summary(month="2000-01", review_id="pr-2")["request_count"], 0
        )


if __name__ == "__main__":
    unittest.main()
