"""Capture Forgejo PR cases and replay them from frozen local inputs."""

import argparse
import math
import hashlib
import json
import os
import re
import subprocess
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from ralph import trace as core_trace
from . import config, forgejo, repository, research, stats, stats_page
from ralph import repository as local_repository
from ralph import engine, profiles, spend, runner, upstream
from ralph import research as frozen_research


SCHEMA_VERSION = 6


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise ValueError(f"{path} is not a directory")
    path.chmod(0o700)
    return path


def write_private_json(path, data):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, sort_keys=True, ensure_ascii=True)
        file.write("\n")


def read_secret(path, name):
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"{name} file must not be empty")
    return value


def read_engine_config(path):
    if path is None:
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Engine configuration must be a JSON object")
    return value


def digest(value):
    data = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(data).hexdigest()


def timestamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def source_code_sha256():
    sources = sorted(Path(engine.__file__).resolve().parent.glob("*.py"))
    checksum = hashlib.sha256()
    for path in sources:
        checksum.update(path.name.encode())
        checksum.update(b"\x00")
        checksum.update(path.read_bytes())
        checksum.update(b"\x00")
    return checksum.hexdigest()


@contextmanager
def offline_git():
    previous = os.environ.get("GIT_NO_LAZY_FETCH")
    os.environ["GIT_NO_LAZY_FETCH"] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("GIT_NO_LAZY_FETCH", None)
        else:
            os.environ["GIT_NO_LAZY_FETCH"] = previous


def verify_local_objects(checkout, base_sha, head_sha, *originals):
    """Reject partial clones that would fetch Git objects during replay."""
    if not checkout.is_absolute():
        raise ValueError("Frozen checkout path must be absolute")
    if any(
        not local_repository.SHA.fullmatch(sha)
        for sha in (base_sha, head_sha, *originals)
    ):
        raise ValueError("Frozen Git object IDs are invalid")
    result = subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "rev-list",
            "--objects",
            "--missing=print",
            base_sha,
            head_sha,
            *originals,
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "GIT_NO_LAZY_FETCH": "1"},
    )
    if result.returncode or any(
        line.startswith("?") for line in result.stdout.splitlines()
    ):
        raise ValueError("Frozen Git objects are unavailable in the local checkout")


def verify_case_pins(checkout, manifest):
    case_id = manifest["case_id"]
    for role in ("base", "head"):
        pin = manifest[f"{role}_pin"]
        if pin != f"refs/review-cases/{case_id}/{role}":
            raise ValueError("Frozen case ref is invalid")
        result = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "--verify", pin],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "GIT_NO_LAZY_FETCH": "1"},
        )
        if result.returncode or result.stdout.strip() != manifest[f"{role}_sha"]:
            raise ValueError("Frozen case ref no longer pins its Git object")


def pull_request_details(bot_config, token, number):
    pull = forgejo.forgejo_request(bot_config, token, f"/pulls/{number}")
    base_ref = (pull.get("base") or {}).get("ref") or ""
    api_head = (pull.get("head") or {}).get("sha") or ""
    title = pull.get("title") or ""
    description = pull.get("body") or ""
    if (
        not isinstance(base_ref, str)
        or not local_repository.BRANCH.fullmatch(base_ref)
        or base_ref.startswith("-")
        or ".." in base_ref
        or "//" in base_ref
        or base_ref.endswith("/")
        or base_ref.endswith(".lock")
    ):
        raise ValueError(f"PR {number} has an invalid base branch")
    if api_head and (
        not isinstance(api_head, str) or not local_repository.SHA.fullmatch(api_head)
    ):
        raise ValueError(f"PR {number} has an invalid API head SHA")
    if not isinstance(title, str) or not isinstance(description, str):
        raise ValueError(f"PR {number} has invalid title or description")
    details = {
        "base_ref": base_ref,
        "api_head": api_head,
        "title": title,
        "description": description,
    }
    for key in ("created_at", "updated_at", "closed_at", "merged_at"):
        if key in pull:
            details[key] = pull[key]
    labels = pull.get("labels", [])
    if not isinstance(labels, list) or any(
        not isinstance(label, dict) or not isinstance(label.get("name"), str)
        for label in labels
    ):
        raise ValueError(f"PR {number} has invalid labels")
    details["labels"] = sorted({label["name"] for label in labels})
    return details


