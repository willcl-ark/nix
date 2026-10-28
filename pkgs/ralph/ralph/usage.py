"""Durable provider request outcomes and reported usage."""

import json
import sqlite3
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


def web_search_calls(response):
    """Count hosted searches, excluding page opens and in-page finds."""
    return sum(
        1
        for item in response.get("output", [])
        if isinstance(item, dict)
        and item.get("type") == "web_search_call"
        and (item.get("action") or {}).get("type") not in {"open_page", "find_in_page"}
    )


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _normalized_usage(response):
    usage = response.get("usage") or {}
    inputs = usage.get("input_tokens_details") or {}
    outputs = usage.get("output_tokens_details") or {}
    return {
        "input_tokens": _count(usage.get("input_tokens")),
        "output_tokens": _count(usage.get("output_tokens")),
        "cached_tokens": _count(
            inputs.get("cached_tokens", usage.get("cached_tokens"))
        ),
        "cache_write_tokens": _count(
            inputs.get("cache_write_tokens", usage.get("cache_write_tokens"))
        ),
        "reasoning_tokens": _count(
            outputs.get("reasoning_tokens", usage.get("reasoning_tokens"))
        ),
        "web_search_calls": web_search_calls(response)
        if isinstance(response.get("output"), list)
        else None,
    }


class Ledger:
    """Record every attempt without predicting cost or subscription consumption."""

    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS requests (
                token TEXT PRIMARY KEY,
                review_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                model TEXT NOT NULL,
                month TEXT NOT NULL,
                response_id TEXT,
                status TEXT NOT NULL,
                usage_json TEXT,
                requested_at INTEGER NOT NULL,
                completed_at INTEGER,
                http_status INTEGER
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS requests_month ON requests(month)")
            db.execute(
                "CREATE INDEX IF NOT EXISTS requests_review ON requests(review_id)"
            )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS completed_response "
                "ON requests(response_id) WHERE status='completed'"
            )

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            yield db
        finally:
            db.close()

    def start(self, stage, model, review_id):
        token = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        with self._connection() as db:
            db.execute(
                "INSERT INTO requests "
                "(token,review_id,stage,model,month,status,requested_at) "
                "VALUES (?,?,?,?,?,'pending',?)",
                (
                    token,
                    review_id,
                    stage,
                    model,
                    now.strftime("%Y-%m"),
                    int(now.timestamp()),
                ),
            )
        return token

    def complete(self, token, response):
        """Record a response once, retaining duplicate attempts for diagnostics."""
        response_id = response.get("id")
        if response_id is not None:
            response_id = str(response_id)
        usage_json = json.dumps(_normalized_usage(response), sort_keys=True)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status,model FROM requests WHERE token=?", (token,)
            ).fetchone()
            if row is None:
                raise KeyError(token)
            if row[0] != "pending":
                return False
            duplicate = (
                response_id is not None
                and db.execute(
                    "SELECT 1 FROM requests WHERE response_id=? AND status='completed'",
                    (response_id,),
                ).fetchone()
                is not None
            )
            db.execute(
                "UPDATE requests SET response_id=?,model=?,status=?,usage_json=?,"
                "completed_at=? WHERE token=?",
                (
                    response_id,
                    response.get("model") or row[1],
                    "duplicate" if duplicate else "completed",
                    usage_json,
                    int(time.time()),
                    token,
                ),
            )
            db.commit()
        return not duplicate

    def fail(self, token, usage_unknown=True, http_status=None):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status FROM requests WHERE token=?", (token,)
            ).fetchone()
            if row is None:
                raise KeyError(token)
            if row[0] != "pending":
                return False
            db.execute(
                "UPDATE requests SET status=?,completed_at=?,http_status=? WHERE token=?",
                (
                    "uncertain" if usage_unknown else "failed",
                    int(time.time()),
                    http_status,
                    token,
                ),
            )
            db.commit()
        return True

    def summary(self, month=None, review_id=None):
        if month is None and review_id is None:
            month = datetime.now(timezone.utc).strftime("%Y-%m")
        query = "SELECT status,usage_json FROM requests WHERE 1=1"
        params = []
        if month is not None:
            query += " AND month=?"
            params.append(month)
        if review_id is not None:
            query += " AND review_id=?"
            params.append(review_id)
        with self._connection() as db:
            rows = db.execute(query, params).fetchall()
        statuses = Counter(status for status, _ in rows)
        usages = [json.loads(raw) for status, raw in rows if status == "completed"]
        totals = {}
        for field in (*TOKEN_FIELDS, "web_search_calls"):
            known = [usage[field] for usage in usages if usage[field] is not None]
            totals[field] = sum(known) if known else None
        unknown = statuses["pending"] + statuses["uncertain"]
        incomplete = sum(
            any(usage[field] is None for field in totals) for usage in usages
        )
        return {
            "month": month,
            "request_count": len(rows),
            "status_counts": dict(statuses),
            "tokens": {field: totals[field] for field in TOKEN_FIELDS},
            "web_search_calls": totals["web_search_calls"],
            "unknown_request_count": unknown,
            "incomplete_usage_count": incomplete,
            "usage_complete": not (unknown or incomplete),
        }


class RequestUsage:
    """Attach request records to one review."""

    def __init__(self, ledger, review_id):
        self.ledger = ledger
        self.review_id = review_id

    def start(self, stage, data):
        return self.ledger.start(stage, data["model"], self.review_id)

    def complete(self, token, response):
        return self.ledger.complete(token, response)

    def fail(self, token, usage_unknown=True, http_status=None):
        return self.ledger.fail(
            token, usage_unknown=usage_unknown, http_status=http_status
        )

    def summary(self):
        return self.ledger.summary(review_id=self.review_id)
