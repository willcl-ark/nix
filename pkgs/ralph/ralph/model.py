"""OpenAI response requests and bounded model tool calls."""

import hashlib
import json
import subprocess
import time
import urllib.error
import urllib.request

from . import research, upstream
from .repository import (
    find_paths,
    read_file,
    read_diff,
    search_code,
    blame_base,
    read_commit,
)
from .spend import BudgetExceeded, web_search_calls

MAX_TOOL_CALLS = 48
MAX_OUTPUT_TOKENS = 6_000
MAX_AUDIT_OUTPUT_TOKENS = 4_000
MAX_AUDIT_OUTPUT_BYTES = 4_000
MAX_COLLATOR_OUTPUT_BYTES = 10_000
MAX_CONTEXT_CALLS = 8
MAX_HISTORY_CALLS = 8
MAX_WEB_SEARCH_CALLS_PER_RESPONSE = 2
SAFE_NO_CHARGE_HTTP_STATUSES = {400, 401, 403, 404, 429}
WEB_SEARCH_TOOL_TYPES = {"web_search"}
WEB_SEARCH_TOOL = {"type": "web_search", "search_context_size": "low"}
BACKPORT_TOOLS = [
    {
        "type": "function",
        "name": "read_upstream_commit",
        "strict": True,
        "description": "Read the message and bounded patch of an original source commit, "
        "including an omitted commit listed in backport evidence. Use source file tools "
        "for additional context when the patch is truncated.",
        "parameters": {
            "type": "object",
            "properties": {"commit": {"type": "string"}},
            "required": ["commit"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "read_upstream_file",
        "strict": True,
        "description": "Read a tracked file at a resolved original backport commit, "
        "or its parent with before=true. Use full original SHAs from backport evidence.",
        "parameters": {
            "type": "object",
            "properties": {
                "commit": {"type": "string"},
                "before": {"type": "boolean"},
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
            },
            "required": ["commit", "before", "path", "start_line"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "search_upstream_code",
        "strict": True,
        "description": "Search tracked source at a resolved original backport commit "
        "or its parent with before=true. Use a literal query and a full evidence SHA.",
        "parameters": {
            "type": "object",
            "properties": {
                "commit": {"type": "string"},
                "before": {"type": "boolean"},
                "query": {"type": "string"},
            },
            "required": ["commit", "before", "query"],
            "additionalProperties": False,
        },
    },
]
TOOLS = [
    {
        "type": "function",
        "name": "find_paths",
        "strict": True,
        "description": "Find tracked file paths at the PR head containing a case-insensitive "
        "substring. Use when you do not know a file's exact path.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Filename or path substring"}
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "read_file",
        "strict": True,
        "description": "Read numbered lines from a tracked text file at the PR head. "
        "Returns up to 300 lines and 12,000 UTF-8 bytes. Use another call for later lines.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repository-relative file path",
                },
                "start_line": {
                    "type": "integer",
                    "description": "First line, starting at 1",
                },
            },
            "required": ["path", "start_line"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "read_base_file",
        "strict": True,
        "description": "Read numbered lines from a tracked text file at the comparison base. "
        "Use to compare behavior before the PR.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repository-relative file path at the comparison base",
                },
                "start_line": {
                    "type": "integer",
                    "description": "First line, starting at 1",
                },
            },
            "required": ["path", "start_line"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "read_diff",
        "strict": True,
        "description": "Read numbered lines from one changed file's PR diff. "
        "Use for large patches or to revisit a specific change. "
        "Line numbers count lines in the diff, not the source file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Changed repository-relative file path",
                },
                "start_line": {
                    "type": "integer",
                    "description": "First diff line, starting at 1",
                },
            },
            "required": ["path", "start_line"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "search_code",
        "strict": True,
        "description": "Search tracked text files at the PR head for a literal string. "
        "Use to find definitions, callers, tests, and conventions.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Literal code or path fragment",
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "search_discussions",
        "strict": True,
        "description": "Search this repository's other issues and PRs for a specific "
        "term. The current PR is excluded. Open a relevant result "
        "with read_discussion.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Specific term or phrase"}
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "read_discussion",
        "strict": True,
        "description": "Read another same-repository issue or PR by number, "
        "including its title, description, and a small sample of "
        "ordinary comments. The current PR is excluded.",
        "parameters": {
            "type": "object",
            "properties": {
                "number": {"type": "integer", "description": "Issue or PR number"}
            },
            "required": ["number"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "blame_base",
        "strict": True,
        "description": "Trace up to 20 lines of an existing tracked file at the PR merge "
        "base to the last commits that changed them.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repository-relative file path",
                },
                "start_line": {
                    "type": "integer",
                    "description": "First line, starting at 1",
                },
            },
            "required": ["path", "start_line"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "read_commit",
        "strict": True,
        "description": "Read an ancestor commit's message and bounded diff for one "
        "tracked file at the comparison base.",
        "parameters": {
            "type": "object",
            "properties": {
                "commit": {
                    "type": "string",
                    "description": "Full commit SHA from blame_base",
                },
                "path": {
                    "type": "string",
                    "description": "Repository-relative file path",
                },
            },
            "required": ["commit", "path"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "find_base_paths",
        "strict": True,
        "description": "Find tracked file paths at the pinned comparison base, before the change.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "search_base_code",
        "strict": True,
        "description": "Search tracked text at the pinned comparison base for a literal string.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
]
PR_DISCUSSION_TOOLS = [
    {
        "type": "function",
        "name": "read_current_pr_discussion",
        "strict": True,
        "description": "Read the current PR's title, description, ordinary comments, "
        "inline comments, and pull review summaries. The page argument "
        "bounds pagination from 1 to 1000. For inline comments, first "
        "read reviews, then pass the review id. Use only for conceptual history "
        "or verifying a conceptual assessment; do not use as code evidence.",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["comments", "inline", "reviews"]},
                "page": {"type": "integer", "description": "Page to fetch, 1 to 1000"},
                "review_id": {
                    "type": "integer",
                    "description": "Forgejo review id for inline comments, otherwise 0",
                },
            },
            "required": ["kind", "page", "review_id"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "read_github_discussion",
        "strict": True,
        "description": "Read a public GitHub pull request or issue discussion, "
        "including ordinary comments, inline review comments, and "
        "review summaries. Use for original upstream PR threads linked "
        "from mirrored reviews. The page argument bounds pagination from 1 to 1000.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Public GitHub pull or issue URL",
                },
                "kind": {"type": "string", "enum": ["comments", "inline", "reviews"]},
                "page": {"type": "integer", "description": "Page to fetch, 1 to 1000"},
            },
            "required": ["url", "kind", "page"],
            "additionalProperties": False,
        },
    },
]
FOCUSED_TOOLS = TOOLS[:5]
BASE_TOOLS = [
    tool
    for tool in TOOLS
    if tool["name"] in {"find_base_paths", "read_base_file", "search_base_code"}
]
ARCHAEOLOGY_TOOLS = list(TOOLS) + PR_DISCUSSION_TOOLS + [WEB_SEARCH_TOOL]
VERIFIER_TOOLS = list(TOOLS) + PR_DISCUSSION_TOOLS + [WEB_SEARCH_TOOL]
LIVE_RESEARCH_TOOL_NAMES = {
    "search_discussions",
    "read_discussion",
    "read_current_pr_discussion",
    "read_github_discussion",
}
FINISH_WITH_AVAILABLE_EVIDENCE = (
    "Budget is near the review limit. Do not call tools. Finish now with the "
    "evidence already available, and preserve uncertainty for any claim that "
    "still lacks decisive support."
)


