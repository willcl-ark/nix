"""Responses streaming with a deadline for the entire HTTP attempt."""

import asyncio
import io
import json
import urllib.error

import aiohttp


class StreamError(Exception):
    """A provider error delivered after the HTTP response started."""

    def __init__(self, error):
        super().__init__("Model response stream failed")
        self.error = error if isinstance(error, dict) else {}
        code = self.error.get("code")
        try:
            status = int(code)
        except (TypeError, ValueError):
            status = 0
        self.retryable = self.error.get("retryable") is not False and (
            status == 429
            or 500 <= status < 600
            or code in ("server_error", "rate_limit_exceeded")
            or self.error.get("type") in ("server_error", "rate_limit_error")
        )


async def _events(content):
    pending = b""
    data = []
    async for chunk in content.iter_any():
        pending += chunk
        *lines, pending = pending.split(b"\n")
        for line in lines:
            line = line.removesuffix(b"\r")
            if line.startswith(b"data:"):
                data.append(line[5:].removeprefix(b" "))
            elif not line and data:
                payload = b"\n".join(data)
                data.clear()
                if payload == b"[DONE]":
                    return
                event = json.loads(payload)
                if not isinstance(event, dict):
                    raise ValueError("Model stream event was not a JSON object")
                yield event


async def _request(url, payload, api_key, timeout):
    # Disable deadline rounding so the configured allowance is a real limit.
    deadline = aiohttp.ClientTimeout(total=timeout, ceil_threshold=float("inf"))
    async with aiohttp.ClientSession(timeout=deadline, trust_env=True) as session:
        async with session.post(
            url,
            data=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
        ) as response:
            if response.status >= 400:
                try:
                    body = await response.content.readexactly(8192)
                except asyncio.IncompleteReadError as error:
                    body = error.partial
                except (TimeoutError, aiohttp.ClientError):
                    # The headers already establish whether this was a definite
                    # rejection; diagnostics must not change charge accounting.
                    body = b""
                raise urllib.error.HTTPError(
                    url,
                    response.status,
                    response.reason,
                    response.headers,
                    io.BytesIO(body),
                )
            if response.content_type != "text/event-stream":
                raise ValueError("Model response was not an event stream")
            async for event in _events(response.content):
                kind = event.get("type")
                if kind == "error":
                    raise StreamError(event.get("error", event))
                if kind in (
                    "response.completed",
                    "response.incomplete",
                    "response.failed",
                ):
                    result = event.get("response")
                    if not isinstance(result, dict) or result.get(
                        "status"
                    ) != kind.removeprefix("response."):
                        raise ValueError("Invalid terminal model response")
                    # Preserve incomplete/failed usage and status for the existing
                    # callers; only completed responses may execute tools.
                    return result
            raise ConnectionError("Model stream ended before a terminal response")


def request(url, payload, api_key, timeout):
    try:
        return asyncio.run(_request(url, payload, api_key, timeout))
    except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError) as error:
        raise ConnectionError("Model stream connection failed") from error