def capture_case(
    forgejo_token,
    checkout,
    output_dir,
    number,
    bot_config,
    review_engine,
    research_requests=None,
    research_cutoff=None,
):
    """Make one private manifest with complete local Git objects."""
    output_dir = private_dir(output_dir)
    checkout = checkout.resolve()
    captured_at = research.utc_now()
    discussion_cutoff = research_cutoff or captured_at
    details = pull_request_details(bot_config, forgejo_token, number)
    if research_cutoff is not None and not forgejo._visible_before_cutoff(
        details, discussion_cutoff
    ):
        raise ValueError(
            f"PR {number} text cannot be frozen at cutoff {discussion_cutoff}: "
            "current metadata is after the cutoff or incomplete"
        )
    expected_head = repository.current_head(bot_config, number)
    if not local_repository.SHA.fullmatch(expected_head):
        raise ValueError(f"PR {number} has no mirrored pull head")
    base_sha, head_sha, review, skip = repository.collect_review(
        checkout,
        number,
        details["base_ref"],
        expected_head,
        details["title"],
        details["description"],
        bot_config,
    )
    if skip:
        raise ValueError(f"PR {number} cannot be frozen: {skip}")
    case_id = uuid.uuid4().hex
    base_pin = f"refs/review-cases/{case_id}/base"
    head_pin = f"refs/review-cases/{case_id}/head"
    source_prefix = f"refs/review-cases/{case_id}"
    try:
        local_repository.git(checkout, "update-ref", base_pin, base_sha)
        local_repository.git(checkout, "update-ref", head_pin, head_sha)
        # collect_review uses a partial fetch. Request the captured object IDs,
        # so a force push cannot quietly replace the case with a newer head.
        try:
            local_repository.git(
                checkout,
                "fetch",
                "--refetch",
                "--no-tags",
                "--no-filter",
                "origin",
                base_sha,
                head_sha,
            )
        except subprocess.CalledProcessError as exc:
            raise ValueError(
                "Captured Git objects could not be fetched after PR refs moved"
            ) from exc
        with offline_git():
            verify_local_objects(checkout, base_sha, head_sha)
            snapshot = local_repository.snapshot_repository(
                checkout, base_sha, head_sha
            )
        backport_evidence = repository.collect_backport_evidence(
            snapshot,
            {
                "title": details["title"],
                "body": details["description"],
                "labels": details["labels"],
                "base_ref": details["base_ref"],
                "repository": bot_config.github_repository or bot_config.repository,
            },
            bot_config,
            source_prefix,
        )
        backport_pins = {}
        for original in sorted(upstream.original_commits(backport_evidence)):
            if local_repository.has_commit(checkout, original):
                pin = f"{source_prefix}/originals/{original}"
                local_repository.git(checkout, "update-ref", pin, original)
                backport_pins[original] = pin
        research_evidence = research.capture(
            bot_config, number, research_requests, discussion_cutoff, captured_at
        )
        frozen_config = {"bot": asdict(bot_config), "engine": review_engine.identity}
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "case_id": case_id,
            "captured_at": captured_at,
            "discussion_cutoff": research_evidence["cutoff"],
            "pr": number,
            "checkout": str(checkout),
            "base_ref": details["base_ref"],
            "api_head": details["api_head"],
            "expected_head": expected_head,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "base_pin": base_pin,
            "head_pin": head_pin,
            "merge_base": snapshot.merge_base,
            "title": details["title"],
            "description": details["description"],
            "review_input": review,
            "review_input_sha256": hashlib.sha256(review.encode()).hexdigest(),
            "config": frozen_config,
            "config_sha256": digest(frozen_config),
            "backport_evidence": backport_evidence,
            "backport_evidence_sha256": digest(backport_evidence),
            "backport_pins": backport_pins,
            "research_evidence": research_evidence,
            "research_evidence_sha256": digest(research_evidence),
            "source_code_sha256": source_code_sha256(),
        }
        manifest["manifest_sha256"] = digest(manifest)
        path = (
            output_dir / f"case-{number}-{head_sha[:12]}-{timestamp()}-{case_id}.json"
        )
        write_private_json(path, manifest)
        return path
    except Exception:
        repository.release_source_refs(checkout, source_prefix)
        for pin in (base_pin, head_pin):
            subprocess.run(
                ["git", "-C", str(checkout), "update-ref", "-d", pin],
                capture_output=True,
                timeout=30,
            )
        raise


