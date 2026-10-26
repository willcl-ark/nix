"""Structured stage results and the boundary between findings and prose."""

import html
import json
import re
import subprocess
from urllib.parse import urlsplit

from .repository import MAX_TOOL_BYTES, read_file


class InvalidReview(ValueError):
    pass


def object_schema(properties):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


STRING = {"type": "string"}
STRINGS = {"type": "array", "items": STRING}
PROVENANCE = {
    "type": "string",
    "enum": ["current_pr", "prior_discussion", "history", "web", "agent_inference"],
}
COVERAGE = object_schema(
    {
        "status": {"type": "string", "enum": ["complete", "partial"]},
        "limitations": STRINGS,
    }
)
LOCATION = {
    "path": STRING,
    "line": {"type": "integer"},
    "side": {"type": "string", "enum": ["head", "base"]},
}
SOURCE_EVIDENCE = object_schema({**LOCATION, "excerpt": STRING})
CANDIDATE = object_schema(
    {
        "kind": {"type": "string", "enum": ["defect", "design", "suggestion"]},
        **LOCATION,
        **{
            key: STRING
            for key in (
                "title",
                "claim",
                "consequence",
                "evidence",
                "correction",
                "uncertainty",
            )
        },
    }
)
SECURITY_ASSESSMENT = object_schema(
    {
        "classification": {
            "type": "string",
            "enum": ["potential_vulnerability", "non_security", "uncertain"],
        },
        "reason": STRING,
    }
)
FINDING = object_schema(
    {
        "kind": CANDIDATE["properties"]["kind"],
        "severity": {
            "type": "string",
            "enum": ["critical", "major", "minor", "suggestion"],
        },
        "security": SECURITY_ASSESSMENT,
        **LOCATION,
        "title": STRING,
        "body": STRING,
        "source_evidence": {"type": "array", "items": SOURCE_EVIDENCE, "minItems": 1},
    }
)
DISCOVERY_SCHEMA = object_schema(
    {
        "coverage": COVERAGE,
        "requires_sensitive_review": {"type": "boolean"},
        "findings": {"type": "array", "items": CANDIDATE},
    }
)
CONCEPT_ALTERNATIVE = object_schema(
    {
        "name": STRING,
        "concept": STRING,
        "benefit": STRING,
        "cost": STRING,
        "unresolved": STRING,
        "provenance": PROVENANCE,
        "citations": STRINGS,
    }
)
CONCEPT_ASSESSMENT = object_schema(
    {
        "goal": STRING,
        "problem": STRING,
        "baseline": STRING,
        "delivered_benefit": STRING,
        "relevant_history": STRING,
        "assessment": {
            "type": "string",
            "enum": [
                "worth_pursuing",
                "needs_motivation",
                "rework_approach",
                "not_worth_pursuing",
            ],
        },
        "alternatives": {"type": "array", "items": CONCEPT_ALTERNATIVE},
        "recommendation": STRING,
        "proposed_review": {
            "type": "string",
            "enum": ["continue", "would_stop", "undetermined"],
        },
        "review_reason": STRING,
        "decisive_question": STRING,
        "technical_assumptions": STRINGS,
        "citations": STRINGS,
    }
)
ARCHAEOLOGY_SCHEMA = object_schema(
    {
        "coverage": COVERAGE,
        "assessment": CONCEPT_ASSESSMENT,
    }
)
ALTERNATIVES_SCHEMA = object_schema(
    {
        "coverage": COVERAGE,
        "alternatives": {"type": "array", "items": CONCEPT_ALTERNATIVE},
    }
)
CONCEPT_VERIFICATION = object_schema(
    {
        "disposition": {
            "type": "string",
            "enum": ["publish", "drop", "unresolved", "no_concern"],
        },
        "reason": STRING,
        "assessment": {"anyOf": [CONCEPT_ASSESSMENT, {"type": "null"}]},
        "proposed_review": CONCEPT_ASSESSMENT["properties"]["proposed_review"],
        "review_reason": STRING,
    }
)
VERIFIER_SCHEMA = object_schema(
    {
        "coverage": COVERAGE,
        "concept": CONCEPT_VERIFICATION,
        "alternatives": {"type": "array", "items": CONCEPT_ALTERNATIVE},
        "decisions": {
            "type": "array",
            "items": object_schema(
                {
                    "candidate_ids": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "minItems": 1,
                    },
                    "disposition": {
                        "type": "string",
                        "enum": ["publish", "drop", "unresolved"],
                    },
                    "reason": STRING,
                    "finding": {"anyOf": [FINDING, {"type": "null"}]},
                }
            ),
        },
    }
)
BACKPORT_ASSESSMENT = object_schema(
    {
        "verdict": {
            "type": "string",
            "enum": [
                "Faithful",
                "Incomplete",
                "Missing",
                "Defective",
                "Needs clarification",
            ],
        },
        "summary": STRING,
        "evidence": STRINGS,
        "required_fixes": STRINGS,
        "limitations": STRINGS,
    }
)
BACKPORT_VERIFIER_SCHEMA = object_schema(
    {**VERIFIER_SCHEMA["properties"], "backport_assessment": BACKPORT_ASSESSMENT}
)


