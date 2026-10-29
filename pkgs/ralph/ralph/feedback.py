"""PR-local author feedback, with durable findings and code-checked decisions."""

import copy
import json
from collections import Counter

from .protocol import (
    SOURCE_EVIDENCE,
    STRING,
    STRINGS,
    InvalidReview,
    _object,
    _source_evidence,
    _strings,
    object_schema,
)

POSITIONS = ("none", "rejected", "address_elsewhere", "wontfix")
MAPPING = object_schema(
    {
        "prior_key": STRING,
        "candidate_ids": STRINGS,
        "comment_ids": STRINGS,
        "author_position": {"type": "string", "enum": list(POSITIONS)},
        "reason": STRING,
    }
)
FEEDBACK_SCHEMA = object_schema({"mappings": {"type": "array", "items": MAPPING}})
ASSESSMENT = object_schema(
    {
        "prior_key": STRING,
        "status": {
            "type": "string",
            "enum": ["still_present", "fixed", "invalid", "unclear"],
        },
        "duplicate_of": {"type": ["string", "null"]},
        "candidate_ids": STRINGS,
        "unchanged": {"type": "boolean"},
        "unchanged_since_report": {"type": "boolean"},
        "reason": STRING,
        "source_evidence": {"type": "array", "items": SOURCE_EVIDENCE},
    }
)
RECONCILIATION_SCHEMA = {"type": "array", "items": ASSESSMENT}


def validate_packet(packet, source, snapshot):
    """Reject feedback belonging to another PR or revision before model calls."""
    if packet is None:
        return
    if not isinstance(packet, dict) or not isinstance(source, dict):
        raise InvalidReview("Invalid PR feedback packet")
    if (
        not isinstance(packet.get("repository"), str)
        or not packet["repository"]
        or packet["repository"] != source.get("repository")
        or type(packet.get("number")) is not int
        or packet["number"] < 1
        or packet["number"] != source.get("number")
    ):
        raise InvalidReview("Feedback must belong to the current PR")
    revisions = packet.get("revisions")
    if (
        not isinstance(revisions, dict)
        or revisions.get("new_head") != snapshot.head_sha
    ):
        raise InvalidReview("Feedback must target the current revision")
    if packet.get("new_head", snapshot.head_sha) != snapshot.head_sha:
        raise InvalidReview("Feedback head disagrees with the current revision")
    for field in ("findings", "comments"):
        if not isinstance(packet.get(field), list):
            raise InvalidReview("Invalid feedback records")
    keys = []
    for entry in packet["findings"]:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("key"), str)
            or not entry["key"]
        ):
            raise InvalidReview("Invalid prior finding identity")
        keys.append(entry["key"])
    if len(keys) != len(set(keys)):
        raise InvalidReview("Duplicate prior finding identity")
    ids = []
    for comment in packet["comments"]:
        if not isinstance(comment, dict) or not isinstance(
            comment.get("id"), (str, int)
        ):
            raise InvalidReview("Invalid author comment identity")
        ids.append(str(comment["id"]))
    if len(ids) != len(set(ids)):
        raise InvalidReview("Duplicate author comment identity")


def _references(value, allowed):
    _strings(value)
    if len(value) != len(set(value)) or not set(value) <= allowed:
        raise InvalidReview("Unknown or duplicate feedback reference")


def parse_feedback(text, packet, candidates):
    """Validate mapping references without granting comments decision authority."""
    try:
        result = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise InvalidReview("Invalid feedback JSON") from exc
    _object(result, FEEDBACK_SCHEMA["properties"])
    if not isinstance(result["mappings"], list):
        raise InvalidReview("Invalid feedback mappings")
    prior = {entry["key"] for entry in packet.get("findings", [])}
    comments = {str(entry["id"]) for entry in packet.get("comments", [])}
    current = {entry["id"] for entry in candidates}
    seen = set()
    for mapping in result["mappings"]:
        _object(mapping, MAPPING["properties"])
        key = mapping["prior_key"]
        if not isinstance(key, str) or key not in prior or key in seen:
            raise InvalidReview("Unknown or duplicate prior finding")
        seen.add(key)
        _references(mapping["candidate_ids"], current)
        _references(mapping["comment_ids"], comments)
        if mapping["author_position"] not in POSITIONS:
            raise InvalidReview("Invalid author position")
        if mapping["author_position"] != "none" and not mapping["comment_ids"]:
            raise InvalidReview("Author position requires an explicit comment")
        if not isinstance(mapping["reason"], str) or not mapping["reason"].strip():
            raise InvalidReview("Feedback mapping requires a reason")
    return result


