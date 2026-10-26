"""Plain-text review posts for the Telegram Bot API."""

import json
import urllib.error
import urllib.request
from http.client import HTTPException
from urllib.parse import quote

from ralph import trace

MESSAGE_LIMIT = 4096


def message_length(text):
    return len(text.encode("utf-16-le")) // 2


def truncate_message(text, limit=MESSAGE_LIMIT):
    if message_length(text) <= limit:
        return text
    return (
        text.encode("utf-16-le")[: max(0, limit - 1) * 2].decode(
            "utf-16-le", errors="ignore"
        )
        + "…"
    )


def review_message(repository_url, number, result):
    run = result["run"]
    findings = trace.findings_export(result) or []
    marker = (
        "🚨 Major finding\n"
        if any(finding.get("severity") == "major" for finding in findings)
        else ""
    )
    title = " ".join(run.get("pr_title", f"Pull request #{number}").split())
    heading = f"{marker}PR #{number}: {title}"
    links = (
        f"\n{repository_url.rstrip('/')}/pulls/{number}"
        f"\nRead full review: {run['report_url']}"
    )
    content = result["content"]
    full = f"{heading}{links}\n\n{content}"
    if message_length(full) <= MESSAGE_LIMIT:
        return full
    # Keep both links even if a PR has an unusually long title.
    return truncate_message(heading, MESSAGE_LIMIT - message_length(links)) + links


class TelegramError(Exception):
    def __init__(self, retry_after=60):
        super().__init__("Telegram message delivery failed")
        self.retry_after = retry_after


def api_request(token, method, data, *, timeout=30):
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{quote(token, safe=':')}/{method}",
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read(65536))
        except (ValueError, OSError, HTTPException):
            payload = {}
        finally:
            exc.close()
        parameters = payload.get("parameters") if isinstance(payload, dict) else None
        raise TelegramError(
            parameters.get("retry_after", 60) if isinstance(parameters, dict) else 60
        ) from None
    except (OSError, ValueError, HTTPException):
        # urllib errors can include the token-bearing request URL.
        raise TelegramError() from None
    if (
        not isinstance(payload, dict)
        or payload.get("ok") is not True
        or "result" not in payload
    ):
        parameters = payload.get("parameters") if isinstance(payload, dict) else None
        retry_after = (
            parameters.get("retry_after", 60) if isinstance(parameters, dict) else 60
        )
        raise TelegramError(retry_after) from None
    return payload["result"]


def send_message(token, chat_id, text):
    return api_request(
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        },
    )
