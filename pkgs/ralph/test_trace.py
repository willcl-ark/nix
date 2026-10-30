import json
import unittest

from ralph import trace
from ralph.trace import request_metrics, result_metrics, stage_metrics


class TraceTests(unittest.TestCase):
    def test_challenges_keep_original_candidates_and_downstream_disagreements(self):
        debug = {
            "candidate_sources": {
                "tests:1": "tests",
                "tests:2": "tests",
                "tests:3": "tests",
            },
            "challenges": [
                {
                    "candidate_id": "tests:1",
                    "outcome": "refuted",
                    "reason": "The guard excludes this input.",
                    "source_evidence": [],
                },
                {
                    "candidate_id": "tests:2",
                    "outcome": "not_refuted",
                    "reason": "The boundary remains uncovered.",
                    "source_evidence": [],
                },
                {
                    "candidate_id": "tests:3",
                    "outcome": "unresolved",
                    "reason": "Refutation evidence failed validation.",
                    "source_evidence": [],
                },
            ],
            "stages": {
                "tests": {
                    "raw_output": json.dumps(
                        {
                            "findings": [
                                {
                                    "title": "Wrong activation boundary",
                                    "path": "test/example.py",
                                    "line": 12,
                                    "side": "head",
                                },
                                {"title": "Missing activation assertion"},
                                {"title": "Uninspected fixture"},
                            ]
                        }
                    )
                },
                "challenger": {
                    "raw_output": json.dumps(
                        {
                            "challenges": [
                                {"candidate_id": "tests:3", "outcome": "refuted"}
                            ]
                        }
                    )
                },
                "verifier": {
                    "decisions": [
                        {
                            "candidate_ids": ["tests:1", "tests:2"],
                            "disposition": "supported",
                            "reason": "The guard checks another condition.",
                        }
                    ]
                },
            },
            "decisions": [
                {
                    "candidate_ids": ["tests:1", "tests:2"],
                    "disposition": "publish",
                    "reason": "The source establishes the gap.",
                    "finding": {"id": "finding:1", "title": "Merged boundary finding"},
                }
            ],
        }
        records = trace.challenge_records(debug)
        self.assertEqual(
            [row["title"] for row in records],
            [
                "Wrong activation boundary",
                "Missing activation assertion",
                "Uninspected fixture",
            ],
        )
        self.assertEqual(
            [row["final_disposition"] for row in records], ["publish", "publish", None]
        )
        self.assertEqual(records[0]["verifier_disposition"], "supported")
        self.assertEqual(records[0]["finding_id"], "finding:1")
        self.assertEqual(records[0]["source"], "tests")
        self.assertEqual(records[0]["path"], "test/example.py")
        self.assertEqual(records[2]["outcome"], "unresolved")
        self.assertIsNone(records[2]["verifier_disposition"])
        debug["decisions"] = debug["stages"]["verifier"]["decisions"]
        interrupted = trace.challenge_records(debug)
        self.assertEqual(interrupted[0]["verifier_disposition"], "supported")
        self.assertIsNone(interrupted[0]["final_disposition"])
        del debug["challenges"]
        self.assertIsNone(trace.challenge_records(debug))
        debug["challenges"] = []
        self.assertEqual(trace.challenge_records(debug), [])

    def test_usage_totals_do_not_depend_on_model_pricing(self):
        stage = {
            "model": "unlisted-subscription-model",
            "status": "completed",
            "turns": [
                {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cached_tokens": 80,
                    "cache_write_tokens": 10,
                    "reasoning_tokens": 12,
                    "elapsed_seconds": 1.25,
                    "request_bytes": 200,
                },
                {
                    "input_tokens": 200,
                    "output_tokens": 30,
                    "elapsed_seconds": 2.5,
                    "request_bytes": 250,
                },
                {
                    "input_tokens": 50,
                    "output_tokens": None,
                    "elapsed_seconds": 0.5,
                    "request_bytes": 250,
                },
            ],
            "tools": [{"name": "read_file"}],
        }
        metrics = request_metrics([stage])
        self.assertEqual(metrics["known_usage_count"], 2)
        self.assertEqual(metrics["unknown_usage_count"], 1)
        self.assertEqual(metrics["incomplete_usage_count"], 1)
        self.assertFalse(metrics["usage_complete"])
        self.assertEqual(metrics["total_input_tokens"], 350)
        self.assertEqual(metrics["total_output_tokens"], 50)
        self.assertEqual(metrics["total_cached_tokens"], 80)
        self.assertEqual(metrics["total_cache_write_tokens"], 10)
        self.assertEqual(metrics["total_reasoning_tokens"], 12)
        self.assertEqual(metrics["total_model_seconds"], 4.25)
        self.assertEqual(metrics["tool_calls"], 1)
        self.assertEqual(metrics["model_turns"], 3)
        self.assertEqual(metrics["model_responses"], 3)
        per_stage = stage_metrics({"stages": {"reviewer": stage}})["reviewer"]
        for key, value in metrics.items():
            self.assertEqual(per_stage[key], value)

    def test_saved_metrics_expose_current_fields_only(self):
        result = {
            "run": {
                "metrics": {
                    "estimated_cost_usd": 9.0,
                    "total_input_tokens": 100,
                    "unknown_usage_count": 1,
                }
            }
        }
        self.assertEqual(
            result_metrics(result),
            {"total_input_tokens": 100, "unknown_usage_count": 1},
        )
        self.assertIsNone(result_metrics({}))

    def test_reported_zero_usage_is_known(self):
        metrics = request_metrics(
            [
                {
                    "turns": [
                        {
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "cached_tokens": 0,
                            "cache_write_tokens": 0,
                            "reasoning_tokens": 0,
                        }
                    ]
                }
            ]
        )
        self.assertEqual(metrics["known_usage_count"], 1)
        self.assertEqual(metrics["unknown_usage_count"], 0)
        self.assertTrue(metrics["usage_complete"])


if __name__ == "__main__":
    unittest.main()
