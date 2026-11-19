"""Classify explicit provider refusals without interpreting review prose."""

KINDS = {"cyber_refusal", "provider_refusal"}


def failure_kind(record):
    kind = record.get("failure_kind")
    if kind in KINDS:
        return kind
    error = record.get("error")
    error = error if isinstance(error, dict) else {}
    messages = [record.get("error_message"), error.get("message")]
    refused = any(
        error.get(key) in {"refusal", "content_policy_violation", "content_filter"}
        for key in ("code", "type")
    )
    for item in record.get("output") or []:
        if not isinstance(item, dict):
            continue
        for part in [item, *(item.get("content") or [])]:
            if isinstance(part, dict) and part.get("type") == "refusal":
                refused = True
                messages.append(part.get("refusal"))
    if any(
        isinstance(message, str)
        and "flagged for possible cybersecurity risk" in message.casefold()
        for message in messages
    ):
        return "cyber_refusal"
    if refused:
        return "provider_refusal"
    # Older records retained terminal provider errors on turns, not stages.
    kinds = {
        failure_kind(
            {
                key: turn[key]
                for key in ("failure_kind", "error", "error_message")
                if key in turn
            }
        )
        for turn in record.get("turns") or []
        if isinstance(turn, dict) and turn.get("status") in {"failed", "invalid"}
    }
    if "cyber_refusal" in kinds:
        return "cyber_refusal"
    return "provider_refusal" if "provider_refusal" in kinds else None


def description(kind):
    if kind == "cyber_refusal":
        return "Provider declined the request because of a cybersecurity restriction."
    return "Provider declined the request."