def reconcile(
    packet, assessments, candidates, snapshot, mappings=None, *, validation_errors=None
):
    """Retain every historical finding; incomplete or invalid checks fail open."""
    entries = copy.deepcopy(packet.get("findings", []))
    prior = {entry["key"] for entry in entries}
    current = {entry["id"] for entry in candidates}
    decisions = {}
    errors = validation_errors if validation_errors is not None else []
    if not isinstance(assessments, list):
        errors.extend(
            {"prior_key": entry["key"], "error": "Missing reconciliation"}
            for entry in entries
        )
        assessments = []
    counts = Counter(
        item["prior_key"]
        for item in assessments
        if isinstance(item, dict) and isinstance(item.get("prior_key"), str)
    )
    for assessment in assessments:
        key = assessment.get("prior_key") if isinstance(assessment, dict) else None
        try:
            # Reject every row for an ambiguous key, including when one of the
            # repeated rows is malformed. Independent assessments still count.
            if isinstance(key, str) and counts[key] > 1:
                raise InvalidReview("Duplicate prior assessment")
            _object(assessment, ASSESSMENT["properties"])
            if not isinstance(key, str) or key not in prior:
                raise InvalidReview("Unknown prior assessment")
            _references(assessment["candidate_ids"], current)
            duplicate = assessment["duplicate_of"]
            if duplicate is not None and (
                not isinstance(duplicate, str)
                or duplicate not in prior
                or duplicate == key
            ):
                raise InvalidReview("Invalid duplicate finding reference")
            if assessment["status"] not in ASSESSMENT["properties"]["status"]["enum"]:
                raise InvalidReview("Invalid reconciliation status")
            if (
                type(assessment["unchanged"]) is not bool
                or type(assessment["unchanged_since_report"]) is not bool
            ):
                raise InvalidReview("Invalid unchanged assessment")
            if (
                not isinstance(assessment["reason"], str)
                or not assessment["reason"].strip()
            ):
                raise InvalidReview("Reconciliation requires a reason")
            evidence = assessment["source_evidence"]
            if not isinstance(evidence, list):
                raise InvalidReview("Invalid reconciliation evidence")
            if evidence or assessment["status"] != "unclear":
                _source_evidence(evidence, snapshot)
            if assessment["status"] != "unclear" and not any(
                item["side"] == "head" for item in evidence
            ):
                raise InvalidReview("Reconciliation requires current source evidence")
            decisions[key] = assessment
        except (InvalidReview, KeyError, TypeError) as exc:
            error = {
                "prior_key": key if isinstance(key, str) else None,
                "error": str(exc),
            }
            if error not in errors:
                errors.append(error)
    # Only source-checked, direct links may merge historical reports for display.
    # Validate against the original decisions so a rejected chain cannot become
    # acceptable merely because an earlier link was cleared.
    duplicates = {}
    for key, decision in decisions.items():
        target = decisions.get(decision["duplicate_of"])
        duplicates[key] = (
            decision["duplicate_of"]
            if target is not None
            and target["duplicate_of"] is None
            and decision["status"] != "unclear"
            and decision["status"] == target["status"]
            else None
        )
    try:
        mapping_rows = parse_feedback(
            json.dumps(mappings or {"mappings": []}), packet, candidates
        )["mappings"]
    except (InvalidReview, TypeError):
        mapping_rows = []
    mapping_by_key = {item["prior_key"]: item for item in mapping_rows}
    comments_by_id = {
        str(comment["id"]): comment for comment in packet.get("comments", [])
    }
    for entry in entries:
        entry.setdefault(
            "original_source_evidence", copy.deepcopy(entry.get("source_evidence", []))
        )
        decision = decisions.get(entry["key"])
        mapping = mapping_by_key.get(entry["key"], {})
        # Preserve the original finding and previous assessments as audit history.
        history = entry.setdefault("assessment_history", [])
        if entry.get("status"):
            history.append(
                {
                    key: copy.deepcopy(entry[key])
                    for key in (
                        "status",
                        "reason",
                        "source_evidence",
                        "author_position",
                        "comment_ids",
                        "collapsed",
                        "unchanged_since_report",
                        "checked_head",
                        "current_candidate_ids",
                        "duplicate_of",
                    )
                    if key in entry
                }
            )
        entry.update(
            status=decision["status"] if decision else "unclear",
            reason=decision["reason"]
            if decision
            else "Previously reported; not reverified against this revision because "
            "no valid verification assessment is available.",
            source_evidence=decision["source_evidence"] if decision else [],
            current_candidate_ids=decision["candidate_ids"] if decision else [],
            current_finding_ids=[],
            checked_head=snapshot.head_sha if decision else None,
            unchanged_since_report=bool(
                decision
                and decision["status"] == "still_present"
                and decision["unchanged_since_report"]
            ),
            duplicate_of=duplicates[entry["key"]]
            if decision
            else entry.get("duplicate_of"),
            author_position=mapping.get(
                "author_position", entry.get("author_position", "none")
            ),
            comment_ids=mapping.get("comment_ids", entry.get("comment_ids", [])),
            collapsed=False,
        )
        if mapping:
            entry["author_comments"] = [
                copy.deepcopy(comments_by_id[comment_id])
                for comment_id in mapping["comment_ids"]
            ]
        else:
            entry.setdefault("author_comments", [])
        entry["feedback_cutoff"] = packet.get("cutoff")
        if decision and mapping:
            entry["collapsed"] = (
                decision["status"] == "still_present"
                and decision["unchanged"]
                and mapping["author_position"] != "none"
                and set(decision["candidate_ids"]) == set(mapping["candidate_ids"])
                and {item["side"] for item in decision["source_evidence"]}
                == {"base", "head"}
            )
    # Partial reassessment may change a former canonical record into a duplicate.
    # Do not keep older links through it, or follow unverified chains.
    roots = {entry["key"]: entry.get("duplicate_of") for entry in entries}
    for entry in entries:
        target = entry["duplicate_of"]
        if target is not None and (
            target not in roots or target == entry["key"] or roots[target] is not None
        ):
            entry["duplicate_of"] = None
    return entries
