import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ralph import config, routing
from ralph.protocol import InvalidReview
from ralph.repository import MAX_REVIEW_BYTES
from ralph.spend import BudgetExceeded


def route(tier="routine", audits=(), profiles=(), evidence=("Router evidence",),
          missing_context=()):
    return json.dumps({
        "tier": tier,
        "audits": list(audits),
        "profiles": list(profiles),
        "evidence": list(evidence),
        "missing_context": list(missing_context),
    })


class RoutingTests(unittest.TestCase):
    def test_script_specialist_requires_semantic_selection(self):
        for path in ("src/policy/policy.cpp", "src/script/interpreter.cpp",
                     "src/primitives/transaction.h", "src/validation.cpp",
                     "src/txmempool.cpp", "src/wallet/spend.cpp"):
            with self.subTest(path=path):
                plan = routing.validate_plan(route(), {path}, "routine")
                self.assertNotIn("script", plan["audits"])
                plan = routing.validate_plan(route(audits=("script",)), {path}, "routine")
                self.assertIn("script", plan["audits"])
                self.assertEqual(plan["tier"], "sensitive")

    def test_script_specialist_skips_docs_tests_and_generic_fallbacks(self):
        for path in ("doc/transaction.md", "src/test/script_tests.cpp"):
            with self.subTest(path=path):
                plan = routing.validate_plan(route(audits=("script",)), {path}, "routine")
                self.assertNotIn("script", plan["audits"])
        self.assertNotIn("script", routing.full_plan("Routing unavailable")["audits"])

    def test_explicit_full_review_includes_script_specialist(self):
        debug = {}
        with patch("ralph.model.run_audit") as run:
            plan = routing.plan_review("key", "Patch", self.snapshot("src/util/time.cpp"),
                                       self.prompt_config(), debug, mode="full")
        run.assert_not_called()
        self.assertIn("script", plan["audits"])

    def test_explicit_shadow_review_includes_script_specialist(self):
        debug = {}
        with patch("ralph.model.run_audit",
                   return_value=(route(), {"status": "completed"})):
            plan = routing.plan_review("key", "Patch", self.snapshot("src/util/time.cpp"),
                                       self.prompt_config(), debug, mode="shadow")
        self.assertNotIn("script", debug["routing"]["proposed"]["audits"])
        self.assertIn("script", plan["audits"])

    def snapshot(self, *paths):
        return SimpleNamespace(changed_paths=set(paths))

    def prompt_config(self):
        return SimpleNamespace(models={"router": "gpt-6-luna"},
                               audit_prompts={"router": "Route this PR."})

    def test_full_plan_selects_every_audit_and_profile(self):
        plan = routing.full_plan("Fallback")
        self.assertEqual(plan["tier"], "sensitive")
        self.assertEqual(plan["audits"], [name for name in config.AUDIT_NAMES if name != "script"])
        self.assertEqual(plan["profiles"], list(config.ADVERSARIAL_PROFILES))

    def test_sensitive_path_still_calls_router_and_adds_only_relevant_floor(self):
        snapshot = self.snapshot("src/validation.cpp")
        debug = {}
        with patch("ralph.model.run_audit",
                   return_value=(route(), {"status": "completed"})) as run:
            plan = routing.plan_review("key", "Patch:\n+validation", snapshot,
                                       self.prompt_config(), debug)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(plan["tier"], "sensitive")
        self.assertEqual(plan["profiles"], ["consensus"])
        self.assertEqual(set(plan["audits"]), {"tests", "design"})
        self.assertNotIn("state", plan["audits"])

    def test_full_fallback_for_incomplete_patch_router_failure_and_missing_context(self):
        prompt_config = self.prompt_config()
        snapshot = self.snapshot("src/example.cpp")
        review = f"Patch exceeds {MAX_REVIEW_BYTES} input bytes. Use read_diff"
        debug = {}
        with patch("ralph.model.run_audit") as run:
            plan = routing.plan_review("key", review, snapshot, prompt_config, debug)
        run.assert_not_called()
        self.assertEqual(plan, routing.full_plan("Initial patch is incomplete"))

        debug = {}
        with patch("ralph.model.run_audit",
                   return_value=('{"tier": "routine"}', {"status": "completed"})):
            plan = routing.plan_review("key", "Patch:\n+change", snapshot, prompt_config, debug)
        self.assertEqual(plan["audits"], [name for name in config.AUDIT_NAMES if name != "script"])
        self.assertEqual(plan["profiles"], list(config.ADVERSARIAL_PROFILES))
        self.assertEqual(debug["stages"]["router"]["status"], "failed")

        plan = routing.validate_plan(
            route(missing_context=("caller omitted",)), {"src/example.cpp"}, "standard")
        self.assertEqual(plan, routing.full_plan("Router reported missing context"))

    def test_router_budget_exhaustion_propagates_instead_of_full_fallback(self):
        debug = {}
        with patch("ralph.model.run_audit",
                   side_effect=BudgetExceeded("review spend limit would be exceeded")) as run:
            with self.assertRaises(BudgetExceeded):
                routing.plan_review("key", "Patch:\n+change", self.snapshot("src/example.cpp"),
                                    self.prompt_config(), debug)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(debug["stages"]["router"]["status"], "budget_exhausted")
        self.assertEqual(debug["stages"]["router"]["error_type"], "BudgetExceeded")
        self.assertNotIn("routing", debug)

    def test_generic_header_stays_standard_but_known_domains_are_sensitive(self):
        self.assertEqual(routing.minimum_tier({"src/util/translation.h"}), "standard")
        cases = {
            "src/validation.h": ("consensus",),
            "src/pow.cpp": ("consensus",),
            "src/chain.h": ("consensus",),
            "src/chainparams.cpp": ("consensus",),
            "src/undo.h": ("consensus",),
            "src/primitives/block.h": ("consensus",),
            "src/net.h": ("p2p",),
            "src/net_processing.h": ("p2p",),
            "src/wallet/wallet.h": ("wallet",),
            "src/kernel.h": ("consensus",),
            "src/blockstorage.h": ("consensus",),
            "src/chainstate.h": ("consensus",),
        }
        for path, profiles in cases.items():
            with self.subTest(path=path):
                self.assertEqual(routing.minimum_tier({path}), "sensitive")
                plan = routing.validate_plan(route(), {path}, "routine")
                self.assertEqual(plan["tier"], "sensitive")
                self.assertEqual(tuple(plan["profiles"]), profiles)

        plan = routing.validate_plan(route(), {"src/sync.h"}, "routine")
        self.assertEqual(plan["tier"], "sensitive")
        self.assertEqual(plan["audits"], ["concurrency", "tests", "design"])
        self.assertEqual(plan["profiles"], [])

        plan = routing.validate_plan(route(), {"src/validationinterface.cpp"}, "routine")
        self.assertEqual(plan["audits"], ["concurrency", "tests", "design"])

    def test_test_only_changes_are_routine_tests_unless_router_adds_design(self):
        paths = {"src/test/validation_tests.cpp"}
        self.assertEqual(routing.minimum_tier(paths), "routine")
        plan = routing.validate_plan(route(), paths, "routine")
        self.assertEqual(plan["tier"], "routine")
        self.assertEqual(plan["audits"], ["tests"])
        self.assertEqual(plan["profiles"], [])

        plan = routing.validate_plan(route(audits=("design",)), paths, "routine")
        self.assertEqual(plan["audits"], ["tests", "design"])

    def test_invalid_schema_rejects_old_or_unknown_route_objects(self):
        with self.assertRaisesRegex(InvalidReview, "Invalid routing fields"):
            routing.validate_plan(json.dumps({
                "tier": "routine", "audits": [], "evidence": [],
                "missing_context": [],
            }), set(), "routine")
        with self.assertRaisesRegex(InvalidReview, "Unknown adversarial profile"):
            routing.validate_plan(route(profiles=("compiler",)), set(), "routine")

    def test_conditional_domains_add_only_their_audits_and_profiles(self):
        plan = routing.validate_plan(route(audits=("public_contract",)), {
            "src/node/blockstorage.cpp",
            "src/policy/fees.cpp",
            "src/rpc/mempool.cpp",
            "depends/packages/libevent.mk",
        }, "standard")
        self.assertEqual(plan["tier"], "sensitive")
        self.assertEqual(plan["profiles"], ["consensus", "p2p"])
        self.assertEqual(set(plan["audits"]),
                         {"state", "public_contract", "tests", "design", "build"})

    def test_public_contract_domains_and_build_only_floor(self):
        for path in ("src/httprpc.cpp", "src/httpserver.cpp", "src/rest.cpp",
                     "src/qt/rpcconsole.h", "src/wallet/rpc/spend.cpp"):
            with self.subTest(path=path):
                plan = routing.validate_plan(route(), {path}, "routine")
                self.assertEqual(plan["tier"], "sensitive")
                self.assertIn("public_contract", plan["audits"])
                self.assertIn("tests", plan["audits"])

        plan = routing.validate_plan(route(), {"depends/packages/libevent.mk"}, "routine")
        self.assertEqual(plan["tier"], "sensitive")
        self.assertEqual(plan["audits"], ["build"])
        self.assertEqual(plan["profiles"], [])

        plan = routing.validate_plan(route(audits=("tests",)),
                                     {"depends/packages/libevent.mk"}, "routine")
        self.assertEqual(plan["audits"], ["tests", "build"])

    def test_profiles_force_sensitive_tier(self):
        plan = routing.validate_plan(route(profiles=("wallet",)), {"src/util/time.cpp"},
                                     "standard")
        self.assertEqual(plan["tier"], "sensitive")
        self.assertEqual(plan["profiles"], ["wallet"])


if __name__ == "__main__":
    unittest.main()
