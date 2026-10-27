"""Validated review prompts."""

from dataclasses import dataclass
from pathlib import Path

from .profiles import (
    DEFAULT_AGENTS_DIR,
    STAGE_NAMES,
    ReviewProfile,
    get_profile,
    load_agent_configs,
)


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
SHARED_PROMPTS = {
    "common": "shared/discovery.md",
    "backport": "shared/backport.md",
    "reconciliation": "verifier/reconciliation.md",
    **{name: f"adversarial/domains/{name}.md" for name in ADVERSARIAL_PROFILES},
}
PROMPT_NAMES = (
    *[name for name in STAGE_NAMES if name != "independent"],
    *SHARED_PROMPTS,
)


def load_prompt_file(path):
    return path.read_text(encoding="utf-8").removesuffix("\n")


@dataclass(frozen=True)
class PromptConfig:
    instructions: str
    audit_prompts: dict[str, str]
    profile: ReviewProfile

    @property
    def models(self):
        return {name: settings.model for name, settings in self.profile.stages.items()}

    @classmethod
    def load(
        cls,
        agents_dir=DEFAULT_AGENTS_DIR,
        profile="default",
    ):
        agents_dir = Path(agents_dir).resolve()
        configs = load_agent_configs(agents_dir)
        selected = get_profile(profile, agents_dir, agent_configs=configs)
        prompts = {}
        for name, values in configs.items():
            path = (agents_dir / name / values.get("prompt", "prompt.md")).resolve()
            if not path.is_relative_to(agents_dir):
                raise ValueError(
                    f"{name}: prompt must stay inside the agents directory"
                )
            prompts[name] = load_prompt_file(path)
        prompts.update(
            {
                name: load_prompt_file(agents_dir / path)
                for name, path in SHARED_PROMPTS.items()
            }
        )
        if any(not prompt.strip() for prompt in prompts.values()):
            raise ValueError("agent prompt files must not be empty")
        finding_format = load_prompt_file(agents_dir / "shared/finding-format.md")
        if not finding_format.strip():
            raise ValueError("shared finding format must not be empty")
        for name in ("verifier", "collator"):
            prompts[name] += f"\n\n{finding_format}"
        return cls(prompts.pop("independent"), prompts, selected)
