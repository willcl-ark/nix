"""Learn from merged pull requests without reviewing them again."""

import argparse
import hashlib
import json
import logging
import re
import sqlite3
import subprocess
import time
from collections import Counter
from contextlib import closing
from datetime import datetime
from pathlib import Path

from ralph import model, stats_changes, usage as usage_tracking
from ralph.config import load_prompt_file

from . import config, followup_runner

LOGGER = logging.getLogger(__name__)

AUDITS = Path(__file__).resolve().parent.parent / "audits"
DIRECTORY = "post-mortem"
DATABASE = "post_mortem.sqlite3"
TRIAGE_MODEL = "gpt-6-luna"
CONFIRM_MODEL = "gpt-6-astra"
DAY = 86400
WEEK = 7 * DAY
LOOKBACK = 28 * DAY
LESSON_WINDOW = 56 * DAY
MIN_BODY = 200
MAX_ITEM_CHARS = 1_200
MAX_REPLY_CHARS = 300
MAX_PACKET_CHARS = 40_000
MAX_EXCERPT_CHARS = 8_000
MAX_CONFIRMATIONS = 20
MAX_CONFIRMATIONS_PER_PR = 4
MAX_LESSONS = 3
MAX_PENDING_LESSONS = 6
QUIET_SAMPLE = 2
MAX_OUTPUT_TOKENS = 6_000
HELD_OUT_SHARE = 4
BOTS = {"DrahtBot", "github-actions[bot]"}
IMPACTS = ("high", "medium", "low")
AGENTS = (
    "adversarial adversarial_claude alternatives archaeologist build challenger "
    "commit_flow concurrency design feedback gui independent judge public_contract "
    "script state symbol_usage tests verifier unknown"
).split()


def _schema(name, properties):
    row = {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {name: {"type": "array", "items": row}},
        "required": [name],
        "additionalProperties": False,
    }


def _choice(*values):
    return {"type": "string", "enum": list(values)}


TEXT = {"type": "string"}
TRIAGE_SCHEMA = _schema(
    "items",
    {
        "item_id": TEXT,
        "relation": _choice("published", "withheld", "missed", "excluded"),
        "decision_id": TEXT,
        "outcome": _choice("fixed", "acknowledged", "declined", "unknown"),
        "impact": _choice(*IMPACTS),
        "point": TEXT,
    },
)
CONFIRM_SCHEMA = _schema(
    "verdicts",
    {
        "verdict": _choice("confirmed", "rejected", "unclear"),
        "reason": TEXT,
        "cause": _choice(
            "not_raised", "withheld_by_judgement", "stage_failure", "unknown"
        ),
        "agent": _choice(*AGENTS),
        "failure": TEXT,
    },
)
LESSON_SCHEMA = _schema(
    "lessons",
    {
        "agent": _choice(*AGENTS),
        "wording": TEXT,
        "risk": TEXT,
        "case_ids": {"type": "array", "items": {"type": "integer"}},
    },
)


class BudgetReached(Exception):
    """The weekly token allowance does not cover the next request."""


