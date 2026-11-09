"""Publish repository-scoped reviews to the upstream GitHub pull request."""

import json
import re
import urllib.request
import urllib.error

from final_comment import render_github
from ralph import trace

from .comments import review_comment

MARKER_PREFIX = "<!-- ralph-major:"
MENTION = re.compile(r"(?<!\w)@(?=[A-Za-z0-9])")


def neutralize_mentions(body):
    """Model text must not notify people; a zero-width space stops GitHub linking."""
    return MENTION.sub("@\u200b", body)


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
    marker = f"{MARKER_PREFIX}{repository} -->"
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


def publish_review(
    repository,
    token,
    number,
    result,
    *,
    publication,
    expected_context,
    is_current,
    delivery,
):
    if publication == "off":
        return "disabled"
    if publication not in {"major-only", "full"}:
        raise ValueError("Invalid GitHub publication mode")
    exported = trace.findings_export(result)
    findings = [
        finding
        for finding in exported or []
        if publication == "full" or finding.get("severity") in {"critical", "major"}
    ]
    status = None
    if result["run"].get("status") == "failed":
        status = "failed"
    elif result["run"].get("skipped"):
        status = "skipped"
    elif exported is None:
        status = "unavailable" if publication == "full" else "skipped"
    elif any(
        finding.get("verified_by") != "verifier" or finding.get("judged_by") != "judge"
        for finding in findings
    ):
        status = "unverified"
    if status and publication == "major-only":
        return status
    retained = [
        item
        for item in trace.unresolved_memory(result)
        if item.get("severity") in {"critical", "major"}
    ]
    collapsed = {
        finding_id
        for item in retained
        if item.get("collapsed")
        for finding_id in item.get("current_finding_ids", [])
    }
    if publication == "major-only":
        findings = [
            finding for finding in findings if finding.get("id") not in collapsed
        ]
    marker = f"{MARKER_PREFIX}{repository} -->"
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

    def publication_current():
        if not is_current():
            return False
        # Metadata edits do not invalidate a review of the current head.
        # Recheck upstream immediately before dispatch in case the mirror lags.
        pull = github_request(token, f"{base}/pulls/{number}")
        return (
            pull.get("number") == number
            and pull.get("state") == "open"
            and pull.get("head", {}).get("sha") == result["head_sha"]
            and pull.get("base", {}).get("repo", {}).get("full_name") == repository
            and expected_context is not None
            and pull.get("base", {}).get("ref") == expected_context["base_ref"]
            and is_current()
        )

    if not is_current():
        return "stale"
    # Refresh an existing comment so it cannot pass for a review of this head, but
    # do not start a public thread just to report an attempt with no findings.
    if status and comment is None:
        return status
    if publication == "major-only" and not findings and not retained:
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
            publication_current,
        )
    report_url = result["run"].get("report_url")
    if not report_url:
        raise urllib.error.URLError("GitHub publication requires a Review report URL")
    identity = result.get("engine", {})
    comment_format = None
    if identity.get("name") == "current":
        comment_format = identity["config"]["comment_format"]

    def render_body():
        if status:
            reason = {
                "failed": "The review did not finish successfully.",
                "skipped": "The attempt did not produce a review for publication.",
                "unverified": "The findings did not pass all required verification checks.",
                "unavailable": "The attempt did not provide a verifiable findings export.",
            }[status]
            body = (
                f"{marker}\n\nReview {status} for `{result['head_sha'][:12]}`. "
                f"{reason} No findings are published for this attempt."
                f"\n\nAttempted head: [`{result['head_sha'][:12]}`]"
                f"(<https://github.com/{repository}/commit/{result['head_sha']}>)"
                f" · [Attempt report](<{report_url}>)"
            )
            for previous in delivery.jobs.completed_results_before(delivery.job):
                run = previous["run"]
                previous_findings = trace.findings_export(previous)
                if (
                    run.get("status") == "completed"
                    and not run.get("skipped")
                    and run.get("report_url")
                    and previous_findings is not None
                    and all(
                        item.get("verified_by") == "verifier"
                        and item.get("judged_by") == "judge"
                        for item in previous_findings
                    )
                ):
                    body += (
                        f"\n\nPrevious completed review: "
                        f"[`{previous['head_sha'][:12]}`](<{run['report_url']}>)"
                    )
                    break
            return body
        diagnostics = trace.current_diagnostics(result)
        tier = diagnostics.get("routing", {}).get("selected", {}).get("tier")
        repository_url = f"https://github.com/{repository}"
        if publication == "major-only":
            content = render_github(
                findings,
                head_sha=result["head_sha"],
                report_url=report_url,
                has_retained=bool(retained),
                comment_format=comment_format,
                repository_url=repository_url,
                base_sha=result["merge_base"],
                review_tier=tier,
            )
        else:
            content = review_comment(result, repository_url=repository_url)
            content += (
                f"\n\n---\n\nReviewed: [`{result['head_sha'][:12]}`]"
                f"(<{repository_url}/commit/{result['head_sha']}>)"
                f" · [Review report](<{report_url}>)"
            )
        return marker + "\n\n" + content

    body = delivery.body(lambda: neutralize_mentions(render_body()))
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
        publication_current,
    )
