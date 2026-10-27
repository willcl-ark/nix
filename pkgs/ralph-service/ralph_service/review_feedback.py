"""Freeze PR-local review history and untrusted, identity-checked author replies."""

import hashlib
import json
import subprocess

from ralph import trace

from . import forgejo
from .followup_runner import completed_jobs, history_before

MAX_PAGES = 5
PAGE_SIZE = 50
MAX_BODY = 12_000


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _comments(bot_config, number, cutoff, token):
    comments, errors = [], []
    body_bytes = 0
    github_repo = bot_config.github_repository
    if github_repo:
        base = f"/repos/{github_repo}"

        def request(path):
            return forgejo._github_api(path, token)

        pull = request(f"{base}/pulls/{number}")
        paths = [
            ("comments", f"{base}/issues/{number}/comments"),
            ("inline", f"{base}/pulls/{number}/comments"),
            ("reviews", f"{base}/pulls/{number}/reviews"),
        ]
        parameter = "per_page"
    else:

        def request(path):
            return forgejo.public_discussion_request(bot_config, path)

        pull = request(f"/pulls/{number}")
        if not isinstance(pull, dict):
            raise ValueError("Invalid PR metadata")
        if pull.get("original_author") or pull.get("original_author_id"):
            return [], [
                "Cannot authenticate mirrored author without upstream repository"
            ]
        paths = [
            ("comments", f"/issues/{number}/comments"),
            ("reviews", f"/pulls/{number}/reviews"),
        ]
        parameter = "limit"
    if not isinstance(pull, dict) or not isinstance(pull.get("user"), dict):
        return [], ["PR author identity unavailable"]
    author_id = pull["user"].get("id")
    if type(author_id) is not int or author_id <= 0:
        return [], ["PR author identity unavailable"]

    def accept(item, kind):
        nonlocal body_bytes
        if not isinstance(item, dict):
            return
        user = item.get("user")
        if not isinstance(user, dict):
            return
        if (
            user.get("id") != author_id
            or forgejo._is_bot_comment(item, bot_config)
            or not forgejo._visible_before_cutoff(item, cutoff)
        ):
            return
        if not github_repo and (
            item.get("original_author") or item.get("original_author_id")
        ):
            return
        body = item.get("body") or item.get("content") or ""
        if not isinstance(body, str) or not body.strip():
            return
        if len(body) > MAX_BODY:
            errors.append(f"Author {kind} {item.get('id')} exceeds context limit")
            return
        if body_bytes + len(body.encode()) > 60_000:
            if "Author discussion context limit reached" not in errors:
                errors.append("Author discussion context limit reached")
            return
        body_bytes += len(body.encode())
        record = {
            "id": f"{kind}:{item.get('id')}",
            "kind": kind,
            "url": item.get("html_url")
            or f"{bot_config.repository_url}/pulls/{number}",
            "body": body,
            "author_id": author_id,
            "updated_at": item.get("updated_at")
            or item.get("submitted_at")
            or item.get("created_at"),
        }
        record["sha256"] = _digest(record)
        comments.append(record)

    inline_reviews = 0
    for kind, path in paths:
        for page in range(1, MAX_PAGES + 1):
            items = request(f"{path}?{parameter}={PAGE_SIZE}&page={page}")
            if not isinstance(items, list):
                errors.append(f"Invalid {kind} response")
                break
            for item in items:
                accept(item, kind)
                if (
                    not github_repo
                    and kind == "reviews"
                    and isinstance(item, dict)
                    and isinstance(item.get("id"), int)
                ):
                    if inline_reviews >= 20:
                        if "Inline review limit reached" not in errors:
                            errors.append("Inline review limit reached")
                        continue
                    inline_reviews += 1
                    inline = request(f"/pulls/{number}/reviews/{item['id']}/comments")
                    if isinstance(inline, list):
                        for reply in inline[:PAGE_SIZE]:
                            accept(reply, "inline")
                        if len(inline) > PAGE_SIZE:
                            errors.append("Inline replies truncated")
            if len(items) < PAGE_SIZE:
                break
        else:
            errors.append(f"{kind} pagination limit reached")
    return comments, errors


