"""Run selected independent reviews, verify their evidence, then edit findings."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import coverage, model, protocol, routing
from .config import AUDIT_NAMES, ADVERSARIAL_PROFILES
from .repository import audit_developer_notes, focused_review_input
from .spend import BudgetExceeded

# Domain stages spend the review budget in a predictable order; the three
# adversarial models review the same evidence concurrently.
# Domain correctness precedes design; verification retains protected headroom.
DISCOVERY_ORDER = (
    "script",
    "adversarial",
    "concurrency",
    "state",
    "public_contract",
    "build",
    "tests",
    "dead_code",
    "design",
)
PPQ_ADVERSARIAL_STAGES = ("adversarial_deepseek", "adversarial_gemini")
ADVERSARIAL_STAGES = ("adversarial", *PPQ_ADVERSARIAL_STAGES)
FULL_CONTEXT_STAGES = {
    "script",
    "dead_code",
    "independent",
    *ADVERSARIAL_STAGES,
    "verifier",
    "major_verifier",
}


def _stage_tools(name, runtime):
    research_evidence = runtime.case.research_evidence
    discussion_lookup = runtime.discussion_lookup if research_evidence is None else None
    allow_web_search = runtime.allow_web_search and research_evidence is None
    tools = (
        model.BASE_TOOLS
        if name == "alternatives"
        else model.ARCHAEOLOGY_TOOLS
        if name == "archaeologist"
        else model.VERIFIER_TOOLS
        if name == "verifier"
        else model.TOOLS
        if name in FULL_CONTEXT_STAGES
        else model.FOCUSED_TOOLS
    )
    evidence = research_evidence if name in {"archaeologist", "verifier"} else None
    return model.available_tools(
        tools,
        discussion_lookup
        if name not in {"script", "dead_code", "major_verifier"}
        else None,
        evidence,
        allow_web_search,
    )


def _run_stage(
    runtime, prompt_config, plan, name, input_text, prompt, schema, *, tools=True
):
    debug = runtime.debug
    stages = debug["stages"]
    outputs = debug["stage_outputs"]
    if name not in ADVERSARIAL_STAGES:
        debug["pipeline_stage"] = name
    settings = prompt_config.profile.stages[name]
    stage_model = settings.model
    record = {"model": stage_model, "status": "running", "turns": [], "tools": []}
    stages[name] = record
    if name in ADVERSARIAL_STAGES:
        record["profiles"] = list(plan["profiles"])
    try:
        effort, output_tokens = settings.reasoning_effort, settings.max_output_tokens
        if tools:
            calls = settings.max_tool_calls[plan["tier"]]
            available_tools = _stage_tools(name, runtime)
            answer = runtime.review(
                name,
                input_text,
                prompt=prompt,
                model_name=stage_model,
                record=record,
                tools=available_tools,
                max_tool_calls=calls,
                max_context_calls=settings.max_context_calls,
                max_history_calls=settings.max_history_calls,
                max_web_search_calls_per_response=settings.max_web_search_calls_per_response,
                max_output_tokens=output_tokens,
                reasoning_effort=effort,
                first_tool_required=bool(available_tools)
                and (
                    name in FULL_CONTEXT_STAGES
                    or name in {"archaeologist", "alternatives"}
                ),
                response_schema=schema,
                provider="ppq" if name in PPQ_ADVERSARIAL_STAGES else "openai",
                final_timeout=600 if name in {"verifier", "major_verifier"} else 180,
            )
        else:
            answer, response = runtime.audit(
                name,
                prompt,
                input_text,
                model_name=stage_model,
                max_output_tokens=output_tokens,
                response_schema=schema,
                reasoning_effort=effort,
                debug_record=record,
            )
            if response["status"] != "completed":
                raise protocol.InvalidReview("Stage response was incomplete")
        outputs[name] = answer
        record["raw_output"] = answer
        record["status"] = "completed"
        return answer
    except model.StaleReview:
        record["status"] = "stale"
        raise
    except Exception as exc:
        record["status"] = (
            "budget_exhausted" if isinstance(exc, BudgetExceeded) else "failed"
        )
        record["error_type"] = type(exc).__name__
        if record.get("raw_output"):
            outputs[name] = record["raw_output"]
        raise


@dataclass
class ReviewResult:
    content: str
    findings: list
    concept_concern: bool


@dataclass
class VerifiedReview:
    findings: list
    candidate_ids: dict
    concept: dict | None
    alternatives: list


def _record_validation_failure(record, error):
    if record["status"] == "completed":
        record.update(status="invalid", error_type=type(error).__name__)
    if isinstance(error, protocol.InvalidReview):
        record["validation_error"] = str(error)


def _verify_candidates(
    run_stage, input_text, prompt_config, candidates, snapshot, concept_candidate, debug
):
    stages = debug["stages"]
    verified_concept = None
    verified_alternatives = []
    accepted = []
    finding_candidates = {}
    try:
        verified = run_stage(
            "verifier",
            input_text,
            prompt_config.audit_prompts["verifier"],
            protocol.VERIFIER_SCHEMA,
        )
        result, accepted = protocol.verification(
            verified, candidates, snapshot, concept_candidate
        )
        stages["verifier"]["coverage"] = result["coverage"]
        stages["verifier"]["decisions"] = result["decisions"]
        debug["decisions"] = result["decisions"]
        previous_concept = debug.get("concept_assessment", {})
        concept_status = (
            "verified"
            if result["concept"]["disposition"] == "publish"
            else result["concept"]["disposition"]
        )
        if concept_candidate is None and previous_concept.get("status") in {
            "skipped",
            "failed",
        }:
            concept_status = previous_concept["status"]
        debug["concept_assessment"] = {
            **previous_concept,
            "status": concept_status,
            "verification": result["concept"],
        }
        if result["concept_validation_error"]:
            stages["verifier"]["concept_validation_error"] = result[
                "concept_validation_error"
            ]
        if result["alternatives_validation_error"]:
            stages["verifier"]["alternatives_validation_error"] = result[
                "alternatives_validation_error"
            ]
        verified_alternatives = result["alternatives"]
        verified_concept = (
            result["concept"]["assessment"]
            if result["concept"]["disposition"] == "publish"
            else None
        )
        published = [
            decision
            for decision in result["decisions"]
            if decision["disposition"] == "publish"
        ]
        finding_candidates = {
            finding["id"]: decision["candidate_ids"]
            for finding, decision in zip(accepted, published)
        }
        if result["validation_errors"]:
            stages["verifier"]["validation_errors"] = result["validation_errors"]
    except model.StaleReview:
        raise
    except Exception as exc:
        _record_validation_failure(stages["verifier"], exc)
    return VerifiedReview(
        accepted, finding_candidates, verified_concept, verified_alternatives
    )


def _edit_findings(run_stage, prompt_config, verified, debug):
    stages = debug["stages"]
    accepted = verified.findings
    verified_concept = verified.concept
    concept_summary = None
    # The editor sees only accepted findings. A failed editor can use the
    # verifier's own wording, without discarding paid verification work.
    findings = accepted
    if accepted or verified_concept is not None:
        try:
            edited = run_stage(
                "collator",
                json.dumps(
                    {
                        "findings": accepted,
                        "concept_assessment": verified_concept,
                    }
                ),
                prompt_config.audit_prompts["collator"],
                protocol.COLLATOR_SCHEMA,
                tools=False,
            )
            findings, concept_summary = protocol.collation(
                edited, accepted, verified_concept
            )
            if concept_summary is not None:
                debug["concept_assessment"] = {
                    **debug.get("concept_assessment", {}),
                    "status": "verified",
                    "summary": concept_summary,
                    "edited_by": "collator",
                }
        except model.StaleReview:
            raise
        except Exception as exc:
            _record_validation_failure(stages["collator"], exc)
            stages["collator"]["used_verified_wording"] = True
            concept_summary = protocol.concept_summary(
                verified_concept, debug["concept_assessment"]["verification"]["reason"]
            )
            if concept_summary is not None:
                debug["concept_assessment"] = {
                    **debug.get("concept_assessment", {}),
                    "status": "verified",
                    "summary": concept_summary,
                    "used_verified_wording": True,
                }
    else:
        stages["collator"] = {
            "model": prompt_config.models["collator"],
            "status": "skipped",
            "turns": [],
            "tools": [],
            "reason": "No accepted findings to edit",
        }
    return findings, concept_summary


def _verify_majors(
    run_stage, prompt_config, review, snapshot, verified, findings, debug
):
    majors = [finding for finding in findings if finding["severity"] == "major"]
    stages = debug["stages"]
    if not majors:
        stages["major_verifier"] = {
            "model": prompt_config.models["major_verifier"],
            "status": "skipped",
            "reason": "No major findings to verify",
            "turns": [],
            "tools": [],
        }
        return findings

    try:
        answer = run_stage(
            "major_verifier",
            review
            + "\n\nMajor verification input:\n"
            + json.dumps({"findings": majors}),
            prompt_config.audit_prompts["major_verifier"],
            protocol.MAJOR_VERIFIER_SCHEMA,
        )
        result = protocol.major_verification(answer, majors, snapshot)
        stages["major_verifier"]["coverage"] = result["coverage"]
        stages["major_verifier"]["decisions"] = result["decisions"]
        decisions = result["decisions"]
    except model.StaleReview:
        raise
    except Exception as exc:
        _record_validation_failure(stages["major_verifier"], exc)
        decisions = [
            {
                "finding_id": finding["id"],
                "disposition": "unresolved",
                "reason": "Major finding withheld because second verification did not complete.",
            }
            for finding in majors
        ]

    by_candidates = {
        tuple(verified.candidate_ids[decision["finding_id"]]): decision
        for decision in decisions
    }
    # Keep Luna's decisions in its stage record; public diagnostics use final verdicts.
    final_decisions = []
    for decision in debug["decisions"]:
        verdict = by_candidates.get(tuple(decision["candidate_ids"]))
        if verdict is not None and verdict["disposition"] != "publish":
            decision = {
                **decision,
                "disposition": verdict["disposition"],
                "reason": "Major verification: " + verdict["reason"],
                "finding": None,
            }
        final_decisions.append(decision)
    debug["decisions"] = final_decisions
    withheld = {
        decision["finding_id"]
        for decision in decisions
        if decision["disposition"] != "publish"
    }
    verified.findings = [
        finding for finding in verified.findings if finding["id"] not in withheld
    ]
    return [finding for finding in findings if finding["id"] not in withheld]


def _published_findings(verified, edited, candidate_sources, models, was_edited):
    presented = {finding["id"]: finding for finding in edited}
    published = []
    # Exports retain verification order even when the editor reorders prose.
    for finding in verified.findings:
        identifier = finding["id"]
        verifier = "major_verifier" if finding["severity"] == "major" else "verifier"
        sources = sorted(
            {
                candidate_sources[candidate_id]
                for candidate_id in verified.candidate_ids[identifier]
            }
        )
        published.append(
            {
                **finding,
                "presented_title": presented[identifier]["title"],
                "presented_body": presented[identifier]["body"],
                "raised_by": sources,
                "raised_by_models": sorted({models[stage] for stage in sources}),
                "verified_by": verifier,
                "verified_by_model": models[verifier],
                "edited_by": "collator" if was_edited else None,
                "edited_by_model": models["collator"] if was_edited else None,
            }
        )
    return published


def review_with_independent_passes(
    case,
    runtime,
    prompt_config,
    routing_mode="enabled",
    blind_alternatives=True,
    archaeology=False,
):
    review = case.review
    snapshot = case.snapshot
    debug = runtime.debug
    budget = runtime.budget
    is_current = runtime.is_current
    ppq_api_key = runtime.ppq_api_key
    ppq_budget = runtime.ppq_budget
    research_evidence = case.research_evidence
    if ppq_budget is not None:
        for stage in PPQ_ADVERSARIAL_STAGES:
            ppq_budget.exempt(stage)
    stages = debug.setdefault("stages", {})
    debug.setdefault("stage_outputs", {})
    candidates = []
    candidate_sources = debug.setdefault("candidate_sources", {})
    concept_candidate = None
    alternatives_candidate = None
    research_inventory = (
        model.research.inventory(research_evidence)
        if research_evidence is not None
        else None
    )
    plan = {"tier": "sensitive"}

    def with_research_inventory(input_text):
        if research_inventory is None:
            return input_text
        return input_text + "\n\nFrozen research inventory:\n" + research_inventory

    def verifier_input():
        return (
            with_research_inventory(review)
            + "\n\nVerification input:\n"
            + json.dumps(
                {
                    "candidate_findings": candidates,
                    "inspection_gaps": [
                        issue
                        for issue in coverage.summarize(debug)["issues"]
                        if issue["category"] == "inspection"
                    ],
                    "concept_candidate": concept_candidate,
                    **(
                        {"blind_alternatives": alternatives_candidate}
                        if alternatives_candidate is not None
                        else {}
                    ),
                }
            )
        )

    def protect_verification():
        if budget is not None:
            settings = prompt_config.profile.stages["verifier"]
            effort, output_tokens = (
                settings.reasoning_effort,
                settings.max_output_tokens,
            )
            tools = _stage_tools("verifier", runtime)
            debug["verification_budget"] = budget.protect(
                {
                    "model": prompt_config.models["verifier"],
                    "store": False,
                    "reasoning": {"effort": effort},
                    "instructions": prompt_config.audit_prompts["verifier"],
                    "input": [{"role": "user", "content": verifier_input()}],
                    "tools": tools,
                    "tool_choice": "required",
                    "max_tool_calls": settings.max_web_search_calls_per_response,
                    "max_output_tokens": output_tokens,
                    "text": {
                        "format": {
                            "type": "json_schema",
                            "name": "verifier",
                            "strict": True,
                            "schema": protocol.VERIFIER_SCHEMA,
                        }
                    },
                },
                exempt_stages={
                    "verifier",
                    "major_verifier",
                    "collator",
                    *PPQ_ADVERSARIAL_STAGES,
                },
            )

    protect_verification()
    debug["pipeline_stage"] = "routing"
    plan = routing.plan_review(
        runtime, review, snapshot, prompt_config, debug, routing_mode
    )
    notes = None

    def run_stage(name, input_text, prompt, schema, *, tools=True):
        # Read the current plan after any discovery-driven escalation.
        return _run_stage(
            runtime, prompt_config, plan, name, input_text, prompt, schema, tools=tools
        )

    def discover(name):
        nonlocal notes
        prompt = (
            prompt_config.instructions
            if name == "independent"
            else prompt_config.audit_prompts[
                "adversarial" if name in PPQ_ADVERSARIAL_STAGES else name
            ]
        )
        if name in ADVERSARIAL_STAGES:
            prompt += "\n\n" + "\n\n".join(
                prompt_config.audit_prompts[profile] for profile in plan["profiles"]
            )
        prompt = prompt_config.audit_prompts["common"] + "\n\n" + prompt
        input_text = (
            review
            if name in FULL_CONTEXT_STAGES
            else focused_review_input(review, snapshot, name)
        )
        if name == "design":
            if notes is None:
                notes = audit_developer_notes(snapshot)
            input_text += "\n\nComparison-base developer notes:\n" + notes
        try:
            answer = run_stage(name, input_text, prompt, protocol.DISCOVERY_SCHEMA)
            result = protocol.discovery(answer, name, snapshot)
            stages[name]["coverage"] = result["coverage"]
            return result
        except model.StaleReview:
            raise
        except Exception as exc:
            record = stages[name]
            _record_validation_failure(record, exc)
            return None

    def collect(name, result):
        if result is None:
            return False
        candidates.extend(result["findings"])
        candidate_sources.update(
            {finding["id"]: name for finding in result["findings"]}
        )
        return result["requires_sensitive_review"]

    def run_archaeologist():
        prompt = prompt_config.audit_prompts["archaeologist"]
        input_text = (
            f"Current PR: {case.source['url']}\n"
            f"Changed paths: {json.dumps(sorted(snapshot.changed_paths))}\n\n"
            f"{review}"
        )
        input_text = with_research_inventory(input_text)
        try:
            answer = run_stage(
                "archaeologist", input_text, prompt, protocol.ARCHAEOLOGY_SCHEMA
            )
            result = protocol.archaeology(answer)
            stages["archaeologist"]["coverage"] = result["coverage"]
            debug["concept_assessment"] = {
                "status": "candidate",
                "stage": "archaeologist",
                "candidate": result["assessment"],
                "coverage": result["coverage"],
            }
            return result["assessment"]
        except model.StaleReview:
            raise
        except Exception as exc:
            record = stages["archaeologist"]
            _record_validation_failure(record, exc)
            debug["concept_assessment"] = {
                "status": "failed",
                "error_type": type(exc).__name__,
            }
            return None

    # Spend on selected script correctness before advisory research or overview.
    completed = set()
    script_sensitive = False
    if "script" in plan["audits"]:
        protect_verification()
        script_sensitive = collect("script", discover("script"))
        completed.add("script")

    if archaeology:
        concept_candidate = run_archaeologist()
    else:
        reason = "Archaeology was not requested."
        debug["concept_assessment"] = {"status": "skipped", "reason": reason}
        for name in ("archaeologist", "alternatives"):
            stages[name] = {
                "model": prompt_config.models["archaeologist"],
                "status": "skipped",
                "reason": reason,
                "turns": [],
                "tools": [],
            }
    if blind_alternatives and concept_candidate is not None:
        protect_verification()
        # Only the goal and baseline cross this boundary, never the PR solution
        # or discussion. The tools expose only the pinned comparison-base tree.
        alternative_input = json.dumps(
            {key: concept_candidate[key] for key in ("problem", "goal", "baseline")}
        )
        try:
            answer = run_stage(
                "alternatives",
                alternative_input,
                prompt_config.audit_prompts["alternatives"],
                protocol.ALTERNATIVES_SCHEMA,
            )
            result = protocol.blind_alternatives(answer)
            stages["alternatives"]["coverage"] = result["coverage"]
            alternatives_candidate = result["alternatives"]
        except model.StaleReview:
            raise
        except Exception as exc:
            record = stages["alternatives"]
            if record["status"] == "completed":
                record.update(status="invalid", error_type=type(exc).__name__)
    elif blind_alternatives and archaeology:
        stages["alternatives"] = {
            "model": prompt_config.models["archaeologist"],
            "status": "skipped",
            "reason": "No valid problem and baseline from concept review.",
            "turns": [],
            "tools": [],
        }
    protect_verification()
    sensitive = collect("independent", discover("independent")) or script_sensitive
    selected = set(plan["audits"])
    if sensitive and plan["tier"] != "sensitive":
        selected.update(name for name in AUDIT_NAMES if name != "script")
        plan = {
            **plan,
            "tier": "sensitive",
            "audits": [name for name in AUDIT_NAMES if name in selected],
            "profiles": list(ADVERSARIAL_PROFILES),
            "evidence": plan["evidence"] + ["Overview requested sensitive review"],
        }
        debug["routing"]["selected"] = plan
    if plan["tier"] == "sensitive":
        selected.add("adversarial")
    while pending := [name for name in DISCOVERY_ORDER if name in selected - completed]:
        name = pending[0]
        protect_verification()
        if name == "adversarial" and ppq_api_key:
            debug["pipeline_stage"] = "adversarial"
            with ThreadPoolExecutor(max_workers=len(ADVERSARIAL_STAGES)) as executor:
                futures = {
                    stage: executor.submit(discover, stage)
                    for stage in ADVERSARIAL_STAGES
                }
                results = {stage: future.result() for stage, future in futures.items()}
            escalate = any(
                [collect(stage, results[stage]) for stage in ADVERSARIAL_STAGES]
            )
            completed.update(ADVERSARIAL_STAGES)
        else:
            escalate = collect(name, discover(name))
            completed.add(name)
        if escalate and plan["tier"] != "sensitive":
            selected.update(("adversarial", "state", "concurrency"))
            plan = {
                **plan,
                "tier": "sensitive",
                "profiles": plan["profiles"] or list(ADVERSARIAL_PROFILES),
                "audits": [audit for audit in AUDIT_NAMES if audit in selected],
                "evidence": plan["evidence"] + [f"{name} requested sensitive review"],
            }
            debug["routing"]["selected"] = plan
            debug["routing"]["escalated_by"] = name
    for name in (*DISCOVERY_ORDER, *PPQ_ADVERSARIAL_STAGES):
        if name not in completed:
            stages[name] = {
                "model": prompt_config.models[name],
                "status": "skipped",
                "turns": [],
                "tools": [],
            }
            if name in PPQ_ADVERSARIAL_STAGES and not ppq_api_key:
                stages[name]["reason"] = "PPQ API key is not configured"

    protect_verification()

    if is_current is not None and not is_current():
        raise model.StaleReview()
    verified = _verify_candidates(
        run_stage,
        verifier_input(),
        prompt_config,
        candidates,
        snapshot,
        concept_candidate,
        debug,
    )
    findings, concept_summary = _edit_findings(
        run_stage, prompt_config, verified, debug
    )
    findings = _verify_majors(
        run_stage, prompt_config, review, snapshot, verified, findings, debug
    )
    finding_candidates = verified.candidate_ids
    verified_alternatives = verified.alternatives

    debug["coverage"] = coverage.summarize(debug)
    debug["alternatives"] = verified_alternatives
    was_edited = stages["collator"]["status"] == "completed" and not stages[
        "collator"
    ].get("used_verified_wording")
    published = _published_findings(
        verified, findings, candidate_sources, prompt_config.models, was_edited
    )
    published_by_id = {finding["id"]: finding for finding in published}
    debug["finding_attribution"] = [
        {
            "finding_id": finding["id"],
            **{key: finding[key] for key in ("title", "path", "line", "side")},
            "presented_body": finding["body"],
            "candidate_ids": finding_candidates[finding["id"]],
            **{
                key: published_by_id[finding["id"]][key]
                for key in (
                    "raised_by",
                    "raised_by_models",
                    "verified_by",
                    "verified_by_model",
                    "edited_by",
                    "edited_by_model",
                )
            },
        }
        for finding in findings
    ]
    if budget is not None:
        debug["budget"] = budget.summary()
    if ppq_budget is not None:
        debug["ppq_budget"] = ppq_budget.summary()
    debug.pop("pipeline_stage", None)
    content = protocol.render(
        findings,
        debug["coverage"]["limitations"],
        concept_summary,
        verified_alternatives,
    )
    concept_concern = (
        verified.concept is not None
        and bool(concept_summary)
        and verified.concept["assessment"] != "worth_pursuing"
    )
    return ReviewResult(content, published, concept_concern)
