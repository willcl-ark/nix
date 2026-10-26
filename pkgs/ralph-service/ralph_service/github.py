"""Publish major findings to the upstream GitHub pull request."""

import json
import urllib.request
import urllib.error

from ralph import trace


def github_request(token, path, method="GET", data=None):
    request = urllib.request.Request(
        "https://api.github.com" + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "ralph/1.0",
            "X-GitHub-Api-Version": "2026-03-10",
        },
        method=method,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return None if response.status == 204 else json.load(response)


def publish_major_findings(repository, token, number, result):
    exported = trace.findings_export(result)
    if exported is None or result["run"].get("skipped"):
        return "skipped"
    findings = [finding for finding in exported if finding.get("severity") == "major"]
    if any(finding.get("verified_by") != "major_verifier" for finding in findings):
        return "unverified"
    marker = f"<!-- ralph-major:{repository} -->"
    base = f"/repos/{repository}"
    user = github_request(token, "/user")
    comment = None
    page = 1
    while True:
        comments = github_request(
            token, f"{base}/issues/{number}/comments?per_page=100&page={page}"
        )
        comment = next(
            (
                item
                for item in comments
                if item.get("user", {}).get("id") == user["id"]
                and marker in (item.get("body") or "")
            ),
            None,
        )
        if comment is not None or len(comments) < 100:
            break
        page += 1
    # Check upstream itself after pagination; the mirror may lag a push or close.
    pull = github_request(token, f"{base}/pulls/{number}")
    if pull["state"] != "open" or pull["head"]["sha"] != result["head_sha"]:
        return "stale"
    if not findings:
        if comment is None:
            return "unchanged"
        github_request(token, f"{base}/issues/comments/{comment['id']}", "DELETE")
        return "deleted"
    report_url = result["run"].get("report_url")
    if not report_url:
        raise urllib.error.URLError("GitHub publication requires a full report URL")
    sections = [marker, f"Ralph found major issues at {result['head_sha']}."]
    for finding in findings:
        title = finding.get("presented_title") or finding["title"]
        body = finding.get("presented_body") or finding["body"]
        location = finding.get("path") or ""
        if finding.get("line") is not None:
            location += f":{finding['line']}"
        sections.append(
            f"**{title}** ({location}, {finding.get('side', 'head')})\n\n{body}"
        )
    sections.append(f"[Full report]({report_url})")
    body = "\n\n".join(sections)
    if comment is None:
        github_request(
            token, f"{base}/issues/{number}/comments", "POST", {"body": body}
        )
        return "created"
    if comment.get("body") == body:
        return "unchanged"
    github_request(
        token, f"{base}/issues/comments/{comment['id']}", "PATCH", {"body": body}
    )
    return "updated"
