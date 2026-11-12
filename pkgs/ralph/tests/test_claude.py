import json
import unittest
from unittest.mock import patch

import test_transport
from ralph import model, transport
from test_transport import event, start_stream


class ClaudeTests(unittest.TestCase):
    server = test_transport.TransportTests.server

    def test_native_request_preserves_schema(self):
        schema = {"type": "object", "properties": {}, "additionalProperties": False}
        data = {
            "model": "claude-opus-5-5",
            "instructions": "Review",
            "input": [{"role": "user", "content": "diff"}],
            "max_output_tokens": 100,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "review",
                    "strict": True,
                    "schema": schema,
                }
            },
        }
        with patch.object(transport, "request", return_value={}) as send:
            model.request_response("key", data, "review", api_base="http://proxy/v1")
        self.assertEqual(send.call_args.args[0], "http://proxy/v1/messages")
        payload = json.loads(send.call_args.args[1])
        self.assertEqual(
            payload["output_config"]["format"],
            {"type": "json_schema", "schema": schema},
        )
        self.assertEqual(payload["messages"], data["input"])

    def test_native_stream_preserves_thinking_tools_and_usage(self):
        def respond(handler):
            self.assertEqual(handler.headers["x-api-key"], "key")
            self.assertEqual(handler.headers["anthropic-version"], "2023-06-01")
            start_stream(handler)
            handler.wfile.write(
                event(
                    "message_start",
                    message={
                        "id": "msg1",
                        "model": "claude-opus-5-5",
                        "content": [],
                        "usage": {
                            "input_tokens": 10,
                            "cache_read_input_tokens": 20,
                            "cache_creation_input_tokens": 5,
                            "output_tokens": 1,
                        },
                    },
                )
                + event(
                    "content_block_start",
                    index=0,
                    content_block={"type": "thinking", "thinking": "", "signature": ""},
                )
                + event(
                    "content_block_delta",
                    index=0,
                    delta={"type": "thinking_delta", "thinking": "reason"},
                )
                + event(
                    "content_block_delta",
                    index=0,
                    delta={"type": "signature_delta", "signature": "signed"},
                )
                + event("content_block_stop", index=0)
                + event(
                    "content_block_start",
                    index=1,
                    content_block={
                        "type": "tool_use",
                        "id": "call1",
                        "name": "read_file",
                        "input": {},
                    },
                )
                + event(
                    "content_block_delta",
                    index=1,
                    delta={"type": "input_json_delta", "partial_json": '{"path":'},
                )
                + event(
                    "content_block_delta",
                    index=1,
                    delta={"type": "input_json_delta", "partial_json": '"a.cpp"}'},
                )
                + event("content_block_stop", index=1)
                + event(
                    "message_delta",
                    delta={"stop_reason": "tool_use"},
                    usage={"output_tokens": 30},
                )
                + event("message_stop")
            )

        with self.server(respond) as (url, _):
            result = transport.request(
                url.replace("/responses", "/messages"), b"{}", "key", 2
            )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["usage"]["input_tokens"], 35)
        self.assertEqual(result["usage"]["output_tokens"], 30)
        data = {
            "model": "claude-opus-5-5",
            "input": [{"role": "user", "content": "diff"}]
            + result["output"]
            + [
                {
                    "type": "function_call_output",
                    "call_id": "call1",
                    "output": "file text",
                }
            ],
            "max_output_tokens": 100,
        }
        payload = json.loads(model._request_payload(data))
        self.assertEqual(
            payload["messages"][1]["content"],
            [
                {"type": "thinking", "thinking": "reason", "signature": "signed"},
                {
                    "type": "tool_use",
                    "id": "call1",
                    "name": "read_file",
                    "input": {"path": "a.cpp"},
                },
            ],
        )
        self.assertEqual(
            payload["messages"][2]["content"][0],
            {"type": "tool_result", "tool_use_id": "call1", "content": "file text"},
        )

    def test_native_stop_reasons_and_hosted_search(self):
        from ralph import claude

        for reason, status in (
            ("end_turn", "completed"),
            ("max_tokens", "incomplete"),
            ("refusal", "failed"),
            ("pause_turn", "incomplete"),
        ):
            with self.subTest(reason=reason):
                result = claude.response({"stop_reason": reason, "content": []})
                self.assertEqual(result["status"], status)
        data = {
            "model": "claude-opus-5-5",
            "tools": [{"type": "web_search"}],
            "max_tool_calls": 2,
        }
        self.assertEqual(
            claude.request(data)["tools"],
            [{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}],
        )
        result = claude.response(
            {
                "stop_reason": "end_turn",
                "content": [
                    {
                        "type": "server_tool_use",
                        "id": "search1",
                        "name": "web_search",
                        "input": {"query": "Bitcoin"},
                    },
                    {
                        "type": "web_search_tool_result",
                        "tool_use_id": "search1",
                        "content": [],
                    },
                ],
            }
        )
        self.assertEqual(model.web_search_calls(result), 1)

    def test_native_eof_and_retryable_error(self):
        for ending, expected in (
            (b"", ConnectionError),
            (
                event("error", error={"type": "overloaded_error", "message": "Busy"}),
                transport.StreamError,
            ),
        ):

            def respond(handler, ending=ending):
                start_stream(handler)
                handler.wfile.write(
                    event("message_start", message={"content": [], "usage": {}})
                    + ending
                )

            with (
                self.server(respond) as (url, _),
                self.assertRaises(expected) as raised,
            ):
                transport.request(
                    url.replace("/responses", "/messages"), b"{}", "key", 2
                )
            if expected is transport.StreamError:
                self.assertTrue(raised.exception.retryable)

    def test_native_deadline_and_cancellation_close_connection(self):
        from contextlib import contextmanager

        @contextmanager
        def native_server(respond):
            with test_transport.TransportTests.server(self, respond) as (url, server):
                yield url.replace("/responses", "/messages"), server

        self.server = native_server
        test_transport.TransportTests.test_local_cancellation_closes_pending_headers_and_active_streams(
            self
        )
        test_transport.TransportTests.test_total_deadline_closes_continuous_heartbeat_and_byte_trickle(
            self
        )

    def test_native_empty_tool_argument_delta_keeps_initial_object(self):
        def respond(handler):
            start_stream(handler)
            handler.wfile.write(
                event("message_start", message={"content": [], "usage": {}})
                + event(
                    "content_block_start",
                    index=0,
                    content_block={
                        "type": "tool_use",
                        "id": "call1",
                        "name": "empty",
                        "input": {},
                    },
                )
                + event(
                    "content_block_delta",
                    index=0,
                    delta={"type": "input_json_delta", "partial_json": ""},
                )
                + event("content_block_stop", index=0)
                + event("message_delta", delta={"stop_reason": "tool_use"})
                + event("message_stop")
            )

        with self.server(respond) as (url, _):
            result = transport.request(
                url.replace("/responses", "/messages"), b"{}", "key", 2
            )
        self.assertEqual(result["output"][1]["arguments"], "{}")

    def test_truncated_tool_json_preserves_terminal_usage(self):
        def respond(handler):
            start_stream(handler)
            handler.wfile.write(
                event(
                    "message_start",
                    message={"content": [], "usage": {"input_tokens": 10}},
                )
                + event(
                    "content_block_start",
                    index=0,
                    content_block={
                        "type": "tool_use",
                        "id": "call1",
                        "name": "read_file",
                        "input": {},
                    },
                )
                + event(
                    "content_block_delta",
                    index=0,
                    delta={"type": "input_json_delta", "partial_json": '{"path":'},
                )
                + event("content_block_stop", index=0)
                + event(
                    "message_delta",
                    delta={"stop_reason": "max_tokens"},
                    usage={"output_tokens": 100},
                )
                + event("message_stop")
            )

        with self.server(respond) as (url, _):
            result = transport.request(
                url.replace("/responses", "/messages"), b"{}", "key", 2
            )
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["incomplete_details"]["reason"], "max_output_tokens")
        self.assertEqual(result["usage"]["output_tokens"], 100)
        self.assertEqual(result["output"][1]["arguments"], '{"path":')
