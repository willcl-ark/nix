"""The existing routed Ralph review engine."""

import json
from dataclasses import asdict
from pathlib import Path

from . import config, pipeline
from .engine import Output


def configure(value):
    value = dict(value)
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
            "models": config.validate_models(
                json.loads(Path(value.get("models_json", audit_dir / "models.json"))
                           .read_text(encoding="utf-8"))),
        }
    else:
        prompts = dict(prompts)
        if (not isinstance(prompts.get("instructions"), str)
                or not isinstance(prompts.get("audit_prompts"), dict)
                or not isinstance(prompts.get("models"), dict)):
            raise ValueError("prompts must contain instructions, audit_prompts, and models")
        config.validate_models(prompts["models"])
        if set(prompts["audit_prompts"]) != set(config.PROMPT_NAMES):
            raise ValueError("prompt config must name each review prompt")
        if "prompt_file" in value:
            prompts["instructions"] = config.load_prompt_file(Path(value["prompt_file"]))
        if "audit_prompt_dir" in value:
            audit_dir = Path(value["audit_prompt_dir"])
            prompts["audit_prompts"] = {
                name: config.load_prompt_file(audit_dir / f"{name}.md")
                for name in config.PROMPT_NAMES
            }
        if "models_json" in value:
            prompts["models"] = config.validate_models(
                json.loads(Path(value["models_json"]).read_text(encoding="utf-8")))
    prompt_config = config.PromptConfig(**prompts)
    if any(not prompt.strip() for prompt in prompt_config.audit_prompts.values()):
        raise ValueError("audit prompt files must not be empty")
    normalized = {"prompts": asdict(prompt_config),
                  "routing_mode": value.get("routing_mode", "enabled"),
                  "blind_alternatives": value.get("blind_alternatives", True)}
    if normalized["routing_mode"] not in {"enabled", "shadow", "full"}:
        raise ValueError("Unknown routing mode")
    if type(normalized["blind_alternatives"]) is not bool:
        raise ValueError("blind_alternatives must be a boolean")
    return normalized


def exports(debug):
    """Return current-engine findings and badge inputs in consumer form."""
    from .trace import published_concept_concern

    decisions = debug.get("decisions", [])
    attribution = {item.get("finding_id"): item
                   for item in debug.get("finding_attribution", [])}
    findings = []
    index = 0
    for decision in decisions:
        if decision.get("disposition") != "publish":
            continue
        finding = decision.get("finding")
        if not isinstance(finding, dict):
            continue
        index += 1
        finding_id = f"finding:{index}"
        item = attribution.get(finding_id, {})
        findings.append({
            "id": finding_id,
            **{key: finding.get(key) for key in (
                "kind", "severity", "path", "line", "side", "source_evidence")},
            "title": finding.get("title"),
            "body": finding.get("body"),
            "presented_title": item.get("title"),
            "presented_body": item.get("presented_body"),
            **{key: item.get(key) for key in (
                "raised_by", "raised_by_models", "verified_by",
                "verified_by_model", "edited_by", "edited_by_model")},
        })
    verified_objection = published_concept_concern(debug)
    return {"findings": findings,
            "actionable": [*[item["id"] for item in findings],
                           *(["concept_objection"] if verified_objection else [])]}


def run(case, runtime, configured):
    runtime.debug.setdefault("schema_version", 1)
    prompt_config = config.PromptConfig(**configured["prompts"])
    content = pipeline.review_with_independent_passes(
        case, runtime, prompt_config, configured["routing_mode"],
        configured["blind_alternatives"])
    data = {**runtime.debug, "schema_version": 1}
    return Output(content, data, exports(runtime.debug))
