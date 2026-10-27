"""The existing routed Ralph review engine."""

from urllib.parse import urlsplit

from final_comment import load_format, validate_format

from . import config, pipeline, profiles
from .engine import Output


def configure(value):
    value = dict(value)
    removed = value.keys() & {"prompt_file", "audit_prompt_dir", "models_json"}
    if removed:
        raise ValueError(
            f"Removed engine settings: {', '.join(sorted(removed))}; "
            "use agents_dir and stages"
        )
    agents_dir = value.get("agents_dir", config.DEFAULT_AGENTS_DIR)
    selected_profile = value.get("profile", "default")
    prompts = value.get("prompts")
    if prompts is None or "agents_dir" in value:
        loaded = config.PromptConfig.load(
            agents_dir,
            profile=selected_profile
            if isinstance(selected_profile, str)
            else selected_profile["name"],
        )
        profile = loaded.profile
        if isinstance(selected_profile, dict) and "agents_dir" not in value:
            profile = profiles.ReviewProfile.from_dict(selected_profile)
        prompts = {
            "instructions": loaded.instructions,
            "audit_prompts": loaded.audit_prompts,
        }
    else:
        profile = (
            profiles.get_profile(selected_profile, agents_dir)
            if isinstance(selected_profile, str)
            else profiles.ReviewProfile.from_dict(selected_profile)
        )
        prompts = dict(prompts)
        if set(prompts) != {"instructions", "audit_prompts"}:
            raise ValueError("prompts must contain only instructions and audit_prompts")
        if not isinstance(prompts.get("instructions"), str) or not isinstance(
            prompts.get("audit_prompts"), dict
        ):
            raise ValueError("prompts must contain instructions and audit_prompts")
        if set(prompts["audit_prompts"]) != set(config.PROMPT_NAMES):
            raise ValueError("prompt config must name each review prompt")
    profile = profile.with_stages(value.get("stages", {}))
    if any(
        not isinstance(prompt, str) or not prompt.strip()
        for prompt in (prompts["instructions"], *prompts["audit_prompts"].values())
    ):
        raise ValueError("agent prompt files must not be empty")
    normalized = {
        "prompts": prompts,
        "profile": profile.to_dict(),
        "comment_format": validate_format(value["comment_format"])
        if "comment_format" in value
        else load_format(),
        "routing_mode": value.get("routing_mode", "enabled"),
        "blind_alternatives": value.get("blind_alternatives", True),
        "archaeology": value.get("archaeology", False),
        "openai_api_base": value.get("openai_api_base", "https://api.openai.com/v1"),
    }
    if normalized["routing_mode"] not in {"enabled", "shadow", "full"}:
        raise ValueError("Unknown routing mode")
    if type(normalized["blind_alternatives"]) is not bool:
        raise ValueError("blind_alternatives must be a boolean")
    if type(normalized["archaeology"]) is not bool:
        raise ValueError("archaeology must be a boolean")
    if not isinstance(normalized["openai_api_base"], str):
        raise ValueError("openai_api_base must be an HTTP URL without credentials")
    normalized["openai_api_base"] = normalized["openai_api_base"].rstrip("/")
    endpoint = urlsplit(normalized["openai_api_base"])
    if (
        endpoint.scheme not in {"http", "https"}
        or not endpoint.hostname
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("openai_api_base must be an HTTP URL without credentials")
    return normalized


def run(case, runtime, configured):
    if "openai_api_base" not in configured:
        raise ValueError(
            "Saved engine configuration has no OpenAI endpoint; "
            "requeue the review or recapture the evaluation"
        )
    try:
        validate_format(configured["comment_format"])
    except ValueError as exc:
        raise ValueError(
            "Saved comment format is incompatible; "
            "requeue the review or recapture the evaluation"
        ) from exc
    runtime.openai_api_base = configured["openai_api_base"]
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
        comment_format=configured["comment_format"],
    )
    data = {**runtime.debug, "schema_version": 2}
    exports = {
        "findings": result.findings,
        "actionable": [
            *[finding["id"] for finding in result.findings],
            *(["concept_objection"] if result.concept_concern else []),
        ],
    }
    if case.backport_evidence is not None:
        exports["backport_assessment"] = data["backport_assessment"]
    if case.review_feedback is not None:
        exports["review_memory"] = data.get("review_memory", [])
    verification_failed = (
        data.get("coverage", {}).get("verification", {}).get("status") == "failed"
    )
    return Output(
        result.content,
        data,
        exports,
        schema_version=2,
        status="failed" if verification_failed else "completed",
    )