def backport_assessment(value, evidence):
    _object(value, BACKPORT_ASSESSMENT["properties"])
    if value["verdict"] not in BACKPORT_ASSESSMENT["properties"]["verdict"]["enum"]:
        raise InvalidReview("Invalid backport verdict")
    _text(value, ("summary",))
    for key in ("evidence", "required_fixes", "limitations"):
        if not isinstance(value[key], list) or any(
            not isinstance(item, str) or not item.strip() for item in value[key]
        ):
            raise InvalidReview("Invalid backport assessment " + key)
    value = {
        **value,
        "limitations": list(
            dict.fromkeys([*value["limitations"], *evidence.get("limitations", [])])
        ),
    }
    if evidence.get("complete") is not True and not value["limitations"]:
        value["limitations"].append("Backport source evidence is incomplete.")
    if value["verdict"] == "Faithful" and (
        not value["evidence"] or value["required_fixes"]
    ):
        value["limitations"].append(
            "A faithful verdict requires evidence and no outstanding fixes."
        )
    if value["verdict"] == "Faithful" and value["limitations"]:
        value["verdict"] = "Needs clarification"
        value["summary"] = (
            "Fidelity could not be established from the available evidence."
        )
    return value


MAJOR_VERIFIER_SCHEMA = object_schema(
    {
        "coverage": COVERAGE,
        "decisions": {
            "type": "array",
            "items": object_schema(
                {
                    "finding_id": {"type": "string", "minLength": 1},
                    "disposition": {
                        "type": "string",
                        "enum": ["publish", "drop", "unresolved"],
                    },
                    "reason": STRING,
                    "source_evidence": {"type": "array", "items": SOURCE_EVIDENCE},
                }
            ),
        },
    }
)
COLLATOR_SCHEMA = object_schema(
    {
        "concept_summary": {"anyOf": [STRING, {"type": "null"}]},
        "findings": {
            "type": "array",
            "items": object_schema(
                {
                    "id": STRING,
                    "title": STRING,
                    "body": STRING,
                }
            ),
        },
    }
)


