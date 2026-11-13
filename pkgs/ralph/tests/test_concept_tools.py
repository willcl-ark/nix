import json
from contextlib import nullcontext
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ralph import model, coverage


class ConceptToolTests(unittest.TestCase):
    def test_tool_failures_constrain_coverage_independently_of_model(self):
        for name, args, outcome, required in (
            ("read_file", {"path": "new.cpp", "start_line": 1}, "unavailable", True),
            (
                "read_file",
                {"path": "missing.cpp", "start_line": 1},
                "invalid_request",
                True,
            ),
            ("read_discussion", {"number": 1}, "unavailable", False),
        ):
            with self.subTest(name=name, outcome=outcome):
                debug = {}
                with (
                    patch.object(model, "read_file", side_effect=TimeoutError)
                    if outcome == "unavailable" and required
                    else nullcontext()
                ):
                    self.run_tools(
                        [(name, args)],
                        debug=debug,
                        stage_name="tests",
                        discussion_lookup=Mock(side_effect=TimeoutError),
                    )
                self.assertEqual(debug["tools"][0]["outcome"], outcome)
                debug.update(
                    status="completed",
                    coverage={"status": "complete", "limitations": []},
                )
                summary = coverage.summarize(
                    {
                        "stages": {
                            "tests": debug,
                            "verifier": {
                                "status": "completed",
                                "coverage": {"status": "complete"},
                            },
                        }
                    }
                )
                self.assertEqual(
                    summary["status"],
                    "partial" if required and outcome == "unavailable" else "complete",
                )
                if not required:
                    self.assertEqual(summary["context"]["status"], "partial")

    def test_focused_pr_commit_reads_use_pinned_endpoints(self):
        arguments = {"commit": "c" * 40, "path": "new.cpp", "start_line": 25}
        with patch.object(
            model, "read_pr_commit", create=True, return_value="Pinned patch"
        ) as read:
            requests = self.run_tools(
                [("read_pr_commit", arguments)], tools=model.FOCUSED_TOOLS
            )
        read.assert_called_once_with(
            "checkout",
            "base-sha",
            "head-sha",
            "c" * 40,
            "new.cpp",
            25,
            dependencies=(),
        )
        self.assertEqual(requests[-1]["input"][-1]["output"], "Pinned patch")

    def setUp(self):
        self.snapshot = SimpleNamespace(
            checkout="checkout",
            head_files={"new.cpp": "head"},
            base_files={"old.cpp": "base"},
            changed_paths={"new.cpp"},
            merge_base="other",
            diff_base_sha="base-sha",
            head_sha="head-sha",
        )

    def run_tools(self, calls, **kwargs):
        responses = [
            {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": str(index),
                        "name": name,
                        "arguments": json.dumps(args),
                    }
                    for index, (name, args) in enumerate(calls)
                ],
            },
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": "Assessment complete."}
                        ],
                    }
                ],
            },
        ]
        requests = []

        def request(_key, data, *_args, **_kwargs):
            requests.append(json.loads(json.dumps(data)))
            return responses.pop(0), json.dumps(data).encode(), 0.01

        with patch.object(model, "request_response", side_effect=request):
            model.openai_review(
                "key",
                "Problem and baseline.",
                self.snapshot,
                prompt="Review.",
                model="gpt-6-luna",
                max_output_tokens=model.MAX_OUTPUT_TOKENS,
                **kwargs,
            )
        return requests

    def test_blind_tools_search_only_base_and_reject_head_and_discussions(self):
        with (
            patch.object(model, "search_code", return_value="base matches") as search,
            patch.object(model, "read_file", return_value="base contents") as read,
            patch.object(model, "read_diff") as diff,
            patch.object(model, "read_commit") as discussion,
        ):
            requests = self.run_tools(
                [
                    ("find_base_paths", {"query": ".cpp"}),
                    ("search_base_code", {"query": "existing facility"}),
                    ("read_base_file", {"path": "old.cpp", "start_line": 1}),
                    ("read_file", {"path": "new.cpp", "start_line": 1}),
                    ("read_diff", {"path": "new.cpp", "start_line": 1}),
                    (
                        "read_current_pr_discussion",
                        {"kind": "comments", "page": 1, "review_id": 0},
                    ),
                ],
                tools=model.BASE_TOOLS,
            )
        search.assert_called_once_with(
            "checkout", "base-sha", "existing facility", dependencies=(), side="base"
        )
        read.assert_called_once_with("checkout", self.snapshot.base_files, "old.cpp", 1)
        diff.assert_not_called()
        discussion.assert_not_called()
        outputs = [
            item["output"]
            for item in requests[-1]["input"]
            if item.get("type") == "function_call_output"
        ]
        self.assertEqual(outputs[0], "old.cpp\n")
        self.assertEqual(outputs[-3:], ["Unknown or unavailable tool."] * 3)

    def test_frozen_research_never_advertises_hosted_web_or_calls_live_readers(self):
        evidence = {"frozen": "fixture"}
        for allow_live in (False, True):
            live = Mock()
            with (
                self.subTest(allow_live=allow_live),
                patch.object(
                    model.research, "lookup", return_value="Frozen discussion"
                ) as lookup,
                patch.object(model, "read_commit"),
            ):
                requests = self.run_tools(
                    [
                        (
                            "read_current_pr_discussion",
                            {"kind": "comments", "page": 1, "review_id": 0},
                        ),
                    ],
                    tools=model.ARCHAEOLOGY_TOOLS,
                    discussion_lookup=live if allow_live else None,
                    allow_web_search=True,
                    research_evidence=evidence,
                )
            live.assert_not_called()
            lookup.assert_called_once_with(
                evidence,
                "read_current_pr_discussion",
                {"kind": "comments", "page": 1, "review_id": 0},
            )
            self.assertNotIn(
                "web_search", {tool["type"] for tool in requests[0]["tools"]}
            )
            self.assertEqual(requests[-1]["input"][-1]["output"], "Frozen discussion")

    def test_bkb_calls_share_context_budget_and_capture_passages(self):
        debug = {}
        live = Mock(
            return_value='{"url":"https://delvingbitcoin.org/t/1","body":"Rationale"}'
        )
        requests = self.run_tools(
            [
                ("bkb_search", {"query": "replacement incentives"}),
                ("bkb_get_document", {"id": "delving_topic:1"}),
            ],
            tools=model.ARCHAEOLOGY_TOOLS,
            discussion_lookup=live,
            max_context_calls=1,
            debug=debug,
        )
        live.assert_called_once()
        self.assertEqual(
            debug["research_entries"][0]["arguments"],
            {
                "query": "replacement incentives",
                "source_type": "",
                "source_repo": "",
                "limit": 5,
            },
        )
        self.assertIn("Rationale", debug["research_entries"][0]["output"])
        self.assertEqual(debug["research_entries"][1]["outcome"], "unavailable")
        self.assertIn("limit reached", requests[-1]["input"][-1]["output"])

    def test_bkb_outage_stops_remote_retries_but_keeps_other_research(self):
        from ralph.evidence import ToolFailure

        live = Mock(
            side_effect=[ToolFailure("unavailable", "BKB timeout"), "GitHub rationale"]
        )
        debug = {}
        self.run_tools(
            [
                ("bkb_search", {"query": "incentives"}),
                ("bkb_search", {"query": "fees"}),
                ("read_discussion", {"number": 99}),
            ],
            tools=model.ARCHAEOLOGY_TOOLS,
            discussion_lookup=live,
            debug=debug,
        )
        self.assertEqual(live.call_count, 2)
        self.assertEqual(debug["research_entries"][1]["outcome"], "unavailable")
        self.assertEqual(debug["research_entries"][2]["output"], "GitHub rationale")

    def test_repeated_live_research_is_stable_and_replayable(self):
        from ralph import research

        live = Mock(side_effect=["First passage", "Second passage"])
        debug = {}
        self.run_tools(
            [
                ("bkb_get_document", {"id": "one"}),
                ("bkb_get_document", {"id": "one"}),
                ("bkb_get_document", {"id": "two"}),
            ],
            tools=model.ARCHAEOLOGY_TOOLS,
            discussion_lookup=live,
            debug=debug,
        )
        self.assertEqual(live.call_count, 2)
        self.assertEqual(len(debug["tools"]), 3)
        entries = debug["research_entries"]
        self.assertEqual(len(entries), 2)
        evidence = {
            "schema_version": research.SCHEMA_VERSION,
            "captured_at": entries[0]["captured_at"],
            "cutoff": entries[0]["cutoff"],
            "entries": entries,
        }
        research.validate(evidence)
        self.assertEqual(
            research.lookup(evidence, "bkb_get_document", {"id": "one"}),
            "First passage",
        )

    def test_bkb_frozen_replay_never_falls_back_to_network(self):
        from ralph import research

        timestamp = "2026-11-13T12:00:00Z"
        args = {"id": "delving_topic:1"}
        evidence = {
            "schema_version": research.SCHEMA_VERSION,
            "captured_at": timestamp,
            "cutoff": timestamp,
            "entries": [
                {
                    "key": research._entry_key("bkb_get_document", args),
                    "name": "bkb_get_document",
                    "arguments": args,
                    "output": "Captured rationale",
                    "outcome": "success",
                    "captured_at": timestamp,
                    "cutoff": timestamp,
                }
            ],
        }
        live = Mock()
        debug = {}
        requests = self.run_tools(
            [("bkb_get_document", args), ("bkb_get_document", {"id": "missing"})],
            tools=model.ARCHAEOLOGY_TOOLS,
            discussion_lookup=live,
            research_evidence=evidence,
            debug=debug,
        )
        live.assert_not_called()
        self.assertEqual(debug["research_entries"][0]["output"], "Captured rationale")
        self.assertEqual(debug["research_entries"][0]["captured_at"], timestamp)
        self.assertEqual(debug["research_entries"][1]["outcome"], "unavailable")
        self.assertIn(
            "Frozen research evidence is unavailable",
            requests[-1]["input"][-1]["output"],
        )

    def test_experiment_dispatch_requires_writer_and_advertised_tool(self):
        from ralph.experiments import TOOLS

        session = Mock()
        session.dispatch.return_value = '{"exit_code": 1}'
        arguments = {"finding_id": "F1", "side": "head", "command": "pytest"}
        calls = [("experiment_run", arguments)]
        requests = self.run_tools(
            calls,
            tools=TOOLS,
            stage_name="write_verifier",
            experiment_session=session,
        )
        session.dispatch.assert_called_once_with("experiment_run", arguments)
        self.assertEqual(requests[-1]["input"][-1]["output"], '{"exit_code": 1}')
        session.reset_mock()
        self.run_tools(
            calls,
            tools=model.BASE_TOOLS,
            stage_name="write_verifier",
            experiment_session=session,
        )
        session.dispatch.assert_not_called()
        with self.assertRaisesRegex(ValueError, "restricted to write_verifier"):
            self.run_tools(
                calls, tools=TOOLS, stage_name="verifier", experiment_session=session
            )
        session.dispatch.assert_not_called()

    def test_experiment_cancellation_aborts_model_loop(self):
        from ralph.experiments import TOOLS

        session = Mock()
        session.dispatch.side_effect = model.StaleReview()
        with self.assertRaises(model.StaleReview):
            self.run_tools(
                [
                    (
                        "experiment_run",
                        {"finding_id": "F1", "side": "head", "command": "true"},
                    )
                ],
                tools=TOOLS,
                stage_name="write_verifier",
                experiment_session=session,
            )


if __name__ == "__main__":
    unittest.main()
