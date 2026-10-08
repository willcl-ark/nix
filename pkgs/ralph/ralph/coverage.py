"""Separate code inspection, advisory context, and verification coverage."""

import re


# Only discard an entire, ordinary static-review caveat. Mixed statements that
# also name missing evidence stay limitations, including historical outputs.
_SCOPE_NOTE = re.compile(
    r"(?:static (?:review|inspection) only[;:.] ?)?"
    r"no (?:builds?|tests?|test runs?|test executions?|sanitizers?|sanitizer runs?|"
    r"compilation|compiles?)(?:(?:,? (?:or|and) |, )(?:builds?|tests?|test runs?|"
    r"test executions?|sanitizers?|sanitizer runs?|compilation|compiles?))*"
    r" (?:were|was) (?:run|performed|executed)"
    r"(?: \((?:as expected|expected for this review type)\)|, which is expected for this review(?: type)?)?"
    r"(?:; this is a static review)?[.]?", re.IGNORECASE)

CONTEXT_STAGES = {"archaeologist", "alternatives"}
FAILURES = {"failed", "invalid", "budget_exhausted"}


def _failure(stage):
    status = stage.get("status")
    if status == "budget_exhausted":
        return "Stage budget exhausted before the review completed."
    if status == "invalid":
        return "Output failed validation: " + stage.get("validation_error", "invalid review output")
    if (stage.get("incomplete_reason") == "max_output_tokens"
            or any(isinstance(turn, dict)
                   and turn.get("incomplete_reason") == "max_output_tokens"
                   for turn in stage.get("turns", []))):
        limit = stage.get("max_output_tokens")
        if isinstance(limit, int) and limit > 0:
            return f"Model response reached its {limit:,}-token output limit."
        return "Model response reached its output-token limit."
    error = stage.get("error_type")
    return {
        "HTTPError": "Model API request failed.",
        "TimeoutError": "Model API request timed out.",
        "TimeoutExpired": "Repository inspection timed out.",
    }.get(error, "Stage did not complete" + (f" ({error})." if error else "."))


def _status(statuses):
    for status in ("failed", "partial", "unknown"):
        if status in statuses:
            return status
    return "complete" if statuses else "unknown"


def summarize(debug):
    """Derive coverage from recorded evidence without altering model outputs."""
    stages = debug.get("stages") or {}
    issues = []
    notes = []
    inspections = []
    contexts = []
    verification = "unknown"
    concept = (debug.get("concept_assessment") or {}).get("verification") or {}
    concept_reason = concept.get("reason") if concept.get("disposition") == "unresolved" else None

    def issue(name, category, reason):
        item = {"stage": name, "category": category, "reason": reason}
        if item not in issues:
            issues.append(item)

    for name, stage in stages.items():
        if name in {"router", "collator"} or stage.get("status") == "skipped":
            continue
        category = ("context" if name in CONTEXT_STAGES else
                    "verification" if name == "verifier" else "inspection")
        state = stage.get("status")
        if state in FAILURES:
            status = "failed"
            issue(name, category, _failure(stage))
        elif state != "completed":
            status = "unknown"
            issue(name, category, "Stage completion was not recorded.")
        else:
            coverage = stage.get("coverage") or {}
            limitations = []
            for reason in coverage.get("limitations", []):
                if _SCOPE_NOTE.fullmatch(reason.strip()):
                    if reason not in notes:
                        notes.append(reason)
                elif name == "verifier" and reason == concept_reason:
                    issue(name, "context", reason)
                else:
                    limitations.append(reason)
                    issue(name, category, reason)
            status = coverage.get("status", "unknown")
            if limitations:
                status = "partial"
            elif coverage.get("limitations"):
                # The stage's partial flag came solely from expected scope or
                # advisory concept evidence. Its original response is retained.
                status = "complete"
            elif status == "partial":
                issue(name, category, "Stage reported incomplete evidence without identifying the missing evidence.")
            if name == "verifier":
                for error in stage.get("validation_errors", []):
                    if not any(error["error"] in item["reason"] for item in issues
                               if item["stage"] == name and item["category"] == category):
                        issue(name, category, "Finding withheld: " + error["error"])
                    status = "partial"
                for decision in debug.get("decisions", []):
                    if decision.get("disposition") == "unresolved":
                        if not any(decision["reason"] in item["reason"] for item in issues
                                   if item["stage"] == name and item["category"] == category):
                            issue(name, category, "Unresolved candidate: " + decision["reason"])
                        status = "partial"
        if category == "context":
            contexts.append(status)
        elif category == "verification":
            verification = status
        else:
            # Failed discovery reduces inspection coverage, but only failed
            # verification prevents us from publishing verified findings.
            inspections.append("partial" if status == "failed" else status)

    if concept_reason:
        issue("verifier", "context", concept_reason)
        contexts.append("partial")
    limitations = [f"{item['stage']}: {item['reason']}" for item in issues
                   if item["category"] != "context"]
    context_limits = [f"{item['stage']}: {item['reason']}" for item in issues
                      if item["category"] == "context"]
    verification_limits = [item["reason"] for item in issues
                           if item["category"] == "verification"]
    return {
        "status": _status([*inspections, verification]),
        "limitations": list(dict.fromkeys(limitations)),
        "context": {"status": _status(contexts), "limitations": context_limits},
        "verification": {"status": verification, "limitations": verification_limits},
        "issues": issues,
        "notes": notes,
    }