def run_case(
    manifest_path,
    api_key,
    output_dir,
    ledger,
    routing_mode=None,
    prompt_file=None,
    audit_dir=None,
    models_json=None,
    labels=None,
    ppq_api_key=None,
    ppq_ledger=None,
    blind_alternatives=None,
    engine_name=None,
    engine_config=None,
):
    """Replay a captured case without Forgejo or remote Git reads."""
    output_dir = private_dir(output_dir)
    artifact = {
        "manifest": str(manifest_path.resolve()),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    engine_identity = {
        "name": None,
        "code_sha256": None,
        "config_sha256": None,
        "config": None,
    }
    case_id = "unknown"
    budget = None
    ppq_budget = None
    attempt = {}
    runtime = None
    review_id = None
    base_sha = head_sha = merge_base = None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("Unsupported frozen case schema")
        if digest(
            {key: value for key, value in manifest.items() if key != "manifest_sha256"}
        ) != manifest.get("manifest_sha256"):
            raise ValueError("Frozen case hash mismatch")
        if not re.fullmatch(r"[0-9a-f]{32}", manifest["case_id"]):
            raise ValueError("Frozen case ID is invalid")
        case_id = manifest["case_id"]
        frozen_config = manifest["config"]
        if digest(frozen_config) != manifest["config_sha256"]:
            raise ValueError("Frozen configuration hash mismatch")
        research_evidence = frozen_research.validate(manifest.get("research_evidence"))
        if digest(research_evidence) != manifest.get("research_evidence_sha256"):
            raise ValueError("Frozen research evidence hash mismatch")
        backport_evidence = manifest["backport_evidence"]
        if digest(backport_evidence) != manifest["backport_evidence_sha256"]:
            raise ValueError("Frozen backport evidence hash mismatch")
        bot_config = config.BotConfig(**frozen_config["bot"])
        captured_engine = frozen_config["engine"]
        selected_name = engine_name or captured_engine["name"]
        explicit_engine_config = engine_config is not None
        selected_config = (
            engine_config
            if explicit_engine_config
            else captured_engine["config"]
            if selected_name == captured_engine["name"]
            else {}
        )
        selected_config = dict(selected_config or {})
        current_only = (
            prompt_file is not None
            or audit_dir is not None
            or models_json is not None
            or routing_mode is not None
            or blind_alternatives is not None
        )
        if selected_name != "current" and current_only:
            raise ValueError(
                "prompt, model, routing, and alternatives options apply only to current"
            )
        if selected_name == "current":
            selected_config.update(
                {
                    key: value
                    for key, value in {
                        "prompt_file": prompt_file,
                        "audit_prompt_dir": audit_dir,
                        "models_json": models_json,
                    }.items()
                    if value is not None
                }
            )
            if routing_mode is not None:
                selected_config["routing_mode"] = routing_mode
            selected_config.setdefault("routing_mode", "enabled")
            if blind_alternatives is not None:
                selected_config["blind_alternatives"] = blind_alternatives
            selected_config.setdefault("blind_alternatives", True)
            selected_config.setdefault("archaeology", True)
        use_captured_config = (
            not explicit_engine_config
            and selected_name == captured_engine["name"]
            and not current_only
        )
        loaded_engine = engine.load(
            selected_name, selected_config, configured=use_captured_config
        )
        engine_identity = loaded_engine.identity
        if selected_name == "current":
            ledger = profiles.ReviewProfile.from_dict(
                loaded_engine.config["profile"]
            ).apply_budget(ledger)
        code_hash = source_code_sha256()
        review_limit = getattr(ledger, "review_limit_micros", None)
        monthly_limit = getattr(ledger, "monthly_limit_micros", None)
        ppq_limit = getattr(ppq_ledger, "review_limit_micros", None)
        effective_config = {
            "bot": asdict(bot_config),
            "engine": engine_identity,
            "ppq_review_budget_usd": None
            if ppq_limit is None
            else ppq_limit / 1_000_000,
            "review_budget_usd": None
            if review_limit is None
            else review_limit / 1_000_000,
            "monthly_budget_usd": None
            if monthly_limit is None
            else monthly_limit / 1_000_000,
            "research_evidence_sha256": manifest["research_evidence_sha256"],
            "backport_evidence_sha256": manifest["backport_evidence_sha256"],
            "source_code_sha256": code_hash,
        }
        artifact["effective_config"] = effective_config
        artifact["effective_config_sha256"] = digest(effective_config)
        artifact["source_code_sha256"] = code_hash
        artifact["capture_code_sha256"] = manifest["source_code_sha256"]
        artifact["code_changed_since_capture"] = (
            code_hash != manifest["source_code_sha256"]
        )
        artifact["engine_code_changed_since_capture"] = (
            engine_identity["code_sha256"] != captured_engine["code_sha256"]
        )
        artifact["research_evidence_inventory"] = frozen_research.inventory(
            research_evidence
        )
        checkout = Path(manifest["checkout"])
        base_sha, head_sha = manifest["base_sha"], manifest["head_sha"]
        merge_base = manifest["merge_base"]
        review = manifest["review_input"]
        if (
            hashlib.sha256(review.encode()).hexdigest()
            != manifest["review_input_sha256"]
        ):
            raise ValueError("Frozen review input hash mismatch")
        with offline_git():
            verify_case_pins(checkout, manifest)
            originals = upstream.original_commits(backport_evidence)
            for original in originals - manifest["backport_pins"].keys():
                if local_repository.has_commit(checkout, original):
                    raise ValueError(
                        "Previously unavailable frozen backport object is now present"
                    )
            for original, pin in manifest["backport_pins"].items():
                if (
                    original not in originals
                    or pin != f"refs/review-cases/{case_id}/originals/{original}"
                    or local_repository.resolve_commit(checkout, pin) != original
                ):
                    raise ValueError("Frozen backport ref no longer pins its original")
            verify_local_objects(
                checkout, base_sha, head_sha, *manifest["backport_pins"]
            )
            snapshot = local_repository.snapshot_repository(
                checkout, base_sha, head_sha
            )
            if snapshot.merge_base != manifest["merge_base"]:
                raise ValueError("Frozen merge base mismatch")
            review_id = f"eval:{case_id}:{uuid.uuid4().hex}"
            budget = spend.RequestBudget(ledger, review_id)
            ppq_budget = (
                spend.RequestBudget(ppq_ledger, review_id)
                if ppq_ledger is not None
                else None
            )
            artifact["review_id"] = review_id
            case = engine.ReviewCase(
                review,
                snapshot,
                source={
                    "kind": "pull_request",
                    "number": manifest["pr"],
                    "url": f"{bot_config.repository_url}/pulls/{manifest['pr']}",
                },
                research_evidence=research_evidence,
                backport_evidence=backport_evidence,
            )
            runtime = engine.Runtime(
                case=case,
                api_key=api_key,
                ppq_api_key=ppq_api_key,
                budget=budget,
                ppq_budget=ppq_budget,
                allow_web_search=False,
                attempt=attempt,
            )
            result = runner.run_review(case, runtime, loaded_engine)
            if result.pop("diff_base_sha") != snapshot.merge_base:
                raise ValueError("frozen PR review must use the merge base")
            attempt = runtime.attempt
            artifact.update(result)
            artifact.update(
                {
                    "case_id": case_id,
                    "pr": manifest["pr"],
                    "review_input_sha256": manifest["review_input_sha256"],
                }
            )
    except Exception as exc:
        artifact["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        artifact["finished_at"] = datetime.now(timezone.utc).isoformat()
        if "run" not in artifact:
            artifact.update(
                {
                    "schema_version": 1,
                    "engine": engine_identity,
                    "base_sha": base_sha,
                    "head_sha": head_sha,
                    "merge_base": merge_base,
                    "content": None,
                    "output": None,
                    "run": {
                        "review_id": review_id,
                        "status": "failed",
                        "metrics": runtime.metrics() if runtime else {},
                        "budget": None,
                        "ppq_budget": None,
                        "attempt": attempt,
                    },
                }
            )
        if budget is not None:
            try:
                artifact["run"]["budget"] = budget.summary()
            except Exception as exc:
                artifact["run"]["budget"] = attempt.get("budget")
                artifact["budget_error"] = type(exc).__name__
        if ppq_budget is not None:
            try:
                artifact["run"]["ppq_budget"] = ppq_budget.summary()
            except Exception as exc:
                artifact["run"]["ppq_budget"] = attempt.get("ppq_budget")
                artifact["ppq_budget_error"] = type(exc).__name__
        if isinstance(labels, dict) and case_id in labels:
            artifact["expected_findings"] = labels[case_id]
        config_hash = artifact.get("effective_config_sha256", "unknown")
        path = (
            output_dir
            / f"run-{case_id}-{timestamp()}-{uuid.uuid4().hex}-{config_hash[:12]}.json"
        )
        write_private_json(path, artifact)
    return path


def _run_paths(paths):
    for path in paths:
        if path.is_dir():
            yield from sorted(path.glob("run-*.json"))
        else:
            yield path


def _iso_seconds(start, finish):
    if not isinstance(start, str) or not isinstance(finish, str):
        return None
    try:
        started = datetime.fromisoformat(start.replace("Z", "+00:00"))
        finished = datetime.fromisoformat(finish.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, round((finished - started).total_seconds(), 2))


def _p95(values):
    values = sorted(values)
    if not values:
        return None
    return values[math.ceil(len(values) * 0.95) - 1]


def _mean(values):
    return None if not values else sum(values) / len(values)


def _series(values):
    return {
        "total": _rounded(sum(values)),
        "mean": _rounded(_mean(values)),
        "p95": _rounded(_p95(values)),
    }


def _rounded(value):
    return None if value is None else round(value, 6)


def _research_requests(path):
    if path is None:
        return []
    requests = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(requests, list):
        raise ValueError("Research requests JSON must contain a list")
    return requests


def _run_has_budget_exhaustion(artifact):
    error = artifact.get("error") or {}
    if error.get("type") == "BudgetExceeded":
        return True
    debug = core_trace.current_diagnostics(artifact)
    stages = debug.get("stages") or {}
    return any(
        stage.get("status") == "budget_exhausted"
        for stage in stages.values()
        if isinstance(stage, dict)
    )


def _run_has_partial_coverage(artifact):
    debug = core_trace.current_diagnostics(artifact)
    coverage = debug.get("coverage") or {}
    return coverage.get("status") in {"partial", "failed"}


def _summary_group_key(artifact):
    key = artifact.get("effective_config_sha256")
    if isinstance(key, str) and key:
        return key
    config = artifact.get("effective_config")
    if not isinstance(config, dict):
        config = {"engine": core_trace.result_engine(artifact)}
    return digest(config)


def _new_summary_group():
    return {
        "runs": 0,
        "completed": 0,
        "failed": 0,
        "partial_coverage": 0,
        "budget_exhausted": 0,
        "labels_present": 0,
        "decision_counts": Counter(),
        "accepted_findings": 0,
        "findings_export_status_counts": Counter(),
        "current_diagnostics_status_counts": Counter(),
        "archaeology_research_completed": 0,
        "published_conceptual_concerns": 0,
        "conceptual_concern_status_counts": Counter(),
        "would_stop": {
            "candidate": {
                "counts": Counter(),
                "would_stop_runs": 0,
                "downstream_accepted_findings": 0,
                "downstream_known_cost_usd": 0.0,
                "downstream_stage_cost_usd": Counter(),
            },
            "verified": {
                "counts": Counter(),
                "would_stop_runs": 0,
                "downstream_accepted_findings": 0,
                "downstream_known_cost_usd": 0.0,
                "downstream_stage_cost_usd": Counter(),
            },
        },
        "known_costs": [],
        "wall_seconds": [],
        "unknown_usage_runs": 0,
        "unknown_usage_turns": 0,
        "incomplete_usage_runs": 0,
        "incomplete_usage_turns": 0,
        "max_output_token_incomplete_turns": 0,
        "stage_metrics": defaultdict(
            lambda: {
                "runs": 0,
                "status_counts": Counter(),
                "turns": 0,
                "concept_stage": False,
                "conceptual_concerns": 0,
                "tool_calls": 0,
                "known_cost_usd": 0.0,
                "unknown_usage_turns": 0,
                "incomplete_usage_turns": 0,
                "max_output_token_incomplete_turns": 0,
                "published_candidates": 0,
                "dropped_candidates": 0,
                "unresolved_candidates": 0,
                "undisposed_candidates": 0,
                "sole_accepted_findings": 0,
                "shared_accepted_findings": 0,
            }
        ),
    }


def _concept_label(record):
    if not isinstance(record, dict):
        return None
    for key in ("proposed_review", "assessment", "would_stop"):
        value = record.get(key)
        if isinstance(value, bool):
            return "would_stop" if value else "continue"
        if isinstance(value, str) and value:
            return value
    return None


def _concept_would_stop(label):
    if not isinstance(label, str):
        return False
    normalized = label.lower().replace("-", "_").replace(" ", "_")
    return normalized in {
        "stop",
        "would_stop",
        "do_not_review",
        "skip_review",
        "withhold",
        "block",
    }


def _downstream_summary(debug):
    metrics = core_trace.stage_metrics(debug)
    accepted = sum(
        1
        for decision in debug.get("decisions", [])
        if isinstance(decision, dict) and decision.get("disposition") == "publish"
    )
    known_cost = 0.0
    stage_costs = Counter()
    for name, record in metrics.items():
        if name == "archaeologist":
            continue
        cost = record.get("known_estimated_cost_usd", 0.0)
        known_cost += cost
        if cost:
            stage_costs[name] += cost
    return accepted, known_cost, stage_costs


def _add_would_stop_metrics(group, debug):
    concept = core_trace.concept_assessment(debug)
    records = {
        "candidate": concept.get("candidate") if isinstance(concept, dict) else None
    }
    verification = concept.get("verification") if isinstance(concept, dict) else None
    if isinstance(verification, dict):
        records["verified"] = verification.get("assessment")
        if isinstance(records["verified"], dict):
            records["verified"] = {
                **records["verified"],
                **{
                    key: verification[key]
                    for key in ("proposed_review", "review_reason")
                    if key in verification
                },
            }
        else:
            records["verified"] = verification
    accepted, known_cost, stage_costs = _downstream_summary(debug)
    for stage, record in records.items():
        label = _concept_label(record)
        if label is None:
            continue
        bucket = group["would_stop"][stage]
        bucket["counts"][label] += 1
        if _concept_would_stop(label):
            bucket["would_stop_runs"] += 1
            bucket["downstream_accepted_findings"] += accepted
            bucket["downstream_known_cost_usd"] += known_cost
            bucket["downstream_stage_cost_usd"].update(stage_costs)


def _add_stage_metrics(group, debug):
    stages = debug.get("stages") or {}
    traced = core_trace.stage_metrics(debug)
    for name, record in traced.items():
        metrics = group["stage_metrics"][name]
        metrics["concept_stage"] = metrics["concept_stage"] or bool(
            record.get("concept_stage")
        )
        metrics["conceptual_concerns"] += record.get("conceptual_concerns", 0)
        metrics["turns"] += record["calls"]
        metrics["tool_calls"] += record["tool_calls"]
        metrics["known_cost_usd"] += record["known_estimated_cost_usd"]
        metrics["unknown_usage_turns"] += record["unknown_usage_count"]
        metrics["incomplete_usage_turns"] += record["incomplete_usage_count"]
        candidate_counts = record.get("candidate_counts") or {}
        metrics["published_candidates"] += candidate_counts.get("publish", 0)
        metrics["dropped_candidates"] += candidate_counts.get("drop", 0)
        metrics["unresolved_candidates"] += candidate_counts.get("unresolved", 0)
        metrics["undisposed_candidates"] += candidate_counts.get("undisposed", 0)
        metrics["sole_accepted_findings"] += record["sole_source_findings"]
        metrics["shared_accepted_findings"] += record["shared_findings"]
    for name, stage in stages.items():
        if not isinstance(stage, dict):
            continue
        metrics = group["stage_metrics"][name]
        metrics["runs"] += 1
        metrics["status_counts"][stage.get("status", "unknown")] += 1
        turn_max_output = 0
        for turn in stage.get("turns", []):
            if turn.get("incomplete_reason") == "max_output_tokens":
                turn_max_output += 1
        if (
            not turn_max_output
            and stage.get("incomplete_reason") == "max_output_tokens"
        ):
            turn_max_output = 1
        metrics["max_output_token_incomplete_turns"] += turn_max_output
        group["max_output_token_incomplete_turns"] += turn_max_output


def _finalize_group(group):
    runs = group["runs"]
    completed = group["completed"]
    accepted_findings = (
        None
        if group["findings_export_status_counts"]["unsupported"]
        else group["accepted_findings"]
    )
    stage_metrics = {}
    for name, metrics in sorted(group["stage_metrics"].items()):
        stage_runs = metrics["runs"]
        stage_metrics[name] = {
            "runs": stage_runs,
            "status_counts": dict(sorted(metrics["status_counts"].items())),
            "concept_stage": metrics["concept_stage"],
            "conceptual_concerns": metrics["conceptual_concerns"],
            "turns": metrics["turns"],
            "tool_calls": metrics["tool_calls"],
            "mean_turns_per_run": _rounded(
                metrics["turns"] / stage_runs if stage_runs else None
            ),
            "known_cost_usd": _rounded(metrics["known_cost_usd"]),
            "unknown_usage_turns": metrics["unknown_usage_turns"],
            "incomplete_usage_turns": metrics["incomplete_usage_turns"],
            "max_output_token_incomplete_turns": metrics[
                "max_output_token_incomplete_turns"
            ],
            "published_candidates": metrics["published_candidates"],
            "dropped_candidates": metrics["dropped_candidates"],
            "unresolved_candidates": metrics["unresolved_candidates"],
            "undisposed_candidates": metrics["undisposed_candidates"],
            "accepted_findings": (
                metrics["sole_accepted_findings"] + metrics["shared_accepted_findings"]
            ),
            "sole_accepted_findings": metrics["sole_accepted_findings"],
            "shared_accepted_findings": metrics["shared_accepted_findings"],
        }
    would_stop = {}
    for name, record in group["would_stop"].items():
        would_stop[name] = {
            "counts": dict(sorted(record["counts"].items())),
            "would_stop_runs": record["would_stop_runs"],
            "downstream_accepted_findings": record["downstream_accepted_findings"],
            "downstream_known_cost_usd": _rounded(record["downstream_known_cost_usd"]),
            "downstream_stage_cost_usd": {
                stage: _rounded(cost)
                for stage, cost in sorted(record["downstream_stage_cost_usd"].items())
            },
        }
    return {
        "runs": runs,
        "completed": completed,
        "failed": group["failed"],
        "partial_coverage": (
            group["partial_coverage"]
            if group["current_diagnostics_status_counts"]["supported"]
            else None
        ),
        "budget_exhausted": group["budget_exhausted"],
        "labels_present": group["labels_present"],
        "unknown_usage_runs": group["unknown_usage_runs"],
        "unknown_usage_turns": group["unknown_usage_turns"],
        "incomplete_usage_runs": group["incomplete_usage_runs"],
        "incomplete_usage_turns": group["incomplete_usage_turns"],
        "max_output_token_incomplete_turns": (
            group["max_output_token_incomplete_turns"]
            if group["current_diagnostics_status_counts"]["supported"]
            else None
        ),
        "decision_counts": {
            "published": group["decision_counts"]["publish"],
            "dropped": group["decision_counts"]["drop"],
            "unresolved": group["decision_counts"]["unresolved"],
        }
        if group["current_diagnostics_status_counts"]["supported"]
        else None,
        "accepted_findings": accepted_findings,
        "findings_export_status_counts": dict(
            sorted(group["findings_export_status_counts"].items())
        ),
        "current_diagnostics_status_counts": dict(
            sorted(group["current_diagnostics_status_counts"].items())
        ),
        "archaeology_research_completed": (
            group["archaeology_research_completed"]
            if group["current_diagnostics_status_counts"]["supported"]
            else None
        ),
        "published_conceptual_concerns": (
            group["published_conceptual_concerns"]
            if group["current_diagnostics_status_counts"]["supported"]
            else None
        ),
        "conceptual_concern_status_counts": (
            dict(sorted(group["conceptual_concern_status_counts"].items()))
            if group["current_diagnostics_status_counts"]["supported"]
            else None
        ),
        "would_stop": (
            would_stop
            if group["current_diagnostics_status_counts"]["supported"]
            else None
        ),
        "cost_usd": {
            **_series(group["known_costs"]),
            "known_per_accepted_finding": _rounded(
                sum(group["known_costs"]) / accepted_findings
                if accepted_findings
                else None
            ),
        },
        "wall_seconds": _series(group["wall_seconds"]),
        "stage_metrics": stage_metrics,
    }


def summarize_runs(paths):
    groups = defaultdict(_new_summary_group)
    artifacts = 0
    for path in _run_paths(paths):
        artifact = json.loads(path.read_text(encoding="utf-8"))
        artifacts += 1
        key = _summary_group_key(artifact)
        group = groups[key]
        group["runs"] += 1
        run = artifact.get("run") or {}
        if run.get("status") == "completed":
            group["completed"] += 1
        else:
            group["failed"] += 1
        if _run_has_partial_coverage(artifact):
            group["partial_coverage"] += 1
        if _run_has_budget_exhaustion(artifact):
            group["budget_exhausted"] += 1
        if "expected_findings" in artifact:
            group["labels_present"] += 1
        debug = core_trace.current_diagnostics(artifact)
        group["current_diagnostics_status_counts"][
            "supported" if debug else "unsupported"
        ] += 1
        metrics = core_trace.result_metrics(artifact) or {}
        run_cost = metrics.get("estimated_cost_usd", 0.0)
        group["known_costs"].append(run_cost)
        if metrics.get("unknown_usage_count", 0):
            group["unknown_usage_runs"] += 1
        group["unknown_usage_turns"] += metrics.get("unknown_usage_count", 0)
        if metrics.get("incomplete_usage_count", 0):
            group["incomplete_usage_runs"] += 1
        group["incomplete_usage_turns"] += metrics.get("incomplete_usage_count", 0)
        wall = _iso_seconds(artifact.get("started_at"), artifact.get("finished_at"))
        if wall is not None:
            group["wall_seconds"].append(wall)
        findings = core_trace.findings_export(artifact)
        if findings is None:
            group["findings_export_status_counts"]["unsupported"] += 1
        else:
            group["findings_export_status_counts"]["supported"] += 1
            group["accepted_findings"] += len(findings)
        decisions = [
            decision
            for decision in debug.get("decisions", [])
            if isinstance(decision, dict)
        ]
        group["decision_counts"].update(
            decision.get("disposition", "unknown") for decision in decisions
        )
        assessment = core_trace.concept_assessment(debug)
        if core_trace.published_concept_concern(debug):
            group["published_conceptual_concerns"] += 1
        if assessment:
            group["conceptual_concern_status_counts"][
                assessment.get("status", "unknown")
            ] += 1
        if any(
            isinstance(stage, dict)
            and name == "archaeologist"
            and stage.get("status") == "completed"
            for name, stage in (debug.get("stages") or {}).items()
        ):
            group["archaeology_research_completed"] += 1
        if debug:
            _add_stage_metrics(group, debug)
            _add_would_stop_metrics(group, debug)
    return {
        "schema_version": 1,
        "run_artifacts": artifacts,
        "groups": {
            key: _finalize_group(group) for key, group in sorted(groups.items())
        },
        "pricing_note": (
            "Known costs are estimated from saved token usage and "
            "configured rates. Unknown usage is excluded from "
            "known cost totals and counted separately."
        ),
        "labels_note": (
            "Expected labels are counted only as present. This "
            "summary does not match labels to findings or establish "
            "recall."
        ),
    }


def add_config_args(parser):
    parser.add_argument("--origin", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--forgejo-api", required=True)
    parser.add_argument("--repository-url")
    parser.add_argument("--comment-marker")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture", help="freeze PR inputs and Git objects")
    capture.add_argument("--state-dir", type=Path, required=True)
    capture.add_argument("--output-dir", type=Path, required=True)
    add_config_args(capture)
    capture.add_argument("--forgejo-token-file", type=Path, required=True)
    capture.add_argument("--engine", default="current")
    capture.add_argument("--engine-config", type=Path)
    capture.add_argument("--prompt-file", type=Path)
    capture.add_argument("--audit-prompt-dir", type=Path)
    capture.add_argument("--models-json", type=Path)
    capture.add_argument(
        "--research-cutoff",
        help="ISO timestamp cutoff for captured discussion evidence",
    )
    capture.add_argument(
        "--research-requests-json",
        type=Path,
        help="Optional list of extra frozen discussion tool calls",
    )
    capture.add_argument("prs", type=int, nargs="+")

    run = commands.add_parser("run", help="replay frozen cases without Forgejo")
    run.add_argument("--state-dir", type=Path, required=True)
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--openai-key-file", type=Path, required=True)
    run.add_argument("--ppq-key-file", type=Path, required=True)
    run.add_argument("--engine")
    run.add_argument("--engine-config", type=Path)
    run.add_argument("--prompt-file", type=Path)
    run.add_argument("--audit-prompt-dir", type=Path)
    run.add_argument("--models-json", type=Path)
    run.add_argument("--routing-mode", choices=("enabled", "shadow", "full"))
    run.add_argument(
        "--blind-alternatives",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Run the eval-only blind alternatives experiment",
    )
    run.add_argument("--review-budget-usd", type=float, default=1.00)
    run.add_argument("--ppq-review-budget-usd", type=float)
    run.add_argument("--monthly-budget-usd", type=float)
    run.add_argument(
        "--labels-json",
        type=Path,
        help="Optional local labels keyed by case ID; never sent to models",
    )
    run.add_argument("manifests", type=Path, nargs="+")

    summary = commands.add_parser("spend", help="show current month spend")
    summary.add_argument("--state-dir", type=Path, required=True)

    summarize = commands.add_parser(
        "summarize", help="summarize saved run JSON artifacts"
    )
    summarize.add_argument("runs", type=Path, nargs="+")
    dashboard = commands.add_parser(
        "stats", help="write public production review statistics"
    )
    dashboard.add_argument("--state-dir", type=Path, required=True)
    dashboard.add_argument("--report-dir", type=Path, required=True)
    dashboard.add_argument("--repository-url", required=True)
    dashboard.add_argument("--report-base-url", required=True)
    args = parser.parse_args(argv)

    if args.command == "capture":
        bot_config = config.BotConfig(
            args.origin,
            args.repository,
            args.forgejo_api,
            args.repository_url,
            args.comment_marker,
        )
        current_only = (
            args.prompt_file is not None
            or args.audit_prompt_dir is not None
            or args.models_json is not None
        )
        if args.engine != "current" and current_only:
            parser.error("prompt and model options apply only to --engine current")
        try:
            engine_config = read_engine_config(args.engine_config)
            engine_config.update(
                {
                    key: value
                    for key, value in {
                        "prompt_file": args.prompt_file,
                        "audit_prompt_dir": args.audit_prompt_dir,
                        "models_json": args.models_json,
                    }.items()
                    if value is not None
                }
            )
            if args.engine == "current":
                engine_config.setdefault("archaeology", True)
            review_engine = engine.load(args.engine, engine_config)
        except (
            OSError,
            json.JSONDecodeError,
            ImportError,
            TypeError,
            ValueError,
        ) as exc:
            parser.error(f"could not configure review engine: {exc}")
        token = read_secret(args.forgejo_token_file, "Forgejo token")
        requests = _research_requests(args.research_requests_json)
        checkout = args.state_dir / "evaluation-checkout"
        for number in args.prs:
            print(
                capture_case(
                    token,
                    checkout,
                    args.output_dir,
                    number,
                    bot_config,
                    review_engine,
                    requests,
                    args.research_cutoff,
                )
            )
        return 0
    if args.command == "spend":
        path = args.state_dir / "spend.sqlite3"
        if not path.exists():
            parser.error(f"No spend ledger at {path}")
        print(json.dumps(spend.Ledger(path).summary(), sort_keys=True))
        return 0
    if args.command == "summarize":
        print(json.dumps(summarize_runs(args.runs), sort_keys=True))
        return 0
    if args.command == "stats":
        summary = stats.collect_stats(args.state_dir, args.report_dir)
        stats_page.save_stats(
            args.report_dir / "stats",
            summary,
            args.repository_url,
            args.report_base_url,
        )
        return 0

    api_key = read_secret(args.openai_key_file, "OpenAI key")
    ppq_api_key = read_secret(args.ppq_key_file, "PPQ key")
    ledger = spend.Ledger(
        args.state_dir / "spend.sqlite3",
        review_limit_usd=args.review_budget_usd,
        monthly_limit_usd=args.monthly_budget_usd,
    )
    ppq_ledger = spend.Ledger(
        args.state_dir / "ppq-spend.sqlite3",
        review_limit_usd=args.ppq_review_budget_usd,
    )
    labels = (
        json.loads(args.labels_json.read_text(encoding="utf-8"))
        if args.labels_json
        else {}
    )
    try:
        engine_config = read_engine_config(args.engine_config)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        parser.error(f"could not read engine configuration: {exc}")
    result = 0
    for manifest in args.manifests:
        path = run_case(
            manifest,
            api_key,
            args.output_dir,
            ledger,
            args.routing_mode,
            args.prompt_file,
            args.audit_prompt_dir,
            args.models_json,
            labels,
            ppq_api_key=ppq_api_key,
            ppq_ledger=ppq_ledger,
            blind_alternatives=args.blind_alternatives,
            engine_name=args.engine,
            engine_config=(engine_config if args.engine_config else None),
        )
        print(path)
        if json.loads(path.read_text(encoding="utf-8"))["run"]["status"] != "completed":
            result = 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
