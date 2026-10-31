import json
import threading
import time
import unittest
import urllib.error
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

from ralph import transport


def event(kind, **fields):
    return (
        f"event: {kind}\ndata: " + json.dumps({"type": kind, **fields}) + "\n\n"
    ).encode()


def start_stream(handler):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Connection", "close")
    handler.end_headers()


def observe_close(handler, heartbeat=None):
    handler.connection.settimeout(0.02 if heartbeat else 2)
    end = time.monotonic() + 2
    while time.monotonic() < end:
        try:
            if heartbeat:
                handler.wfile.write(heartbeat)
                handler.wfile.flush()
            if handler.connection.recv(1) == b"":
                return True
        except TimeoutError:
            continue
        except (BrokenPipeError, ConnectionResetError):
            return True
    return False


class TransportTests(unittest.TestCase):
    @contextmanager
    def server(self, respond):
        errors = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.close_connection = True
                self.server.payload = self.rfile.read(
                    int(self.headers["Content-Length"])
                )
                self.server.authorization = self.headers["Authorization"]
                try:
                    respond(self)
                except Exception as exc:  # noqa: BLE001 - re-raised in the test thread
                    errors.append(exc)

            def log_message(self, *_args):
                pass

        with HTTPServer(("127.0.0.1", 0), Handler) as server:
            server.timeout = 2
            thread = threading.Thread(target=server.handle_request, daemon=True)
            thread.start()
            try:
                yield f"http://127.0.0.1:{server.server_port}/responses", server
            finally:
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive(), "test HTTP server did not finish")
                if errors:
                    raise errors[0]

    def test_fragmented_multiline_stream_preserves_terminal_response(self):
        response = {
            "id": "response-1",
            "status": "completed",
            "output": [
                {
                    "type": "reasoning",
                    "encrypted_content": "signed-thinking",
                    "summary": [],
                },
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "read_file",
                    "arguments": '{"path":"src/a.cpp","start_line":1}',
                },
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Review café"}],
                },
            ],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 7,
                "input_tokens_details": {"cached_tokens": 4},
                "output_tokens_details": {"reasoning_tokens": 2},
            },
        }
        completed = json.dumps(
            {"type": "response.completed", "response": response},
            ensure_ascii=False,
            indent=2,
        )
        body = (
            b": keep-alive\r\n\r\n"
            + event("response.output_text.delta", delta="Partial text")
            + b"event: response.completed\r\nid: final\r\n"
            + "".join(f"data: {line}\r\n" for line in completed.splitlines()).encode()
            + b"\r\n"
        )

        def respond(handler):
            start_stream(handler)
            for offset in range(0, len(body), 7):
                handler.wfile.write(body[offset : offset + 7])
                handler.wfile.flush()
                time.sleep(0.001)

        payload = b'{"model":"test","stream":true}'
        with self.server(respond) as (url, server):
            result = transport.request(url, payload, "test-key", 2)
            self.assertEqual(server.payload, payload)
            self.assertEqual(server.authorization, "Bearer test-key")
        self.assertEqual(result, response)

    def test_explicit_incomplete_or_failed_terminal_preserves_usage_and_reason(self):
        for status, details in (
            ("incomplete", {"incomplete_details": {"reason": "max_output_tokens"}}),
            ("failed", {"error": {"code": "server_error", "message": "Failed"}}),
        ):
            with self.subTest(status=status):
                response = {
                    "status": status,
                    "usage": {"input_tokens": 10, "output_tokens": 100},
                    "output": [],
                    **details,
                }

                def respond(handler, status=status, response=response):
                    start_stream(handler)
                    handler.wfile.write(event(f"response.{status}", response=response))

                with self.server(respond) as (url, _server):
                    self.assertEqual(transport.request(url, b"{}", "key", 2), response)

    def test_eof_or_error_after_partial_output_never_succeeds(self):
        error = {"code": "server_error", "message": "Provider failed"}
        endings = [
            (b"", ConnectionError),
            (event("error", **error), transport.StreamError),
        ]
        for ending, expected in endings:
            with self.subTest(ending=ending):

                def respond(handler, ending=ending):
                    start_stream(handler)
                    handler.wfile.write(
                        event("response.output_text.delta", delta="Partial review")
                        + ending
                    )

                with (
                    self.server(respond) as (url, _server),
                    self.assertRaises(expected) as raised,
                ):
                    transport.request(url, b"{}", "key", 2)
                if expected is transport.StreamError:
                    self.assertEqual(
                        raised.exception.error["message"], error["message"]
                    )
                    self.assertTrue(raised.exception.retryable)

    def test_http_error_retains_status_retry_header_and_body(self):
        body = b'{"error":{"message":"Too many requests"}}'

        def respond(handler):
            handler.send_response(429)
            handler.send_header("Retry-After", "3")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body[:10])
            handler.wfile.flush()
            time.sleep(0.01)
            handler.wfile.write(body[10:])

        with (
            self.server(respond) as (url, _server),
            self.assertRaises(urllib.error.HTTPError) as raised,
        ):
            transport.request(url, b"{}", "key", 2)
        with raised.exception as error:
            self.assertEqual(error.code, 429)
            self.assertEqual(error.headers["Retry-After"], "3")
            self.assertEqual(error.read(), body)

    def test_stalled_http_rejection_body_preserves_status_and_deadline(self):
        closed = threading.Event()

        def respond(handler):
            handler.send_response(400)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", "100")
            handler.end_headers()
            if observe_close(handler):
                closed.set()

        with self.server(respond) as (url, _server):
            started = time.monotonic()
            with self.assertRaises(urllib.error.HTTPError) as raised:
                transport.request(url, b"{}", "key", 0.3)
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertTrue(closed.wait(1), "request socket remained open")
        with raised.exception as error:
            self.assertEqual(error.code, 400)

    def test_total_deadline_closes_continuous_heartbeat_and_byte_trickle(self):
        for heartbeat in (b": keep-alive\n\n", b":"):
            with self.subTest(heartbeat=heartbeat):
                closed = threading.Event()

                def respond(handler, heartbeat=heartbeat, closed=closed):
                    start_stream(handler)
                    if observe_close(handler, heartbeat):
                        closed.set()

                with self.server(respond) as (url, _server):
                    started = time.monotonic()
                    with self.assertRaises(TimeoutError):
                        transport.request(url, b"{}", "key", 0.3)
                    elapsed = time.monotonic() - started
                    self.assertLess(elapsed, 1.5)
                    self.assertTrue(closed.wait(1), "request socket remained open")

    def test_total_deadline_closes_silent_response_and_delayed_headers(self):
        for send_headers in (True, False):
            with self.subTest(send_headers=send_headers):
                closed = threading.Event()

                def respond(handler, send_headers=send_headers, closed=closed):
                    if send_headers:
                        start_stream(handler)
                        handler.wfile.write(b": keep-alive\n\n")
                        handler.wfile.flush()
                    if observe_close(handler):
                        closed.set()

                with self.server(respond) as (url, _server):
                    started = time.monotonic()
                    with self.assertRaises(TimeoutError):
                        transport.request(url, b"{}", "key", 0.3)
                    elapsed = time.monotonic() - started
                    self.assertLess(elapsed, 1.5)
                    self.assertTrue(closed.wait(1), "request socket remained open")


if __name__ == "__main__":
    unittest.main()