def init(path):
    with followup_runner._write_db(path) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS merges (
            repo_id TEXT NOT NULL, number INTEGER NOT NULL,
            merge_commit TEXT NOT NULL, merged_at TEXT NOT NULL,
            item_count INTEGER NOT NULL, processed_at INTEGER NOT NULL,
            PRIMARY KEY (repo_id, merge_commit)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS cases (
            id INTEGER PRIMARY KEY, repo_id TEXT NOT NULL, number INTEGER NOT NULL,
            merge_commit TEXT NOT NULL, item_id TEXT NOT NULL, relation TEXT NOT NULL,
            outcome TEXT NOT NULL, impact TEXT NOT NULL, point TEXT NOT NULL,
            url TEXT NOT NULL, head TEXT, status TEXT NOT NULL,
            held_out INTEGER NOT NULL, detail TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            UNIQUE (repo_id, merge_commit, item_id)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS lessons (
            id INTEGER PRIMARY KEY, agent TEXT NOT NULL, wording TEXT NOT NULL,
            risk TEXT NOT NULL, case_ids TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', note TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL, decided_at INTEGER
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY, finished_at INTEGER NOT NULL, summary TEXT NOT NULL
        )""")
        stats_changes.install(db, "cases", "id")


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def merged_pulls(metadata_dir, since, numbers=()):
    """Read recently changed backup files, so old pull requests cost one stat."""
    paths = (
        [metadata_dir / f"{number}.json" for number in numbers]
        if numbers
        else sorted(
            path
            for path in metadata_dir.glob("*.json")
            if path.stat().st_mtime >= since
        )
    )
    for path in paths:
        try:
            metadata = json.loads(path.read_text())
            pull = metadata["pull"]
            if not pull.get("merged_at") or not pull.get("merge_commit_sha"):
                continue
            if numbers or timestamp(pull["merged_at"]) >= since:
                yield metadata
        except (OSError, ValueError, KeyError, TypeError):
            LOGGER.warning("Could not read merge metadata %s", path.name)


def _login(entry):
    return (entry.get("user") or {}).get("login")


def _clip(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[:limit] + " [truncated]"


def _reviewed_head(commit, created_at, pushes, heads):
    """Find the reviewed head to check; the source check proves the point applied.

    Comments often name a commit inside the series or an unreviewed push.
    """
    if commit in heads:
        return commit
    earlier = [
        head for at, head in pushes if head in heads and created_at and at <= created_at
    ]
    return earlier[-1] if earlier else None


def human_items(metadata, heads):
    """Select human points that a head Ralph reviewed may have contained.

    Points older than every reviewed push are not Ralph's misses.
    """
    pushes = sorted(
        (event["created_at"], event["commit_id"])
        for event in metadata.get("events") or []
        if event.get("event") == "head_ref_force_pushed"
        and event.get("created_at")
        and event.get("commit_id")
    )
    author = _login(metadata["pull"])
    ignored = {author, None} | BOTS
    comments = metadata.get("comments") or []
    replies = {}
    for comment in comments:
        replies.setdefault(comment.get("in_reply_to_id"), []).append(comment)
    items = []
    for comment in comments:
        if comment.get("in_reply_to_id") or _login(comment) in ignored:
            continue
        head = _reviewed_head(
            comment.get("original_commit_id"), comment.get("created_at"), pushes, heads
        )
        if head is None:
            continue
        items.append(
            {
                "item_id": f"c{comment['id']}",
                "head": head,
                "path": comment.get("path"),
                "line": comment.get("original_line") or comment.get("line"),
                "body": _clip(comment.get("body"), MAX_ITEM_CHARS),
                "hunk": (comment.get("diff_hunk") or "")[-600:],
                "replies": [
                    f"{'author' if _login(reply) == author else 'reviewer'}: "
                    + _clip(reply.get("body"), MAX_REPLY_CHARS)
                    for reply in replies.get(comment["id"], [])[:4]
                ],
                "url": comment.get("html_url") or "",
            }
        )
    events = metadata.get("events") or []
    for position, event in enumerate(events):
        kind = event.get("event")
        if kind not in {"reviewed", "commented"}:
            continue
        body = event.get("body") or ""
        if _login(event) in ignored or len(body) < MIN_BODY:
            continue
        head = _reviewed_head(
            event.get("commit_id"),
            event.get("submitted_at") or event.get("created_at"),
            pushes,
            heads,
        )
        if head is None:
            continue
        items.append(
            {
                "item_id": f"e{event['id']}",
                "head": head,
                "path": None,
                "line": None,
                "body": _clip(body, MAX_ITEM_CHARS),
                "hunk": "",
                "replies": [
                    "author: " + _clip(later.get("body"), MAX_REPLY_CHARS)
                    for later in events[position + 1 :]
                    if later.get("event") == "commented" and _login(later) == author
                ][:2],
                "url": event.get("html_url") or "",
            }
        )
    return items


def _result_data(job):
    return ((job["review_result"].get("output") or {}).get("data")) or {}


def _head(job):
    return job["review_result"].get("head_sha") or job["head"]


def ralph_decisions(jobs):
    """Describe each distinct decision once across every saved review."""
    decisions = {}
    for job in jobs:
        for index, decision in enumerate(_result_data(job).get("decisions") or []):
            finding = decision.get("finding") or {}
            title = finding.get("title") or ""
            published = decision.get("disposition") == "publish"
            key = (published, title or decision.get("reason"))
            if key in decisions:
                continue
            decisions[key] = {
                "decision_id": f"{job['id']}.{index}",
                "head": _head(job)[:12],
                "disposition": decision.get("disposition"),
                "title": title,
                "location": f"{finding['path']}:{finding.get('line')}"
                if finding.get("path")
                else "",
                "reason": _clip(decision.get("reason"), 300),
            }
    return list(decisions.values())


def _packets(pull, items, decisions):
    """Bound each request; a long discussion continues in another packet."""
    base = {
        "title": pull.get("title"),
        "description": _clip(pull.get("body"), 1_500),
        "ralph_decisions": decisions,
    }
    while len(json.dumps(base)) > MAX_PACKET_CHARS // 2 and base["ralph_decisions"]:
        base["ralph_decisions"] = base["ralph_decisions"][1:]
    batch, size = [], len(json.dumps(base))
    for item in items:
        length = len(json.dumps(item))
        if batch and (size + length > MAX_PACKET_CHARS or len(batch) == 25):
            yield batch, {**base, "human_items": batch}
            batch, size = [], len(json.dumps(base))
        batch.append(item)
        size += length
    if batch:
        yield batch, {**base, "human_items": batch}


class Client:
    """Issue structured requests within one weekly token allowance."""

    def __init__(self, api_key, ledger, budget, *, api_base, clock=time.time):
        self.api_key = api_key
        self.ledger = ledger
        self.budget = budget
        self.api_base = api_base
        self.clock = clock

    def spent(self):
        total = 0
        with self.ledger._connection() as db:
            rows = db.execute(
                "SELECT status, usage_json FROM requests WHERE requested_at >= ?",
                (int(self.clock() - WEEK),),
            ).fetchall()
        for status, raw in rows:
            usage = json.loads(raw) if raw else {}
            tokens = [usage.get("input_tokens"), usage.get("output_tokens")]
            if None not in tokens:
                total += sum(tokens)
            elif status != "failed":
                # Assume the largest request when the provider reported no usage.
                total += MAX_PACKET_CHARS // 3 + MAX_OUTPUT_TOKENS
        return total

    def ask(self, stage, prompt, value, schema, *, model_name, effort, review_id):
        text = json.dumps(value, sort_keys=True)
        estimate = (len(prompt) + len(text)) // 3 + MAX_OUTPUT_TOKENS
        if self.spent() + estimate > self.budget:
            raise BudgetReached
        [name] = schema["properties"]
        result, _payload, _elapsed = model.request_response(
            self.api_key,
            {
                "model": model_name,
                "store": False,
                "reasoning": {"effort": effort},
                "instructions": prompt,
                "input": [{"role": "user", "content": text}],
                "max_output_tokens": MAX_OUTPUT_TOKENS,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": stage,
                        "strict": True,
                        "schema": schema,
                    }
                },
            },
            stage,
            usage=usage_tracking.RequestUsage(self.ledger, review_id),
            api_base=self.api_base,
        )
        if result.get("status") != "completed":
            raise ValueError("post-mortem response did not complete")
        answer = json.loads(model.output_text(result.get("output", [])))
        expected = schema["properties"][name]["items"]["properties"]
        rows = answer[name]
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or set(row) != set(expected) for row in rows
        ):
            raise ValueError("invalid post-mortem response")
        for row in rows:
            for field, rule in expected.items():
                if "enum" in rule and row[field] not in rule["enum"]:
                    raise ValueError("unexpected post-mortem value")
        return rows


def triage(client, prompt, pull, jobs, items):
    decisions = ralph_decisions(jobs)
    known = {decision["decision_id"]: decision for decision in decisions}
    rows = []
    for batch, packet in _packets(pull, items, decisions):
        answer = client.ask(
            "post_mortem_triage",
            prompt,
            {
                **packet,
                "human_items": [
                    {k: v for k, v in item.items() if k != "url"} for item in batch
                ],
            },
            TRIAGE_SCHEMA,
            model_name=TRIAGE_MODEL,
            effort="medium",
            review_id=f"post-mortem:{pull['number']}",
        )
        by_id = {}
        for row in answer:
            by_id.setdefault(row["item_id"], []).append(row)
        for item in batch:
            matches = by_id.get(item["item_id"], [])
            if len(matches) != 1:
                raise ValueError("triage must answer each human item once")
            row = matches[0]
            # A match Ralph never made cannot count as caught or withheld.
            if row["relation"] in {"published", "withheld"}:
                decision = known.get(row["decision_id"])
                if decision is None:
                    row["relation"] = "missed"
                else:
                    row["relation"] = (
                        "published"
                        if decision["disposition"] == "publish"
                        else "withheld"
                    )
            rows.append((item, row, known.get(row["decision_id"])))
    return rows


def _held_out(number):
    """Split whole pull requests, so related cases stay together."""
    digest = hashlib.sha256(str(number).encode()).digest()
    return digest[0] % HELD_OUT_SHARE == 0


def save_merge(path, repo_id, metadata, item_count, rows, *, quiet=False):
    pull = metadata["pull"]
    number, commit, now = pull["number"], pull["merge_commit_sha"], int(time.time())
    with followup_runner._write_db(path) as db:
        db.execute(
            "INSERT OR IGNORE INTO merges VALUES (?, ?, ?, ?, ?, ?)",
            (repo_id, number, commit, pull["merged_at"], item_count, now),
        )
        if quiet:
            rows = [
                (
                    {
                        "item_id": "quiet",
                        "url": pull.get("html_url") or "",
                        "head": None,
                    },
                    {
                        "relation": "quiet",
                        "outcome": "unknown",
                        "impact": "low",
                        "point": "No comparable human point; sampled for inspection.",
                    },
                    None,
                )
            ]
        for item, row, decision in rows:
            if row["relation"] == "excluded":
                continue
            # Authors often fix a point without replying, so the source decides.
            candidate = row["relation"] in {"missed", "withheld"} and (
                row["impact"] != "low" or row["outcome"] in {"fixed", "acknowledged"}
            )
            db.execute(
                "INSERT OR IGNORE INTO cases (repo_id, number, merge_commit, item_id, "
                "relation, outcome, impact, point, url, head, status, held_out, detail, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    repo_id,
                    number,
                    commit,
                    item["item_id"],
                    row["relation"],
                    row["outcome"],
                    row["impact"],
                    row["point"][:500],
                    item["url"],
                    item["head"],
                    "candidate" if candidate else "recorded",
                    _held_out(number),
                    json.dumps({"item": item, "decision": decision}),
                    now,
                ),
            )


def _git(checkout, *args):
    return subprocess.run(
        ["git", "-C", str(checkout), *args],
        check=True,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=900,
    ).stdout


def _mentioned(text, pattern):
    return list(dict.fromkeys(re.findall(pattern, text)))


def source_evidence(checkout, origin, case, item, final_head):
    """Show the reviewed source, uses of named symbols and the later change."""
    try:
        followup_runner.prepare_checkout(checkout, origin)
        _git(checkout, "fetch", "--no-tags", "origin", case["head"], final_head)
        files = set(
            _git(checkout, "ls-tree", "-r", "--name-only", case["head"]).split()
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"gap": "The reviewed source could not be read."}
    text = f"{case['point']}\n{item['body']}"
    paths = [item["path"]] if item.get("path") else []
    for name in _mentioned(text, r"[\w./-]+\.\w+"):
        matches = [path for path in files if path == name or path.endswith("/" + name)]
        if len(matches) == 1 and matches[0] not in paths:
            paths.append(matches[0])
    symbols = [
        symbol
        for symbol in _mentioned(text, r"\b[A-Za-z_]\w{3,}\b")
        if "_" in symbol or re.search(r"[a-z][A-Z]", symbol)
    ]
    evidence = {"reviewed_head": case["head"], "files": [], "symbol_uses": {}}
    for path in paths[:3]:
        entry = {"path": path}
        try:
            if path in files:
                lines = _git(checkout, "show", f"{case['head']}:{path}").splitlines()
                center = item.get("line") if path == item.get("path") else None
                if center is None:
                    center = next(
                        (
                            number
                            for number, line in enumerate(lines, 1)
                            if any(symbol in line for symbol in symbols)
                        ),
                        1,
                    )
                entry["reviewed_source"] = _clip(
                    "\n".join(
                        f"{number}: {line}"
                        for number, line in enumerate(lines, 1)
                        if center - 40 <= number <= center + 40
                    ),
                    MAX_EXCERPT_CHARS // 2,
                )
            entry["diff_to_final_head"] = _clip(
                _git(checkout, "diff", case["head"], final_head, "--", path),
                MAX_EXCERPT_CHARS // 2,
            )
        except (OSError, subprocess.SubprocessError):
            entry["gap"] = "This file could not be read."
        evidence["files"].append(entry)
    for symbol in symbols[:6]:
        # git grep exits 1 when a symbol has no uses, which is itself evidence.
        try:
            found = _git(
                checkout, "grep", "-n", "-w", "-F", "-I", "-e", symbol, case["head"]
            )
        except subprocess.CalledProcessError as exc:
            found = "" if exc.returncode == 1 else "[search failed]"
        uses = [line.split(":", 1)[-1][:160] for line in found.splitlines()]
        evidence["symbol_uses"][symbol] = {"count": len(uses), "first": uses[:10]}
    return evidence


def _stage_status(jobs, head):
    return {
        name: stage.get("status")
        for job in jobs
        if _head(job) == head
        for name, stage in (_result_data(job).get("stages") or {}).items()
        if isinstance(stage, dict) and stage.get("status") != "completed"
    }


def candidate_cases(path, repo_id):
    """Order candidates by likely value, limiting any one pull request's share."""
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM cases WHERE repo_id = ? AND status = 'candidate' "
            "ORDER BY CASE impact WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, "
            "json_extract(detail, '$.item.path') IS NULL, relation != 'withheld', "
            "CASE outcome WHEN 'fixed' THEN 0 WHEN 'acknowledged' THEN 1 ELSE 2 END, id",
            (repo_id,),
        ).fetchall()
    selected, per_pull = [], Counter()
    for row in rows:
        per_pull[row["number"]] += 1
        if per_pull[row["number"]] <= MAX_CONFIRMATIONS_PER_PR:
            selected.append(row)
    return rows, selected[:MAX_CONFIRMATIONS]


def confirm(client, prompt, path, jobs_db, checkout, origin, case, final_head):
    detail = json.loads(case["detail"])
    jobs = followup_runner.completed_jobs(jobs_db, case["repo_id"], case["number"])
    value = {
        "point": case["point"],
        "human_item": {k: v for k, v in detail["item"].items() if k != "url"},
        "ralph_decision": detail["decision"],
        "incomplete_stages": _stage_status(jobs, case["head"]),
        "source": source_evidence(checkout, origin, case, detail["item"], final_head),
    }
    rows = client.ask(
        "post_mortem_confirm",
        prompt,
        value,
        CONFIRM_SCHEMA,
        model_name=CONFIRM_MODEL,
        effort="medium",
        review_id=f"post-mortem:{case['number']}",
    )
    if len(rows) != 1:
        raise ValueError("confirmation must return one verdict")
    [verdict] = rows
    detail["verdict"] = verdict
    with followup_runner._write_db(path) as db:
        db.execute(
            "UPDATE cases SET status = ?, detail = ? "
            "WHERE id = ? AND status = 'candidate'",
            (verdict["verdict"], json.dumps(detail), case["id"]),
        )
    return verdict["verdict"]


def suggest_lessons(client, prompt, path, repo_id):
    """Offer at most three new lessons, and none while many await a decision."""
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        cases = db.execute(
            "SELECT * FROM cases WHERE repo_id = ? AND status = 'confirmed' "
            "AND held_out = 0 AND created_at >= ?",
            (repo_id, int(client.clock() - LESSON_WINDOW)),
        ).fetchall()
        lessons = db.execute("SELECT agent, wording, status FROM lessons").fetchall()
    pending = sum(lesson["status"] == "pending" for lesson in lessons)
    allowed = min(MAX_LESSONS, MAX_PENDING_LESSONS - pending)
    numbers = {case["id"]: case["number"] for case in cases}
    if allowed <= 0 or len(set(numbers.values())) < 2:
        return 0
    rows = client.ask(
        "post_mortem_lessons",
        prompt,
        {
            "cases": [
                {
                    "case_id": case["id"],
                    "pull_request": case["number"],
                    "relation": case["relation"],
                    "impact": case["impact"],
                    "point": case["point"],
                    **{
                        key: json.loads(case["detail"])["verdict"][key]
                        for key in ("cause", "agent", "failure")
                    },
                }
                for case in cases
            ],
            "existing_lessons": [dict(lesson) for lesson in lessons],
        },
        LESSON_SCHEMA,
        model_name=CONFIRM_MODEL,
        effort="medium",
        review_id="post-mortem:lessons",
    )
    saved = 0
    existing = {lesson["wording"].casefold() for lesson in lessons}
    with followup_runner._write_db(path) as db:
        for row in rows:
            supported = set(row["case_ids"])
            wording = " ".join(row["wording"].split())
            if (
                saved == allowed
                or not supported <= set(numbers)
                or len({numbers[case_id] for case_id in supported}) < 2
                or not 0 < len(wording) <= 500
                or wording.casefold() in existing
            ):
                continue
            db.execute(
                "INSERT INTO lessons (agent, wording, risk, case_ids, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    row["agent"],
                    wording,
                    row["risk"][:300],
                    json.dumps(sorted(supported)),
                    int(time.time()),
                ),
            )
            saved += 1
    return saved


