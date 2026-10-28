import hashlib
import io
import json
import subprocess
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ralph import model


class FakeUsage:
    def __init__(self):
        self.events = []
        self.requests = []
        self.http_statuses = []

    def start(self, stage, data):
        self.events.append(("start", stage, data["model"]))
        self.requests.append(data)
        return "token"

    def complete(self, token, response):
        self.events.append(("complete", token, response.get("id")))

    def fail(self, token, usage_unknown=True, http_status=None):
        self.events.append(("fail", token, usage_unknown))
        self.http_statuses.append(http_status)


class ModelUsageTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = SimpleNamespace(
            checkout=".",
            head_files=[],
            base_files=[],
            changed_paths=[],
            merge_base="other",
            diff_base_sha="base",
            head_sha="head",
        )

    def test_records_usage_before_callback_and_distinguishes_http_rejections(self):
        usage = FakeUsage()
        events = usage.events
        result = {
            "id": "response-1",
            "model": "gpt-6-luna",
            "status": "completed",
            "output": [],
            "usage": {"input_tokens": 10, "output_tokens": 4},
        }

        def callback(stage, _payload, _response):
            events.append(("callback", stage))

        with patch.object(model.transport, "request", return_value=result) as send:
            model.request_response(
                "secret",
                {"model": "gpt-6-luna", "stream": False},
                "tests",
                callback,
                usage,
            )
        self.assertIs(json.loads(send.call_args.args[1])["stream"], True)
        self.assertEqual(
            [event[0] for event in events], ["start", "complete", "callback"]
        )

        for code, expected_unknown in ((400, False), (429, False), (500, True)):
            usage.events.clear()
            error = urllib.error.HTTPError("url", code, "rejected", {}, None)
            with (
                patch.object(model.transport, "request", side_effect=error),
                patch.object(model.time, "sleep"),
                self.assertRaises(urllib.error.HTTPError),
            ):
                model.request_response(
                    "secret", {"model": "gpt-6-luna"}, "tests", usage=usage
                )
            self.assertEqual(usage.events[-1], ("fail", "token", expected_unknown))
            self.assertEqual(usage.http_statuses[-1], code)

    def test_stage_deadline_bounds_request_retries_without_new_attempts(self):
        usage = FakeUsage()
        clock = [100.0]

        def request(_url, _payload, _key, timeout):
            self.assertEqual(timeout, 2.0)
            clock[0] += timeout
            raise TimeoutError("request deadline")

        with (
            patch.object(model.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(model.time, "sleep"),
            patch.object(model.transport, "request", side_effect=request) as send,
            self.assertRaisesRegex(TimeoutError, "stage deadline expired"),
        ):
            model.request_response(
                "secret",
                {"model": "gpt-6.1-sol"},
                "write_verifier",
                usage=usage,
                timeout=1800,
                deadline=102.0,
            )
        self.assertEqual(send.call_count, 1)
        self.assertEqual([event[0] for event in usage.events], ["start", "fail"])

    def test_transient_requests_retry_same_conversation_and_account_each_attempt(self):
        usage = FakeUsage()
        debug = {}
        rejected = urllib.error.HTTPError(
            "url",
            429,
            "rejected",
            {"Retry-After": "60"},
            io.BytesIO(b'{"error":{"message":"Too many requests"}}'),
        )
        unavailable = urllib.error.HTTPError(
            "url",
            503,
            "unavailable",
            {},
            io.BytesIO(b'{"detail":"Upstream unavailable"}'),
        )
        result = {
            "id": "finished",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Review complete"}],
                }
            ],
        }
        with (
            patch.object(
                model.transport,
                "request",
                side_effect=[rejected, unavailable, result],
            ) as send,
            patch.object(model.time, "sleep") as sleep,
        ):
            answer, _ = model.run_audit(
                "secret",
                "verifier",
                "Verify.",
                "conversation",
                model_name="gpt-6-luna",
                max_output_tokens=100,
                usage=usage,
                debug=debug,
            )
        self.assertEqual(answer, "Review complete")
        self.assertEqual(len({call.args[1] for call in send.call_args_list}), 1)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [60, 2])
        self.assertEqual(
            usage.events,
            [
                ("start", "verifier", "gpt-6-luna"),
                ("fail", "token", False),
                ("start", "verifier", "gpt-6-luna"),
                ("fail", "token", True),
                ("start", "verifier", "gpt-6-luna"),
                ("complete", "token", "finished"),
            ],
        )
        self.assertEqual(
            [turn["http_status"] for turn in debug["turns"][:-1]], [429, 503]
        )
        self.assertEqual(debug["turns"][0]["error_message"], "Too many requests")
        self.assertTrue(debug["turns"][0]["retry"])

    def test_retry_after_delay_is_bounded(self):
        error = urllib.error.HTTPError(
            "url", 429, "rejected", {"Retry-After": "3600"}, None
        )
        with (
            patch.object(model.transport, "request", side_effect=error) as send,
            patch.object(model.time, "sleep") as sleep,
            self.assertRaises(urllib.error.HTTPError),
        ):
            model.request_response("secret", {"model": "gpt-6-luna"}, "tests")
        self.assertEqual(send.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [300, 300])

    def test_freshness_failure_preserves_safe_subprocess_diagnostic(self):
        for tool_loop in (False, True):
            with self.subTest(tool_loop=tool_loop):
                debug = {}
                usage = FakeUsage()
                error = subprocess.CalledProcessError(
                    128,
                    ["git", "ls-remote", "https://user:password@example.org/repo"],
                    stderr=(
                        b"fatal: unable to access https://user:password@example.org/repo: "
                        b"HTTP 503; ssh://transport-user:transport-pass@example.org/repo; "
                        b"Bearer credential; token=hidden; " + b"x" * 1000
                    ),
                )
                with (
                    patch.object(model.transport, "request") as send,
                    patch.object(model.time, "sleep") as sleep,
                    self.assertRaises(subprocess.CalledProcessError),
                ):
                    if tool_loop:
                        model.openai_review(
                            "secret",
                            "input",
                            self.snapshot,
                            prompt="Review.",
                            model="gpt-6-luna",
                            max_output_tokens=100,
                            debug=debug,
                            usage=usage,
                            is_current=Mock(side_effect=error),
                        )
                    else:
                        model.run_audit(
                            "secret",
                            "design",
                            "Review.",
                            "input",
                            model_name="gpt-6-luna",
                            max_output_tokens=100,
                            debug=debug,
                            usage=usage,
                            is_current=Mock(side_effect=error),
                        )
                send.assert_not_called()
                sleep.assert_not_called()
                self.assertEqual(usage.events, [])
                self.assertEqual(debug["status"], "failed")
                message = debug["error_message"]
                self.assertIn("exit status 128", message)
                self.assertIn("fatal: unable to access", message)
                self.assertIn("HTTP 503", message)
                self.assertLessEqual(len(message), 500)
                self.assertEqual(debug["turns"][-1]["error_message"], message)
                for private in (
                    "user:password",
                    "transport-user:transport-pass",
                    "credential",
                    "hidden",
                ):
                    self.assertNotIn(private, json.dumps(debug))

    def test_terminal_http_error_preserves_safe_provider_message_without_retry(self):
        debug = {}
        error = urllib.error.HTTPError(
            "url",
            400,
            "rejected",
            {},
            io.BytesIO(
                json.dumps(
                    {
                        "detail": (
                            "Unsupported parameter: max_tool_calls; Bearer secret; "
                            'api_key=hidden; {"token": "oauth credential", '
                            '"refresh_token": "refresh credential"}'
                        ),
                        "request": {"input": "private conversation"},
                    }
                ).encode()
            ),
        )
        with (
            patch.object(model.transport, "request", side_effect=error) as send,
            patch.object(model.time, "sleep") as sleep,
            self.assertRaises(urllib.error.HTTPError),
        ):
            model.run_audit(
                "secret",
                "verifier",
                "Verify.",
                "private conversation",
                model_name="gpt-6-luna",
                max_output_tokens=100,
                debug=debug,
            )
        send.assert_called_once()
        sleep.assert_not_called()
        self.assertIn("Unsupported parameter: max_tool_calls", debug["error_message"])
        self.assertNotIn("secret", debug["error_message"])
        self.assertNotIn("hidden", debug["error_message"])
        self.assertNotIn("oauth credential", debug["error_message"])
        self.assertNotIn("refresh credential", debug["error_message"])
        self.assertNotIn("private conversation", json.dumps(debug))
        self.assertEqual(debug["http_status"], 400)

    def test_stream_errors_keep_unknown_charges_and_retry_only_transient_failures(self):
        for code, retryable in (
            ("server_error", True),
            ("invalid_request_error", False),
        ):
            with self.subTest(code=code):
                usage = FakeUsage()
                failures = []
                callback = Mock()
                error = model.transport.StreamError(
                    {"code": code, "message": "Provider failed; Bearer secret"}
                )
                result = {"id": "finished", "status": "completed", "output": []}
                with (
                    patch.object(
                        model.transport, "request", side_effect=[error, result]
                    ) as send,
                    patch.object(model.time, "sleep") as sleep,
                ):
                    if retryable:
                        response, _, _ = model.request_response(
                            "secret",
                            {"model": "gpt-6-luna"},
                            "verifier",
                            callback,
                            usage,
                            on_retry_failure=failures.append,
                        )
                        self.assertEqual(response, result)
                    else:
                        with self.assertRaises(model.transport.StreamError):
                            model.request_response(
                                "secret",
                                {"model": "gpt-6-luna"},
                                "verifier",
                                callback,
                                usage,
                                on_retry_failure=failures.append,
                            )
                self.assertEqual(send.call_count, 2 if retryable else 1)
                self.assertEqual(callback.call_count, 1 if retryable else 0)
                self.assertEqual(sleep.call_count, 1 if retryable else 0)
                self.assertEqual(usage.events[1], ("fail", "token", True))
                message = model.request_failure_record({}, error)["error_message"]
                self.assertIn("Provider failed", message)
                self.assertNotIn("secret", message)
                if retryable:
                    self.assertEqual(
                        len({call.args[1] for call in send.call_args_list}), 1
                    )
                    self.assertEqual(failures[0]["error_message"], message)
                    self.assertEqual(
                        usage.events[-1], ("complete", "token", "finished")
                    )

    def test_transport_retry_is_bounded_and_checks_staleness_before_reserving(self):
        for stale in (False, True):
            with self.subTest(stale=stale):
                usage = FakeUsage()
                debug = {}
                states = iter([True, False] if stale else [True] * 3)
                expected = model.StaleReview if stale else TimeoutError
                with (
                    patch.object(
                        model.transport, "request", side_effect=TimeoutError
                    ) as send,
                    patch.object(model.time, "sleep"),
                    self.assertRaises(expected),
                ):
                    model.run_audit(
                        "secret",
                        "verifier",
                        "Verify.",
                        "conversation",
                        model_name="gpt-6-luna",
                        max_output_tokens=100,
                        usage=usage,
                        debug=debug,
                        is_current=lambda states=states: next(states),
                    )
                self.assertEqual(send.call_count, 1 if stale else 3)
                self.assertEqual(len(usage.requests), send.call_count)
                self.assertEqual(len(debug["turns"]), send.call_count)
                self.assertTrue(
                    all(
                        event[-1] is True
                        for event in usage.events
                        if event[0] == "fail"
                    )
                )

    def test_callback_failure_never_retries_a_completed_response(self):
        usage = FakeUsage()
        callback = Mock(side_effect=TimeoutError("local callback failed"))
        with (
            patch.object(
                model.transport, "request", return_value={"id": "paid"}
            ) as send,
            patch.object(model.time, "sleep") as sleep,
            self.assertRaises(TimeoutError),
        ):
            model.request_response(
                "secret",
                {"model": "gpt-6-luna"},
                "verifier",
                callback,
                usage,
            )
        send.assert_called_once()
        sleep.assert_not_called()
        self.assertEqual([event[0] for event in usage.events], ["start", "complete"])

    def test_inspection_limit_returns_final_answer_and_records_skipped_calls(self):
        calls = [
            {
                "type": "function_call",
                "call_id": f"call-{i}",
                "name": name,
                "arguments": json.dumps(args),
            }
            for i, (name, args) in enumerate(
                [
                    ("search_discussions", {"query": "prior change"}),
                    ("search_discussions", {"query": "another change"}),
                    ("read_commit", {"commit": "base", "path": "src/a.cpp"}),
                    ("read_commit", {"commit": "head", "path": "src/a.cpp"}),
                    ("read_file", {"path": "src/a.cpp", "start_line": 1}),
                    ("read_file", {"path": "src/b.cpp", "start_line": 1}),
                ]
            )
        ]
        raw = '{"coverage":{"status":"partial","limitations":["Caller not inspected"]}}'
        results = [
            {
                "id": "tools",
                "status": "completed",
                "output": [{"type": "web_search_call", "action": {"type": "search"}}]
                + calls,
                "usage": {"output_tokens": 30},
            },
            {
                "id": "final",
                "status": "completed",
                "usage": {"output_tokens": 20},
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": raw}],
                    }
                ],
            },
        ]
        debug = {}
        usage = FakeUsage()
        discussion = Mock(return_value="Discussion")
        with (
            patch.object(
                model.transport,
                "request",
                side_effect=results,
            ) as send,
            patch.object(model, "read_file", return_value="1: evidence") as read,
            patch.object(
                model, "read_commit", return_value="Commit evidence"
            ) as history,
        ):
            answer = model.openai_review(
                "secret",
                "patch",
                self.snapshot,
                debug,
                prompt="Review.",
                model="gpt-6-luna",
                max_tool_calls=6,
                max_context_calls=1,
                max_history_calls=1,
                max_web_search_calls_per_response=1,
                tools=model.ARCHAEOLOGY_TOOLS,
                allow_web_search=True,
                discussion_lookup=discussion,
                usage=usage,
                reasoning_effort="xhigh",
                max_output_tokens=25_000,
            )
        self.assertEqual(answer, raw)
        for call in send.call_args_list:
            self.assertNotIn("max_tool_calls", json.loads(call.args[1]))
        self.assertEqual(usage.requests[0]["max_tool_calls"], 1)
        read.assert_called_once()
        history.assert_called_once()
        discussion.assert_called_once()
        final_request = json.loads(send.call_args_list[-1].args[1])
        self.assertEqual(final_request["tool_choice"], "none")
        self.assertEqual(final_request["reasoning"], {"effort": "xhigh"})
        self.assertEqual(final_request["max_output_tokens"], 25_000)
        self.assertIn("Inspection limit reached", final_request["input"][-1]["output"])
        self.assertEqual(debug["max_tool_calls"], 6)
        self.assertEqual(debug["reasoning_effort"], "xhigh")
        self.assertEqual(debug["max_output_tokens"], 25_000)
        self.assertEqual(debug["tools"][-1]["skipped"], "inspection_limit")
        self.assertNotIn("skipped", debug["tools"][0])
        self.assertEqual(
            debug["limits"],
            {
                "output_tokens": {"limit": 25_000, "used": 30, "hit": False},
                "tool_calls": {"limit": 6, "used": 6, "hit": True},
                "context_calls": {"limit": 1, "used": 1, "hit": True},
                "history_calls": {"limit": 1, "used": 1, "hit": True},
                "web_search_calls_per_response": {"limit": 1, "used": 1, "hit": None},
            },
        )
        self.assertEqual(
            [event[0] for event in usage.events],
            ["start", "complete", "start", "complete"],
        )

    def test_failed_search_request_records_transmitted_payload(self):
        debug = {}
        error = urllib.error.HTTPError("url", 400, "rejected", {}, None)
        with (
            patch.object(model.transport, "request", side_effect=error) as send,
            self.assertRaises(urllib.error.HTTPError),
        ):
            model.openai_review(
                "secret",
                "patch",
                self.snapshot,
                debug,
                prompt="Review.",
                model="gpt-6-luna",
                max_output_tokens=model.MAX_OUTPUT_TOKENS,
                tools=model.ARCHAEOLOGY_TOOLS,
                allow_web_search=True,
            )
        payload = send.call_args.args[1]
        self.assertNotIn("max_tool_calls", json.loads(payload))
        self.assertEqual(debug["turns"][0]["request_bytes"], len(payload))
        self.assertEqual(
            debug["turns"][0]["request_sha256"], hashlib.sha256(payload).hexdigest()
        )

    def test_stale_review_between_turns_does_not_start_again(self):
        call = {
            "type": "function_call",
            "call_id": "call-1",
            "name": "read_file",
            "arguments": '{"path":"src/a.cpp","start_line":1}',
        }
        first = {
            "id": "response-tool",
            "model": "gpt-6-luna",
            "status": "completed",
            "output": [call],
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }
        debug = {}
        usage = FakeUsage()
        states = iter([True, False])
        with (
            patch.object(model.transport, "request", return_value=first) as send,
            patch.object(model, "read_file", return_value="1: content"),
            self.assertRaises(model.StaleReview),
        ):
            model.openai_review(
                "secret",
                "patch",
                self.snapshot,
                debug,
                prompt="Review.",
                model="gpt-6-luna",
                max_output_tokens=model.MAX_OUTPUT_TOKENS,
                usage=usage,
                max_tool_calls=1,
                is_current=lambda: next(states),
            )
        send.assert_called_once()
        self.assertEqual([event[0] for event in usage.events], ["start", "complete"])
        self.assertEqual(debug["limits"]["tool_calls"]["used"], 1)
        self.assertEqual(debug["limits"]["output_tokens"]["used"], 2)
        self.assertFalse(debug["limits"]["tool_calls"]["hit"])

    def test_claude_uses_auto_tools_preserves_thinking_and_requires_inspection(self):
        thinking = {
            "type": "reasoning",
            "id": "thinking-1",
            "encrypted_content": "opaque-signed-thinking",
            "summary": [],
        }
        first = {
            "status": "completed",
            "output": [
                thinking,
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "read_file",
                    "arguments": '{"path":"src/a.cpp","start_line":1}',
                },
            ],
        }
        final = {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": '{"findings":[]}'}],
                }
            ],
        }
        options = {
            "prompt": "Inspect code before returning findings.",
            "model": "claude-opus-5-5",
            "reasoning_effort": "xhigh",
            "max_output_tokens": 64_000,
            "max_tool_calls": 1,
            "first_tool_required": True,
        }
        with (
            patch.object(
                model.transport,
                "request",
                side_effect=[first, final],
            ) as send,
            patch.object(model, "read_file", return_value="1: evidence") as read,
        ):
            answer = model.openai_review("proxy-key", "patch", self.snapshot, **options)
        self.assertEqual(json.loads(answer), {"findings": []})
        read.assert_called_once()
        sent = [json.loads(call.args[1]) for call in send.call_args_list]
        self.assertEqual([request["tool_choice"] for request in sent], ["auto", "none"])
        self.assertEqual(sent[0]["reasoning"]["effort"], "xhigh")
        self.assertEqual(sent[1]["input"][1], thinking)
        self.assertEqual(sent[1]["input"][-1]["type"], "function_call_output")

        with (
            patch.object(model.transport, "request", return_value=final) as send,
            self.assertRaisesRegex(ValueError, "without inspecting"),
        ):
            model.openai_review("proxy-key", "patch", self.snapshot, **options)
        send.assert_called_once()

    def test_audit_output_limit_preserves_partial_usage(self):
        result = {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "usage": {"output_tokens": 100},
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Partial review"}],
                }
            ],
        }
        debug = {}
        with patch.object(model.transport, "request", return_value=result):
            answer, record = model.run_audit(
                "secret",
                "router",
                "Review.",
                "patch",
                model_name="gpt-6-luna",
                max_output_tokens=100,
                debug=debug,
            )
        self.assertEqual(answer, "Partial review")
        self.assertEqual(
            debug["limits"],
            {
                "output_tokens": {"limit": 100, "used": 100, "hit": True},
            },
        )
        self.assertEqual(record["limits"], debug["limits"])

        usage = FakeUsage()
        debug = {}
        with (
            patch.object(model.transport, "request", return_value=result) as send,
            self.assertRaisesRegex(ValueError, "did not complete"),
        ):
            model.openai_review(
                "secret",
                "patch",
                self.snapshot,
                debug,
                prompt="Review.",
                model="gpt-6-luna",
                max_output_tokens=100,
                usage=usage,
            )
        send.assert_called_once()
        self.assertEqual([event[0] for event in usage.events], ["start", "complete"])
        self.assertEqual(debug["status"], "incomplete")
        self.assertEqual(debug["raw_output"], "Partial review")
        self.assertEqual(debug["turns"][0]["output_tokens"], 100)


if __name__ == "__main__":
    unittest.main()
