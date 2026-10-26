import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ralph import engine, profiles, routing
from ralph.repository import MAX_REVIEW_BYTES
from ralph.spend import BudgetExceeded


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
    def runtime(self):
        case = engine.ReviewCase("Patch", None)
        return engine.Runtime(case=case, api_key="key")

    def snapshot(self, *paths):
        return SimpleNamespace(changed_paths=set(paths))

    def prompt_config(self):
        return SimpleNamespace(
            models={"router": "gpt-6-luna"},
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
        with patch("ralph.model.run_audit") as run:
            plan = routing.plan_review(
                self.runtime(), review, snapshot, prompt_config, debug
            )
        run.assert_not_called()
        self.assertEqual(plan, routing.full_plan("Initial patch is incomplete"))

        for answer, status in (
            ('{"tier": "routine"}', "completed"),
            (route(), "incomplete"),
        ):
            with self.subTest(status=status):
                debug = {}
                with patch(
                    "ralph.model.run_audit", return_value=(answer, {"status": status})
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
            route(missing_context=("caller omitted",)), {"src/example.cpp"}, "standard"
        )
        self.assertEqual(plan, routing.full_plan("Router reported missing context"))

    def test_router_budget_exhaustion_propagates_instead_of_full_fallback(self):
        debug = {}
        with (
            patch(
                "ralph.model.run_audit",
                side_effect=BudgetExceeded("review spend limit would be exceeded"),
            ) as run,
            self.assertRaises(BudgetExceeded),
        ):
            routing.plan_review(
                self.runtime(),
                "Patch:\n+change",
                self.snapshot("src/example.cpp"),
                self.prompt_config(),
                debug,
            )
        self.assertEqual(run.call_count, 1)
        self.assertEqual(debug["stages"]["router"]["status"], "budget_exhausted")
        self.assertEqual(debug["stages"]["router"]["error_type"], "BudgetExceeded")
        self.assertNotIn("routing", debug)


if __name__ == "__main__":
    unittest.main()
