import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import urllib.error
from unittest.mock import Mock, patch

from ralph import current_engine, engine, model, research, trace


class EngineTests(unittest.TestCase):
    def test_findings_exports_are_independent_of_native_schema(self):
        for exports, expected in (({}, None), ({"findings": []}, []),
                                  ({"findings": [{"id": "finding:1"}]},
                                   [{"id": "finding:1"}])):
            with self.subTest(exports=exports):
                result = {"schema_version": 1, "engine": {"name": "current"},
                          "output": {"schema_version": 7,
                                     "data": {"schema_version": 7, "native": "value"},
                                     "exports": exports}}
                self.assertEqual(trace.findings_export(result), expected)
                self.assertEqual(trace.current_diagnostics(result), {})
        result["schema_version"] = 2
        self.assertIsNone(trace.findings_export(result))

    def test_engine_identity_ignores_companion_changes_and_tracks_core_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core = root / "ralph"
            companion = root / "ralph_service"
            core.mkdir()
            companion.mkdir()
            engine_path = core / "engine.py"
            engine_path.write_text("core engine")
            module_path = core / "current_engine.py"
            module_path.write_text("current engine")
            service_path = companion / "service.py"
            service_path.write_text("original service")
            module = SimpleNamespace(__file__=str(module_path))
            with patch.object(engine, "__file__", str(engine_path)):
                original = engine.LoadedEngine("current", module, {}).identity
                service_path.write_text("changed service")
                self.assertEqual(engine.LoadedEngine("current", module, {}).identity, original)
                engine_path.write_text("changed core engine")
                changed = engine.LoadedEngine("current", module, {}).identity
            self.assertNotEqual(changed["code_sha256"], original["code_sha256"])
            self.assertEqual(changed["config_sha256"], original["config_sha256"])

    def test_archaeology_is_normalized_and_requires_context_before_execution(self):
        default = current_engine.configure({})
        self.assertIs(default["archaeology"], False)
        with self.assertRaisesRegex(ValueError, "boolean"):
            current_engine.configure({"archaeology": "on"})
        configured = current_engine.configure({"archaeology": True})
        case = engine.ReviewCase("Patch", None)
        runtime = engine.Runtime(case=case, api_key="secret")
        with patch.object(current_engine.pipeline, "review_with_independent_passes") as run:
            with self.assertRaisesRegex(ValueError, "requires PR context"):
                current_engine.run(case, runtime, configured)
        run.assert_not_called()

    def test_archaeology_accepts_live_or_frozen_pr_evidence_and_uses_recorded_flag(self):
        frozen = {"schema_version": 1, "captured_at": "2026-10-23T00:00:00Z",
                  "cutoff": "2026-10-23T00:00:00Z", "entries": []}
        source = {"kind": "pull_request", "url": "https://github.com/o/r/pull/7", "number": 7}
        for evidence, reader in ((None, Mock()), (frozen, None)):
            with self.subTest(frozen=evidence is not None):
                case = engine.ReviewCase("Patch", None, source, evidence)
                runtime = engine.Runtime(case=case, api_key="secret", discussion_lookup=reader)
                with patch.object(current_engine.pipeline, "review_with_independent_passes",
                                  return_value="Review") as run:
                    current_engine.run(case, runtime, current_engine.configure({"archaeology": True}))
                self.assertIs(run.call_args.args[-1], True)
        case = engine.ReviewCase("Patch", None)
        runtime = engine.Runtime(case=case, api_key="secret")
        with patch.object(current_engine.pipeline, "review_with_independent_passes",
                          return_value="Review") as run:
            current_engine.run(case, runtime, current_engine.configure({}))
        self.assertIs(run.call_args.args[-1], False)

    def test_runtime_keeps_original_request_exceptions_and_attempt_records(self):
        case = engine.ReviewCase("Patch", None)
        runtime = engine.Runtime(case=case, api_key="secret")
        error = urllib.error.HTTPError("url", 429, "limit", {}, None)
        record = {}
        with patch.object(model, "openai_review", side_effect=error):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                runtime.review("independent", "Patch", prompt="Review", model_name="model",
                               tools=[], max_tool_calls=0, max_output_tokens=10,
                               reasoning_effort="low", response_schema=None, record=record)
        self.assertIs(caught.exception, error)
        self.assertIs(runtime._records[0], record)

    def test_frozen_case_overrides_per_call_evidence_and_live_sources(self):
        evidence = {"schema_version": 1, "captured_at": "2026-10-23T00:00:00Z",
                    "cutoff": "2026-10-23T00:00:00Z", "entries": []}
        case = engine.ReviewCase("Patch", None, research_evidence=evidence)
        live = Mock()
        runtime = engine.Runtime(case=case, api_key="secret", discussion_lookup=live,
                                 allow_web_search=True)
        with patch.object(model, "openai_review", return_value="done") as review:
            runtime.review("verifier", "Patch", prompt="Review", model_name="model",
                           tools=[], max_tool_calls=0, max_output_tokens=10,
                           reasoning_effort="low", response_schema=None)
        self.assertIs(review.call_args.kwargs["research_evidence"], evidence)
        tools = model.available_tools(model.ARCHAEOLOGY_TOOLS, live, evidence, True)
        self.assertFalse(any(tool["type"] in model.WEB_SEARCH_TOOL_TYPES for tool in tools))
        self.assertEqual(research.lookup(evidence, "search_discussions", {"query": "missing"}),
                         research.MISSING_RESEARCH)
        live.assert_not_called()

    def test_web_permission_and_discussion_availability_are_independent(self):
        live = Mock()
        tools = model.available_tools(model.ARCHAEOLOGY_TOOLS, live)
        self.assertIn("search_discussions", {tool.get("name") for tool in tools})
        self.assertFalse(any(tool["type"] in model.WEB_SEARCH_TOOL_TYPES for tool in tools))
        tools = model.available_tools(model.ARCHAEOLOGY_TOOLS, allow_web_search=True)
        self.assertNotIn("search_discussions", {tool.get("name") for tool in tools})
        self.assertTrue(any(tool["type"] in model.WEB_SEARCH_TOOL_TYPES for tool in tools))


if __name__ == "__main__":
    unittest.main()
