"""Choose bounded review work from repository evidence and conservative rules."""

import json
import re

from .config import ADVERSARIAL_PROFILES, AUDIT_NAMES
from .model import TOOL_SETS, StaleReview
from .protocol import STRINGS, InvalidReview, object_schema
from .repository import MAX_REVIEW_BYTES, is_test_path

ROUTER_SCHEMA = object_schema(
    {
        "tier": {"type": "string", "enum": ["routine", "standard", "sensitive"]},
        "audits": {
            "type": "array",
            "items": {"type": "string", "enum": list(AUDIT_NAMES)},
        },
        "profiles": {
            "type": "array",
            "items": {"type": "string", "enum": list(ADVERSARIAL_PROFILES)},
        },
        "evidence": STRINGS,
        "missing_context": STRINGS,
    }
)
TIERS = ("routine", "standard", "sensitive")
DOC_EXTENSIONS = (".md", ".rst", ".txt")
BUILD = re.compile(
    r"^(?:depends/|cmake/|contrib/guix/|ci/|build-aux/|build_msvc/|"
    r"(?:CMakeLists\.txt|configure\.ac|guix\.sigs)(?:$|/)|"
    r".*/CMakeLists\.txt$|(?:Cargo|flake)\.lock$)"
)
CONCURRENCY = re.compile(
    r"^src/(?:sync|threadsafety|scheduler|checkqueue|validationinterface|"
    r"util/thread|util/threadinterrupt)(?:[./_]|$)"
)
PUBLIC_CONTRACT = re.compile(
    r"^src/(?:(?:rpc|wallet/rpc)/|qt/rpcconsole(?:[./_]|$)|"
    r"(?:bitcoin-cli|httprpc|httpserver|rest|init|common/args|util/system)"
    r"(?:[./_]|$))"
)
STATE = re.compile(
    r"^src/(?:(?:db|index|wallet/(?:bdb|db|sqlite))/|"
    r"(?:dbwrapper|txdb|blockstorage|chainstate)(?:[./_]|$)|"
    r"node/(?:chainstate|blockstorage)(?:[./_]|$))"
)


def _doc_path(path):
    return path.endswith(DOC_EXTENSIONS) and path.rsplit("/", 1)[-1] not in {
        "CMakeLists.txt",
        "requirements.txt",
    }


def _production_path(path):
    return not _doc_path(path) and not is_test_path(path) and not BUILD.search(path)


def domain_audits(paths):
    audits = set()
    paths = [path for path in paths if not _doc_path(path)]
    if paths:
        audits.add("dead_code")
    if any(is_test_path(path) or _production_path(path) for path in paths):
        audits.add("tests")
    if any(_production_path(path) for path in paths):
        audits.add("design")
    for path in paths:
        if BUILD.search(path):
            audits.add("build")
        if is_test_path(path):
            continue
        if CONCURRENCY.search(path):
            audits.add("concurrency")
        if STATE.search(path):
            audits.add("state")
        if PUBLIC_CONTRACT.search(path):
            audits.add("public_contract")
    return audits


def full_plan(reason):
    # Expensive script review needs a semantic trigger, not generic uncertainty.
    return {
        "tier": "sensitive",
        "audits": [name for name in AUDIT_NAMES if name != "script"],
        "profiles": list(ADVERSARIAL_PROFILES),
        "evidence": [reason],
        "missing_context": [],
    }


def validate_plan(text, paths):
    result = json.loads(text)
    if not isinstance(result, dict) or set(result) != set(ROUTER_SCHEMA["properties"]):
        raise InvalidReview("Invalid routing fields")
    if result["tier"] not in TIERS:
        raise InvalidReview("Invalid routing tier")
    for key in ("audits", "profiles", "evidence", "missing_context"):
        if not isinstance(result[key], list) or any(
            not isinstance(item, str) for item in result[key]
        ):
            raise InvalidReview("Invalid routing list")
    if set(result["audits"]) - set(AUDIT_NAMES):
        raise InvalidReview("Unknown audit role")
    if set(result["profiles"]) - set(ADVERSARIAL_PROFILES):
        raise InvalidReview("Unknown adversarial profile")
    if result["missing_context"]:
        return full_plan("Router reported missing context")
    selected_profiles = set(result["profiles"])
    if selected_profiles:
        result["tier"] = "sensitive"
    selected = set(result["audits"]) | domain_audits(paths)
    if all(_doc_path(path) or is_test_path(path) for path in paths):
        selected.discard("script")
    if "script" in selected:
        result["tier"] = "sensitive"
    result["audits"] = [name for name in AUDIT_NAMES if name in selected]
    result["profiles"] = [
        name for name in ADVERSARIAL_PROFILES if name in selected_profiles
    ]
    return result


def plan_review(runtime, review, snapshot, prompt_config, debug, mode="enabled"):
    if mode not in {"enabled", "shadow", "full"}:
        raise ValueError("Unknown routing mode")
    paths = sorted(snapshot.changed_paths)
    settings = prompt_config.profile.stages["router"]
    record = {
        "model": prompt_config.models["router"],
        "status": "skipped",
        "turns": [],
        "tools": [],
    }
    debug.setdefault("stages", {})["router"] = record
    if mode == "full":
        proposed = full_plan("Full review requested")
        proposed["audits"] = list(AUDIT_NAMES)
    elif f"Patch exceeds {MAX_REVIEW_BYTES} input bytes." in review:
        proposed = full_plan("Initial patch is incomplete")
    else:
        # The complete manifest precedes the patch; a model cannot silently
        # classify only the excerpt it happened to receive.
        router_input = json.dumps({"changed_paths": paths}) + "\n" + review
        try:
            answer = runtime.review(
                "router",
                router_input,
                prompt=prompt_config.audit_prompts["router"],
                model_name=prompt_config.models["router"],
                tools=TOOL_SETS[settings.tool_set],
                # There is no selected tier until routing completes.
                max_tool_calls=settings.max_tool_calls["standard"],
                max_context_calls=settings.max_context_calls,
                max_history_calls=settings.max_history_calls,
                max_web_search_calls_per_response=settings.max_web_search_calls_per_response,
                max_output_tokens=settings.max_output_tokens,
                reasoning_effort=settings.reasoning_effort,
                response_schema=ROUTER_SCHEMA,
                record=record,
                timeout=settings.timeout,
                final_timeout=settings.final_timeout,
            )
            record["status"] = "completed"
            record["raw_output"] = answer
            debug.setdefault("stage_outputs", {})["router"] = answer
            proposed = validate_plan(answer, paths)
        except StaleReview:
            raise
        except Exception as exc:
            record.update(status="failed", error_type=type(exc).__name__)
            proposed = full_plan("Routing unavailable; conservative review required")
    actual = (
        full_plan("Shadow routing retains full review")
        if mode == "shadow"
        else proposed
    )
    if mode == "shadow":
        actual["audits"] = list(AUDIT_NAMES)
    debug["routing"] = {
        "mode": mode,
        "proposed": proposed,
        "selected": actual,
    }
    return actual
