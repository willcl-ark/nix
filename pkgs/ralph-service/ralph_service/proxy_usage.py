"""Collect subscription quota without exposing proxy credentials or making inference calls."""

import json
import math
import re
import urllib.request
from datetime import UTC, datetime
from http.client import HTTPException

_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_TRANSPORT_ERRORS = (
    OSError,
    HTTPException,
    ValueError,
    TypeError,
    UnicodeError,
    RecursionError,
    OverflowError,
)
_PLANS = {"free", "plus", "pro", "team", "business", "enterprise", "edu"}


def _request(base_url, management_key, path, data=None):
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v8/management/" + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={
            "Authorization": f"Bearer {management_key}",
            "Content-Type": "application/json",
        },
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read(_MAX_RESPONSE_BYTES + 1)
        if not 200 <= response.status < 300 or len(body) > _MAX_RESPONSE_BYTES:
            raise ValueError("Unavailable management response")
    result = json.loads(body)
    if not isinstance(result, dict):
        raise TypeError("Invalid management response")
    return result


def _percent(value):
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= 100
    ):
        return value
    return None


def _reset(value):
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            parsed = datetime.fromtimestamp(value, UTC)
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                return None
        else:
            return None
        return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return None


def _window(windows, name, used, resets_at):
    used = _percent(used)
    if used is not None:
        windows.append(
            {"name": name, "used_percent": used, "resets_at": _reset(resets_at)}
        )


def _codex_windows(windows, rate_limit, prefix=""):
    if not isinstance(rate_limit, dict):
        return
    for key, fallback in (
        ("primary_window", "Primary"),
        ("secondary_window", "Secondary"),
    ):
        window = rate_limit.get(key)
        if not isinstance(window, dict):
            continue
        name = {18000: "5-hour", 604800: "Weekly"}.get(
            window.get("limit_window_seconds"), fallback
        )
        _window(
            windows, prefix + name, window.get("used_percent"), window.get("reset_at")
        )


def _codex(account, payload):
    plan = payload.get("plan_type")
    account["plan"] = plan if isinstance(plan, str) and plan in _PLANS else None
    windows = account["windows"]
    _codex_windows(windows, payload.get("rate_limit"))
    _codex_windows(windows, payload.get("code_review_rate_limit"), "Code review: ")
    additional = payload.get("additional_rate_limits")
    if isinstance(additional, list):
        for index, limit in enumerate(additional, 1):
            if isinstance(limit, dict):
                _codex_windows(
                    windows, limit.get("rate_limit"), f"Additional quota {index}: "
                )
    credits = payload.get("credits")
    if isinstance(credits, dict) and all(
        isinstance(credits.get(key), bool) for key in ("has_credits", "unlimited")
    ):
        balance = credits.get("balance")
        if isinstance(balance, bool) or not (
            isinstance(balance, (int, float))
            and math.isfinite(balance)
            or isinstance(balance, str)
            and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", balance)
        ):
            balance = None
        account["credits"] = {
            "has_credits": credits["has_credits"],
            "unlimited": credits["unlimited"],
            "balance": balance,
        }


def _claude(account, payload):
    windows = account["windows"]
    present = set()
    limits = payload.get("limits")
    if isinstance(limits, list):
        for index, limit in enumerate(limits, 1):
            if not isinstance(limit, dict):
                continue
            kind = limit.get("kind")
            name = {"session": "5-hour", "weekly_all": "Weekly"}.get(kind)
            if kind == "weekly_scoped":
                name = f"Model quota {index}: Weekly"
                scope = limit.get("scope")
                model = scope.get("model") if isinstance(scope, dict) else None
                if isinstance(model, dict):
                    model_id = model.get("id")
                    if isinstance(model_id, str):
                        for family in ("sonnet", "opus", "fable"):
                            if family in model_id.lower():
                                name = f"{family.title()}: Weekly"
                                break
            if name and _percent(limit.get("percent")) is not None:
                _window(windows, name, limit["percent"], limit.get("resets_at"))
                present.add(name)
    for key, name in (
        ("five_hour", "5-hour"),
        ("seven_day", "Weekly"),
        ("seven_day_sonnet", "Sonnet: Weekly"),
        ("seven_day_opus", "Opus: Weekly"),
    ):
        window = payload.get(key)
        if name not in present and isinstance(window, dict):
            _window(windows, name, window.get("utilization"), window.get("resets_at"))


def collect_usage(base_url: str, management_key: str) -> dict:
    """Return an allowlisted quota snapshot; unavailable values remain unknown."""
    snapshot = {
        "checked_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "status": "unavailable",
        "accounts": [],
    }
    try:
        credentials = _request(base_url, management_key, "credentials").get("files")
        if not isinstance(credentials, list):
            return snapshot
    except _TRANSPORT_ERRORS:
        return snapshot
    for credential in credentials:
        if not isinstance(credential, dict) or credential.get("disabled") is True:
            continue
        provider = credential.get("provider")
        if provider not in ("codex", "claude"):
            continue
        account = {
            "provider": provider,
            "status": "unavailable",
            "plan": None,
            "windows": [],
        }
        snapshot["accounts"].append(account)
        auth_index = credential.get("auth_index")
        if not isinstance(auth_index, str) or not auth_index:
            continue
        header = {"Authorization": "Bearer $TOKEN$"}
        if provider == "codex":
            url = "https://chatgpt.com/backend-api/wham/usage"
            header["User-Agent"] = "codex-cli/1.0.0"
            claims = credential.get("id_token")
            account_id = (
                claims.get("chatgpt_account_id") if isinstance(claims, dict) else None
            )
            if isinstance(account_id, str) and account_id:
                header["Chatgpt-Account-Id"] = account_id
        else:
            url = "https://api.anthropic.com/api/oauth/usage"
            header.update(
                {
                    "Accept": "application/json",
                    "anthropic-version": "2023-06-01",
                    "anthropic-beta": "oauth-2025-04-20",
                    "User-Agent": "claude-cli/1.0.83 (external, cli)",
                }
            )
        try:
            response = _request(
                base_url,
                management_key,
                "requests/api-call",
                {
                    "auth_index": auth_index,
                    "method": "GET",
                    "url": url,
                    "header": header,
                },
            )
            status_code = response.get("status_code")
            if not isinstance(status_code, int) or not 200 <= status_code < 300:
                continue
            if not isinstance(response.get("body"), str):
                continue
            payload = json.loads(response["body"])
            if not isinstance(payload, dict):
                continue
            if provider == "codex":
                _codex(account, payload)
            else:
                _claude(account, payload)
            if account["windows"] or "credits" in account:
                account["status"] = "ok"
        except (*_TRANSPORT_ERRORS, KeyError):
            continue
    available = sum(account["status"] == "ok" for account in snapshot["accounts"])
    if available:
        snapshot["status"] = (
            "ok" if available == len(snapshot["accounts"]) else "partial"
        )
    return snapshot
