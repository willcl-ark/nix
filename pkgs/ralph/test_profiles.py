import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from ralph import config, current_engine, engine, pipeline


class StageConfigurationTests(unittest.TestCase):
    def test_stage_override_survives_high_profile_and_changes_identity(self):
        baseline = engine.load(config={"profile": "high"})
        changed = engine.load(
            config={
                "profile": "high",
                "stages": {
                    "verifier": {
                        "model": "claude-opus-5-5",
                        "reasoning_effort": "high",
                        "timeout": 600,
                        "final_timeout": 600,
                    }
                },
            }
        )
        settings = changed.config["profile"]["stages"]["verifier"]
        self.assertEqual(settings["model"], "claude-opus-5-5")
        self.assertEqual(settings["reasoning_effort"], "high")
        self.assertEqual(settings["timeout"], 600)
        self.assertEqual(settings["final_timeout"], 600)
        self.assertEqual(
            settings["max_output_tokens"],
            baseline.config["profile"]["stages"]["verifier"]["max_output_tokens"],
        )
        self.assertNotEqual(
            changed.identity["config_sha256"], baseline.identity["config_sha256"]
        )
        self.assertEqual(current_engine.configure(changed.config), changed.config)

    def test_invalid_stage_override_fails_configuration(self):
        for stages, message in (
            ({"verifer": {}}, "Unknown review stage: verifer"),
            ({"verifier": {"effort": "high"}}, "Unknown settings for verifier: effort"),
            ({"verifier": {"timeout": 0}}, "verifier.*timeouts"),
            ({"collator": {"tool_set": "full"}}, "collator does not support tools"),
        ):
            with (
                self.subTest(stages=stages),
                self.assertRaisesRegex(ValueError, message),
            ):
                current_engine.configure({"stages": stages})

    def test_tool_settings_reach_dispatch_without_backport_reenabling_tools(self):
        prompts = config.PromptConfig.load()
        prompts = replace(
            prompts,
            profile=prompts.profile.with_stages(
                {
                    "independent": {
                        "tool_set": "none",
                        "max_tool_calls": dict.fromkeys(
                            ("routine", "standard", "sensitive"), 1
                        ),
                    }
                }
            ),
        )
        runtime = SimpleNamespace(
            case=SimpleNamespace(research_evidence=None, backport_evidence={}),
            discussion_lookup=None,
            allow_web_search=True,
            debug={"stages": {}, "stage_outputs": {}},
            review=Mock(return_value="finished"),
        )
        pipeline._run_stage(
            runtime,
            prompts,
            {"tier": "sensitive"},
            "independent",
            "input",
            "prompt",
            None,
        )
        settings = runtime.review.call_args.kwargs
        self.assertEqual(settings["tools"], [])
        self.assertEqual(settings["max_tool_calls"], 1)
        self.assertFalse(settings["first_tool_required"])

    def test_comment_templates_change_identity_and_survive_config_reload(self):
        baseline = engine.load()
        settings = baseline.config
        settings["comment_format"]["finding"] = (
            "${body}\n\n${title} ${location} ${side}"
        )
        changed = engine.load(config=settings)
        self.assertNotEqual(
            changed.identity["config_sha256"], baseline.identity["config_sha256"]
        )
        self.assertEqual(current_engine.configure(changed.config), changed.config)
