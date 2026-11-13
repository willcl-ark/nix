"""Bounded, read-only access to the Bitcoin Knowledge Base HTTP API."""

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request

from ralph.evidence import ToolFailure
from ralph.repository import MAX_TOOL_BYTES

BASE_URL = "https://bitcoinknowledge.dev"
MAX_RESPONSE_BYTES = 500_000
TIMEOUT_SECONDS = 20


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(path):
    request = urllib.request.Request(
        BASE_URL + path, headers={"Accept": "application/json"}
    )
    try:
        # Do not let redirects turn a fixed public endpoint into arbitrary access.
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            content = response.read(MAX_RESPONSE_BYTES + 1)
        if len(content) > MAX_RESPONSE_BYTES:
            raise ToolFailure("unavailable", "BKB response exceeds the byte limit.")
        return json.loads(content)
    except (OSError, ValueError, RecursionError, http.client.HTTPException) as exc:
        raise ToolFailure(
            "unavailable", "BKB response is unavailable or invalid."
        ) from exc


_FIELDS = (
    "id",
    "url",
    "title",
    "source_type",
    "source_repo",
    "source_id",
    "matched_source_id",
    "author",
    "created_at",
    "updated_at",
)


def _record(value, passage):
    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        raise ToolFailure("unavailable", "BKB returned an invalid document.")
    result = {key: value[key] for key in _FIELDS if key in value}
    result[passage] = value.get(passage)
    return result


def _format(value):
    records = value.get("results", [value.get("document", {})])
    passages = [
        (record, key)
        for record in records
        for key in ("body", "snippet")
        if isinstance(record.get(key), str)
    ]
    while True:
        output = json.dumps(value, ensure_ascii=False, indent=2)
        if len(output.encode()) <= MAX_TOOL_BYTES:
            return output
        record, key = max(
            passages, key=lambda item: len(item[0][item[1]]), default=({}, "body")
        )
        if not record.get(key):
            raise ToolFailure("unavailable", "BKB attribution exceeds the byte limit.")
        record[key] = record[key][: len(record[key]) // 2]
        value["truncated"] = True
        value["limitation"] = "BKB passage truncated; remaining content omitted."


def _text(value, maximum, optional=False):
    return (
        isinstance(value, str)
        and (optional or bool(value.strip()))
        and len(value) <= maximum
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def search(query, source_type="", source_repo="", limit=5):
    if (
        not _text(query, 500)
        or not _text(source_type, 100, optional=True)
        or not _text(source_repo, 200, optional=True)
        or type(limit) is not int
        or not 1 <= limit <= 10
    ):
        raise ToolFailure(
            "invalid_request", "Invalid BKB search arguments; limit must be 1 to 10."
        )
    params = {"q": query, "limit": limit}
    if source_type:
        params["source_type"] = source_type
    if source_repo:
        params["source_repo"] = source_repo
    value = _request("/search?" + urllib.parse.urlencode(params))
    if not isinstance(value, dict) or not isinstance(value.get("results"), list):
        raise ToolFailure("unavailable", "BKB returned invalid search results.")
    results = []
    for item in value["results"][:limit]:
        record = _record(item, "snippet")
        snippet = record.pop("snippet")
        record["provenance"] = item.get("provenance", [])
        record["snippet"] = snippet
        results.append(record)
    return _format(
        {
            "provider": BASE_URL,
            "snapshot": "Current indexed content; not a historical snapshot",
            "total_count": value.get("total_count"),
            "results": results,
            "results_omitted": len(value["results"]) > limit,
        }
    )


def get_document(id):
    if not _text(id, 1000) or id in {".", ".."}:
        raise ToolFailure(
            "invalid_request",
            "BKB document ID must be 1 to 1000 characters on one line.",
        )
    value = _request("/document/" + urllib.parse.quote(id, safe=""))
    if not isinstance(value, dict):
        raise ToolFailure("unavailable", "BKB returned an invalid document.")
    record = _record(value.get("document"), "body")
    record["url"] = value.get("url")
    # Put attribution before the passage so long bodies retain their provenance.
    body = record.pop("body")
    record["provenance"] = value.get("provenance", [])
    record["body"] = body
    return _format(
        {
            "provider": BASE_URL,
            "snapshot": "Current indexed content; not a historical snapshot",
            "document": record,
        }
    )
