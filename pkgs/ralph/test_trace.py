import unittest

from ralph.trace import request_metrics, result_metrics, stage_metrics


class TraceTests(unittest.TestCase):
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
