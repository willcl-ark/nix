"""Anonymous report votes and private, operator-reviewed explanations."""

import argparse
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from final_comment import report_findings

TOKEN = re.compile(r"[0-9a-f]{64}")
REPOSITORY = re.compile(r"[a-z][a-z0-9_-]*")
REPORT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")
MAX_BODY = 16384
MAX_COMMENT = 2000


class FeedbackStore:
    def __init__(self, path):
        self.path = Path(path)
        self._requests = {}
        self._lock = threading.Lock()
        with self._connection() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo_id TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    report_id TEXT NOT NULL,
                    head TEXT NOT NULL,
                    finding_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    report_url TEXT NOT NULL,
                    voter TEXT NOT NULL,
                    vote INTEGER NOT NULL CHECK (vote IN (-1, 1)),
                    comment TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    updated_at INTEGER NOT NULL,
                    reviewed_at INTEGER,
                    UNIQUE (repo_id, report_id, finding_id, voter)
                )
            """)
        self.path.chmod(0o600)

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def allow_write(self, voter):
        now = time.monotonic()
        with self._lock:
            self._requests = {
                key: times
                for key, times in self._requests.items()
                if times[-1] > now - 60
            }
            times = self._requests.setdefault(voter, deque())
            while times and times[0] <= now - 60:
                times.popleft()
            if len(times) >= 30:
                return False
            times.append(now)
            return True

    def save(self, record, finding, report_url, voter, vote, comment):
        key = (record["repository_id"], record["report_id"], finding["id"], voter)
        with self._connection() as db:
            if vote is None:
                db.execute(
                    "DELETE FROM feedback WHERE repo_id=? AND report_id=? "
                    "AND finding_id=? AND voter=?",
                    key,
                )
                return
            db.execute(
                """
                INSERT INTO feedback
                    (repo_id, repository, report_id, head, finding_id, title,
                     report_url, voter, vote, comment, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (repo_id, report_id, finding_id, voter) DO UPDATE SET
                    vote=excluded.vote, comment=excluded.comment,
                    revision=feedback.revision + 1,
                    updated_at=excluded.updated_at, reviewed_at=NULL
                WHERE feedback.vote != excluded.vote OR feedback.comment != excluded.comment
            """,
                (
                    record["repository_id"],
                    record["repository"],
                    record["report_id"],
                    record["head"],
                    finding["id"],
                    finding.get("presented_title") or finding["title"],
                    report_url,
                    voter,
                    vote,
                    comment,
                    int(time.time()),
                ),
            )

    def summary(self, repo_id, report_id, findings, voter):
        result = {
            finding["id"]: {"up": 0, "down": 0, "mine": None} for finding in findings
        }
        with self._connection() as db:
            for row in db.execute(
                "SELECT finding_id, vote, COUNT(*) AS count FROM feedback "
                "WHERE repo_id=? AND report_id=? GROUP BY finding_id, vote",
                (repo_id, report_id),
            ):
                if row["finding_id"] in result:
                    result[row["finding_id"]]["up" if row["vote"] == 1 else "down"] = (
                        row["count"]
                    )
            if voter:
                for row in db.execute(
                    "SELECT finding_id, vote, comment FROM feedback "
                    "WHERE repo_id=? AND report_id=? AND voter=?",
                    (repo_id, report_id, voter),
                ):
                    if row["finding_id"] in result:
                        result[row["finding_id"]]["mine"] = {
                            "vote": row["vote"],
                            "comment": row["comment"],
                        }
        return {"findings": result}

    def pending(self):
        with self._connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id, repo_id, repository, report_id, head, finding_id, "
                    "title, report_url, vote, comment, revision, updated_at "
                    "FROM feedback WHERE reviewed_at IS NULL ORDER BY updated_at, id"
                )
            ]

    def mark_reviewed(self, feedback_id, revision):
        with self._connection() as db:
            return (
                db.execute(
                    "UPDATE feedback SET reviewed_at=? WHERE id=? AND revision=? "
                    "AND reviewed_at IS NULL",
                    (int(time.time()), feedback_id, revision),
                ).rowcount
                == 1
            )


def _reply(handler, status, value):
    body = json.dumps(value, ensure_ascii=False).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(body)


def _load_report(bot, report_id):
    prefix = "" if bot.repo_id == "core" else bot.repo_id
    record = json.loads(
        (Path(bot.report_dir) / prefix / f"{report_id}.json").read_text()
    )
    if (
        record.get("report_id") != report_id
        or record.get("repository_id") != bot.repo_id
        or record.get("repository") != bot.repository
        or not isinstance(record.get("head"), str)
    ):
        raise ValueError("Report identity does not match configuration")
    verification = (record.get("coverage") or {}).get("verification") or {}
    findings = []
    if record.get("findings_export_status") == "supported":
        findings = report_findings(
            record.get("findings") or [],
            record.get("review_memory"),
            head_sha=record["head"],
            verification_failed=record.get("status") == "failed"
            or verification.get("status") == "failed",
        )
    return record, [finding for finding in findings if finding.get("id")]


def handle_request(handler, contexts, store):
    """Handle the public feedback routes, returning False for other paths."""
    path = urlsplit(handler.path).path
    if not path.startswith("/feedback/"):
        return False
    parts = path.split("/")[2:]
    method = handler.command
    if len(parts) != (3 if method == "POST" else 2) or method not in {"GET", "POST"}:
        _reply(handler, 404, {"error": "Unknown feedback endpoint"})
        return True
    repo_id, report_id = parts[:2]
    if (
        not REPOSITORY.fullmatch(repo_id)
        or not REPORT.fullmatch(report_id)
        or repo_id not in contexts
    ):
        _reply(handler, 404, {"error": "Unknown report"})
        return True
    bot = contexts[repo_id]["bot"]
    if not bot.report_dir or not bot.report_base_url:
        _reply(handler, 404, {"error": "Reports are unavailable"})
        return True
    token = handler.headers.get("X-Feedback-Token")
    if token is not None and not TOKEN.fullmatch(token):
        _reply(handler, 400, {"error": "Invalid browser token"})
        return True
    voter = hashlib.sha256(token.encode()).hexdigest() if token else None
    try:
        record, findings = _load_report(bot, report_id)
    except FileNotFoundError:
        _reply(handler, 404, {"error": "Unknown report"})
        return True
    except (OSError, ValueError, TypeError, KeyError):
        logging.exception("Could not load feedback report")
        _reply(handler, 503, {"error": "Report is unavailable"})
        return True
    try:
        if method == "POST":
            public = urlsplit(bot.report_base_url)
            if handler.headers.get("Origin") != f"{public.scheme}://{public.netloc}":
                _reply(
                    handler,
                    403,
                    {"error": "Feedback must be sent from the report site"},
                )
                return True
            if not voter:
                _reply(handler, 400, {"error": "A browser token is required"})
                return True
            finding_id = unquote(parts[2])
            finding = next(
                (item for item in findings if item["id"] == finding_id), None
            )
            if finding is None:
                _reply(handler, 404, {"error": "Unknown finding"})
                return True
            length = handler.headers.get("Content-Length", "")
            if not length.isdecimal() or int(length) > MAX_BODY:
                _reply(handler, 413, {"error": "Feedback is too large"})
                return True
            if handler.headers.get_content_type() != "application/json":
                _reply(handler, 415, {"error": "Expected JSON feedback"})
                return True
            handler.connection.settimeout(10)
            try:
                payload = json.loads(handler.rfile.read(int(length)))
            except (ValueError, TimeoutError):
                _reply(handler, 400, {"error": "Invalid JSON feedback"})
                return True
            if not isinstance(payload, dict):
                _reply(handler, 400, {"error": "Invalid feedback"})
                return True
            vote, comment = payload.get("vote"), payload.get("comment", "")
            if (
                "vote" not in payload
                or (vote is not None and (type(vote) is not int or vote not in {-1, 1}))
                or not isinstance(comment, str)
                or len(comment) > MAX_COMMENT
            ):
                _reply(
                    handler,
                    400,
                    {
                        "error": "Choose a vote and keep explanations within 2,000 characters"
                    },
                )
                return True
            try:
                comment.encode("utf-8")
            except UnicodeEncodeError:
                _reply(handler, 400, {"error": "Explanation contains invalid text"})
                return True
            if not store.allow_write(voter):
                _reply(
                    handler, 429, {"error": "Too many changes; please wait a minute"}
                )
                return True
            prefix = "" if repo_id == "core" else repo_id + "/"
            report_url = bot.report_base_url + "/" + prefix + report_id + ".html"
            store.save(record, finding, report_url, voter, vote, comment.strip())
        _reply(handler, 200, store.summary(repo_id, report_id, findings, voter))
    except sqlite3.Error:
        logging.exception("Could not access finding feedback")
        _reply(handler, 503, {"error": "Feedback is temporarily unavailable"})
    return True


def _html_queue(rows):
    sections = []
    for row in rows:
        url = row["report_url"] + "#finding-" + quote(row["finding_id"], safe="")
        sections.append(
            '<article><h2><a href="'
            + html.escape(url, quote=True)
            + '">'
            + html.escape(row["title"])
            + "</a></h2><p>"
            + html.escape(
                f"{row['repository']} · {row['head']} · {row['report_id']} · {row['finding_id']}"
            )
            + "</p><p>"
            + ("Useful" if row["vote"] == 1 else "Not useful")
            + "</p><pre>"
            + html.escape(row["comment"] or "No explanation supplied.")
            + "</pre><p>Mark reviewed: <code>ralph-feedback --state-dir DIR review "
            + f"{row['id']} {row['revision']}"
            + "</code></p></article>"
        )
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>Unreviewed finding feedback</title>'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<style>body{max-width:70rem;margin:2rem auto;padding:0 1rem;font:16px/1.5 system-ui}"
        "article{border-top:1px solid #aaa;margin-top:2rem}pre{white-space:pre-wrap;overflow-wrap:anywhere}"
        "p{overflow-wrap:anywhere}</style><h1>Unreviewed finding feedback</h1>"
        + ("".join(sections) or "<p>No unreviewed feedback.</p>")
        + "</html>"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List unreviewed feedback")
    listing.add_argument("--html", type=Path, help="Write a private HTML triage view")
    review = commands.add_parser("review", help="Mark the specified revision reviewed")
    review.add_argument("id", type=int)
    review.add_argument("revision", type=int)
    args = parser.parse_args(argv)
    path = args.state_dir / "feedback.sqlite3"
    if not path.exists():
        parser.error("No feedback database exists in this state directory")
    store = FeedbackStore(path)
    if args.command == "review":
        if not store.mark_reviewed(args.id, args.revision):
            parser.error(
                "Feedback changed, was withdrawn, or was already reviewed; refresh the list"
            )
    else:
        rows = store.pending()
        if args.html:
            descriptor = os.open(
                args.html, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(_html_queue(rows))
        else:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0
