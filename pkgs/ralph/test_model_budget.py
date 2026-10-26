import hashlib
import json
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ralph import model
from ralph.spend import BudgetExceeded


class Response:
    def __init__(self, result):
        self.result = result

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, *_args):
        return json.dumps(self.result).encode()


class FakeBudget:
    def __init__(self):
        self.events = []
        self.requests = []

    def reserve(self, stage, data):
        self.events.append(("reserve", stage, data["model"]))
        self.requests.append(data)
        return "token"

    def settle(self, token, response):
        self.events.append(("settle", token, response.get("id")))

    def fail(self, token, charged_unknown=True):
        self.events.append(("fail", token, charged_unknown))


class ModelBudgetTests(unittest.TestCase):
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

    def test_settles_before_callback_and_refunds_only_definite_http_rejections(self):
        budget = FakeBudget()
        events = budget.events
        result = {
            "id": "response-1",
            "model": "gpt-6-luna",
            "status": "completed",
            "output": [],
            "usage": {"input_tokens": 10, "output_tokens": 4},
        }

        def callback(stage, _payload, _response):
            events.append(("callback", stage))

        with patch.object(
            model.urllib.request, "urlopen", return_value=Response(result)
        ):
            model.request_response(
                "secret", {"model": "gpt-6-luna"}, "tests", callback, budget
            )
        self.assertEqual(
            [event[0] for event in events], ["reserve", "settle", "callback"]
        )

        for code, expected_unknown in ((400, False), (429, False), (500, True)):
            budget.events.clear()
            error = urllib.error.HTTPError("url", code, "rejected", {}, None)
            with (
                patch.object(model.urllib.request, "urlopen", side_effect=error),
                self.assertRaises(urllib.error.HTTPError),
            ):
                model.request_response(
                    "secret", {"model": "gpt-6-luna"}, "tests", budget=budget
                )
            self.assertEqual(budget.events[-1], ("fail", "token", expected_unknown))

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
        budget = FakeBudget()
        discussion = Mock(return_value="Discussion")
        with (
            patch.object(
                model.urllib.request,
                "urlopen",
                side_effect=[Response(item) for item in results],
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
                budget=budget,
                reasoning_effort="xhigh",
                max_output_tokens=25_000,
            )
        self.assertEqual(answer, raw)
        for call in send.call_args_list:
            self.assertNotIn("max_tool_calls", json.loads(call.args[0].data))
        self.assertEqual(budget.requests[0]["max_tool_calls"], 1)
        read.assert_called_once()
        history.assert_called_once()
        discussion.assert_called_once()
        final_request = json.loads(send.call_args_list[-1].args[0].data)
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
        self.assertFalse(debug["budget_limited"])
        self.assertEqual(
            [event[0] for event in budget.events],
            ["reserve", "settle", "reserve", "settle"],
        )

    def test_failed_search_request_records_transmitted_payload(self):
        debug = {}
        error = urllib.error.HTTPError("url", 400, "rejected", {}, None)
        with (
            patch.object(model.urllib.request, "urlopen", side_effect=error) as send,
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
        payload = send.call_args.args[0].data
        self.assertNotIn("max_tool_calls", json.loads(payload))
        self.assertEqual(debug["turns"][0]["request_bytes"], len(payload))
        self.assertEqual(
            debug["turns"][0]["request_sha256"], hashlib.sha256(payload).hexdigest()
        )

    def test_stale_review_between_turns_does_not_reserve_again(self):
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
        budget = FakeBudget()
        states = iter([True, False])
        with (
            patch.object(
                model.urllib.request, "urlopen", return_value=Response(first)
            ) as send,
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
                budget=budget,
                max_tool_calls=1,
                is_current=lambda: next(states),
            )
        send.assert_called_once()
        self.assertEqual([event[0] for event in budget.events], ["reserve", "settle"])
        self.assertEqual(debug["limits"]["tool_calls"]["used"], 1)
        self.assertEqual(debug["limits"]["output_tokens"]["used"], 2)
        self.assertFalse(debug["budget_limited"])
        self.assertFalse(debug["limits"]["tool_calls"]["hit"])

    def test_budget_rejection_has_no_request_turn(self):
        class RejectedBudget:
            def __init__(self):
                self.choices = []

            def reserve(self, _stage, _data):
                self.choices.append(_data["tool_choice"])
                raise BudgetExceeded("limit")

        budget = RejectedBudget()
        stage = {"model": "gpt-6-luna", "status": "running", "turns": [], "tools": []}
        with (
            patch.object(model.urllib.request, "urlopen") as send,
            self.assertRaises(BudgetExceeded),
        ):
            model.openai_review(
                "secret",
                "patch",
                self.snapshot,
                stage,
                prompt="Review.",
                model="gpt-6-luna",
                max_output_tokens=model.MAX_OUTPUT_TOKENS,
                budget=budget,
            )
        send.assert_not_called()
        self.assertEqual(budget.choices, ["required"])
        self.assertEqual(stage["turns"], [])
        self.assertEqual(stage["status"], "budget_exhausted")
        self.assertTrue(stage["budget_limited"])
        self.assertIsNone(stage["limits"]["output_tokens"]["used"])

    def test_budget_rejection_after_inspection_finishes_without_tools(self):
        class OneInspectionBudget:
            def __init__(self):
                self.events = []

            def reserve(self, stage, data):
                self.events.append(("reserve", stage, data["tool_choice"]))
                if data["tool_choice"] == "auto":
                    raise BudgetExceeded("limit")
                return "token"

            def settle(self, token, response):
                self.events.append(("settle", token, response.get("id")))

            def fail(self, token, charged_unknown=True):
                self.events.append(("fail", token, charged_unknown))

        raw = json.dumps(
            {
                "coverage": {
                    "status": "partial",
                    "limitations": ["Budget prevented more inspection"],
                },
                "findings": [],
                "requires_sensitive_review": False,
            }
        )
        first = {
            "id": "first-tool",
            "model": "gpt-6-luna",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "read_file",
                    "arguments": '{"path":"src/a.cpp","start_line":1}',
                }
            ],
        }
        final = {
            "id": "forced-final",
            "model": "gpt-6-luna",
            "status": "completed",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": raw}]}
            ],
        }
        schema = {"type": "object", "properties": {}, "additionalProperties": True}
        debug = {}
        budget = OneInspectionBudget()
        with (
            patch.object(
                model.urllib.request,
                "urlopen",
                side_effect=[Response(first), Response(final)],
            ) as send,
            patch.object(model, "read_file", return_value="1: evidence") as read,
        ):
            answer = model.openai_review(
                "secret",
                "patch",
                self.snapshot,
                debug,
                prompt="Review.",
                model="gpt-6-luna",
                max_output_tokens=model.MAX_OUTPUT_TOKENS,
                budget=budget,
                response_schema=schema,
                api_base="https://api.ppq.ai/v1",
            )
        self.assertEqual(answer, raw)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(
            [call.args[0].full_url for call in send.call_args_list],
            ["https://api.ppq.ai/v1/responses"] * 2,
        )
        sent = json.loads(send.call_args_list[-1].args[0].data)
        self.assertEqual(sent["tool_choice"], "none")
        self.assertEqual(sent["input"][0]["role"], "user")
        self.assertEqual(sent["input"][1]["type"], "function_call")
        self.assertEqual(sent["input"][2]["type"], "function_call_output")
        self.assertIn("Budget is near", sent["input"][-1]["content"])
        self.assertEqual(
            [turn["tool_choice"] for turn in debug["turns"]], ["required", "none"]
        )
        self.assertTrue(debug["turns"][1]["budget_forced_final"])
        self.assertTrue(debug["budget_limited"])
        self.assertFalse(debug["limits"]["tool_calls"]["hit"])
        self.assertEqual(
            budget.events,
            [
                ("reserve", "independent", "required"),
                ("settle", "token", "first-tool"),
                ("reserve", "independent", "auto"),
                ("reserve", "independent", "none"),
                ("settle", "token", "forced-final"),
            ],
        )

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
                model.urllib.request,
                "urlopen",
                side_effect=[Response(first), Response(final)],
            ) as send,
            patch.object(model, "read_file", return_value="1: evidence") as read,
        ):
            answer = model.openai_review("proxy-key", "patch", self.snapshot, **options)
        self.assertEqual(json.loads(answer), {"findings": []})
        read.assert_called_once()
        sent = [json.loads(call.args[0].data) for call in send.call_args_list]
        self.assertEqual([request["tool_choice"] for request in sent], ["auto", "none"])
        self.assertEqual(sent[0]["reasoning"]["effort"], "xhigh")
        self.assertEqual(sent[1]["input"][1], thinking)
        self.assertEqual(sent[1]["input"][-1]["type"], "function_call_output")

        with (
            patch.object(
                model.urllib.request, "urlopen", return_value=Response(final)
            ) as send,
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
        with patch.object(
            model.urllib.request, "urlopen", return_value=Response(result)
        ):
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
        self.assertFalse(debug["budget_limited"])


if __name__ == "__main__":
    unittest.main()
