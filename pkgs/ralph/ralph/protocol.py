"""Structured stage results and the boundary between findings and prose."""

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
        "summary": {"type": "string", "minLength": 1},
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
TEST_COVERAGE = object_schema(
    {
        "paths": {"type": "array", "items": STRING, "minItems": 1},
        "status": {
            "type": "string",
            "enum": ["checked", "deferred", "not_applicable"],
        },
        "evidence": {"type": "array", "items": SOURCE_EVIDENCE},
        "regression": STRING,
        "reason": STRING,
    }
)
TESTS_DISCOVERY_SCHEMA = object_schema(
    {
        **DISCOVERY_SCHEMA["properties"],
        "test_coverage": {"type": "array", "items": TEST_COVERAGE},
    }
)
CHALLENGER_SCHEMA = object_schema(
    {
        "coverage": COVERAGE,
        "challenges": {
            "type": "array",
            "items": object_schema(
                {
                    "candidate_id": STRING,
                    "outcome": {
                        "type": "string",
                        "enum": ["refuted", "not_refuted", "unresolved"],
                    },
                    "reason": STRING,
                    "source_evidence": {"type": "array", "items": SOURCE_EVIDENCE},
                }
            ),
        },
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
                        "enum": ["supported", "refuted", "unresolved"],
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


WRITE_VERIFIER_SCHEMA = object_schema(
    {
        "results": {
            "type": "array",
            "items": object_schema(
                {
                    "finding_id": STRING,
                    "outcome": {
                        "type": "string",
                        "enum": ["reproduced", "refuted", "inconclusive"],
                    },
                    "reason": STRING,
                    "command_ids": STRINGS,
                }
            ),
        }
    }
)

JUDGE_SCHEMA = object_schema(
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
                    "title": STRING,
                    "summary": STRING,
                    "body": STRING,
                }
            ),
        },
        "concept": {
            "anyOf": [
                object_schema(
                    {
                        "disposition": {
                            "type": "string",
                            "enum": ["publish", "drop", "unresolved"],
                        },
                        "reason": STRING,
                        "summary": STRING,
                    }
                ),
                {"type": "null"},
            ]
        },
    }
)


def _object(value, keys):
    if not isinstance(value, dict):
        raise InvalidReview("Expected a review object")
    missing = set(keys) - set(value)
    unexpected = set(value) - set(keys)
    if missing or unexpected:
        details = []
        if missing:
            details.append("missing: " + ", ".join(sorted(missing)))
        if unexpected:
            details.append("unexpected: " + ", ".join(sorted(unexpected)))
        raise InvalidReview("Invalid review fields (" + "; ".join(details) + ")")


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


def tests_discovery(text, snapshot, required_paths, *, stage="tests"):
    value = json.loads(text)
    _object(value, TESTS_DISCOVERY_SCHEMA["properties"])
    assessments = value.pop("test_coverage")
    result = discovery(json.dumps(value), stage, snapshot)
    result["test_coverage"] = []
    limitations = result["coverage"]["limitations"]
    accounted = set()
    if not isinstance(assessments, list):
        limitations.append("Test coverage assessments must be a list.")
        assessments = []
    for index, assessment in enumerate(assessments, 1):
        try:
            _object(assessment, TEST_COVERAGE["properties"])
            paths = assessment["paths"]
            _strings(paths)
            if not paths or any(path not in snapshot.changed_paths for path in paths):
                raise InvalidReview("Test coverage must identify changed paths")
            status = assessment["status"]
            if status not in ("checked", "deferred", "not_applicable"):
                raise InvalidReview("Invalid test coverage status")
            for field in ("regression", "reason"):
                if not isinstance(assessment[field], str):
                    raise InvalidReview(f"Test coverage {field} must be text")
            if status == "checked" and not assessment["regression"].strip():
                raise InvalidReview("Checked test coverage must describe a regression")
            if status != "checked" and not assessment["reason"].strip():
                raise InvalidReview(
                    "Deferred or inapplicable test coverage needs a reason"
                )
            evidence = assessment["evidence"]
            if not isinstance(evidence, list):
                raise InvalidReview("Test coverage evidence must be a list")
            if evidence or status == "checked":
                _source_evidence(evidence, snapshot)
        except InvalidReview as exc:
            # An unsupported coverage claim must not erase separately valid leads.
            limitations.append(f"Test coverage assessment {index}: {exc}")
            continue
        result["test_coverage"].append(assessment)
        accounted.update(paths)
        if status == "deferred":
            limitations.append(
                f"Deferred test inspection for {', '.join(paths)}: {assessment['reason']}"
            )
    missing = sorted(set(required_paths) - accounted)
    if missing:
        limitations.append("Test coverage was not assessed for: " + ", ".join(missing))
    if limitations:
        result["coverage"]["status"] = "partial"
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