def collect_review_feedback(
    jobs_db,
    job,
    bot_config,
    checkout,
    head_sha,
    *,
    github_token=None,
    cutoff=None,
    allow_discussions=True,
    historical_cutoff=False,
):
    """No discussion calls on first reviews; absent evidence never resolves history."""
    scope = {"repository": bot_config.repository_url, "number": job["number"]}
    completed = completed_jobs(jobs_db)
    if job.get("generation") is None:
        job = {
            **job,
            "generation": max((row["generation"] for row in completed), default=0) + 1,
        }
    history = [
        row
        for row in history_before(completed, job)
        if (row["review_result"].get("run", {}).get("review_scope", scope) == scope)
    ]
    if cutoff and historical_cutoff:
        cutoff_time = forgejo._parse_time(cutoff)
        history = [
            row
            for row in history
            if cutoff_time is not None
            and (
                moment := forgejo._parse_time(
                    row["review_result"].get("run", {}).get("review_completed_at")
                )
            )
            is not None
            and moment <= cutoff_time
        ]
    previous = {}
    for row in reversed(history):
        result = row["review_result"]
        exports = result.get("output", {}).get("exports", {})
        memory = exports.get("review_memory", [])
        if isinstance(memory, dict):
            memory = memory.get("findings", [])
        if not isinstance(memory, list):
            memory = []
        represented_ids = {
            candidate_id
            for entry in memory
            if isinstance(entry, dict)
            for candidate_id in entry.get("current_finding_ids", [])
        }
        findings = trace.findings_export(result) or []
        for item in findings:
            if (
                not isinstance(item, dict)
                or not item.get("id")
                or item["id"] in represented_ids
            ):
                continue
            finding = {
                **item,
                "finding_id": item["id"],
                "source_job_id": row["id"],
                "source_generation": row["generation"],
                "source_head": result.get("head_sha") or row["head"],
                "source_report_url": result.get("run", {}).get("report_url"),
                "status": "unclear",
                "key": f"{row['id']}:{row['generation']}:{item['id']}",
            }
            previous[(row["id"], row["generation"], item["id"])] = finding
        for item in memory if isinstance(memory, list) else []:
            if not isinstance(item, dict):
                continue
            key = tuple(
                item.get(field)
                for field in ("source_job_id", "source_generation", "finding_id")
            )
            if all(value is not None for value in key):
                source_report_url = previous.get(key, {}).get("source_report_url")
                if source_report_url and not item.get("source_report_url"):
                    item = {**item, "source_report_url": source_report_url}
                last_presented = [
                    {
                        **{
                            field: finding.get(f"presented_{field}")
                            or finding.get(field)
                            for field in ("title", "summary", "body")
                            if finding.get(f"presented_{field}") or finding.get(field)
                        },
                        **{
                            field: finding[field]
                            for field in ("severity", "kind", "path", "line", "side")
                            if field in finding
                        },
                    }
                    for finding in findings
                    if isinstance(finding, dict)
                    and finding.get("id") in item.get("current_finding_ids", [])
                ]
                last_presented = (
                    last_presented
                    or item.get("last_presented_findings")
                    or previous.get(key, {}).get("last_presented_findings")
                )
                if last_presented:
                    item = {**item, "last_presented_findings": last_presented}
                previous[key] = item
    if not previous:
        return None
    packet = {
        "schema_version": 1,
        **scope,
        "repository": bot_config.github_repository or bot_config.repository,
        "new_head": head_sha,
        "cutoff": cutoff or forgejo._retrieved_at(),
        "findings": list(previous.values()),
        "comments": [],
        "revisions": {
            "old_head": history[0]["review_result"].get("head_sha"),
            "new_head": head_sha,
        },
        "errors": [],
    }
    if allow_discussions:
        try:
            packet["comments"], errors = _comments(
                bot_config, job["number"], packet["cutoff"], github_token
            )
            packet["errors"].extend(errors)
        except (OSError, ValueError, TimeoutError) as exc:
            packet["errors"].append(
                f"Author discussion unavailable: {type(exc).__name__}"
            )
    old_head = packet["revisions"]["old_head"]
    if old_head:
        try:
            diff = subprocess.run(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    old_head,
                    head_sha,
                    "--",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout
            packet["revisions"]["diff"] = diff[:40_000]
            if len(diff) > 40_000:
                packet["errors"].append("Revision diff truncated")
        except (OSError, subprocess.SubprocessError):
            packet["errors"].append("Previous revision diff unavailable")
    packet["fingerprint"] = _digest(
        {key: value for key, value in packet.items() if key != "cutoff"}
    )
    return packet
