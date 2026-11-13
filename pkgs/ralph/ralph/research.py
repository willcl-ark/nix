"""Frozen discussion evidence for offline review replay."""

from datetime import datetime
import hashlib
import json
import urllib.parse

from .evidence import ToolFailure

SCHEMA_VERSION = 2
MISSING_RESEARCH = (
    "Frozen research evidence is unavailable for this tool call. Continue "
    "without live discussion or web lookup."
)


def _parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _canonical(value):
    return json.loads(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    )


def _entry_key(name, arguments):
    payload = json.dumps(
        [name, _canonical(arguments)],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _args(name, arguments):
    if not isinstance(arguments, dict):
        raise ValueError("research tool arguments must be an object")
    if name == "bkb_search":
        return {
            "query": arguments.get("query"),
            "source_type": arguments.get("source_type", ""),
            "source_repo": arguments.get("source_repo", ""),
            "limit": arguments.get("limit", 5),
        }
    if name == "bkb_get_document":
        return {"id": arguments.get("id")}
    if name == "search_discussions":
        return {"query": arguments.get("query")}
    if name == "read_discussion":
        return {"number": arguments.get("number")}
    if name == "read_current_pr_discussion":
        return {
            "kind": arguments.get("kind", "comments"),
            "page": arguments.get("page", 1),
            "review_id": arguments.get("review_id", 0),
        }
    if name == "read_github_discussion":
        return {
            "url": arguments.get("url"),
            "kind": arguments.get("kind", "comments"),
            "page": arguments.get("page", 1),
        }
    raise ValueError(f"unsupported research tool: {name}")


def validate(evidence):
    if evidence is None:
        return None
    if not isinstance(evidence, dict):
        raise ValueError("Frozen research evidence must be an object")
    if set(evidence) != {"schema_version", "captured_at", "cutoff", "entries"}:
        raise ValueError("Frozen research evidence has unexpected fields")
    if evidence["schema_version"] != SCHEMA_VERSION:
        raise ValueError("Unsupported frozen research evidence schema")
    captured = _parse_time(evidence["captured_at"])
    cutoff = _parse_time(evidence["cutoff"])
    if captured is None or cutoff is None:
        raise ValueError("Frozen research evidence timestamps are invalid")
    if cutoff > captured:
        raise ValueError("Frozen research evidence cutoff is after capture time")
    if not isinstance(evidence["entries"], list):
        raise ValueError("Frozen research evidence entries must be a list")
    seen = set()
    for entry in evidence["entries"]:
        if not isinstance(entry, dict):
            raise ValueError("Frozen research evidence entry must be an object")
        if set(entry) != {
            "key",
            "name",
            "arguments",
            "output",
            "outcome",
            "captured_at",
            "cutoff",
        }:
            raise ValueError("Frozen research evidence entry has unexpected fields")
        name = entry["name"]
        if not isinstance(name, str):
            raise ValueError("Frozen research evidence tool name is invalid")
        arguments = _args(name, entry["arguments"])
        if entry["arguments"] != arguments:
            raise ValueError("Frozen research evidence arguments are not canonical")
        if entry["outcome"] not in {"success", "unavailable", "invalid_request"}:
            raise ValueError("Invalid frozen evidence outcome")
        if not isinstance(entry["output"], str):
            raise ValueError("Frozen research evidence output must be text")
        if (
            entry["captured_at"] != evidence["captured_at"]
            or entry["cutoff"] != evidence["cutoff"]
        ):
            raise ValueError("Frozen research evidence entry timestamps differ")
        key = _entry_key(name, arguments)
        if entry["key"] != key:
            raise ValueError("Frozen research evidence key mismatch")
        if key in seen:
            raise ValueError("Frozen research evidence contains duplicate tool calls")
        seen.add(key)
    return evidence


def lookup(evidence, name, args):
    evidence = validate(evidence)
    if evidence is None:
        raise ToolFailure("unavailable", MISSING_RESEARCH)
    try:
        arguments = _args(name, args)
    except ValueError as exc:
        raise ToolFailure("invalid_request", str(exc)) from exc
    key = _entry_key(name, arguments)
    for entry in evidence["entries"]:
        if entry["key"] == key:
            if entry["outcome"] != "success":
                raise ToolFailure(entry["outcome"], entry["output"])
            return entry["output"]
    raise ToolFailure("unavailable", MISSING_RESEARCH)


def inventory(evidence):
    evidence = validate(evidence)
    if evidence is None:
        return "No frozen discussion evidence is available."
    lines = [
        f"Frozen discussion evidence captured at {evidence['captured_at']}.",
        f"Cutoff: {evidence['cutoff']}.",
    ]
    if not evidence["entries"]:
        lines.append("No discussion tool calls were captured.")
    for entry in evidence["entries"]:
        arguments = json.dumps(
            entry["arguments"], sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        lines.append(
            f"- {entry['name']} {arguments}: {len(entry['output'].encode())} bytes, "
            f"key {entry['key'][:12]}"
        )
    return "\n".join(lines)


def _discussion_identity(url):
    if not isinstance(url, str):
        return None
    parsed = urllib.parse.urlsplit(url)
    parts = urllib.parse.unquote(parsed.path).strip("/").split("/")
    if (
        parsed.scheme not in {"http", "https"}
        or len(parts) < 4
        or parts[2] not in {"pull", "pulls", "issues"}
        or not parts[3].isdigit()
    ):
        return None
    return (parsed.hostname, parts[0].lower(), parts[1].lower(), int(parts[3]))


def filter_current_pr(name, args, output, current_url):
    """Exclude current-PR discussion ownership, not links mentioned in passages."""
    current = _discussion_identity(current_url)
    if current is None:
        return output

    def excluded(record):
        if not isinstance(record, dict):
            return False
        if _discussion_identity(record.get("url")) == current:
            return True
        # Github issue and PR records use the shared issue number. Comment IDs
        # are unrelated numbers, so comments must match through their source URL.
        if current[0] == "github.com":
            ids = {
                f"{kind}:{current[1]}/{current[2]}:{current[3]}"
                for kind in ("github_pr", "github_issue")
            }
            if any(
                record.get(key) in ids
                for key in ("id", "matched_source_id", "parent_id")
            ):
                return True
        return False

    def owned(record):
        return excluded(record) or any(
            excluded(origin) for origin in record.get("provenance", [])
        )

    if name == "read_current_pr_discussion" or (
        name == "read_github_discussion"
        and _discussion_identity(args.get("url")) == current
    ):
        raise ToolFailure(
            "invalid_request",
            "Current PR discussion is excluded from historical research.",
        )
    if name not in {"bkb_search", "bkb_get_document"}:
        return output
    try:
        value = json.loads(output)
    except ValueError as exc:
        # A truncated record cannot reliably establish which PR owns it.
        raise ToolFailure(
            "unavailable", "Cannot exclude current PR from truncated BKB evidence."
        ) from exc
    if name == "bkb_get_document":
        if owned(value["document"]) or excluded(args):
            raise ToolFailure(
                "invalid_request",
                "Current PR discussion is excluded from historical research.",
            )
        return output
    results = value["results"]
    value["results"] = [record for record in results if not owned(record)]
    if len(value["results"]) == len(results):
        return output
    value["current_pr_results_omitted"] = len(results) - len(value["results"])
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
