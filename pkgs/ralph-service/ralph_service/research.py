"""Capture remote discussion evidence for offline review."""
from datetime import datetime, timezone
from . import forgejo
from ralph.research import (SCHEMA_VERSION, _args, _entry_key, _parse_time,
                            validate)

def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z")


def _capture_call(bot_config, current_pr, request, cutoff, captured_at):
    if not isinstance(request, dict):
        raise ValueError("research request must be an object")
    name = request.get("name")
    arguments = _args(name, request.get("arguments", {}))
    if name == "search_discussions":
        output = forgejo.search_discussions(
            bot_config, arguments["query"], current_pr, cutoff=cutoff)
    elif name == "read_discussion":
        output = forgejo.read_discussion(
            bot_config, arguments["number"], current_pr, cutoff=cutoff)
    elif name == "read_current_pr_discussion":
        output = forgejo.read_current_pr_discussion(
            bot_config, current_pr, arguments["kind"], arguments["page"],
            arguments["review_id"], cutoff=cutoff)
    elif name == "read_github_discussion":
        output = forgejo.read_github_discussion(
            bot_config, arguments["url"], arguments["kind"], arguments["page"],
            cutoff=cutoff)
    else:
        raise ValueError(f"unsupported research tool: {name}")
    return {"key": _entry_key(name, arguments), "name": name,
            "arguments": arguments, "output": output,
            "captured_at": captured_at, "cutoff": cutoff}


def capture(bot_config, current_pr, requests=None, cutoff=None, captured_at=None):
    captured_at = captured_at or utc_now()
    cutoff = cutoff or captured_at
    captured = _parse_time(captured_at)
    cutoff_time = _parse_time(cutoff)
    if captured is None or cutoff_time is None:
        raise ValueError("Frozen research evidence timestamps are invalid")
    if cutoff_time > captured:
        raise ValueError("Frozen research evidence cutoff is after capture time")
    requests = [
        {"name": "read_current_pr_discussion",
         "arguments": {"kind": "comments", "page": 1, "review_id": 0}},
        {"name": "read_current_pr_discussion",
         "arguments": {"kind": "reviews", "page": 1, "review_id": 0}},
        *list(requests or []),
    ]
    evidence = {"schema_version": SCHEMA_VERSION, "captured_at": captured_at,
                "cutoff": cutoff, "entries": []}
    seen = set()
    for request in requests:
        entry = _capture_call(bot_config, current_pr, request, cutoff, captured_at)
        if entry["key"] in seen:
            continue
        seen.add(entry["key"])
        evidence["entries"].append(entry)
    return validate(evidence)


def discussion_lookup(bot_config, current_pr):
    """Bind live discussion tools to this service's PR context."""
    def lookup(name, arguments):
        return _capture_call(bot_config, current_pr,
                             {"name": name, "arguments": arguments},
                             None, None)["output"]
    return lookup
