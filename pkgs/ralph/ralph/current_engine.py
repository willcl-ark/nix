"""The existing routed Ralph review engine."""

import json
from pathlib import Path

from . import config, pipeline, profiles
from .engine import Output


def configure(value):
    value = dict(value)
    selected_profile = value.get("profile", "default")
    profile = (
        profiles.get_profile(selected_profile)
        if isinstance(selected_profile, str)
        else profiles.ReviewProfile.from_dict(selected_profile)
    )
    prompts = value.get("prompts")
    if prompts is None:
        prompt_file = Path(value.get("prompt_file", config.DEFAULT_PROMPT_FILE))
        audit_dir = Path(value.get("audit_prompt_dir", config.DEFAULT_AUDIT_DIR))
        prompts = {
            "instructions": config.load_prompt_file(prompt_file),
            "audit_prompts": {
                name: config.load_prompt_file(audit_dir / f"{name}.md")
                for name in config.PROMPT_NAMES
            },
        }
    else:
        prompts = dict(prompts)
        if set(prompts) != {"instructions", "audit_prompts"}:
            raise ValueError("prompts must contain only instructions and audit_prompts")
        if not isinstance(prompts.get("instructions"), str) or not isinstance(
            prompts.get("audit_prompts"), dict
        ):
            raise ValueError("prompts must contain instructions and audit_prompts")
        if set(prompts["audit_prompts"]) != set(config.PROMPT_NAMES):
            raise ValueError("prompt config must name each review prompt")
        if "prompt_file" in value:
            prompts["instructions"] = config.load_prompt_file(
                Path(value["prompt_file"])
            )
        if "audit_prompt_dir" in value:
            audit_dir = Path(value["audit_prompt_dir"])
            prompts["audit_prompts"] = {
                name: config.load_prompt_file(audit_dir / f"{name}.md")
                for name in config.PROMPT_NAMES
            }
    if "models_json" in value:
        profile = profile.with_models(
            config.validate_models(
                json.loads(Path(value["models_json"]).read_text(encoding="utf-8"))
            )
        )
    if any(
        not isinstance(prompt, str) or not prompt.strip()
        for prompt in prompts["audit_prompts"].values()
    ):
        raise ValueError("audit prompt files must not be empty")
    normalized = {
        "prompts": prompts,
        "profile": profile.to_dict(),
        "routing_mode": value.get("routing_mode", "enabled"),
        "blind_alternatives": value.get("blind_alternatives", True),
        "archaeology": value.get("archaeology", False),
    }
    if normalized["routing_mode"] not in {"enabled", "shadow", "full"}:
        raise ValueError("Unknown routing mode")
    if type(normalized["blind_alternatives"]) is not bool:
        raise ValueError("blind_alternatives must be a boolean")
    if type(normalized["archaeology"]) is not bool:
        raise ValueError("archaeology must be a boolean")
    return normalized


def run(case, runtime, configured):
    if configured["archaeology"]:
        if (
            not isinstance(case.source, dict)
            or case.source.get("kind") != "pull_request"
            or not case.source.get("url")
            or (case.research_evidence is None and runtime.discussion_lookup is None)
        ):
            raise ValueError(
                "archaeology requires PR context and discussion evidence or a reader"
            )
    runtime.debug["schema_version"] = 2
    prompt_config = config.PromptConfig(
        **configured["prompts"],
        profile=profiles.ReviewProfile.from_dict(configured["profile"]),
    )
    result = pipeline.review_with_independent_passes(
        case,
        runtime,
        prompt_config,
        configured["routing_mode"],
        configured["blind_alternatives"],
        configured["archaeology"],
    )
    data = {**runtime.debug, "schema_version": 2}
    exports = {
        "findings": result.findings,
        "actionable": [
            *[finding["id"] for finding in result.findings],
            *(["concept_objection"] if result.concept_concern else []),
        ],
    }
    return Output(result.content, data, exports, schema_version=2)
