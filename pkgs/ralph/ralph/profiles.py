"""Complete model and request limits for each review stage."""

import math
from copy import copy
from dataclasses import asdict, dataclass, fields, replace

from .spend import supports_model

PROFILE_NAMES = ("default", "high")
TIERS = ("routine", "standard", "sensitive")


@dataclass(frozen=True)
class StageSettings:
    model: str
    reasoning_effort: str
    max_output_tokens: int
    max_tool_calls: dict[str, int]
    max_context_calls: int = 8
    max_history_calls: int = 8
    max_web_search_calls_per_response: int = 2
    timeout: int = 180
    final_timeout: int = 180

    def __post_init__(self):
        if not isinstance(self.model, str) or not supports_model(self.model):
            raise ValueError("Unsupported stage model")
        if self.reasoning_effort not in {"low", "medium", "high", "xhigh"}:
            raise ValueError("Unknown reasoning effort")
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
        if set(self.stages) != set(_DEFAULT_STAGES):
            raise ValueError("Profile must name every review stage")
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
                model=models["archaeologist" if name == "alternatives" else name],
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


def _stage(
    model="gpt-6-luna",
    effort="low",
    output=4_000,
    calls=None,
    *,
    timeout=180,
    final_timeout=180,
):
    limits = (
        dict(zip(TIERS, (12, 24, 48))) if calls is None else dict.fromkeys(TIERS, calls)
    )
    return StageSettings(
        model, effort, output, limits, timeout=timeout, final_timeout=final_timeout
    )


_DEFAULT_STAGES = {
    "router": _stage(calls=0),
    "independent": _stage(),
    "adversarial": _stage("gpt-6.1-sol", "high", 25_000, 72),
    "adversarial_claude": _stage(
        "claude-opus-5-5", "xhigh", 64_000, 72, timeout=600, final_timeout=600
    ),
    "script": _stage("gpt-6.1-sol", "high", 25_000, 48),
    "concurrency": _stage(effort="medium", output=8_000),
    "state": _stage(),
    "public_contract": _stage("gpt-6.1-sol", output=25_000),
    "tests": _stage(),
    "dead_code": _stage(effort="medium", output=8_000, calls=48),
    "design": _stage(effort="xhigh", output=25_000),
    "build": _stage(),
    "archaeologist": _stage(calls=12),
    "alternatives": _stage(calls=12),
    "verifier": _stage(effort="xhigh", output=128_000, calls=72, final_timeout=600),
    "major_verifier": _stage("gpt-6.1-sol", "medium", 25_000, 72, final_timeout=600),
    "collator": _stage(output=6_000, calls=0),
}
CORE_STAGES = (
    "independent",
    "adversarial",
    "script",
    "concurrency",
    "state",
    "public_contract",
    "dead_code",
    "design",
    "verifier",
    "major_verifier",
)


def get_profile(name="default"):
    # Return fresh mappings so overrides cannot change subsequent reviews.
    stages = {
        key: replace(value, max_tool_calls=dict(value.max_tool_calls))
        for key, value in _DEFAULT_STAGES.items()
    }
    if name == "high":
        for key in CORE_STAGES:
            stage = stages[key]
            stages[key] = replace(
                stage,
                model="gpt-6.1-sol",
                reasoning_effort="xhigh"
                if stage.reasoning_effort == "xhigh"
                else "high",
                max_output_tokens=max(stage.max_output_tokens, 16_000),
            )
    return ReviewProfile(name, stages, 5 if name == "high" else 1)
