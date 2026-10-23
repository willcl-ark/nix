import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from ralph import spend
from ralph.spend import (MICRODOLLARS, FINAL_TOOL_OUTPUT_HEADROOM_TOKENS,
                                      BudgetExceeded, Ledger, RequestBudget,
                                      price_usd, supports_model)


class SpendTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "spend.sqlite"

    def tearDown(self):
        self.directory.cleanup()

    @staticmethod
    def payload(output=100):
        return {"max_output_tokens": output, "input": "review"}

    def legacy_ledger(self):
        with sqlite3.connect(self.path) as db:
            db.execute("""CREATE TABLE requests (
                token TEXT PRIMARY KEY, review_id TEXT NOT NULL,
                stage TEXT NOT NULL, model TEXT NOT NULL, month TEXT NOT NULL,
                response_id TEXT, status TEXT NOT NULL, usage_json TEXT,
                cost_micros INTEGER, reserved_micros INTEGER NOT NULL
            )""")
            db.executemany(
                "INSERT INTO requests VALUES (?, 'pr-1', 'audit', 'gpt-6-luna', "
                "?, NULL, ?, NULL, ?, ?)",
                [("old", "2026-09", "settled", 100, 0),
                 ("pending", "2026-10", "uncertain", None, 500)])

    def test_timestamp_migration_preserves_history_and_backs_up_once(self):
        self.legacy_ledger()
        with sqlite3.connect(self.path) as db:
            original = db.execute("SELECT * FROM requests ORDER BY token").fetchall()
        self.assertTrue(spend.migrate_state(self.path))
        backup = Path(f"{self.path}.pre-timestamps.bak")
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(backup) as db:
            self.assertEqual(db.execute("SELECT * FROM requests ORDER BY token").fetchall(),
                             original)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT * FROM requests ORDER BY token").fetchall(),
                             [row + (None,) for row in original])
        backup_bytes = backup.read_bytes()
        self.assertFalse(spend.migrate_state(self.path))
        self.assertEqual(backup.read_bytes(), backup_bytes)
        ledger = Ledger(self.path)
        self.assertEqual(ledger.summary(month="2026-09")["estimated_total_usd"], 0.0001)
        self.assertEqual(ledger.summary(month="2026-10")["reserved_total_usd"], 0.0005)

    def test_timestamp_migration_refuses_to_overwrite_existing_backup(self):
        self.legacy_ledger()
        backup = Path(f"{self.path}.pre-timestamps.bak")
        backup.write_bytes(b"existing backup")
        original = self.path.read_bytes()
        with self.assertRaises(FileExistsError):
            spend.migrate_state(self.path)
        self.assertEqual(backup.read_bytes(), b"existing backup")
        self.assertEqual(self.path.read_bytes(), original)

    def test_timestamp_migration_rolls_back_schema_on_commit_failure(self):
        self.legacy_ledger()
        real_connect = sqlite3.connect

        class FailedCommit(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError("commit failed")

        def connect(path, *args, **kwargs):
            if "isolation_level" in kwargs:
                kwargs["factory"] = FailedCommit
            return real_connect(path, *args, **kwargs)

        with patch("ralph.spend.sqlite3.connect", side_effect=connect):
            with self.assertRaisesRegex(sqlite3.OperationalError, "commit failed"):
                spend.migrate_state(self.path)
        with sqlite3.connect(self.path) as db:
            self.assertNotIn("requested_at", {
                row[1] for row in db.execute("PRAGMA table_info(requests)")})
            self.assertEqual(db.execute("SELECT SUM(cost_micros) FROM requests").fetchone()[0],
                             100)
        self.assertTrue(Path(f"{self.path}.pre-timestamps.bak").is_file())

    def test_timestamp_migration_backups_include_wal_rows(self):
        self.legacy_ledger()
        db = sqlite3.connect(self.path)
        self.addCleanup(db.close)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("UPDATE requests SET cost_micros=123 WHERE token='old'")
        db.commit()
        self.assertTrue(spend.migrate_state(self.path))
        with sqlite3.connect(f"{self.path}.pre-timestamps.bak") as backup:
            self.assertEqual(backup.execute(
                "SELECT cost_micros FROM requests WHERE token='old'").fetchone()[0], 123)

    def test_ledger_initialization_upgrades_existing_timestamps(self):
        self.legacy_ledger()
        ledger = Ledger(self.path)
        token = ledger.reserve("audit", "gpt-6-luna", self.payload(), "pr-2")
        with sqlite3.connect(self.path) as db:
            self.assertIsInstance(db.execute(
                "SELECT requested_at FROM requests WHERE token=?", (token,)).fetchone()[0],
                                  int)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM requests WHERE requested_at IS NULL").fetchone()[0], 2)

    def test_reservation_records_one_utc_clock_read_and_keeps_it_on_completion(self):
        ledger = Ledger(self.path)
        before = datetime(2026, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
        after = datetime(2027, 1, 1, tzinfo=timezone.utc)
        with patch("ralph.spend.datetime", wraps=datetime) as clock:
            clock.now.side_effect = [before, after, after, after]
            tokens = [ledger.reserve("audit", "gpt-6-luna", self.payload(), "pr-1")
                      for _ in range(4)]
            self.assertEqual(clock.now.call_count, 4)
        response = {"id": "response", "model": "gpt-6-luna",
                    "usage": {"input_tokens": 100, "output_tokens": 10}}
        ledger.settle(tokens[0], response)
        ledger.fail(tokens[1])
        ledger.fail(tokens[2], charged_unknown=False)
        ledger.settle(tokens[3], response)
        with sqlite3.connect(self.path) as db:
            for index, token in enumerate(tokens):
                row = db.execute("SELECT requested_at, month FROM requests WHERE token=?",
                                 (token,)).fetchone()
                expected = before if index == 0 else after
                self.assertEqual(row, (int(expected.timestamp()), expected.strftime("%Y-%m")))
        self.assertFalse(Path(f"{self.path}.pre-timestamps.bak").exists())

    def test_migration_skips_missing_ledger_without_creating_it(self):
        self.assertFalse(spend.migrate_state(self.path))
        self.assertFalse(self.path.exists())

    def test_web_search_reserves_limit_and_settles_search_actions(self):
        ledger = Ledger(self.path, review_limit_usd=1, input_padding_tokens=0)
        payload = {**self.payload(), "tools": [{"type": "web_search"}],
                   "max_tool_calls": 3}
        estimate = ledger.reserve_estimate_micros("gpt-6-luna", payload)
        self.assertGreater(estimate, 30_000)
        self.assertLess(ledger.reserve_estimate_micros(
            "gpt-6-luna", {**payload, "tool_choice": "none"}), 1_000)
        with self.assertRaisesRegex(ValueError, "max_tool_calls"):
            ledger.reserve_estimate_micros(
                "gpt-6-luna", {**self.payload(), "tools": payload["tools"]})
        token = ledger.reserve("archaeologist", "gpt-6-luna", payload, "pr-1")
        response = {"id": "search-response", "model": "gpt-6-luna",
                    "usage": {"input_tokens": 100, "output_tokens": 10},
                    "output": [{"type": "web_search_call", "action": {"type": action}}
                               for action in ("search", "open_page", "find_in_page", "search")]}
        ledger.settle(token, response)
        self.assertEqual(ledger.summary(review_id="pr-1")["estimated_total_usd"],
                         float(price_usd("gpt-6-luna", {
                             **response["usage"], "web_search_calls": 2})))
        self.assertGreater(ledger.summary(review_id="pr-1")["estimated_total_usd"], 0.02)
        # Replayed provider responses do not charge the searches twice.
        ledger.settle(token, response)
        self.assertLess(ledger.summary(review_id="pr-1")["estimated_total_usd"], 0.021)

    def test_price_uses_request_model_and_long_context_rates(self):
        normal = {"input_tokens": 1_000_000,
                  "input_tokens_details": {"cached_tokens": 100_000,
                                           "cache_write_tokens": 100_000},
                  "output_tokens": 1_000_000}
        self.assertEqual(price_usd("gpt-6-luna", normal).as_tuple().exponent, -6)
        self.assertEqual(str(price_usd("gpt-6-luna", normal)), "0.937000")

        long_context = {"input_tokens": 272_001,
                        "input_tokens_details": {"cached_tokens": 0,
                                                 "cache_write_tokens": 0},
                        "output_tokens": 100}
        self.assertEqual(str(price_usd("gpt-6-luna", long_context)), "0.054476")
        self.assertEqual(str(price_usd("gpt-6.1-sol", long_context)), "1.089504")
        snapshot = "gpt-6-luna-2026-09-29"
        self.assertEqual(price_usd(snapshot, long_context),
                         price_usd("gpt-6-luna", long_context))
        self.assertIsNone(price_usd("gpt-6-luna-unrecognized", long_context))
        self.assertTrue(supports_model("gpt-6.1-sol"))
        self.assertFalse(supports_model("gpt-6-luna-unrecognized"))

    def test_glm_rates_have_no_cache_discount_or_long_context_multiplier(self):
        usage = {"input_tokens": 1_000_000,
                 "input_tokens_details": {"cached_tokens": 100_000,
                                          "cache_write_tokens": 100_000},
                 "output_tokens": 1_000_000}
        self.assertEqual(str(price_usd("glm-5.3", usage)), "6.119000")
        self.assertEqual(price_usd("z-ai/glm-5.3", usage),
                         price_usd("glm-5.3", usage))
        self.assertTrue(supports_model("glm-5.3"))
        self.assertTrue(supports_model("z-ai/glm-5.3"))
        del usage["input_tokens_details"]
        self.assertEqual(str(price_usd("glm-5.3", usage)), "6.119000")
        ledger = Ledger(self.path, review_limit_usd=10, input_padding_tokens=0)
        payload = {"max_output_tokens": 1_000_000}
        expected_input = ledger._payload_bytes(payload) + 1_000_000
        estimate = ledger.reserve_estimate_micros(
            "glm-5.3", payload, extra_input_tokens=1_000_000)
        self.assertEqual(estimate,
                         int(price_usd("glm-5.3", {"input_tokens": expected_input,
                             "output_tokens": 1_000_000}) * MICRODOLLARS))

    def test_ppq_adversarial_rates_and_long_context_estimates(self):
        usage = {"input_tokens": 1_000_000,
                 "input_tokens_details": {"cached_tokens": 100_000,
                                          "cache_write_tokens": 100_000},
                 "output_tokens": 1_000_000}
        for model, expected in (("deepseek/deepseek-v4-pro-0813", "2.717891"),
                                ("google/gemini-4-argon", "5.814375")):
            with self.subTest(model=model):
                self.assertTrue(supports_model(model))
                self.assertEqual(str(price_usd(model, usage)), expected)
                ledger = Ledger(self.path, review_limit_usd=10, input_padding_tokens=0)
                payload = {"max_output_tokens": 1_000_000}
                expected_input = ledger._payload_bytes(payload) + 1_000_000
                estimate = ledger.reserve_estimate_micros(
                    model, payload, extra_input_tokens=1_000_000)
                self.assertEqual(estimate, int(price_usd(model, {
                    "input_tokens": expected_input, "output_tokens": 1_000_000,
                }) * MICRODOLLARS))

    def test_missing_usage_is_unknown_and_missing_cache_writes_is_conservative(self):
        self.assertIsNone(price_usd("gpt-6-luna", {"input_tokens": 10}))
        usage = {"input_tokens": 1_000_000, "output_tokens": 0,
                 "input_tokens_details": {"cached_tokens": 0}}
        self.assertEqual(str(price_usd("gpt-6-luna", usage)), "0.250000")

        ledger = Ledger(self.path, review_limit_usd=1)
        token = ledger.reserve("audit", "gpt-6-luna", self.payload(), "pr-1")
        self.assertTrue(ledger.settle(token, {"id": "resp-1", "model": "gpt-6-luna",
                                             "usage": {"input_tokens": 10}}))
        summary = ledger.summary(review_id="pr-1")
        self.assertEqual(summary["unknown_request_count"], 1)
        self.assertEqual(summary["incomplete_usage_count"], 1)
        self.assertGreater(summary["reserved_total_usd"], 0)

    def test_reservations_enforce_limits_in_parallel(self):
        for monthly in (False, True):
            with self.subTest(monthly=monthly):
                ledger = Ledger(self.path.with_name(f"parallel-{monthly}.sqlite3"),
                                review_limit_usd=1 if monthly else 0.0008,
                                monthly_limit_usd=0.0008 if monthly else None,
                                input_padding_tokens=0)
                barrier = threading.Barrier(2)
                result = []

                def reserve(review_id):
                    barrier.wait(timeout=5)
                    try:
                        result.append(ledger.reserve("audit", "gpt-6-luna",
                                                     self.payload(1000), review_id))
                    except BudgetExceeded:
                        result.append("blocked")

                review_ids = ("pr-2", "pr-3" if monthly else "pr-2")
                threads = [threading.Thread(target=reserve, args=(review_id,))
                           for review_id in review_ids]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                self.assertEqual(result.count("blocked"), 1)
                self.assertEqual(len(result), 2)
                self.assertEqual(ledger.summary()["unknown_request_count"], 1)

    def test_settlement_deduplicates_response_and_known_refund_releases_reservation(self):
        ledger = Ledger(self.path, review_limit_usd=0.001, input_padding_tokens=0)
        first = ledger.reserve("audit", "gpt-6-luna", self.payload(100), "pr-3")
        second = ledger.reserve("audit", "gpt-6-luna", self.payload(100), "pr-3")
        response = {"id": "same-response", "model": "gpt-6-luna",
                    "usage": {"input_tokens": 100,
                              "input_tokens_details": {"cached_tokens": 0,
                                                       "cache_write_tokens": 0},
                              "output_tokens": 100}}
        self.assertTrue(ledger.settle(first, response))
        self.assertFalse(ledger.settle(second, response))
        self.assertFalse(ledger.settle(first, response))
        self.assertEqual(ledger.summary(review_id="pr-3")["unknown_request_count"], 0)

        third = ledger.reserve("audit", "gpt-6-luna", self.payload(), "pr-3")
        self.assertTrue(ledger.fail(third, charged_unknown=False))
        self.assertEqual(ledger.summary(review_id="pr-3")["reserved_total_usd"], 0)

    def test_restart_and_uncertain_failure_keep_the_originating_month(self):
        ledger = Ledger(self.path, review_limit_usd=1)
        token = ledger.reserve("verify", "gpt-6.1-sol", self.payload(), "pr-4")
        month = ledger.summary(review_id="pr-4")["month"]
        ledger.fail(token)

        restarted = Ledger(self.path, review_limit_usd=1)
        summary = restarted.summary(month=month, review_id="pr-4")
        self.assertEqual(summary["unknown_request_count"], 1)
        self.assertGreater(summary["reserved_total_usd"], 0)
        with self.assertRaises(ValueError):
            Ledger(self.path, review_limit_usd=1, monthly_limit_usd=0)
        with self.assertRaises(BudgetExceeded):
            Ledger(self.path, review_limit_usd=1, monthly_limit_usd=0.000001).reserve(
                "verify", "gpt-6.1-sol", self.payload(), "pr-4")

    def test_limits_are_finite_and_positive(self):
        for value in (0, -1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Ledger(self.path, review_limit_usd=value)

    def test_review_summary_and_cap_span_months(self):
        ledger = Ledger(self.path, review_limit_usd=0.001, input_padding_tokens=0)
        with patch("ralph.spend.datetime", wraps=datetime) as clock:
            clock.now.side_effect = [datetime(2026, 9, 30, tzinfo=timezone.utc),
                                     datetime(2026, 10, 1, tzinfo=timezone.utc)]
            token = ledger.reserve("audit", "gpt-6-luna", self.payload(1000), "pr-5")
            self.assertTrue(ledger.settle(token, {
                "id": "resp-month", "model": "gpt-6-luna",
                "usage": {"input_tokens": 1000,
                          "input_tokens_details": {"cached_tokens": 0,
                                                   "cache_write_tokens": 0},
                          "output_tokens": 1000}}))
            with self.assertRaises(BudgetExceeded):
                ledger.reserve("audit", "gpt-6-luna", self.payload(1000), "pr-5")
        summary = ledger.summary(review_id="pr-5")
        self.assertIsNone(summary["month"])
        self.assertAlmostEqual(summary["estimated_total_usd"], 0.0006)
        self.assertTrue(summary["usage_complete"])

    def test_protected_verifier_floor_preserves_start_and_final(self):
        sizing = Ledger(self.path.with_name("sizing.sqlite3"),
                        review_limit_usd=1, input_padding_tokens=0)
        verifier = {"model": "gpt-6.1-sol", "max_output_tokens": 25_000,
                    "input": "Candidate findings: []", "tools": [{"type": "function"}],
                    "tool_choice": "required"}
        discovery = {"model": "gpt-6-luna", "max_output_tokens": 1_000,
                     "input": "review"}
        initial = sizing.reserve_estimate_micros(verifier["model"], verifier)
        continuation = sizing.reserve_estimate_micros(
            verifier["model"], {**verifier, "tool_choice": "none"},
            extra_input_tokens=FINAL_TOOL_OUTPUT_HEADROOM_TOKENS)
        floor = initial + continuation
        discovery_cost = sizing.reserve_estimate_micros(discovery["model"], discovery)
        limit = (floor + discovery_cost) / MICRODOLLARS

        ledger = Ledger(self.path.with_name("protected.sqlite3"),
                        review_limit_usd=limit, input_padding_tokens=0)
        budget = RequestBudget(ledger, "pr-6")
        protection = budget.protect(verifier, exempt_stages={"verifier"})

        self.assertEqual(protection["protected_micros"], floor)
        self.assertEqual(protection["initial_usd"], initial / MICRODOLLARS)
        self.assertEqual(protection["continuation_usd"], continuation / MICRODOLLARS)
        budget.reserve("independent", discovery)
        token = budget.reserve("verifier", verifier)
        self.assertTrue(token)
        self.assertEqual(ledger.summary(review_id="pr-6")["unknown_request_count"], 2)

    def test_verifier_protection_uses_configured_model_rates(self):
        ledger = Ledger(self.path, review_limit_usd=1, input_padding_tokens=0)
        budget = RequestBudget(ledger, "pr-7")
        payload = {"model": "gpt-6-luna", "max_output_tokens": 8_000,
                   "input": "Candidate findings: []"}
        luna = budget.protect(payload)["protected_micros"]
        sol = budget.protect({**payload, "model": "gpt-6.1-sol"})[
            "protected_micros"]
        self.assertGreater(sol, luna)


if __name__ == "__main__":
    unittest.main()