def _verified_finding(finding, snapshot):
    _object(finding, FINDING["properties"])
    _location(finding, snapshot)
    if finding["severity"] not in FINDING["properties"]["severity"]["enum"]:
        raise InvalidReview("Invalid severity")
    if finding["kind"] not in FINDING["properties"]["kind"]["enum"]:
        raise InvalidReview("Invalid finding kind")
    _text(finding, ("title", "summary", "body"))
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
        # Unsupported decisions cannot contain a finding, so an omitted null
        # is unambiguous. Keep all other fields subject to strict validation.
        if isinstance(decision, dict) and decision.get("disposition") in (
            "refuted",
            "unresolved",
        ):
            decision.setdefault("finding", None)
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
        if disposition not in {"supported", "refuted", "unresolved"}:
            raise InvalidReview("Invalid verifier disposition")
        finding = decision["finding"]
        if disposition == "supported":
            try:
                _verified_finding(finding, snapshot)
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
            raise InvalidReview("Only supported decisions may introduce a finding")
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


def write_verification(text, findings, execution):
    """Validate interpretations against command records from the trusted runner."""
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidReview("Invalid write verifier JSON") from exc
    _object(result, WRITE_VERIFIER_SCHEMA["properties"])
    if not isinstance(result["results"], list):
        raise InvalidReview("Invalid write verifier results")
    expected = {finding["id"] for finding in findings}
    commands = {command["id"]: command for command in execution["commands"]}
    seen = set()
    for item in result["results"]:
        _object(
            item,
            WRITE_VERIFIER_SCHEMA["properties"]["results"]["items"]["properties"],
        )
        _text(item, ("finding_id", "reason"))
        identifier = item["finding_id"]
        if identifier not in expected or identifier in seen:
            raise InvalidReview("Unknown or repeated write verifier finding ID")
        seen.add(identifier)
        if item["outcome"] not in ("reproduced", "refuted", "inconclusive"):
            raise InvalidReview("Invalid write verifier outcome")
        references = item["command_ids"]
        if not isinstance(references, list) or any(
            not isinstance(reference, str) for reference in references
        ):
            raise InvalidReview("Invalid write verifier command IDs")
        if len(set(references)) != len(references) or any(
            reference not in commands or commands[reference]["finding_id"] != identifier
            for reference in references
        ):
            raise InvalidReview("Unknown, repeated, or mismatched experiment command")
        if item["outcome"] in {"reproduced", "refuted"} and not any(
            (item["outcome"] == "refuted" or commands[reference]["side"] == "head")
            and commands[reference]["status"] == "completed"
            and commands[reference].get("capture_complete") is True
            for reference in references
        ):
            raise InvalidReview(
                "Conclusive evidence requires a completed command with captured evidence, at head for reproduction"
            )
    if seen != expected:
        raise InvalidReview("Write verifier omitted finding results")
    return result


def _decision_records(items, expected, field, label):
    if not isinstance(items, list):
        raise InvalidReview(f"Invalid {label} decisions")
    records = {}
    errors = []
    for item in items:
        identifier = item.get(field) if isinstance(item, dict) else None
        if not isinstance(identifier, str) or identifier not in expected:
            errors.append({field: identifier, "error": f"Unknown {label} ID"})
            continue
        records.setdefault(identifier, []).append(item)
    return records, errors


