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


def upstream(payload, status=200):
    return Response({"status_code": status, "body": json.dumps(payload)})


class ProxyUsageTests(unittest.TestCase):
    def collect(self, responses):
        with patch.object(
            proxy_usage.urllib.request, "urlopen", side_effect=responses
        ) as request:
            result = proxy_usage.collect_usage(
                "http://localhost:8317/", "management-secret"
            )
        return result, request

    def test_native_management_flow_and_private_fields_are_not_published(self):
        result, request = self.collect(
            [
                Response(
                    {
                        "files": [
                            {
                                "provider": "codex",
                                "auth_index": "private-codex-index",
                                "email": "private@example.com",
                                "path": "/private/credential.json",
                                "id_token": {"chatgpt_account_id": "private-workspace"},
                            },
                            {
                                "provider": "claude",
                                "auth_index": "private-claude-index",
                            },
                            {
                                "provider": "codex",
                                "disabled": True,
                                "auth_index": "disabled-index",
                            },
                        ]
                    }
                ),
                upstream(
                    {
                        "plan_type": "pro",
                        "email": "private@example.com",
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 25,
                                "limit_window_seconds": 18000,
                                "reset_at": 1793023200,
                            },
                            "secondary_window": {
                                "used_percent": 40,
                                "limit_window_seconds": 604800,
                            },
                        },
                        "code_review_rate_limit": {
                            "primary_window": {"used_percent": 5}
                        },
                        "credits": {
                            "has_credits": True,
                            "unlimited": False,
                            "balance": "123.45",
                        },
                    }
                ),
                upstream(
                    {
                        "five_hour": {
                            "utilization": 9,
                            "resets_at": "2026-10-26T18:00:00Z",
                        },
                        "seven_day": {"utilization": 68},
                    }
                ),
            ]
        )
        self.assertEqual(result["status"], "ok")
        codex, claude = result["accounts"]
        self.assertEqual(codex["plan"], "pro")
        self.assertEqual(
            [window["name"] for window in codex["windows"]],
            ["5-hour", "Weekly", "Code review: Primary"],
        )
        self.assertEqual(codex["credits"]["balance"], "123.45")
        self.assertEqual(
            claude["windows"][0],
            {
                "name": "5-hour",
                "used_percent": 9,
                "resets_at": "2026-10-26T18:00:00Z",
            },
        )
        self.assertTrue(result["checked_at"].endswith("Z"))
        serialized = json.dumps(result)
        for private in (
            "private",
            "management-secret",
            "auth_index",
            "localhost",
            "https://",
        ):
            self.assertNotIn(private, serialized)
        calls = request.call_args_list
        self.assertEqual(len(calls), 3)
        first = calls[0].args[0]
        self.assertEqual(
            first.full_url, "http://localhost:8317/v8/management/credentials"
        )
        self.assertEqual(first.get_method(), "GET")
        self.assertEqual(first.get_header("Authorization"), "Bearer management-secret")
        second = calls[1].args[0]
        self.assertEqual(
            second.full_url, "http://localhost:8317/v8/management/requests/api-call"
        )
        self.assertEqual(second.get_method(), "POST")
        self.assertEqual(
            json.loads(second.data),
            {
                "auth_index": "private-codex-index",
                "method": "GET",
                "url": "https://chatgpt.com/backend-api/wham/usage",
                "header": {
                    "Authorization": "Bearer $TOKEN$",
                    "User-Agent": "codex-cli/1.0.0",
                    "Chatgpt-Account-Id": "private-workspace",
                },
            },
        )
        claude_call = json.loads(calls[2].args[0].data)
        self.assertEqual(
            claude_call["url"], "https://api.anthropic.com/api/oauth/usage"
        )
        self.assertEqual(claude_call["header"]["anthropic-beta"], "oauth-2025-04-20")
        self.assertTrue(all(call.kwargs["timeout"] == 10 for call in calls))

    def test_upstream_failure_is_partial_and_error_details_are_discarded(self):
        result, _ = self.collect(
            [
                Response(
                    {
                        "files": [
                            {"provider": "codex", "auth_index": "one"},
                            {"provider": "claude", "auth_index": "two"},
                        ]
                    }
                ),
                upstream({"rate_limit": {"primary_window": {"used_percent": 0}}}),
                upstream(
                    {"error": "private credential and billing details"}, status=429
                ),
            ]
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["accounts"][0]["windows"][0]["used_percent"], 0)
        self.assertEqual(
            result["accounts"][1],
            {
                "provider": "claude",
                "status": "unavailable",
                "plan": None,
                "windows": [],
            },
        )
        self.assertNotIn("private", json.dumps(result))

    def test_missing_usage_is_unknown_and_scoped_windows_take_precedence(self):
        result, _ = self.collect(
            [
                Response(
                    {
                        "files": [
                            {"provider": "claude", "auth_index": "one"},
                            {"provider": "claude", "auth_index": "two"},
                        ]
                    }
                ),
                upstream({"five_hour": {"resets_at": "2026-10-26T18:00:00Z"}}),
                upstream(
                    {
                        "limits": [
                            {"kind": "session", "percent": 0, "resets_at": "invalid"},
                            {
                                "kind": "weekly_scoped",
                                "percent": 12,
                                "scope": {"model": {"id": "claude-sonnet-4-5"}},
                            },
                        ],
                        "five_hour": {"utilization": 77},
                        "seven_day_sonnet": {"utilization": 88},
                        "seven_day_opus": {"utilization": 100},
                    }
                ),
            ]
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["accounts"][0]["windows"], [])
        self.assertEqual(
            result["accounts"][1]["windows"],
            [
                {"name": "5-hour", "used_percent": 0, "resets_at": None},
                {"name": "Sonnet: Weekly", "used_percent": 12, "resets_at": None},
                {"name": "Opus: Weekly", "used_percent": 100, "resets_at": None},
            ],
        )

    def test_claude_inactive_constraints_still_report_usage(self):
        result, _ = self.collect(
            [
                Response({"files": [{"provider": "claude", "auth_index": "one"}]}),
                upstream(
                    {
                        "limits": [
                            {"kind": "session", "percent": 9, "is_active": False},
                            {"kind": "weekly_all", "percent": 68, "is_active": False},
                            {
                                "kind": "weekly_scoped",
                                "percent": 100,
                                "is_active": True,
                                "scope": {"model": {"id": "claude-opus-4-1"}},
                            },
                        ]
                    }
                ),
            ]
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            result["accounts"][0]["windows"],
            [
                {"name": "5-hour", "used_percent": 9, "resets_at": None},
                {"name": "Weekly", "used_percent": 68, "resets_at": None},
                {"name": "Opus: Weekly", "used_percent": 100, "resets_at": None},
            ],
        )

    def test_transport_and_malformed_responses_are_unavailable(self):
        for response in (
            urllib.error.URLError("private upstream error"),
            Response({"files": "not a credentials list"}),
            Response({"files": [{"provider": "claude", "auth_index": "one"}]}),
        ):
            with self.subTest(response=type(response).__name__):
                responses = [response]
                if isinstance(response, Response):
                    responses.append(Response({"status_code": 200, "body": "not json"}))
                result, _ = self.collect(responses)
                self.assertEqual(result["status"], "unavailable")
                self.assertNotIn("private", json.dumps(result))
        with patch.object(proxy_usage, "_MAX_RESPONSE_BYTES", 1):
            result, _ = self.collect([Response({"files": []})])
        self.assertEqual(result["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
