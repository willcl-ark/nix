"""Complete model and request limits for each review stage."""

import math
import tomllib
from copy import copy
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

from .model import TOOL_SETS
from .spend import supports_model

DEFAULT_AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"
PROFILE_NAMES = ("default", "high")
TIERS = ("routine", "standard", "sensitive")
STAGE_NAMES = (
    "router",
    "independent",
    "adversarial",
    "adversarial_claude",
    "script",
    "concurrency",
    "state",
    "public_contract",
    "tests",
    "dead_code",
    "design",
    "build",
    "archaeologist",
    "alternatives",
    "feedback",
    "verifier",
    "major_verifier",
    "collator",
)


@dataclass(frozen=True)
class StageSettings:
    model: str
    reasoning_effort: str
    max_output_tokens: int
    max_tool_calls: dict[str, int]
    tool_set: str
    max_context_calls: int = 8
    max_history_calls: int = 8
    max_web_search_calls_per_response: int = 2
    timeout: int = 1800
    final_timeout: int = 1800

    def __post_init__(self):
        if not isinstance(self.model, str) or not supports_model(self.model):
            raise ValueError("Unsupported stage model")
        if self.reasoning_effort not in {"low", "medium", "high", "xhigh"}:
            raise ValueError("Unknown reasoning effort")
        if not isinstance(self.tool_set, str) or self.tool_set not in TOOL_SETS:
            raise ValueError("Unknown tool set")
        if not isinstance(self.max_tool_calls, dict) or set(self.max_tool_calls) != set(
            TIERS
        ):
            raise ValueError("Tool limits must name every review tier")
        if any(
            type(value) is not int or value <= 0
            for value in (self.timeout, self.final_timeout)
        ):
            raise ValueError("Stage timeouts must be positive integers")
        limits = [
            self.max_output_tokens,
            *self.max_tool_calls.values(),
            self.max_context_calls,
            self.max_history_calls,
            self.max_web_search_calls_per_response,
        ]
        if (
            any(type(limit) is not int or limit < 0 for limit in limits)
            or self.max_output_tokens == 0
        ):
            raise ValueError(
                "Stage limits must be nonnegative integers with positive output tokens"
            )


@dataclass(frozen=True)
class ReviewProfile:
    name: str
    stages: dict[str, StageSettings]
    budget_multiplier: float = 1

    def __post_init__(self):
        if self.name not in PROFILE_NAMES:
            raise ValueError("Unknown review profile")
        if set(self.stages) != set(STAGE_NAMES):
            raise ValueError("Profile must name every review stage")
        for name in ("router", "collator", "feedback"):
            settings = self.stages[name]
            if settings.tool_set != "none" or any(settings.max_tool_calls.values()):
                raise ValueError(f"{name} does not support tools")
        if (
            type(self.budget_multiplier) not in {int, float}
            or not math.isfinite(self.budget_multiplier)
            or self.budget_multiplier <= 0
        ):
            raise ValueError("Profile budget multiplier must be positive and finite")

    def apply_budget(self, ledger):
        """Scale the review cap while retaining the shared spend ledger."""
        if ledger is None:
            return None
        selected = copy(ledger)
        if selected.review_limit_micros is not None:
            selected.review_limit_micros = math.ceil(
                selected.review_limit_micros * self.budget_multiplier
            )
        return selected

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(
            value["name"],
            {
                name: StageSettings(**settings)
                for name, settings in value["stages"].items()
            },
            value["budget_multiplier"],
        )

    def with_models(self, models):
        stages = {
            name: replace(
                settings,
                model=models[
                    "archaeologist" if name in {"alternatives", "feedback"} else name
                ],
            )
            for name, settings in self.stages.items()
        }
        return replace(self, stages=stages)

    def with_stages(self, overrides):
        if not isinstance(overrides, dict):
            raise ValueError("stages must be an object of stage settings")
        stages = dict(self.stages)
        setting_names = {field.name for field in fields(StageSettings)}
        for name, settings in overrides.items():
            if name not in stages:
                raise ValueError(f"Unknown review stage: {name}")
            if not isinstance(settings, dict):
                raise ValueError(f"Settings for {name} must be an object")
            unknown = settings.keys() - setting_names
            if unknown:
                raise ValueError(
                    f"Unknown settings for {name}: {', '.join(sorted(unknown))}"
                )
            try:
                stages[name] = replace(stages[name], **settings)
            except ValueError as exc:
                raise ValueError(f"Invalid settings for {name}: {exc}") from exc
        return replace(self, stages=stages)


def read_toml(path):
    try:
        with path.open("rb") as source:
            return tomllib.load(source)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path}: {exc}") from exc


def load_agent_configs(agents_dir=DEFAULT_AGENTS_DIR):
    agents_dir = Path(agents_dir)
    names = {path.parent.name for path in agents_dir.glob("*/config.toml")}
    if names != set(STAGE_NAMES):
        missing = sorted(set(STAGE_NAMES) - names)
        unknown = sorted(names - set(STAGE_NAMES))
        raise ValueError(
            f"{agents_dir}: missing agents {missing}; unknown agents {unknown}"
        )
    return {name: read_toml(agents_dir / name / "config.toml") for name in STAGE_NAMES}


def get_profile(name="default", agents_dir=DEFAULT_AGENTS_DIR, *, agent_configs=None):
    agents_dir = Path(agents_dir)
    metadata = read_toml(agents_dir / "profiles.toml")
    if set(metadata) != set(PROFILE_NAMES) or name not in PROFILE_NAMES:
        raise ValueError("Review profiles must name default and high")
    for value in metadata.values():
        if not isinstance(value, dict) or set(value) != {"budget_multiplier"}:
            raise ValueError("Review profile metadata must contain budget_multiplier")
    if agent_configs is None:
        agent_configs = load_agent_configs(agents_dir)
    stages = {}
    for stage, values in agent_configs.items():
        values = dict(values)
        prompt = values.pop("prompt", "prompt.md")
        overrides = values.pop("profiles", {})
        path = agents_dir / stage / "config.toml"
        if not isinstance(prompt, str) or not prompt:
            raise ValueError(f"{path}: prompt must name a Markdown file")
        if not isinstance(overrides, dict) or set(overrides) - {"high"}:
            raise ValueError(f"{path}: only profiles.high overrides are supported")
        selected = overrides.get(name, {})
        if not isinstance(selected, dict):
            raise ValueError(f"{path}: profile overrides must be a table")
        try:
            stages[stage] = StageSettings(**(values | selected))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{path} ({name}): {exc}") from exc
    return ReviewProfile(name, stages, metadata[name]["budget_multiplier"])
