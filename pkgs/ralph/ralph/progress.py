"""Bounded operational events, without prompts, responses, or tool output."""

import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar

_CONTEXT = ContextVar("ralph_progress", default={})
LOGGER = logging.getLogger("ralph.progress")


def emit(event, *, context=None, level=logging.DEBUG, **fields):
    values = {**(_CONTEXT.get() if context is None else context), **fields}
    LOGGER.log(
        level,
        "review %s %s",
        event,
        " ".join(
            f"{key}={json.dumps(value, separators=(',', ':'))}"
            for key, value in values.items()
        ),
    )


@contextmanager
def stage(context, name, model):
    # Each model call binds its own context, including executor threads.
    token = _CONTEXT.set({**context, "stage": name, "model": model})
    started = time.monotonic()
    status = "returned"
    error_type = None
    emit("stage_start")
    try:
        yield
    except Exception as exc:
        from .model import StaleReview

        status = "cancelled" if isinstance(exc, StaleReview) else "failed"
        error_type = type(exc).__name__
        raise
    finally:
        emit(
            "stage_end",
            status=status,
            elapsed_seconds=round(time.monotonic() - started, 2),
            **({"error_type": error_type} if error_type else {}),
        )
        _CONTEXT.reset(token)
