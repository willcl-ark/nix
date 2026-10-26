import unittest

from ralph import current_engine, engine


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
        ):
            with (
                self.subTest(stages=stages),
                self.assertRaisesRegex(ValueError, message),
            ):
                current_engine.configure({"stages": stages})
