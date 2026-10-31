"""Passive quota collection tests without network access."""

import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from ralph_service import proxy_usage


class Response(io.BytesIO):
    status = 200

    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode())


def observation(provider, signals, observed_at="2026-10-31T20:00:00Z", **fields):
    return {
        "provider": provider,
        "quota": {"observed_at": observed_at, "signals": signals},
        **fields,
    }


class ProxyUsageTests(unittest.TestCase):
    def collect(self, response):
        with patch.object(
            proxy_usage.urllib.request, "urlopen", side_effect=[response]
        ) as request:
            result = proxy_usage.collect_usage(
                "http://localhost:8317/", "management-secret"
            )
        request.assert_called_once()
        return result, request.call_args.args[0]

    def test_one_local_read_preserves_observations_and_discards_private_fields(self):
        result, request = self.collect(
            Response(
                {
                    "files": [
                        observation(
                            "codex",
                            {
                                "X-Codex-Plan-Type": "pro",
                                "X-Codex-Primary-Used-Percent": "56",
                                "X-Codex-Primary-Window-Minutes": "10080",
                                "X-Codex-Primary-Reset-At": "1793774355",
                                "X-Codex-Secondary-Window-Minutes": "0",
                                "X-Codex-Secondary-Used-Percent": "0",
                                "X-Codex-Credits-Has-Credits": "False",
                                "X-Codex-Credits-Unlimited": "False",
                                "X-Codex-Credits-Balance": "0",
                                "X-Codex-Unknown": "private upstream metadata",
                            },
                            auth_index="private-index",
                            email="private@example.com",
                            id_token={"chatgpt_account_id": "private-workspace"},
                        ),
                        observation(
                            "claude",
                            {
                                "Anthropic-Ratelimit-Unified-5h-Utilization": "0.03",
                                "Anthropic-Ratelimit-Unified-5h-Reset": "1793487000",
                                "anthropic-ratelimit-unified-7d-utilization": "0.58",
                                "anthropic-ratelimit-unified-7d_oi-utilization": "1.02",
                            },
                        ),
                        observation("codex", {}, disabled=True),
                    ]
                }
            )
        )
        self.assertEqual(
            request.full_url, "http://localhost:8317/v8/management/credentials"
        )
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(
            request.get_header("Authorization"), "Bearer management-secret"
        )
        self.assertEqual(result["status"], "ok")
        codex, claude = result["accounts"]
        self.assertEqual(codex["plan"], "pro")
        self.assertEqual(codex["observed_at"], "2026-10-31T20:00:00Z")
        self.assertEqual(len(codex["windows"]), 1)
        self.assertEqual(codex["windows"][0]["window_seconds"], 604800)
        self.assertEqual(codex["windows"][0]["resets_at"], "2026-11-04T06:39:15Z")
        self.assertEqual(codex["credits"]["has_credits"], False)
        for window, expected in zip(claude["windows"], [3, 58, 100], strict=True):
            self.assertAlmostEqual(window["used_percent"], expected)
        self.assertEqual(claude["windows"][-1]["name"], "Fable: Weekly")
        self.assertEqual(claude["windows"][0]["resets_at"], "2026-10-31T22:50:00Z")
        for private in ("private", "management-secret", "auth_index", "signals"):
            self.assertNotIn(private, json.dumps(result))

    def test_relative_reset_uses_observation_time_not_collection_time(self):
        result, _ = self.collect(
            Response(
                {
                    "files": [
                        observation(
                            "codex",
                            {
                                "x-codex-primary-used-percent": "25",
                                "x-codex-primary-window-minutes": "300",
                                "x-codex-primary-reset-after-seconds": "3600",
                            },
                            observed_at="2020-01-01T01:00:00+01:00",
                        )
                    ]
                }
            )
        )
        account = result["accounts"][0]
        self.assertEqual(account["observed_at"], "2020-01-01T00:00:00Z")
        self.assertEqual(account["windows"][0]["resets_at"], "2020-01-01T01:00:00Z")
        self.assertEqual(account["windows"][0]["window_seconds"], 18000)

    def test_missing_and_malformed_observations_do_not_invent_capacity(self):
        result, _ = self.collect(
            Response(
                {
                    "files": [
                        observation(
                            "claude",
                            {
                                "anthropic-ratelimit-unified-5h-utilization": "nan",
                                "anthropic-ratelimit-unified-7d-utilization": "-0.1",
                                "anthropic-ratelimit-unified-7d_sonnet-utilization": True,
                            },
                        ),
                        observation(
                            "codex",
                            {"x-codex-primary-used-percent": "0"},
                            observed_at="invalid",
                        ),
                        observation(
                            "codex",
                            {
                                "x-codex-primary-used-percent": "0",
                                "x-codex-primary-window-minutes": "Infinity",
                                "x-codex-primary-reset-after-seconds": "1e300",
                                "x-codex-plan-type": "private plan",
                                "x-codex-credits-has-credits": "invalid",
                            },
                        ),
                    ]
                }
            )
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["accounts"][0]["windows"], [])
        self.assertEqual(result["accounts"][1]["windows"], [])
        known = result["accounts"][2]
        self.assertIsNone(known["plan"])
        self.assertIsNone(known["windows"][0]["resets_at"])
        self.assertIsNone(known["windows"][0]["window_seconds"])
        self.assertEqual(known["windows"][0]["used_percent"], 0)
        self.assertNotIn("credits", known)
        self.assertNotIn("private", json.dumps(result))

    def test_transport_and_malformed_responses_are_unavailable(self):
        for response in (
            urllib.error.URLError("private upstream error"),
            Response({"files": "not a credentials list"}),
            Response({"files": [{"provider": "claude"}]}),
        ):
            with self.subTest(response=type(response).__name__):
                result, _ = self.collect(response)
                self.assertEqual(result["status"], "unavailable")
                self.assertNotIn("private", json.dumps(result))
        with patch.object(proxy_usage, "_MAX_RESPONSE_BYTES", 1):
            result, _ = self.collect(Response({"files": []}))
        self.assertEqual(result["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
