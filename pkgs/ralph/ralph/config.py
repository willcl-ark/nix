"""Validated review prompts."""

from dataclasses import asdict, dataclass, field
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
    "gui",
    "state",
    "public_contract",
    "tests",
    "symbol_usage",
    "commit_flow",
    "design",
    "build",
)
ADVERSARIAL_PROFILES = ("consensus", "wallet", "p2p")
SHARED_PROMPTS = {
    "common": "shared/discovery.md",
    "backport": "shared/backport.md",
    "reconciliation": "verifier/reconciliation.md",
    "concept_verifier": "verifier/concept.md",
    **{name: f"adversarial/domains/{name}.md" for name in ADVERSARIAL_PROFILES},
}
PROMPT_NAMES = (
    *[name for name in STAGE_NAMES if name != "independent"],
    *SHARED_PROMPTS,
)


@dataclass(frozen=True)
class ReviewStrategy:
    name: str
    audits: tuple[str, ...]
    adversarial_profiles: tuple[str, ...]

    def __post_init__(self):
        if self.name not in {"core", "gui"}:
            raise ValueError("Unknown review strategy")
        if (
            not self.audits
            or len(set(self.audits)) != len(self.audits)
            or set(self.audits) - set(AUDIT_NAMES)
            or len(set(self.adversarial_profiles)) != len(self.adversarial_profiles)
            or set(self.adversarial_profiles) - set(ADVERSARIAL_PROFILES)
        ):
            raise ValueError("Invalid review strategy stages")
        if self.name == "gui" and (
            "script" in self.audits or set(self.adversarial_profiles) - {"wallet"}
        ):
            raise ValueError("GUI strategy cannot select Core protocol audits")

    def to_dict(self):
        return {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in asdict(self).items()
        }

    @classmethod
    def load(cls, value="core"):
        if isinstance(value, dict):
            if set(value) != {"name", "audits", "adversarial_profiles"}:
                raise ValueError("Invalid review strategy fields")
            return cls(
                value["name"],
                tuple(value["audits"]),
                tuple(value["adversarial_profiles"]),
            )
        if value == "gui":
            return cls(
                "gui",
                tuple(name for name in AUDIT_NAMES if name != "script"),
                ("wallet",),
            )
        return cls(value, AUDIT_NAMES, ADVERSARIAL_PROFILES)


def load_prompt_file(path):
    return path.read_text(encoding="utf-8").removesuffix("\n")


@dataclass(frozen=True)
class PromptConfig:
    instructions: str
    audit_prompts: dict[str, str]
    profile: ReviewProfile
    high_profile: ReviewProfile | None = None
    strategy: ReviewStrategy = field(default_factory=ReviewStrategy.load)

    @property
    def models(self):
        return {name: settings.model for name, settings in self.profile.stages.items()}

    @classmethod
    def load(
        cls,
        agents_dir=DEFAULT_AGENTS_DIR,
        profile="default",
        strategy="core",
    ):
        strategy = ReviewStrategy.load(strategy)
        agents_dir = Path(agents_dir).resolve()
        configs = load_agent_configs(agents_dir)
        selected = get_profile(profile, agents_dir, agent_configs=configs)
        high = get_profile("high", agents_dir, agent_configs=configs)
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
        prompts["judge"] += f"\n\n{finding_format}"
        if strategy.name == "gui":
            prompts["router"] = load_prompt_file(agents_dir / "gui/router.md")
            context = load_prompt_file(agents_dir / "shared/gui.md")
            if not context.strip() or not prompts["router"].strip():
                raise ValueError("GUI strategy instructions must not be empty")
            # Discovery roles already receive common instructions. Add the
            # domain context once to each independently dispatched prompt.
            contextual = {
                "common",
                "router",
                "challenger",
                "verifier",
                "judge",
                "write_verifier",
                "archaeologist",
                "historical_context",
                "alternatives",
                "feedback",
            }
            prompts = {
                name: (context + "\n\n" if name in contextual else "")
                + prompt.replace("a Bitcoin Core reviewer", "a GUI-QML reviewer")
                .replace(
                    "a Bitcoin Core security reviewer", "a GUI-QML security reviewer"
                )
                .replace("a Bitcoin Core pull request", "a GUI-QML pull request")
                for name, prompt in prompts.items()
            }
        return cls(prompts.pop("independent"), prompts, selected, high, strategy)
