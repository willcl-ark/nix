import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from ralph import config, current_engine, engine, pipeline, profiles, spend


class ProfileTests(unittest.TestCase):
    def test_default_preserves_all_stage_settings(self):
        expected = {
            "router": ("gpt-6-luna", "low", 4000, (0, 0, 0)),
            "independent": ("gpt-6-luna", "low", 4000, (12, 24, 48)),
            "adversarial": ("gpt-6.1-sol", "high", 25000, (48, 48, 48)),
            "adversarial_deepseek": (
                "deepseek/deepseek-v4-pro-0813",
                "high",
                32000,
                (48, 48, 48),
            ),
            "adversarial_gemini": (
                "google/gemini-4-argon",
                "high",
                32000,
                (48, 48, 48),
            ),
            "script": ("gpt-6.1-sol", "high", 25000, (48, 48, 48)),
            "concurrency": ("gpt-6-luna", "medium", 8000, (12, 24, 48)),
            "state": ("gpt-6-luna", "low", 4000, (12, 24, 48)),
            "public_contract": ("gpt-6.1-sol", "low", 25000, (12, 24, 48)),
            "tests": ("gpt-6-luna", "low", 4000, (12, 24, 48)),
            "dead_code": ("gpt-6-luna", "medium", 8000, (48, 48, 48)),
            "design": ("gpt-6-luna", "xhigh", 25000, (12, 24, 48)),
            "build": ("gpt-6-luna", "low", 4000, (12, 24, 48)),
            "archaeologist": ("gpt-6-luna", "low", 4000, (12, 12, 12)),
            "alternatives": ("gpt-6-luna", "low", 4000, (12, 12, 12)),
            "verifier": ("gpt-6-luna", "xhigh", 128000, (48, 48, 48)),
            "major_verifier": ("gpt-6.1-sol", "medium", 25000, (48, 48, 48)),
            "collator": ("gpt-6-luna", "low", 6000, (0, 0, 0)),
        }
        profile = profiles.get_profile()
        actual = {
            name: (
                stage.model,
                stage.reasoning_effort,
                stage.max_output_tokens,
                tuple(stage.max_tool_calls[tier] for tier in profiles.TIERS),
            )
            for name, stage in profile.stages.items()
        }
        self.assertEqual(actual, expected)
        self.assertEqual(profile.budget_multiplier, 1)
        for stage in profile.stages.values():
            self.assertEqual(
                (
                    stage.max_context_calls,
                    stage.max_history_calls,
                    stage.max_web_search_calls_per_response,
                ),
                (8, 8, 2),
            )

    def test_high_only_increases_core_review(self):
        default, high = profiles.get_profile(), profiles.get_profile("high")
        self.assertEqual(high.budget_multiplier, 5)
        for name, stage in high.stages.items():
            if name in profiles.CORE_STAGES:
                self.assertEqual(stage.model, "gpt-6.1-sol")
                self.assertIn(stage.reasoning_effort, {"high", "xhigh"})
                self.assertGreaterEqual(stage.max_output_tokens, 16000)
            else:
                self.assertEqual(stage, default.stages[name])
            self.assertEqual(stage.max_tool_calls, default.stages[name].max_tool_calls)
        self.assertEqual(high.stages["verifier"].max_output_tokens, 128000)

    def test_snapshot_roundtrips_and_hashes_complete_profile(self):
        selected = engine.load(config={"profile": "high"})
        self.assertEqual(current_engine.configure(selected.config), selected.config)
        restored = profiles.ReviewProfile.from_dict(
            json.loads(json.dumps(selected.config["profile"]))
        )
        self.assertEqual(restored, profiles.get_profile("high"))
        changed = selected.config
        changed["profile"]["stages"]["state"]["max_context_calls"] += 1
        self.assertNotEqual(
            engine.load(config=changed).identity["config_sha256"],
            selected.identity["config_sha256"],
        )
        self.assertNotEqual(
            engine.load().identity["config_sha256"], selected.identity["config_sha256"]
        )

    def test_model_override_applies_after_profile_and_includes_alternatives(self):
        models = {name: "gpt-6-luna" for name in config.MODEL_NAMES}
        models["archaeologist"] = "gpt-6.1-sol"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            path.write_text(json.dumps(models))
            selected = current_engine.configure(
                {"profile": "high", "models_json": str(path)}
            )
        stages = selected["profile"]["stages"]
        self.assertEqual(stages["alternatives"]["model"], "gpt-6.1-sol")
        self.assertEqual(stages["state"]["model"], "gpt-6-luna")
        self.assertEqual(stages["state"]["reasoning_effort"], "high")

    def test_budget_scales_review_cap_without_changing_shared_monthly_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = spend.Ledger(Path(directory) / "spend.sqlite", 2, 20)
            selected = profiles.get_profile("high").apply_budget(ledger)
            self.assertIsNot(selected, ledger)
            self.assertEqual(selected.review_limit_micros, 10_000_000)
            self.assertEqual(ledger.review_limit_micros, 2_000_000)
            self.assertEqual(selected.monthly_limit_micros, ledger.monthly_limit_micros)
            self.assertEqual(selected.path, ledger.path)
            self.assertIsNone(profiles.get_profile("high").apply_budget(None))

    def test_prompt_snapshot_rejects_old_models_and_other_extra_keys(self):
        for key in ("models", "extra"):
            configured = current_engine.configure({})
            configured["prompts"][key] = {}
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(
                    ValueError,
                    "prompts must contain only instructions and audit_prompts",
                ),
            ):
                current_engine.configure(configured)

    def test_invalid_profiles_rejected(self):
        with self.assertRaises(ValueError):
            profiles.get_profile("unknown")
        for field, value in (
            ("max_output_tokens", 0),
            ("max_context_calls", -1),
            ("max_tool_calls", {"routine": 1}),
            ("model", "unknown"),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                replace(profiles.get_profile().stages["state"], **{field: value})

    def test_pipeline_uses_resolved_high_settings(self):
        prompts = config.PromptConfig.load(profile="high")
        runtime = engine.Runtime(
            case=engine.ReviewCase("patch", None),
            api_key="key",
            attempt={"stages": {}, "stage_outputs": {}},
        )
        with patch.object(runtime, "review", return_value="answer") as review:
            pipeline._run_stage(
                runtime, prompts, {"tier": "routine"}, "state", "input", "prompt", {}
            )
        self.assertEqual(review.call_args.kwargs["model_name"], "gpt-6.1-sol")
        self.assertEqual(review.call_args.kwargs["reasoning_effort"], "high")
        self.assertEqual(review.call_args.kwargs["max_output_tokens"], 16000)
        self.assertEqual(review.call_args.kwargs["max_tool_calls"], 12)
        self.assertEqual(review.call_args.kwargs["max_context_calls"], 8)


if __name__ == "__main__":
    unittest.main()