def run(
    directory,
    jobs_db,
    metadata_dir,
    repo_id,
    origin,
    client,
    *,
    numbers=(),
    prompts=None,
):
    prompts = prompts or {
        name: load_prompt_file(AUDITS / f"post_mortem_{name}.md")
        for name in ("triage", "confirm", "lessons")
    }
    path = directory / DATABASE
    init(path)
    with closing(sqlite3.connect(path)) as db:
        done = {
            row[0]
            for row in db.execute(
                "SELECT merge_commit FROM merges WHERE repo_id = ?", (repo_id,)
            )
        }
    summary = dict.fromkeys(
        ("merged", "quiet", "triaged", "failed", "deferred", "confirmed"), 0
    )
    summary.update(rejected=0, unclear=0, lessons=0, budget_reached=False)
    confirmed = []
    final_heads, quiet = {}, []
    for metadata in merged_pulls(
        Path(metadata_dir), client.clock() - LOOKBACK, numbers
    ):
        pull = metadata["pull"]
        final_heads[pull["number"]] = pull["head"]["sha"]
        if pull["merge_commit_sha"] in done:
            continue
        jobs = followup_runner.completed_jobs(jobs_db, repo_id, pull["number"])
        if not jobs:
            continue
        summary["merged"] += 1
        items = human_items(metadata, {_head(job) for job in jobs})
        if not items:
            summary["quiet"] += 1
            quiet.append(metadata)
            continue
        if summary["budget_reached"]:
            summary["deferred"] += 1
            continue
        try:
            rows = triage(client, prompts["triage"], pull, jobs, items)
        except BudgetReached:
            summary["budget_reached"] = True
            summary["deferred"] += 1
            continue
        except Exception as exc:
            LOGGER.warning(
                "Could not triage PR %d: %s", pull["number"], type(exc).__name__
            )
            summary["failed"] += 1
            continue
        save_merge(path, repo_id, metadata, len(items), rows)
        summary["triaged"] += 1
    sampled = sorted(quiet, key=lambda entry: entry["pull"]["merge_commit_sha"])
    for position, metadata in enumerate(sampled):
        save_merge(path, repo_id, metadata, 0, [], quiet=position < QUIET_SAMPLE)

    checkout = directory / "checkout"
    for case in candidate_cases(path, repo_id)[1]:
        if case["number"] not in final_heads:
            continue
        try:
            verdict = confirm(
                client,
                prompts["confirm"],
                path,
                jobs_db,
                checkout,
                origin,
                case,
                final_heads[case["number"]],
            )
        except BudgetReached:
            summary["budget_reached"] = True
            break
        except Exception as exc:
            LOGGER.warning(
                "Could not confirm case %d: %s", case["id"], type(exc).__name__
            )
            continue
        summary[verdict] += 1
        if verdict == "confirmed":
            confirmed.append(case)
    try:
        summary["lessons"] = suggest_lessons(client, prompts["lessons"], path, repo_id)
    except BudgetReached:
        summary["budget_reached"] = True
    except Exception as exc:
        LOGGER.warning("Could not suggest lessons: %s", type(exc).__name__)
    summary["unchecked"] = len(candidate_cases(path, repo_id)[0])
    summary["tokens"] = client.spent()
    summary["cases"] = [
        f"{case['id']} (#{case['number']}): {_clip(case['point'], 120)}"
        for case in confirmed
    ]
    with followup_runner._write_db(path) as db:
        db.execute(
            "INSERT INTO runs (finished_at, summary) VALUES (?, ?)",
            (int(time.time()), json.dumps(summary)),
        )
    return summary


