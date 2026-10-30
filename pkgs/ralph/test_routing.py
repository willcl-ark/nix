import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ralph import engine, profiles, routing
from ralph.protocol import InvalidReview
from ralph.repository import MAX_REVIEW_BYTES


def route(
    tier="routine",
    audits=(),
    profiles=(),
    evidence=("Router evidence",),
    missing_context=(),
):
    return json.dumps(
        {
            "tier": tier,
            "audits": list(audits),
            "profiles": list(profiles),
            "evidence": list(evidence),
            "missing_context": list(missing_context),
        }
    )


class RoutingTests(unittest.TestCase):
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

        for answer in (
            '{"tier": "routine"}',
            InvalidReview("Router response incomplete"),
        ):
            with self.subTest(answer=answer):
                debug = {}
                with patch(
                    "ralph.model.openai_review",
                    side_effect=answer if isinstance(answer, Exception) else None,
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
        self.assertEqual(
            {tool["name"] for tool in options["tools"]},
            {
                "find_paths",
                "read_file",
                "read_base_file",
                "read_diff",
                "search_code",
                "read_pr_commit",
            },
        )
        self.assertFalse(options["first_tool_required"])
        self.assertEqual(debug["routing"]["selected"], plan)
        self.assertEqual(debug["stages"]["router"]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
