"""Validated review prompts."""

import json
from dataclasses import dataclass
from pathlib import Path

from .profiles import ReviewProfile, get_profile
from .spend import supports_model


DEFAULT_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompt.md"
DEFAULT_AUDIT_DIR = Path(__file__).resolve().parent.parent / "audits"
AUDIT_NAMES = (
    "script",
    "concurrency",
    "state",
    "public_contract",
    "tests",
    "dead_code",
    "design",
    "build",
)
ADVERSARIAL_PROFILES = ("consensus", "wallet", "p2p")
MODEL_NAMES = (
    "router",
    "independent",
    "adversarial",
    "adversarial_claude",
    *AUDIT_NAMES,
    "archaeologist",
    "verifier",
    "major_verifier",
    "collator",
)
PROMPT_NAMES = (
    "common",
    "router",
    "adversarial",
    *ADVERSARIAL_PROFILES,
    *AUDIT_NAMES,
    "archaeologist",
    "alternatives",
    "verifier",
    "major_verifier",
    "collator",
)


def load_prompt_file(path):
    return path.read_text(encoding="utf-8").removesuffix("\n")


def validate_models(models):
    if set(models) != set(MODEL_NAMES) or any(
        not isinstance(model, str) or not supports_model(model)
        for model in models.values()
    ):
        raise ValueError("model config must name each review stage")
    return models


@dataclass(frozen=True)
class PromptConfig:
    instructions: str
    audit_prompts: dict[str, str]
    profile: ReviewProfile

    @property
    def models(self):
        return {name: self.profile.stages[name].model for name in MODEL_NAMES}

    @classmethod
    def load(
        cls,
        prompt_file=DEFAULT_PROMPT_FILE,
        audit_dir=DEFAULT_AUDIT_DIR,
        models_json=None,
        profile="default",
    ):
        prompts = {
            name: load_prompt_file(audit_dir / f"{name}.md") for name in PROMPT_NAMES
        }
        if any(not prompt.strip() for prompt in prompts.values()):
            raise ValueError("audit prompt files must not be empty")
        selected = get_profile(profile)
        if models_json is not None:
            selected = selected.with_models(
                validate_models(json.loads(models_json.read_text(encoding="utf-8")))
            )
        return cls(load_prompt_file(prompt_file), prompts, selected)
