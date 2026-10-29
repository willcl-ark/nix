"""Run selected independent reviews, verify their evidence, then edit findings."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from final_comment import render

from . import coverage, experiments, feedback, model, protocol, routing
from .config import AUDIT_NAMES, ADVERSARIAL_PROFILES
from .repository import audit_developer_notes, focused_review_input

# Domain stages run in a predictable order; the two
# adversarial models review the same evidence concurrently.
# Domain correctness precedes design.
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
ADVERSARIAL_STAGES = ("adversarial", "adversarial_claude")
FULL_CONTEXT_STAGES = {
    "script",
    "dead_code",
    "independent",
    *ADVERSARIAL_STAGES,
    "verifier",
    "write_verifier",
    "major_verifier",
}


def _backport_input(input_text, evidence):
    if evidence is None:
        return input_text
    return "Backport evidence packet:\n" + json.dumps(evidence) + "\n\n" + input_text


def _stage_tools(name, runtime, settings):
    research_evidence = runtime.case.research_evidence
    discussion_lookup = runtime.discussion_lookup if research_evidence is None else None
    allow_web_search = (
        runtime.allow_web_search
        and research_evidence is None
        and name != "write_verifier"
    )
    tools = model.TOOL_SETS[settings.tool_set]
    if name == "alternatives":
        # Blind alternatives can only inspect the comparison base.
        tools = [tool for tool in tools if tool in model.BASE_TOOLS]
    if tools and runtime.case.backport_evidence is not None and name != "alternatives":
        tools = [*tools, *model.BACKPORT_TOOLS]
    evidence = research_evidence if name in {"archaeologist", "verifier"} else None
    return model.available_tools(
        tools,
        discussion_lookup
        if name not in {"script", "dead_code", "write_verifier", "major_verifier"}
        else None,
        evidence,
        allow_web_search,
    )


def _run_stage(
    runtime,
    prompt_config,
    plan,
    name,
    input_text,
    prompt,
    schema,
    *,
    tools=True,
    experiment_session=None,
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
            available_tools = _stage_tools(name, runtime, settings)
            if experiment_session is not None:
                available_tools = [*available_tools, *experiments.TOOLS]
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
                timeout=settings.timeout,
                final_timeout=settings.final_timeout,
                **(
                    {"experiment_session": experiment_session}
                    if experiment_session is not None
                    else {}
                ),
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
                timeout=settings.timeout,
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
        record["status"] = "failed"
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


def _verifier_schema(backport_evidence, feedback_packet):
    schema = (
        protocol.BACKPORT_VERIFIER_SCHEMA
        if backport_evidence is not None
        else protocol.VERIFIER_SCHEMA
    )
    if feedback_packet is not None:
        return protocol.object_schema(
            {
                **schema["properties"],
                "review_reconciliation": feedback.RECONCILIATION_SCHEMA,
            }
        )
    return schema


def _verify_candidates(
    run_stage,
    input_text,
    prompt_config,
    candidates,
    snapshot,
    concept_candidate,
    debug,
    backport_evidence=None,
    feedback_packet=None,
    feedback_mappings=None,
):
    stages = debug["stages"]
    verified_concept = None
    verified_alternatives = []
    accepted = []
    finding_candidates = {}
    assessed_backport = None
    schema = _verifier_schema(backport_evidence, feedback_packet)
    prompt = prompt_config.audit_prompts["verifier"]
    if feedback_packet is not None:
        prompt += "\n\n" + prompt_config.audit_prompts["reconciliation"]
    try:
        verified = run_stage(
            "verifier",
            input_text,
            prompt,
            schema,
        )
        if backport_evidence is not None:
            payload = json.loads(verified)
            assessment = payload.pop("backport_assessment", None)
            try:
                assessed_backport = protocol.backport_assessment(
                    assessment, backport_evidence
                )
            except protocol.InvalidReview as exc:
                debug["backport_assessment"]["limitations"].append(str(exc))
            verified = json.dumps(payload)
        reconciliation = None
        if feedback_packet is not None:
            payload = json.loads(verified)
            reconciliation = payload.pop("review_reconciliation", None)
            verified = json.dumps(payload)
        result, accepted = protocol.verification(
            verified, candidates, snapshot, concept_candidate
        )
        if feedback_packet is not None:
            try:
                reconciliation_errors = []
                debug["review_memory"] = feedback.reconcile(
                    feedback_packet,
                    reconciliation,
                    candidates,
                    snapshot,
                    mappings=feedback_mappings,
                    validation_errors=reconciliation_errors,
                )
                stages["verifier"]["reconciliation_validation_errors"] = (
                    reconciliation_errors
                )
            except Exception as exc:
                stages["verifier"]["feedback_validation_error"] = str(exc)
        if assessed_backport is not None:
            debug["backport_assessment"] = assessed_backport
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


EXPERIMENT_EVIDENCE_CHARS = 48000


def _major_input(review, majors, evidence):
    return (
        review
        + "\n\nMajor verification input:\n"
        + json.dumps({"findings": majors, "experiment_evidence": evidence})
    )


def _experiment_evidence(record):
    # Keep the runner's complete artifacts in the trace. The final verifier gets
    # a bounded excerpt and must not infer anything from omitted evidence.
    evidence = json.dumps(
        {
            key: record[key]
            for key in ("status", "results", "execution", "reason", "error_type")
            if key in record
        }
    )
    if len(evidence) > EXPERIMENT_EVIDENCE_CHARS:
        evidence = (
            evidence[:EXPERIMENT_EVIDENCE_CHARS] + "\n[Experiment evidence truncated]"
        )
    return evidence


def _write_majors(run_stage, runtime, prompt_config, review, snapshot, findings):
    majors = [finding for finding in findings if finding["severity"] == "major"]
    stages = runtime.debug["stages"]
    stages["write_verifier"] = {
        "model": prompt_config.models["write_verifier"],
        "status": "skipped",
        "turns": [],
        "tools": [],
        "reason": "No major findings to investigate",
    }
    if not majors:
        return
    session = None
    try:
        session = experiments.ExperimentSession(
            snapshot,
            [finding["id"] for finding in majors],
            is_current=runtime.is_current,
        )
        with session:
            answer = run_stage(
                "write_verifier",
                review
                + "\n\nExperiment input:\n"
                + json.dumps(
                    {
                        "findings": majors,
                        "head_sha": snapshot.head_sha,
                        "diff_base_sha": snapshot.diff_base_sha,
                    }
                ),
                prompt_config.audit_prompts["write_verifier"],
                protocol.WRITE_VERIFIER_SCHEMA,
                experiment_session=session,
            )
            result = protocol.write_verification(answer, majors, session.record)
            stages["write_verifier"]["results"] = result["results"]
    except model.StaleReview:
        stages["write_verifier"]["status"] = "stale"
        raise
    except Exception as exc:
        record = stages["write_verifier"]
        if record["status"] == "skipped":
            record.update(status="failed", error_type=type(exc).__name__)
        _record_validation_failure(record, exc)
        record["reason"] = (
            "Optional experiment did not complete; static verification continues."
        )
    finally:
        record = stages["write_verifier"]
        if session is not None:
            record["execution"] = session.record


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
            _major_input(
                review, majors, _experiment_evidence(stages["write_verifier"])
            ),
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
                "presented_summary": presented[identifier]["summary"],
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
    *,
    comment_format=None,
):
    review = case.review
    snapshot = case.snapshot
    debug = runtime.debug
    usage = runtime.usage
    is_current = runtime.is_current
    research_evidence = case.research_evidence
    backport_evidence = case.backport_evidence
    feedback_packet = (
        case.review_feedback
        if case.review_feedback and case.review_feedback.get("findings")
        else None
    )
    feedback_mappings = None
    if case.review_feedback is not None:
        debug["review_memory"] = feedback.reconcile(
            case.review_feedback, [], [], snapshot
        )
    if backport_evidence is not None:
        debug["backport_evidence"] = backport_evidence
        debug["backport_assessment"] = {
            "verdict": "Needs clarification",
            "summary": "Backport verification did not complete.",
            "evidence": [],
            "required_fixes": [],
            "limitations": ["No validated backport assessment is available."],
        }
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
                        {
                            "review_feedback": feedback_packet,
                            "feedback_mappings": feedback_mappings,
                        }
                        if feedback_packet is not None
                        else {}
                    ),
                    **(
                        {"blind_alternatives": alternatives_candidate}
                        if alternatives_candidate is not None
                        else {}
                    ),
                }
            )
        )

    debug["pipeline_stage"] = "routing"
    plan = routing.plan_review(
        runtime, review, snapshot, prompt_config, debug, routing_mode
    )
    notes = None

    def run_stage(
        name, input_text, prompt, schema, *, tools=True, experiment_session=None
    ):
        if backport_evidence is not None and name != "collator":
            input_text = _backport_input(input_text, backport_evidence)
            prompt += "\n\n" + prompt_config.audit_prompts["backport"]
        # Read the current plan after any discovery-driven escalation.
        return _run_stage(
            runtime,
            prompt_config,
            plan,
            name,
            input_text,
            prompt,
            schema,
            tools=tools,
            experiment_session=experiment_session,
        )

    def discover(name):
        nonlocal notes
        prompt = (
            prompt_config.instructions
            if name == "independent"
            else prompt_config.audit_prompts[name]
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
        script_sensitive = collect("script", discover("script"))
        completed.add("script")

    if archaeology:
        concept_candidate = run_archaeologist()
    else:
        reason = "Archaeology was not requested."
        debug["concept_assessment"] = {"status": "skipped", "reason": reason}
        for name in ("archaeologist", "alternatives"):
            stages[name] = {
                "model": prompt_config.models[name],
                "status": "skipped",
                "reason": reason,
                "turns": [],
                "tools": [],
            }
    if backport_evidence is not None:
        stages["alternatives"] = {
            "model": prompt_config.models["alternatives"],
            "status": "skipped",
            "reason": "Backport review focuses on fidelity and release suitability.",
            "turns": [],
            "tools": [],
        }
    elif blind_alternatives and concept_candidate is not None:
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
            "model": prompt_config.models["alternatives"],
            "status": "skipped",
            "reason": "No valid problem and baseline from concept review.",
            "turns": [],
            "tools": [],
        }
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
        if name == "adversarial":
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
    for name in (*DISCOVERY_ORDER, "adversarial_claude"):
        if name not in completed:
            stages[name] = {
                "model": prompt_config.models[name],
                "status": "skipped",
                "turns": [],
                "tools": [],
            }

    if feedback_packet is not None:
        settings = prompt_config.profile.stages["feedback"]
        record = {
            "model": settings.model,
            "status": "running",
            "turns": [],
            "tools": [],
            "max_output_tokens": settings.max_output_tokens,
        }
        stages["feedback"] = record
        try:
            answer, response = runtime.audit(
                "feedback",
                prompt_config.audit_prompts["feedback"],
                json.dumps(
                    {
                        "review_feedback": feedback_packet,
                        "candidate_findings": candidates,
                    }
                ),
                model_name=settings.model,
                max_output_tokens=record["max_output_tokens"],
                response_schema=feedback.FEEDBACK_SCHEMA,
                reasoning_effort=settings.reasoning_effort,
                debug_record=record,
                timeout=settings.timeout,
            )
            if response["status"] != "completed":
                raise protocol.InvalidReview("Feedback response was incomplete")
            record["status"] = "completed"
            record["raw_output"] = answer
            feedback_mappings = feedback.parse_feedback(
                answer, feedback_packet, candidates
            )
            record["coverage"] = {"status": "complete", "limitations": []}
            debug["stage_outputs"]["feedback"] = answer
        except model.StaleReview:
            raise
        except Exception as exc:
            record.update(
                status="failed",
                error_type=type(exc).__name__,
            )

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
        backport_evidence,
        feedback_packet,
        feedback_mappings,
    )
    findings, concept_summary = _edit_findings(
        run_stage, prompt_config, verified, debug
    )
    _write_majors(run_stage, runtime, prompt_config, review, snapshot, findings)
    findings = _verify_majors(
        run_stage, prompt_config, review, snapshot, verified, findings, debug
    )
    finding_candidates = verified.candidate_ids
    verified_alternatives = verified.alternatives

    debug["coverage"] = coverage.summarize(debug)
    backport = debug.get("backport_assessment")
    if backport is not None and backport["verdict"] == "Faithful":
        blockers = list(debug["coverage"]["limitations"])
        if any(finding["kind"] == "defect" for finding in findings):
            blockers.append(
                "Verified defects conflict with the claimed faithful backport verdict."
            )
        if blockers or debug["coverage"]["status"] != "complete":
            backport["verdict"] = "Needs clarification"
            backport["summary"] = (
                "The review did not establish a faithful and correct backport."
            )
            backport["limitations"] = list(
                dict.fromkeys(
                    [
                        *backport["limitations"],
                        *(blockers or ["Review coverage is incomplete."]),
                    ]
                )
            )
            debug["coverage"] = coverage.summarize(debug)
    debug["alternatives"] = verified_alternatives
    was_edited = stages["collator"]["status"] == "completed" and not stages[
        "collator"
    ].get("used_verified_wording")
    published = _published_findings(
        verified, findings, candidate_sources, prompt_config.models, was_edited
    )
    for entry in debug.get("review_memory", []):
        candidate_ids = set(entry.get("current_candidate_ids", []))
        entry["current_finding_ids"] = [
            finding["id"]
            for finding in published
            if candidate_ids
            and set(verified.candidate_ids[finding["id"]]) <= candidate_ids
        ]
        if entry["current_finding_ids"] and entry["status"] in {"fixed", "invalid"}:
            entry.update(
                status="unclear",
                reason="The reconciliation conflicts with a verified current finding.",
                collapsed=False,
                unchanged_since_report=False,
            )
        severity_order = {"suggestion": 0, "minor": 1, "major": 2, "critical": 3}
        current_severity = max(
            (
                severity_order.get(finding["severity"], 3)
                for finding in published
                if finding["id"] in entry["current_finding_ids"]
            ),
            default=-1,
        )
        original_severity = severity_order.get(entry.get("severity"), 0)
        if current_severity > original_severity:
            entry["collapsed"] = False
        previous_severity = max(
            (
                severity_order.get(finding.get("severity"), 3)
                for finding in entry.get("last_presented_findings", [])
            ),
            default=original_severity,
        )
        if current_severity > previous_severity:
            entry["unchanged_since_report"] = False
    for finding in published:
        linked_groups = {
            entry.get("duplicate_of") or entry["key"]
            for entry in debug.get("review_memory", [])
            if finding["id"] in entry["current_finding_ids"]
        }
        linked = [
            entry
            for entry in debug.get("review_memory", [])
            if (entry.get("duplicate_of") or entry["key"]) in linked_groups
            and entry["status"] not in {"fixed", "invalid"}
        ]
        # Replies belong to individual reports. One answered report cannot hide
        # a current finding also linked to an unanswered historical report.
        if any(not entry["collapsed"] for entry in linked):
            for entry in linked:
                entry["collapsed"] = False
    published_by_id = {finding["id"]: finding for finding in published}
    debug["finding_attribution"] = [
        {
            "finding_id": finding["id"],
            **{key: finding[key] for key in ("title", "path", "line", "side")},
            "presented_body": finding["body"],
            "presented_summary": finding["summary"],
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
    if usage is not None:
        debug["usage"] = usage.summary()
    debug.pop("pipeline_stage", None)
    content = render(
        findings,
        debug["coverage"]["limitations"],
        concept_summary,
        verified_alternatives,
        debug.get("backport_assessment"),
        review_memory=debug.get("review_memory"),
        review_tier=plan["tier"],
        verification_failed=debug["coverage"]["verification"]["status"] == "failed",
        comment_format=comment_format,
    )
    concept_concern = (
        verified.concept is not None
        and bool(concept_summary)
        and verified.concept["assessment"] != "worth_pursuing"
    )
    return ReviewResult(content, published, concept_concern)