def decide(path, lesson_id, status, note=""):
    """Record one owner decision, leaving an earlier decision unchanged."""
    with followup_runner._write_db(path) as db:
        return (
            db.execute(
                "UPDATE lessons SET status = ?, note = ?, decided_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (status, note[:300], int(time.time()), lesson_id),
            ).rowcount
            == 1
        )


def veto_case(path, case_id):
    with followup_runner._write_db(path) as db:
        return (
            db.execute(
                "UPDATE cases SET status = 'vetoed' WHERE id = ? AND status = 'confirmed'",
                (case_id,),
            ).rowcount
            == 1
        )


def lesson_message(lesson, cases):
    lines = [
        f"Ralph lesson {lesson['id']} for {lesson['agent']}",
        lesson["wording"],
        f"Risk: {lesson['risk']}",
        "Evidence:",
    ]
    for case in cases:
        lines.append(
            f"- case {case['id']}: {_clip(case['point'], 160)}\n  {case['url']}"
        )
    lines += [
        "Commands (channel administrators):",
        f"/approve {lesson['id']} - queue for a tested prompt change; nothing changes now",
        f"/reject {lesson['id']} [reason] - decline and remember why",
        "/veto <case> - remove a case from the evidence",
        "/lessons - list lessons awaiting a decision",
    ]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--jobs-db", type=Path)
    parser.add_argument("--metadata-dir", type=Path, required=True)
    parser.add_argument("--repositories", type=Path, required=True)
    parser.add_argument("--repository", default="core")
    parser.add_argument("--openai-key-file", type=Path, required=True)
    parser.add_argument("--openai-api-base", default="https://api.openai.com/v1")
    parser.add_argument("--weekly-tokens", type=int, default=500_000)
    parser.add_argument("--pull-request", type=int, action="append", default=[])
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    api_key = args.openai_key_file.read_text().strip()
    if not api_key:
        parser.error("openai key file must not be empty")
    directory = args.output_dir or args.state_dir / DIRECTORY
    client = Client(
        api_key,
        usage_tracking.Ledger(directory / "usage.sqlite3"),
        args.weekly_tokens,
        api_base=args.openai_api_base,
    )
    summary = run(
        directory,
        args.jobs_db or args.state_dir / "jobs.sqlite3",
        args.metadata_dir,
        args.repository,
        config.load_repositories(args.repositories)[args.repository]["origin"],
        client,
        numbers=args.pull_request,
    )
    LOGGER.info("post-mortem %s", json.dumps(summary, sort_keys=True))
    return 0