def available_tools(
    tools, discussion_lookup=None, research_evidence=None, allow_web_search=False
):
    """Frozen research may expose discussion readers, but never live web search."""
    return [
        tool
        for tool in tools
        if not (
            tool.get("type") in WEB_SEARCH_TOOL_TYPES
            and (not allow_web_search or research_evidence is not None)
        )
        and not (
            tool.get("name") in LIVE_RESEARCH_TOOL_NAMES
            and discussion_lookup is None
            and research_evidence is None
        )
    ]


class StaleReview(Exception):
    """The review no longer targets the current pull request head."""


def _request_payload(data):
    # Codex rejects this field. Retain it only for search cost reservations.
    return json.dumps({k: v for k, v in data.items() if k != "max_tool_calls"}).encode()


def request_response(
    api_key,
    data,
    stage_name,
    on_response=None,
    budget=None,
    is_current=None,
    api_base="https://api.openai.com/v1",
    timeout=180,
):
    if is_current is not None and not is_current():
        raise StaleReview("review head changed before model request")
    payload = _request_payload(data)
    token = budget.reserve(stage_name, data) if budget is not None else None
    request = urllib.request.Request(
        f"{api_base.rstrip('/')}/responses",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
        if not isinstance(result, dict):
            raise ValueError("OpenAI response was not a JSON object")
    except urllib.error.HTTPError as exc:
        if budget is not None:
            budget.fail(
                token, charged_unknown=exc.code not in SAFE_NO_CHARGE_HTTP_STATUSES
            )
        raise
    except Exception:
        if budget is not None:
            budget.fail(token, charged_unknown=True)
        raise
    elapsed = round(time.monotonic() - started, 2)
    if budget is not None:
        budget.settle(token, result)
    if on_response is not None:
        on_response(stage_name, payload, result)
    return result, payload, elapsed


def response_record(result, payload, elapsed):
    usage = result.get("usage") or {}
    details = usage.get("input_tokens_details") or {}
    cache_write_tokens = details.get("cache_write_tokens")
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    return {
        "model": result.get("model"),
        "response_id": result.get("id"),
        "status": result.get("status"),
        "error": result.get("error"),
        "request_bytes": len(payload),
        "request_sha256": hashlib.sha256(payload).hexdigest(),
        "response_output_sha256": hashlib.sha256(
            json.dumps(result.get("output", [])).encode()
        ).hexdigest(),
        "elapsed_seconds": elapsed,
        "input_tokens": input_tokens,
        "cached_tokens": details.get("cached_tokens", 0),
        "cache_write_tokens": cache_write_tokens,
        "output_tokens": output_tokens,
        "usage_known": input_tokens is not None and output_tokens is not None,
        "usage_complete": (
            input_tokens is not None
            and output_tokens is not None
            and cache_write_tokens is not None
        ),
        "web_search_calls": web_search_calls(result),
        "reasoning_tokens": (usage.get("output_tokens_details") or {}).get(
            "reasoning_tokens"
        ),
    }


def request_failure_record(data, error):
    payload = _request_payload(data)
    record = {
        "model": data.get("model"),
        "response_id": None,
        "status": "failed",
        "request_bytes": len(payload),
        "request_sha256": hashlib.sha256(payload).hexdigest(),
        "response_output_sha256": None,
        "elapsed_seconds": None,
        "input_tokens": None,
        "cached_tokens": None,
        "cache_write_tokens": None,
        "output_tokens": None,
        "usage_known": False,
        "usage_complete": False,
        "reasoning_tokens": None,
        "error": type(error).__name__,
    }
    if isinstance(error, urllib.error.HTTPError):
        record["http_status"] = error.code
    return record


def _stage_record(debug, name, model):
    debug.setdefault("name", name)
    debug.setdefault("model", model)
    debug.setdefault("status", "pending")
    debug.setdefault("turns", [])
    debug.setdefault("tools", [])
    debug.setdefault("raw_output", "")
    return debug


def output_text(output):
    return "\n".join(
        part["text"]
        for item in output
        if item.get("type") == "message"
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    )


def hosted_search_outputs(result):
    return [
        item
        for item in result.get("output", [])
        if isinstance(item, dict) and item.get("type") == "web_search_call"
    ]


def _limit_metrics(**limits):
    return {
        name: {
            "limit": limit,
            "used": None if name == "output_tokens" else 0,
            "hit": None if name == "web_search_calls_per_response" else False,
        }
        for name, limit in limits.items()
    }


def _record_response_limits(limits, result):
    output = limits["output_tokens"]
    used = (result.get("usage") or {}).get("output_tokens")
    if type(used) is int and used >= 0:
        output["used"] = max(output["used"] or 0, used)
    if (
        result.get("status") == "incomplete"
        and (result.get("incomplete_details") or {}).get("reason")
        == "max_output_tokens"
    ):
        output["hit"] = True
    hosted_calls = len(hosted_search_outputs(result))
    if "web_search_calls_per_response" in limits:
        web = limits["web_search_calls_per_response"]
        web["used"] = max(web["used"], hosted_calls)
    if "tool_calls" in limits:
        limits["tool_calls"]["used"] += hosted_calls


def run_audit(
    api_key,
    name,
    prompt,
    review,
    *,
    model_name,
    max_output_tokens,
    on_response=None,
    budget=None,
    response_schema=None,
    reasoning_effort="low",
    is_current=None,
    debug=None,
    api_base="https://api.openai.com/v1",
    timeout=180,
):
    model = model_name
    limits = _limit_metrics(output_tokens=max_output_tokens)
    if debug is not None:
        debug.update(limits=limits, budget_limited=False)
    data = {
        "model": model,
        "store": False,
        "reasoning": {"effort": reasoning_effort},
        "instructions": prompt,
        "input": [{"role": "user", "content": review}],
        "max_output_tokens": max_output_tokens,
    }
    if response_schema is not None:
        data["text"] = {
            "format": {
                "type": "json_schema",
                "name": name,
                "strict": True,
                "schema": response_schema,
            }
        }
    try:
        result, payload, elapsed = request_response(
            api_key,
            data,
            name,
            on_response,
            budget,
            is_current,
            api_base=api_base,
            timeout=timeout,
        )
    except StaleReview:
        if debug is not None:
            debug.update(status="stale", error="StaleReview")
        raise
    except BudgetExceeded:
        if debug is not None:
            debug.update(
                status="budget_exhausted", error="BudgetExceeded", budget_limited=True
            )
        raise
    except Exception as exc:
        failure = request_failure_record(data, exc)
        if debug is not None:
            stage = _stage_record(debug, name, model)
            stage["status"] = "failed"
            stage["error"] = failure["error"]
            stage["turns"].append(failure)
        raise
    _record_response_limits(limits, result)
    answer = output_text(result.get("output", []))
    status = result.get("status")
    record = {
        "name": name,
        "model": model,
        "status": status,
        "incomplete_reason": (result.get("incomplete_details") or {}).get("reason"),
        **response_record(result, payload, elapsed),
        "raw_output": answer,
        "output_truncated": False,
        "limits": limits,
        "budget_limited": False,
    }
    if debug is not None:
        stage = _stage_record(debug, name, model)
        stage.update(
            {
                "status": status,
                "raw_output": answer,
                "incomplete_reason": record["incomplete_reason"],
            }
        )
        stage["turns"].append(record.copy())
    if status != "completed" or not answer.strip():
        if status == "completed":
            record["status"] = "empty"
            record["error"] = "empty response"
            if debug is not None:
                stage["status"] = "empty"
        return answer, record
    return answer, record


def openai_review(
    api_key,
    review,
    snapshot,
    debug=None,
    *,
    prompt,
    model,
    max_output_tokens,
    tools=None,
    max_tool_calls=MAX_TOOL_CALLS,
    max_context_calls=MAX_CONTEXT_CALLS,
    max_history_calls=MAX_HISTORY_CALLS,
    max_web_search_calls_per_response=MAX_WEB_SEARCH_CALLS_PER_RESPONSE,
    first_tool_required=True,
    stage_name="independent",
    on_response=None,
    budget=None,
    response_schema=None,
    reasoning_effort="low",
    discussion_lookup=None,
    allow_web_search=False,
    is_current=None,
    api_base="https://api.openai.com/v1",
    research_evidence=None,
    backport_evidence=None,
    timeout=180,
    final_timeout=180,
):
    tools = available_tools(
        TOOLS if tools is None else tools,
        discussion_lookup,
        research_evidence,
        allow_web_search,
    )
    allowed_tools = {tool["name"] for tool in tools if tool.get("type") == "function"}
    limits = _limit_metrics(output_tokens=max_output_tokens, tool_calls=max_tool_calls)
    if allowed_tools & LIVE_RESEARCH_TOOL_NAMES:
        limits.update(_limit_metrics(context_calls=max_context_calls))
    if allowed_tools & {"blame_base", "read_commit"}:
        limits.update(_limit_metrics(history_calls=max_history_calls))
    if any(tool.get("type") in WEB_SEARCH_TOOL_TYPES for tool in tools):
        limits.update(
            _limit_metrics(
                web_search_calls_per_response=max_web_search_calls_per_response
            )
        )
    checkout = snapshot.checkout
    files = snapshot.head_files
    inputs = [{"role": "user", "content": review}]
    if debug is not None:
        review_bytes = review.encode()
        debug.update(
            {
                "limits": limits,
                "budget_limited": False,
                "instructions": prompt,
                "review_input_bytes": len(review_bytes),
                "review_input_sha256": hashlib.sha256(review_bytes).hexdigest(),
                "turns": [],
                "tools": [],
                "max_tool_calls": max_tool_calls,
                "reasoning_effort": reasoning_effort,
                "max_output_tokens": max_output_tokens,
            }
        )
        stage_debug = _stage_record(debug, stage_name, model)
    else:
        stage_debug = None
    request_options = (
        {"api_base": api_base} if api_base != "https://api.openai.com/v1" else {}
    )
    calls_used = 0
    context_calls = 0
    history_calls = 0
    # Allow one inspection per turn, plus a final answer without tools.
    for turn in range(max_tool_calls + 1):
        input_data = json.dumps(inputs).encode()
        tool_choice = (
            "none"
            if calls_used >= max_tool_calls or turn == max_tool_calls
            else "required"
            if turn == 0 and first_tool_required and not model.startswith("claude-")
            else "auto"
        )
        data = {
            "model": model,
            "store": False,
            "reasoning": {"effort": reasoning_effort},
            "instructions": prompt,
            "input": inputs,
            "tools": tools,
            "tool_choice": tool_choice,
            "max_output_tokens": max_output_tokens,
        }
        if tool_choice != "none" and any(
            tool.get("type") in WEB_SEARCH_TOOL_TYPES for tool in tools
        ):
            per_response_search_limit = min(
                max_web_search_calls_per_response, max(1, max_tool_calls - calls_used)
            )
            data["max_tool_calls"] = per_response_search_limit
            data["include"] = ["web_search_call.action.sources"]
        if response_schema is not None:
            data["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": stage_name,
                    "strict": True,
                    "schema": response_schema,
                }
            }
        sent_tool_choice = tool_choice
        budget_forced_final = False
        try:
            try:
                result, payload, elapsed = request_response(
                    api_key,
                    data,
                    stage_name,
                    on_response,
                    budget,
                    is_current,
                    timeout=(final_timeout if tool_choice == "none" else timeout),
                    **request_options,
                )
            except BudgetExceeded:
                if stage_debug is not None:
                    stage_debug["budget_limited"] = True
                if tool_choice == "none" or (first_tool_required and calls_used == 0):
                    raise
                final_inputs = inputs + [
                    {"role": "user", "content": FINISH_WITH_AVAILABLE_EVIDENCE}
                ]
                final_tools = [
                    tool
                    for tool in tools
                    if tool.get("type") not in WEB_SEARCH_TOOL_TYPES
                ]
                data = {
                    **data,
                    "input": final_inputs,
                    "tool_choice": "none",
                    "tools": final_tools,
                }
                data.pop("max_tool_calls", None)
                data.pop("include", None)
                input_data = json.dumps(final_inputs).encode()
                sent_tool_choice = "none"
                budget_forced_final = True
                result, payload, elapsed = request_response(
                    api_key,
                    data,
                    stage_name,
                    on_response,
                    budget,
                    is_current,
                    timeout=final_timeout,
                    **request_options,
                )
                inputs = final_inputs
        except StaleReview:
            if stage_debug is not None:
                stage_debug.update(status="stale", error="StaleReview")
            raise
        except BudgetExceeded:
            if stage_debug is not None:
                stage_debug.update(
                    status="budget_exhausted",
                    error="BudgetExceeded",
                    budget_limited=True,
                )
            raise
        except Exception as exc:
            failure = request_failure_record(data, exc)
            if debug is not None:
                debug["turns"].append(failure)
                stage_debug["status"] = "failed"
                stage_debug["error"] = failure["error"]
            raise
        if tool_choice == "none" and tools:
            limits["tool_calls"]["hit"] = True
        _record_response_limits(limits, result)
        if debug is not None:
            turn_record = {
                **response_record(result, payload, elapsed),
                "input_bytes": len(input_data),
                "input_sha256": hashlib.sha256(input_data).hexdigest(),
                "tool_choice": sent_tool_choice,
                "incomplete_reason": (result.get("incomplete_details") or {}).get(
                    "reason"
                ),
            }
            if budget_forced_final:
                turn_record["budget_forced_final"] = True
            debug["turns"].append(turn_record)
            stage_debug["status"] = result.get("status")
        if result.get("status") != "completed":
            raw = output_text(result.get("output", []))
            if stage_debug is not None:
                stage_debug["raw_output"] = raw
                stage_debug["incomplete_reason"] = (
                    result.get("incomplete_details") or {}
                ).get("reason")
            raise ValueError("OpenAI response did not complete")
        output = result.get("output", [])
        hosted_search_items = hosted_search_outputs(result)
        calls_used += len(hosted_search_items)
        if debug is not None:
            for item in hosted_search_items:
                action = (
                    item.get("action") if isinstance(item.get("action"), dict) else {}
                )
                tool_record = {
                    "name": "web_search",
                    "arguments": json.dumps(action, sort_keys=True)[:160],
                    "arguments_sha256": hashlib.sha256(
                        json.dumps(action, sort_keys=True).encode()
                    ).hexdigest(),
                    "output_bytes": 0,
                    "output_sha256": hashlib.sha256(b"").hexdigest(),
                    "hosted": True,
                    "action_type": str(action.get("type") or "unknown")[:100],
                    "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                sources = action.get("sources")
                if isinstance(sources, list):
                    normalized = []
                    for source in sources[:10]:
                        if isinstance(source, str):
                            normalized.append({"url": source})
                        elif isinstance(source, dict):
                            record = {
                                key: source[key]
                                for key in ("type", "url", "title")
                                if isinstance(source.get(key), str)
                            }
                            if record:
                                normalized.append(record)
                    if normalized:
                        tool_record["sources"] = normalized
                debug["tools"].append(tool_record)
        calls = [item for item in output if item.get("type") == "function_call"]
        if calls:
            inputs.extend(output)
            for call in calls:
                skipped = calls_used >= max_tool_calls
                if skipped:
                    limits["tool_calls"]["hit"] = True
                    answer = "Inspection limit reached. Finish with evidence already available."
                else:
                    calls_used += 1
                    limits["tool_calls"]["used"] += 1
                    try:
                        args = json.loads(call["arguments"])
                        if call.get("name") not in allowed_tools:
                            answer = "Unknown or unavailable tool."
                        elif call["name"] == "find_paths":
                            answer = find_paths(files, args.get("query"))
                        elif call["name"] == "find_base_paths":
                            answer = find_paths(snapshot.base_files, args.get("query"))
                        elif call["name"] == "read_file":
                            answer = read_file(
                                checkout,
                                files,
                                args.get("path"),
                                args.get("start_line"),
                            )
                        elif call["name"] == "read_base_file":
                            answer = read_file(
                                checkout,
                                snapshot.base_files,
                                args.get("path"),
                                args.get("start_line"),
                            )
                        elif call["name"] == "read_diff":
                            answer = read_diff(
                                checkout,
                                snapshot.changed_paths,
                                snapshot.diff_base_sha,
                                snapshot.head_sha,
                                args.get("path"),
                                args.get("start_line"),
                            )
                        elif call["name"] == "search_code":
                            answer = search_code(
                                checkout, snapshot.head_sha, args.get("query")
                            )
                        elif call["name"] == "search_base_code":
                            answer = search_code(
                                checkout, snapshot.diff_base_sha, args.get("query")
                            )
                        elif call["name"] in {
                            "read_upstream_commit",
                            "read_upstream_file",
                            "search_upstream_code",
                        }:
                            answer = upstream.lookup(
                                checkout, backport_evidence, call["name"], args
                            )
                        elif call["name"] in LIVE_RESEARCH_TOOL_NAMES:
                            if context_calls >= max_context_calls:
                                limits["context_calls"]["hit"] = True
                                answer = "Discussion lookup limit reached."
                            else:
                                context_calls += 1
                                limits["context_calls"]["used"] = context_calls
                                if research_evidence is not None:
                                    answer = research.lookup(
                                        research_evidence, call["name"], args
                                    )
                                elif discussion_lookup is not None:
                                    answer = discussion_lookup(call["name"], args)
                                else:
                                    answer = "Discussion lookup unavailable."
                        elif call["name"] in {"blame_base", "read_commit"}:
                            if history_calls >= max_history_calls:
                                limits["history_calls"]["hit"] = True
                                answer = "History lookup limit reached."
                            else:
                                history_calls += 1
                                limits["history_calls"]["used"] = history_calls
                                if call["name"] == "blame_base":
                                    answer = blame_base(
                                        checkout,
                                        snapshot.base_files,
                                        snapshot.diff_base_sha,
                                        args.get("path"),
                                        args.get("start_line"),
                                    )
                                else:
                                    answer = read_commit(
                                        checkout,
                                        snapshot.base_files,
                                        snapshot.diff_base_sha,
                                        args.get("commit"),
                                        args.get("path"),
                                    )
                        else:
                            answer = "Unknown tool."
                    except (KeyError, TypeError, ValueError):
                        answer = "Invalid tool arguments."
                    except urllib.error.HTTPError as exc:
                        answer = f"Discussion lookup returned HTTP {exc.code}."
                    except (
                        urllib.error.URLError,
                        TimeoutError,
                        subprocess.CalledProcessError,
                    ):
                        answer = (
                            "Context lookup failed; continue with available evidence."
                        )
                inputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": answer,
                    }
                )
                if debug is not None:
                    arguments = str(call.get("arguments", ""))
                    tool_record = {
                        "name": str(call.get("name", ""))[:100],
                        "arguments": arguments[:160],
                        "arguments_sha256": hashlib.sha256(
                            arguments.encode()
                        ).hexdigest(),
                        "output_bytes": len(answer.encode()),
                        "output_sha256": hashlib.sha256(answer.encode()).hexdigest(),
                    }
                    if call.get("name") in LIVE_RESEARCH_TOOL_NAMES and not skipped:
                        if research_evidence is not None:
                            tool_record["frozen"] = True
                        else:
                            tool_record["retrieved_at"] = time.strftime(
                                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                            )
                    if skipped:
                        tool_record["skipped"] = "inspection_limit"
                    debug["tools"].append(tool_record)
            continue
        text = output_text(output)
        if hosted_search_items and not text.strip():
            inputs.extend(output)
            continue
        if stage_debug is not None:
            stage_debug["raw_output"] = text
        if not text.strip():
            if stage_debug is not None:
                stage_debug["status"] = "empty"
                stage_debug["error"] = "OpenAI response contained no review text"
            raise ValueError("OpenAI response contained no review text")
        if first_tool_required and tools and max_tool_calls > 0 and calls_used == 0:
            raise ValueError("Review returned without inspecting repository evidence")
        return text
    raise ValueError("OpenAI review exceeded model turn limit")