def challenge(text, candidates, snapshot):
    result = json.loads(text)
    _object(result, CHALLENGER_SCHEMA["properties"])
    _coverage(result["coverage"])
    expected = [candidate["id"] for candidate in candidates]
    records, errors = _decision_records(
        result["challenges"], set(expected), "candidate_id", "challenger"
    )
    normalized = []
    for identifier in expected:
        matches = records.get(identifier, [])
        try:
            if len(matches) != 1:
                raise InvalidReview(
                    "Challenger must assess each candidate exactly once"
                )
            item = matches[0]
            _object(
                item,
                CHALLENGER_SCHEMA["properties"]["challenges"]["items"]["properties"],
            )
            _text(item, ("candidate_id", "reason"))
            if item["outcome"] not in ("refuted", "not_refuted", "unresolved"):
                raise InvalidReview("Invalid challenger outcome")
            evidence = item["source_evidence"]
            if not isinstance(evidence, list):
                raise InvalidReview("Expected challenger source evidence")
            if evidence or item["outcome"] == "refuted":
                _source_evidence(evidence, snapshot)
        except InvalidReview as exc:
            errors.append({"candidate_id": identifier, "error": str(exc)})
            item = {
                "candidate_id": identifier,
                "outcome": "unresolved",
                "reason": f"Challenge unresolved: {exc}",
                "source_evidence": [],
            }
        if item["outcome"] == "unresolved":
            result["coverage"]["limitations"].append(
                f"Challenge for {identifier} remains unresolved: {item['reason']}"
            )
        normalized.append(item)
    if errors:
        result["coverage"]["limitations"].append(
            "Some challenger decisions failed validation."
        )
    if result["coverage"]["limitations"]:
        result["coverage"]["status"] = "partial"
    result["challenges"] = normalized
    result["validation_errors"] = errors
    return result


def judgment(text, verified_findings, verified_concept, snapshot):
    result = json.loads(text)
    _object(result, JUDGE_SCHEMA["properties"])
    _coverage(result["coverage"])
    originals = {finding["id"]: finding for finding in verified_findings}
    records, errors = _decision_records(
        result["decisions"], originals, "finding_id", "judge"
    )
    normalized = []
    published = []
    for identifier, original in originals.items():
        matches = records.get(identifier, [])
        try:
            if len(matches) != 1:
                raise InvalidReview(
                    "Judge must decide each supported finding exactly once"
                )
            item = matches[0]
            _object(
                item, JUDGE_SCHEMA["properties"]["decisions"]["items"]["properties"]
            )
            _text(item, ("finding_id", "reason"))
            disposition = item["disposition"]
            if disposition not in ("publish", "drop", "unresolved"):
                raise InvalidReview("Invalid judge disposition")
            wording = ("title", "summary", "body")
            _text(item, wording, empty=wording if disposition != "publish" else ())
            evidence = item["source_evidence"]
            if not isinstance(evidence, list):
                raise InvalidReview("Expected judge source evidence")
            if evidence or disposition == "publish":
                _source_evidence(evidence, snapshot)
        except InvalidReview as exc:
            errors.append({"finding_id": identifier, "error": str(exc)})
            item = {
                "finding_id": identifier,
                "disposition": "unresolved",
                "reason": f"Publication withheld: {exc}",
                "source_evidence": [],
                "title": "",
                "summary": "",
                "body": "",
            }
        if item["disposition"] == "publish":
            published.append(
                {
                    **original,
                    **{field: item[field] for field in ("title", "summary", "body")},
                }
            )
        elif item["disposition"] == "unresolved":
            result["coverage"]["limitations"].append(
                f"Finding {identifier} remains unpublished: {item['reason']}"
            )
        normalized.append(item)

    concept = result["concept"]
    concept_summary = None
    try:
        if concept is not None:
            if verified_concept is None:
                raise InvalidReview("Judge introduced an unverified concept")
            _object(concept, ("disposition", "reason", "summary"))
            _text(concept, ("reason", "summary"), empty=("summary",))
            if concept["disposition"] not in ("publish", "drop", "unresolved"):
                raise InvalidReview("Invalid judge concept disposition")
            if concept["disposition"] == "publish":
                _text(concept, ("summary",))
                urls = _summary_urls(concept["summary"])
                citations = set(_concept_citations(verified_concept))
                if citations - urls:
                    raise InvalidReview("Judge omitted a concept citation")
                if urls - citations:
                    raise InvalidReview("Judge added an unverified concept citation")
                concept_summary = concept["summary"]
        elif verified_concept is not None:
            raise InvalidReview("Judge omitted the verified concept decision")
    except InvalidReview as exc:
        errors.append({"finding_id": None, "error": str(exc)})
        concept = {
            "disposition": "unresolved",
            "reason": f"Concept publication withheld: {exc}",
            "summary": "",
        }
    if concept is not None and concept["disposition"] == "unresolved":
        result["coverage"]["limitations"].append(concept["reason"])
    if errors:
        result["coverage"]["limitations"].append(
            "Some judge decisions failed validation."
        )
    if result["coverage"]["limitations"]:
        result["coverage"]["status"] = "partial"
    result["decisions"] = normalized
    result["concept"] = concept
    result["validation_errors"] = errors
    return result, published, concept_summary