def _object(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise InvalidReview("Unexpected review fields")


def _strings(value):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise InvalidReview("Expected a list of strings")


def _citations(value):
    _strings(value)
    if any(not _safe_http_url(item) for item in value):
        raise InvalidReview("Citations must be HTTP(S) links")


def _safe_http_url(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
    )


def _summary_urls(value):
    if not isinstance(value, str):
        return set()
    return {
        url for url in re.findall(r"https?://[^\s<>)\]]+", value) if _safe_http_url(url)
    }


def _text(record, keys, empty=()):
    for key in keys:
        if not isinstance(record[key], str) or (
            key not in empty and not record[key].strip()
        ):
            raise InvalidReview("Missing finding text")


def _coverage(value):
    _object(value, COVERAGE["properties"])
    _strings(value["limitations"])
    if value["status"] not in ("complete", "partial"):
        raise InvalidReview("Invalid coverage status")
    if value["limitations"]:
        value["status"] = "partial"


def _location(value, snapshot):
    if not isinstance(value["side"], str) or value["side"] not in {"head", "base"}:
        raise InvalidReview("Invalid location side")
    path = value["path"]
    if not isinstance(path, str) or path not in snapshot.changed_paths:
        raise InvalidReview(f"Finding must identify a changed file: {repr(path)[:120]}")
    files = snapshot.head_files if value["side"] == "head" else snapshot.base_files
    if path not in files:
        raise InvalidReview(
            f"Finding path is missing on the {value['side']} side: {repr(path)[:120]}"
        )
    if type(value["line"]) is not int or value["line"] < 1:
        raise InvalidReview("Finding must identify a positive line")


def discovery(text, stage, snapshot):
    result = json.loads(text)
    _object(result, DISCOVERY_SCHEMA["properties"])
    _coverage(result["coverage"])
    if type(result["requires_sensitive_review"]) is not bool or not isinstance(
        result["findings"], list
    ):
        raise InvalidReview("Invalid discovery response")
    for index, finding in enumerate(result["findings"], 1):
        _object(finding, CANDIDATE["properties"])
        _location(finding, snapshot)
        if finding["kind"] not in CANDIDATE["properties"]["kind"]["enum"]:
            raise InvalidReview("Invalid candidate kind")
        _text(
            finding,
            ("title", "claim", "consequence", "evidence", "correction", "uncertainty"),
            empty=("correction", "uncertainty"),
        )
        finding["id"] = f"{stage}:{index}"
    return result


def _source_evidence(evidence, snapshot):
    if not isinstance(evidence, list) or not evidence:
        raise InvalidReview("Finding must include source evidence")
    for source in evidence:
        _object(source, SOURCE_EVIDENCE["properties"])
        if source["side"] not in ("head", "base"):
            raise InvalidReview("Invalid source evidence side")
        files = snapshot.head_files if source["side"] == "head" else snapshot.base_files
        if not isinstance(source["path"], str) or source["path"] not in files:
            raise InvalidReview("Source evidence must identify a tracked file")
        if type(source["line"]) is not int or source["line"] < 1:
            raise InvalidReview("Source evidence must identify a positive line")
        _text(source, ("excerpt",))
        lines = source["excerpt"].removesuffix("\n").split("\n")
        if len(lines) > 150 or len(source["excerpt"].encode()) > MAX_TOOL_BYTES:
            raise InvalidReview("Source evidence exceeds the file-read limit")
        try:
            actual = read_file(snapshot.checkout, files, source["path"], source["line"])
        except (OSError, subprocess.SubprocessError) as exc:
            raise InvalidReview("Source evidence could not be read") from exc
        expected = "".join(
            f"{number}: {line}\n" for number, line in enumerate(lines, source["line"])
        )
        if not actual.partition("\n")[2].startswith(expected):
            raise InvalidReview("Source evidence does not match the snapshot")


def _concept_assessment(value):
    _object(value, CONCEPT_ASSESSMENT["properties"])
    _text(
        value,
        (
            "goal",
            "problem",
            "baseline",
            "delivered_benefit",
            "relevant_history",
            "recommendation",
            "review_reason",
            "decisive_question",
        ),
    )
    if (
        value["assessment"]
        not in CONCEPT_ASSESSMENT["properties"]["assessment"]["enum"]
    ):
        raise InvalidReview("Invalid concept assessment")
    _review_proposal(value["proposed_review"], value["review_reason"], value)
    _strings(value["technical_assumptions"])
    _citations(value["citations"])
    _concept_alternatives(value["alternatives"])


def _concept_alternatives(alternatives, limit=4):
    if not isinstance(alternatives, list) or not 0 <= len(alternatives) <= limit:
        raise InvalidReview(f"Expected up to {limit} alternatives")
    for alternative in alternatives:
        _object(alternative, CONCEPT_ALTERNATIVE["properties"])
        _text(alternative, ("name", "concept", "benefit", "cost", "unresolved"))
        _citations(alternative["citations"])
        if (
            alternative["provenance"]
            not in CONCEPT_ALTERNATIVE["properties"]["provenance"]["enum"]
        ):
            raise InvalidReview("Invalid alternative provenance")


def _review_proposal(proposed_review, review_reason, assessment):
    if (
        proposed_review
        not in CONCEPT_ASSESSMENT["properties"]["proposed_review"]["enum"]
    ):
        raise InvalidReview("Invalid proposed review")
    if not isinstance(review_reason, str) or not review_reason.strip():
        raise InvalidReview("Missing proposed review reason")
    if proposed_review == "would_stop" and (
        assessment is None
        or assessment["assessment"] not in {"rework_approach", "not_worth_pursuing"}
    ):
        raise InvalidReview(
            "would_stop requires a verified rework_approach or not_worth_pursuing assessment"
        )


def _concept_citations(assessment):
    if assessment is None:
        return []
    citations = list(assessment["citations"])
    for alternative in assessment["alternatives"]:
        citations.extend(alternative["citations"])
    return list(dict.fromkeys(citations))


def concept_summary(assessment, reason):
    if assessment is None:
        return None
    citations = _concept_citations(assessment)
    source_text = (
        ""
        if not citations
        else " "
        + " ".join(
            f"[{index}]({citation})" for index, citation in enumerate(citations, 1)
        )
    )
    return f"{assessment['recommendation']} {reason}{source_text}"


def archaeology(text):
    result = json.loads(text)
    _object(result, ARCHAEOLOGY_SCHEMA["properties"])
    _coverage(result["coverage"])
    _concept_assessment(result["assessment"])
    return result


def blind_alternatives(text):
    result = json.loads(text)
    _object(result, ALTERNATIVES_SCHEMA["properties"])
    _coverage(result["coverage"])
    _concept_alternatives(result["alternatives"])
    for alternative in result["alternatives"]:
        if alternative["provenance"] != "agent_inference" or alternative["citations"]:
            raise InvalidReview("Blind alternatives must be uncited agent inferences")
    return result


def _concept_decision(concept, concept_assessment):
    _object(concept, CONCEPT_VERIFICATION["properties"])
    if concept["disposition"] not in ("publish", "drop", "unresolved", "no_concern"):
        raise InvalidReview("Invalid concept disposition")
    _text(concept, ("reason", "review_reason"))
    if concept["disposition"] == "publish":
        if concept_assessment is None:
            raise InvalidReview("Concept assessment had no archaeology candidate")
        _concept_assessment(concept["assessment"])
        if concept["assessment"]["assessment"] == "worth_pursuing":
            raise InvalidReview("Favorable concept assessments are not public feedback")
        if len(concept["assessment"]["alternatives"]) > 1:
            raise InvalidReview(
                "Public concept feedback may include at most one alternative"
            )
    elif concept["assessment"] is not None:
        raise InvalidReview("Only publish may include a concept assessment")
    _review_proposal(
        concept["proposed_review"], concept["review_reason"], concept["assessment"]
    )


def _published_finding(finding, snapshot):
    _object(finding, FINDING["properties"])
    _location(finding, snapshot)
    if finding["severity"] not in FINDING["properties"]["severity"]["enum"]:
        raise InvalidReview("Invalid severity")
    if finding["kind"] not in FINDING["properties"]["kind"]["enum"]:
        raise InvalidReview("Invalid finding kind")
    _text(finding, ("title", "body"))
    security = finding["security"]
    _object(security, SECURITY_ASSESSMENT["properties"])
    if (
        security["classification"]
        not in SECURITY_ASSESSMENT["properties"]["classification"]["enum"]
    ):
        raise InvalidReview("Invalid security classification")
    _text(security, ("reason",))
    _source_evidence(finding["source_evidence"], snapshot)


def verification(text, candidates, snapshot, concept_assessment=None):
    result = json.loads(text)
    _object(result, VERIFIER_SCHEMA["properties"])
    _coverage(result["coverage"])
    if not isinstance(result["decisions"], list):
        raise InvalidReview("Invalid verifier decisions")
    result["alternatives_validation_error"] = None
    try:
        _concept_alternatives(result["alternatives"], limit=1)
    except InvalidReview as exc:
        result["alternatives_validation_error"] = str(exc)
        result["alternatives"] = []
    concept = result["concept"]
    result["concept_validation_error"] = None
    try:
        _concept_decision(concept, concept_assessment)
    except InvalidReview as exc:
        error = str(exc)
        concept = {
            "disposition": "unresolved",
            "assessment": None,
            "reason": f"Concept assessment withheld: {error}",
            "proposed_review": "undetermined",
            "review_reason": "The verifier output did not satisfy the concept schema.",
        }
        result["concept"] = concept
        result["concept_validation_error"] = error
    if concept["disposition"] == "unresolved":
        result["coverage"]["status"] = "partial"
        result["coverage"]["limitations"].append(concept["reason"])
    expected_order = [candidate["id"] for candidate in candidates]
    expected = set(expected_order)
    records = []
    candidate_uses = {}
    unsafe_indexes = set()
    validation_errors = []
    for index, decision in enumerate(result["decisions"]):
        _object(
            decision, VERIFIER_SCHEMA["properties"]["decisions"]["items"]["properties"]
        )
        ids = decision["candidate_ids"]
        _strings(ids)
        records.append((decision, ids))
        known_ids = {candidate_id for candidate_id in ids if candidate_id in expected}
        for candidate_id in known_ids:
            candidate_uses.setdefault(candidate_id, []).append(index)
        repeated_ids = [
            candidate_id
            for candidate_id in dict.fromkeys(ids)
            if ids.count(candidate_id) > 1
        ]
        unknown_ids = [
            candidate_id for candidate_id in ids if candidate_id not in expected
        ]
        errors = []
        if not ids:
            errors.append("Decision must identify at least one supplied candidate ID")
        if repeated_ids:
            errors.append(
                "Repeated candidate IDs within decision: " + ", ".join(repeated_ids)
            )
        if unknown_ids:
            errors.append("Unknown candidate IDs: " + ", ".join(unknown_ids))
        if errors:
            unsafe_indexes.add(index)
            validation_errors.append({"candidate_ids": ids, "error": "; ".join(errors)})
    for candidate_id, indexes in candidate_uses.items():
        if len(indexes) > 1:
            unsafe_indexes.update(indexes)
            validation_errors.append(
                {
                    "candidate_ids": [candidate_id],
                    "error": "Candidate ID appears in multiple decisions",
                }
            )

    accepted = []
    normalized_decisions = []
    unsafe_ids = set()
    unsafe_without_known_id = False
    for index, (decision, ids) in enumerate(records):
        if index in unsafe_indexes:
            known_ids = [
                candidate_id for candidate_id in ids if candidate_id in expected
            ]
            unsafe_ids.update(known_ids)
            unsafe_without_known_id = unsafe_without_known_id or not known_ids
            continue
        _text(decision, ("reason",))
        disposition = decision["disposition"]
        if disposition not in {"publish", "drop", "unresolved"}:
            raise InvalidReview("Invalid verifier disposition")
        finding = decision["finding"]
        if disposition == "publish":
            try:
                _published_finding(finding, snapshot)
            except InvalidReview as exc:
                error = str(exc)
                decision.update(
                    disposition="unresolved",
                    finding=None,
                    reason=f"Finding withheld: {error}",
                )
                validation_errors.append({"candidate_ids": ids, "error": error})
                result["coverage"]["status"] = "partial"
                result["coverage"]["limitations"].append(f"Finding withheld: {error}")
            else:
                accepted.append({**finding, "id": f"finding:{len(accepted) + 1}"})
        elif finding is not None:
            raise InvalidReview("Only publish decisions may introduce a finding")
        normalized_decisions.append(decision)

    def candidate_key(candidate_id):
        return expected_order.index(candidate_id)

    if unsafe_ids:
        candidate_ids = sorted(unsafe_ids, key=candidate_key)
        normalized_decisions.append(
            {
                "candidate_ids": candidate_ids,
                "disposition": "unresolved",
                "reason": "Decision withheld: invalid candidate ID accounting",
                "finding": None,
            }
        )
        result["coverage"]["status"] = "partial"
        result["coverage"]["limitations"].append(
            "Decision withheld: invalid candidate ID accounting"
        )
    if unsafe_without_known_id:
        normalized_decisions.append(
            {
                "candidate_ids": [],
                "disposition": "unresolved",
                "reason": "Decision withheld: invalid candidate ID accounting",
                "finding": None,
            }
        )
        result["coverage"]["status"] = "partial"
        result["coverage"]["limitations"].append(
            "Decision withheld: invalid candidate ID accounting"
        )

    represented = {
        candidate_id
        for decision in normalized_decisions
        for candidate_id in decision["candidate_ids"]
    }
    omitted = [
        candidate_id
        for candidate_id in expected_order
        if candidate_id not in represented
    ]
    if omitted:
        normalized_decisions.append(
            {
                "candidate_ids": omitted,
                "disposition": "unresolved",
                "reason": "Verifier omitted candidate decisions",
                "finding": None,
            }
        )
        validation_errors.append(
            {"candidate_ids": omitted, "error": "Verifier omitted candidate decisions"}
        )
        result["coverage"]["status"] = "partial"
        result["coverage"]["limitations"].append("Verifier omitted candidate decisions")
    result["decisions"] = normalized_decisions
    result["validation_errors"] = validation_errors
    return result, accepted


def major_verification(text, findings, snapshot):
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidReview("Invalid Major verifier JSON") from exc
    _object(result, MAJOR_VERIFIER_SCHEMA["properties"])
    _coverage(result["coverage"])
    if not isinstance(result["decisions"], list):
        raise InvalidReview("Invalid Major verifier decisions")
    expected = {finding["id"] for finding in findings}
    seen = set()
    for decision in result["decisions"]:
        _object(
            decision,
            MAJOR_VERIFIER_SCHEMA["properties"]["decisions"]["items"]["properties"],
        )
        _text(decision, ("finding_id", "reason"))
        finding_id = decision["finding_id"]
        if finding_id not in expected or finding_id in seen:
            raise InvalidReview("Unknown or repeated Major finding ID")
        seen.add(finding_id)
        disposition = decision["disposition"]
        if disposition not in ("publish", "drop", "unresolved"):
            raise InvalidReview("Invalid Major verifier disposition")
        evidence = decision["source_evidence"]
        if not isinstance(evidence, list):
            raise InvalidReview("Expected a source evidence list")
        if disposition == "publish" or evidence:
            _source_evidence(evidence, snapshot)
        if disposition == "unresolved":
            result["coverage"]["status"] = "partial"
            result["coverage"]["limitations"].append(
                f"Major finding {finding_id} remains unresolved: {decision['reason']}"
            )
    if seen != expected:
        raise InvalidReview("Major verifier omitted finding decisions")
    return result


def collation(text, accepted, concept_assessment=None):
    result = json.loads(text)
    _object(result, COLLATOR_SCHEMA["properties"])
    if not isinstance(result["findings"], list):
        raise InvalidReview("Invalid collator findings")
    concept_summary = result["concept_summary"]
    if concept_summary is not None and (
        not isinstance(concept_summary, str) or not concept_summary.strip()
    ):
        raise InvalidReview("Invalid concept summary")
    if concept_assessment is None and concept_summary is not None:
        raise InvalidReview("Collator invented a concept summary")
    if concept_assessment is not None and concept_summary is None:
        raise InvalidReview("Collator omitted the accepted concept assessment")
    if concept_summary is not None:
        summary_urls = _summary_urls(concept_summary)
        citations = set(_concept_citations(concept_assessment))
        if citations - summary_urls:
            raise InvalidReview("Collator omitted a concept citation")
        if summary_urls - citations:
            raise InvalidReview("Collator added an unverified concept citation")
    originals = {finding["id"]: finding for finding in accepted}
    seen = set()
    edited = []
    for finding in result["findings"]:
        _object(finding, ("id", "title", "body"))
        _text(finding, ("id", "title", "body"))
        identifier = finding["id"]
        if identifier not in originals or identifier in seen:
            raise InvalidReview("Collator added or repeated a finding")
        seen.add(identifier)
        edited.append({**originals[identifier], **finding})
    if seen != set(originals):
        raise InvalidReview("Collator omitted an accepted finding")
    return edited, concept_summary


def render(
    findings,
    limitations=(),
    concept_summary=None,
    alternatives=(),
    backport=None,
    review_memory=None,
):
    sections = []
    if backport is not None:
        lines = [f"Backport verdict: **{backport['verdict']}**", backport["summary"]]
        for key, heading in (
            ("evidence", "Evidence"),
            ("required_fixes", "Required fixes"),
            ("limitations", "Limitations"),
        ):
            if backport[key]:
                lines.append(
                    heading + ":\n\n" + "\n".join(f"- {item}" for item in backport[key])
                )
        sections.append("\n\n".join(lines))
    memory = review_memory or []
    collapsed_ids = {
        finding_id
        for entry in memory
        if entry.get("collapsed") and entry.get("status") == "still_present"
        for finding_id in entry.get("current_finding_ids", [])
    }
    finding_groups = {}
    design_findings = []
    for finding in findings:
        if finding["id"] in collapsed_ids:
            continue
        if finding["kind"] == "design":
            design_findings.append(finding)
        else:
            finding_groups.setdefault(finding["severity"], []).append(finding)
    rendered_findings = []
    for severity, heading in (
        ("critical", "Critical"),
        ("major", "Major"),
        ("minor", "Minor"),
        ("suggestion", "Suggestions"),
    ):
        group = finding_groups.get(severity, [])
        if group:
            rendered_findings.append(
                f"**{heading}**\n\n"
                + "\n\n".join(
                    f"**{finding['title']}** ({finding['path']}:{finding['line']}, "
                    f"{finding['side']})\n\n{finding['body']}"
                    for finding in group
                )
            )
    if rendered_findings:
        sections.append("##### Findings\n\n" + "\n\n".join(rendered_findings))
    rendered_design = []
    if design_findings:
        rendered_design.append(
            "\n\n".join(
                f"**{finding['title']}** ({finding['path']}:{finding['line']}, "
                f"{finding['side']})\n\n{finding['body']}"
                for finding in design_findings
            )
        )
    if concept_summary:
        rendered_design.append(f"**Concept and approach.** {concept_summary}")
    if rendered_design:
        sections.append("##### Design and approach\n\n" + "\n\n".join(rendered_design))
    if alternatives:
        rendered_alternatives = []
        for alternative in alternatives:
            text = (
                f"**{alternative['name']}.** {alternative['concept']} "
                f"Benefit: {alternative['benefit']} "
                f"Trade-off: {alternative['cost']}"
            )
            if alternative["unresolved"]:
                text += f" Unresolved: {alternative['unresolved']}"
            if alternative["citations"]:
                text += " " + " ".join(
                    f"[Source {index}]({citation})"
                    for index, citation in enumerate(alternative["citations"], 1)
                )
            rendered_alternatives.append(text)
        sections.append("##### Alternatives\n\n" + "\n\n".join(rendered_alternatives))
    if memory:
        unresolved = [
            entry for entry in memory if entry.get("status") not in ("fixed", "invalid")
        ]
        if unresolved:
            severities = {}
            for entry in unresolved:
                severity = entry.get("severity", "unknown")
                severities[severity] = severities.get(severity, 0) + 1
            counts = ", ".join(
                f"{count} {severity}" for severity, count in severities.items()
            )
            sections.append(
                f"Previously reported concerns remain unresolved: {html.escape(counts)}. "
                "Author deferral or disagreement does not resolve a finding."
            )
        history = []
        for entry in memory:

            def escaped(key, default=""):
                return html.escape(str(entry.get(key, default)))

            history.append(
                f"<h6>{escaped('severity')} · {escaped('title')}</h6>\n"
                f"<p>Status: {escaped('status', 'unclear')}. "
                f"Author position: {escaped('author_position', 'none')}.</p>\n"
                f"<p>{escaped('path')}:{escaped('line')} at {escaped('source_head')}</p>\n"
                f"<pre>{escaped('body')}</pre>\n<p>{escaped('reason')}</p>"
            )
            for comment in entry.get("author_comments", []):
                url = comment.get("url", "")
                if urlsplit(url).scheme in ("https", "http"):
                    history.append(
                        f'<p><a href="{html.escape(url, quote=True)}">Author reply</a> '
                        f"{html.escape(str(comment.get('updated_at', '')))}</p>"
                    )
        sections.append(
            "<details>\n<summary>Previous findings and author responses</summary>\n\n"
            + "\n\n".join(history)
            + "\n\n</details>"
        )
    if not sections:
        sections.append(
            "I found no actionable issues in this static review."
            if not limitations
            else "No verified findings are available from this review."
        )
    if limitations:
        sections.append(
            "Review coverage was incomplete.\n\n"
            "<details>\n<summary>Review limitations</summary>\n\n"
            + "\n".join(f"- {limitation}" for limitation in dict.fromkeys(limitations))
            + "\n\n</details>"
        )
    return "\n\n".join(sections)
