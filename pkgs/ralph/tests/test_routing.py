import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ralph import config, coverage, engine, profiles, routing, transport
from ralph.protocol import InvalidReview
from ralph.repository import MAX_REVIEW_BYTES


def route(
    tier="routine",
    audits=(),
    profiles=(),
    evidence=("Router evidence",),
    missing_context=(),
    historical_research=None,
):
    return json.dumps(
        {
            "tier": tier,
            "audits": list(audits),
            "profiles": list(profiles),
            "evidence": list(evidence),
            "missing_context": list(missing_context),
            "historical_research": (
                historical_research
                if historical_research is not None
                else {
                    "mode": "none",
                    "reason": "The patch changes presentation only.",
                    "question": "",
                }
            ),
        }
    )


class RoutingTests(unittest.TestCase):
    def test_gui_fallback_and_explicit_full_review_stay_in_gui_strategy(self):
        prompts = config.PromptConfig.load(strategy="gui")
        for mode in ("enabled", "full", "shadow"):
            with (
                self.subTest(mode=mode),
                patch(
                    "ralph.model.openai_review", side_effect=RuntimeError("unavailable")
                ),
            ):
                plan = routing.plan_review(
                    self.runtime(),
                    "Patch",
                    self.snapshot("qml/models/wallet.cpp"),
                    prompts,
                    {},
                    mode,
                )
            self.assertIn("gui", plan["audits"])
            self.assertNotIn("script", plan["audits"])
            self.assertEqual(plan["profiles"], ["wallet"])
        plan = routing.validate_plan(
            route("standard"), {"qml/models/wallet.cpp", "bitcoin"}, prompts.strategy
        )
        self.assertTrue({"gui", "public_contract", "build"} <= set(plan["audits"]))
        with self.assertRaises(InvalidReview):
            routing.validate_plan(
                route(profiles=("consensus",)), {"bitcoin"}, prompts.strategy
            )

    def test_semantic_tier_is_not_raised_by_critical_paths(self):
        plan = routing.validate_plan(
            route(tier="standard"),
            {"src/wallet/wallet.cpp", "src/validation.cpp", "src/net.cpp"},
        )
        self.assertEqual(plan["tier"], "standard")
        self.assertEqual(plan["profiles"], [])
        self.assertIn("tests", plan["audits"])
        self.assertIn("design", plan["audits"])

        plan = routing.validate_plan(route(), {"src/kernel/bitcoinkernel.h"})
        self.assertEqual(plan["tier"], "routine")

        plan = routing.validate_plan(
            route(tier="standard", profiles=("wallet",)), {"src/wallet/wallet.cpp"}
        )
        self.assertEqual(plan["tier"], "sensitive")
        self.assertEqual(plan["profiles"], ["wallet"])

    def test_gui_coverage_preserves_other_audits_and_semantic_tier(self):
        for path in ("qml/Send.qml", "src/qt/walletmodel.cpp", "src/qt/forms/send.ui"):
            with self.subTest(path=path):
                plan = routing.validate_plan(
                    route(tier="standard", audits=("concurrency",)), {path}
                )
                self.assertEqual(plan["tier"], "standard")
                self.assertEqual(plan["profiles"], [])
                self.assertTrue(
                    {"gui", "concurrency", "tests", "design"} <= set(plan["audits"])
                )
        plan = routing.validate_plan(route(), {"src/qt/README.md"})
        self.assertNotIn("gui", plan["audits"])
        self.assertEqual(plan["tier"], "routine")

    def test_documentation_can_select_symbol_and_behavior_checks(self):
        research = {
            "mode": "focused",
            "reason": "The documentation changes the rationale for a compatibility guard.",
            "question": "Which legacy callers required this guard?",
        }
        for name in ("core", "gui"):
            strategy = config.PromptConfig.load(strategy=name).strategy
            with self.subTest(strategy=name):
                plan = routing.validate_plan(
                    route(
                        audits=("symbol_usage", "design"),
                        historical_research=research,
                    ),
                    {"doc/example.md"},
                    strategy,
                )
                self.assertEqual(set(plan["audits"]), {"symbol_usage", "design"})
                self.assertEqual(plan["tier"], "routine")
                self.assertEqual(plan["historical_research"], research)
                self.assertEqual(
                    routing.validate_plan(route(), {"doc/example.md"}, strategy)[
                        "audits"
                    ],
                    [],
                )

    def test_historical_research_is_independent_of_sensitive_coverage(self):
        plan = routing.validate_plan(
            route(profiles=("wallet",)), {"src/wallet/wallet.cpp"}
        )
        self.assertEqual(plan["tier"], "sensitive")
        self.assertEqual(plan["historical_research"]["mode"], "none")

    def test_invalid_historical_research_falls_back_to_full(self):
        for research in (
            {},
            {"mode": "focused", "reason": "Inspect intent", "question": " "},
            {"mode": "none", "reason": " ", "question": ""},
            {"mode": [], "reason": "Inspect intent", "question": "Why?"},
            {"mode": "full", "reason": "Inspect intent", "question": 1},
            {"mode": "none", "reason": "Mechanical", "question": "", "extra": 1},
        ):
            with self.subTest(research=research):
                with self.assertRaises(InvalidReview):
                    routing.validate_plan(
                        route(historical_research=research), {"doc/example.md"}
                    )
                with patch(
                    "ralph.model.openai_review",
                    return_value=route(historical_research=research),
                ):
                    plan = routing.plan_review(
                        self.runtime(),
                        "Patch",
                        self.snapshot("doc/example.md"),
                        self.prompt_config(),
                        {},
                    )
                self.assertEqual(plan["historical_research"]["mode"], "full")
                self.assertTrue(plan["historical_research"]["question"].strip())

    def test_full_and_shadow_routing_retain_full_historical_research(self):
        for mode in ("full", "shadow"):
            with (
                self.subTest(mode=mode),
                patch("ralph.model.openai_review", return_value=route()) as run,
            ):
                debug = {}
                plan = routing.plan_review(
                    self.runtime(),
                    "Patch",
                    self.snapshot("doc/example.md"),
                    self.prompt_config(),
                    debug,
                    mode,
                )
                self.assertEqual(plan["historical_research"]["mode"], "full")
                if mode == "full":
                    run.assert_not_called()
                else:
                    self.assertEqual(
                        debug["routing"]["proposed"]["historical_research"]["mode"],
                        "none",
                    )

    def runtime(self):
        case = engine.ReviewCase("Patch", None)
        return engine.Runtime(case=case, api_key="key")

    def snapshot(self, *paths):
        return SimpleNamespace(changed_paths=set(paths))

    def prompt_config(self):
        return SimpleNamespace(
            models={"router": profiles.get_profile().stages["router"].model},
            audit_prompts={"router": "Route this PR."},
            profile=profiles.get_profile(),
        )

    def test_full_fallback_for_incomplete_patch_router_failure_and_missing_context(
        self,
    ):
        prompt_config = self.prompt_config()
        snapshot = self.snapshot("src/rpc/misc.cpp", "ci/test/00_setup_env.sh")
        review = f"Patch exceeds {MAX_REVIEW_BYTES} input bytes. Use read_diff"
        debug = {}
        with patch("ralph.model.openai_review") as run:
            plan = routing.plan_review(
                self.runtime(), review, snapshot, prompt_config, debug
            )
        run.assert_not_called()
        self.assertEqual(plan, routing.full_plan("Initial patch is incomplete"))

        def refuse(*args, **kwargs):
            args[3].update(failure_kind="cyber_refusal")
            raise transport.StreamError({"code": "refusal"})

        for answer in (
            '{"tier": "routine"}',
            InvalidReview("Router response incomplete"),
            refuse,
        ):
            with self.subTest(answer=answer):
                debug = {}
                with patch(
                    "ralph.model.openai_review",
                    side_effect=answer
                    if isinstance(answer, Exception) or callable(answer)
                    else None,
                    return_value=answer,
                ):
                    plan = routing.plan_review(
                        self.runtime(),
                        "Patch:\n+change",
                        snapshot,
                        prompt_config,
                        debug,
                    )
                self.assertEqual(
                    plan,
                    routing.full_plan(
                        "Routing unavailable; conservative review required"
                    ),
                )
                self.assertEqual(debug["stages"]["router"]["status"], "failed")
                if answer is refuse:
                    self.assertEqual(
                        debug["stages"]["router"]["failure_kind"], "cyber_refusal"
                    )
                    self.assertEqual(coverage.summarize(debug)["issues"], [])

        plan = routing.validate_plan(
            route(missing_context=("caller omitted",)), {"src/example.cpp"}
        )
        self.assertEqual(plan, routing.full_plan("Router reported missing context"))

    def test_router_can_inspect_pinned_code_without_a_path_floor(self):
        debug = {}
        prompt_config = self.prompt_config()
        settings = prompt_config.profile.stages["router"]
        runtime = self.runtime()
        with patch("ralph.model.openai_review", return_value=route("standard")) as run:
            plan = routing.plan_review(
                runtime,
                "Patch:\n+change",
                self.snapshot("src/wallet/wallet.cpp"),
                prompt_config,
                debug,
            )
        self.assertEqual(plan["tier"], "standard")
        manifest = json.loads(run.call_args.args[1].splitlines()[0])
        self.assertEqual(manifest, {"changed_paths": ["src/wallet/wallet.cpp"]})
        self.assertIs(run.call_args.args[2], runtime.case.snapshot)
        options = run.call_args.kwargs
        self.assertEqual(options["model"], settings.model)
        self.assertEqual(options["max_tool_calls"], settings.max_tool_calls["standard"])
        self.assertGreater(options["max_tool_calls"], 0)
        self.assertEqual(options["reasoning_effort"], settings.reasoning_effort)
        self.assertFalse(options["first_tool_required"])
        self.assertEqual(debug["routing"]["selected"], plan)
        self.assertEqual(debug["stages"]["router"]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
