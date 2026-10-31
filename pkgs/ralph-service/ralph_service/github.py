"""Publish major findings to the upstream GitHub pull request."""

import json
import urllib.request
import urllib.error

from final_comment import render_github
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


def reconcile_delivery(repository, token, number, delivery):
    marker = f"<!-- ralph-major:{repository} -->"
    base = f"/repos/{repository}"
    user = github_request(token, "/user")
    comment = None
    page = 1
    while True:
        comments = github_request(
            token, f"{base}/issues/{number}/comments?per_page=100&page={page}"
        )
        for item in comments:
            if item.get("user", {}).get("id") == user["id"] and marker in (
                item.get("body") or ""
            ):
                if comment is not None:
                    raise ValueError(
                        "Multiple bot review comments require reconciliation"
                    )
                comment = item
        if len(comments) < 100:
            break
        page += 1

    def lookup(path):
        try:
            item = github_request(token, path)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        if item.get("user", {}).get("id") != user["id"]:
            raise ValueError("Delivery target no longer belongs to bot account")
        return item

    delivery.reconcile(comment, lookup)
    return comment, lookup


def publish_major_findings(repository, token, number, result, *, is_current, delivery):
    if result["run"].get("status") == "failed":
        return "failed"
    exported = trace.findings_export(result)
    if exported is None or result["run"].get("skipped"):
        return "skipped"
    findings = [finding for finding in exported if finding.get("severity") == "major"]
    if any(
        finding.get("verified_by") != "verifier" or finding.get("judged_by") != "judge"
        for finding in findings
    ):
        return "unverified"
    retained = [
        item
        for item in trace.unresolved_memory(result)
        if item.get("severity") == "major"
    ]
    collapsed = {
        finding_id
        for item in retained
        if item.get("collapsed")
        for finding_id in item.get("current_finding_ids", [])
    }
    findings = [finding for finding in findings if finding.get("id") not in collapsed]
    marker = f"<!-- ralph-major:{repository} -->"
    base = f"/repos/{repository}"
    comment, lookup = reconcile_delivery(repository, token, number, delivery)

    def dispatch(method, path, intended_body):
        return github_request(
            token,
            path,
            method,
            {"body": intended_body} if intended_body is not None else None,
        )

    delivery.check_hold()
    if delivery.finished():
        return "unchanged"
    # Check upstream itself after pagination; the mirror may lag a push or close.
    pull = github_request(token, f"{base}/pulls/{number}")
    if pull["state"] != "open" or pull["head"]["sha"] != result["head_sha"]:
        return "stale"
    if not is_current():
        return "stale"
    if not findings and not retained:
        if comment is None:
            return "unchanged"
        coverage = trace.current_diagnostics(result).get("coverage", {})
        if (
            coverage.get("status") != "complete"
            or coverage.get("verification", {}).get("status") != "complete"
        ):
            return "incomplete"
        return delivery.execute(
            "DELETE",
            f"{base}/issues/comments/{comment['id']}",
            None,
            comment,
            dispatch,
            is_current,
        )
    report_url = result["run"].get("report_url")
    if not report_url:
        raise urllib.error.URLError("GitHub publication requires a full report URL")
    identity = result.get("engine", {})
    comment_format = None
    if identity.get("name") == "current":
        comment_format = identity["config"]["comment_format"]
    body = delivery.body(
        lambda: (
            marker
            + "\n\n"
            + render_github(
                findings,
                head_sha=result["head_sha"],
                report_url=report_url,
                has_retained=bool(retained),
                comment_format=comment_format,
                repository_url=f"https://github.com/{repository}",
                base_sha=result["merge_base"],
                review_tier=trace.current_diagnostics(result)
                .get("routing", {})
                .get("selected", {})
                .get("tier"),
            )
        )
    )
    if not is_current():
        return "stale"
    return delivery.execute(
        "POST" if comment is None else "PATCH",
        f"{base}/issues/{number}/comments"
        if comment is None
        else f"{base}/issues/comments/{comment['id']}",
        body,
        comment,
        dispatch,
        is_current,
    )
