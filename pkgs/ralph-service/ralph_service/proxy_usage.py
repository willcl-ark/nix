"""Read cached subscription observations from CLIProxy without provider requests."""

import json
import math
import re
import urllib.request
from datetime import UTC, datetime, timedelta
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


def _request(base_url, management_key):
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v8/management/credentials",
        headers={"Authorization": f"Bearer {management_key}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read(_MAX_RESPONSE_BYTES + 1)
        if not 200 <= response.status < 300 or len(body) > _MAX_RESPONSE_BYTES:
            raise ValueError("Unavailable management response")
    result = json.loads(body)
    if not isinstance(result, dict):
        raise TypeError("Invalid management response")
    return result


def _number(value):
    if not isinstance(value, str):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


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


def _window(windows, name, used, resets_at, window_seconds):
    if used is not None:
        windows.append(
            {
                "name": name,
                "used_percent": min(used, 100),
                "resets_at": _reset(resets_at),
                "window_seconds": window_seconds,
            }
        )


def _codex(account, signals):
    plan = signals.get("x-codex-plan-type")
    account["plan"] = plan if plan in _PLANS else None
    for namespace, label in (("", ""), ("code-review-", "Code review: ")):
        for key in ("primary", "secondary"):
            prefix = f"x-codex-{namespace}{key}-"
            minutes = _number(signals.get(prefix + "window-minutes"))
            duration = minutes * 60 if minutes is not None and minutes > 0 else None
            if duration is not None and (
                not math.isfinite(duration) or duration > 604800
            ):
                duration = None
            name = {18000: "5-hour", 604800: "Weekly"}.get(duration, key.title())
            reset = _number(signals.get(prefix + "reset-at"))
            if reset is None:
                after = _number(signals.get(prefix + "reset-after-seconds"))
                if after is not None:
                    try:
                        observed = datetime.fromisoformat(account["observed_at"])
                        reset = (observed + timedelta(seconds=after)).isoformat()
                    except OverflowError:
                        pass
            # A zero-duration secondary window is an absent limit, not free capacity.
            if minutes == 0:
                continue
            _window(
                account["windows"],
                label + name,
                _number(signals.get(prefix + "used-percent")),
                reset,
                duration,
            )
    has_credits = signals.get("x-codex-credits-has-credits", "").lower()
    unlimited = signals.get("x-codex-credits-unlimited", "").lower()
    if has_credits in ("true", "false") and unlimited in ("true", "false"):
        balance = signals.get("x-codex-credits-balance", "")
        account["credits"] = {
            "has_credits": has_credits == "true",
            "unlimited": unlimited == "true",
            "balance": balance
            if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", balance)
            else None,
        }


def _claude(account, signals):
    for key, name, duration in (
        ("5h", "5-hour", 18000),
        ("7d", "Weekly", 604800),
        ("7d_sonnet", "Sonnet: Weekly", 604800),
        ("7d_opus", "Opus: Weekly", 604800),
        ("7d_oi", "Fable: Weekly", 604800),
    ):
        prefix = f"anthropic-ratelimit-unified-{key}-"
        utilization = _number(signals.get(prefix + "utilization"))
        _window(
            account["windows"],
            name,
            min(utilization, 1) * 100 if utilization is not None else None,
            _number(signals.get(prefix + "reset")),
            duration,
        )


def collect_usage(base_url: str, management_key: str) -> dict:
    """Return allowlisted cached observations, retaining their original timestamps."""
    snapshot = {
        "checked_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "status": "unavailable",
        "accounts": [],
    }
    try:
        credentials = _request(base_url, management_key).get("files")
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
            "observed_at": None,
            "windows": [],
        }
        snapshot["accounts"].append(account)
        quota = credential.get("quota")
        if not isinstance(quota, dict):
            continue
        account["observed_at"] = _reset(quota.get("observed_at"))
        raw_signals = quota.get("signals")
        if account["observed_at"] is None or not isinstance(raw_signals, dict):
            continue
        signals = {
            key.lower(): value
            for key, value in raw_signals.items()
            if isinstance(key, str) and isinstance(value, str)
        }
        if provider == "codex":
            _codex(account, signals)
        else:
            _claude(account, signals)
        if account["windows"] or "credits" in account:
            account["status"] = "ok"
    available = sum(account["status"] == "ok" for account in snapshot["accounts"])
    if available:
        snapshot["status"] = (
            "ok" if available == len(snapshot["accounts"]) else "partial"
        )
    return snapshot
