"""Render approved review summaries for pull-request comments."""

from final_comment import render
from ralph import trace


def review_comment(result, *, repository_url=None):
    identity = result.get("engine", {})
    comment_format = (
        identity["config"]["comment_format"]
        if identity.get("name") == "current"
        else None
    )
    diagnostics = trace.current_diagnostics(result)
    coverage = diagnostics.get("coverage", {})
    limitations = list(coverage.get("limitations", []))
    if (
        coverage.get("status") != "complete"
        or coverage.get("verification", {}).get("status") != "complete"
    ):
        limitations.append("Review coverage was incomplete.")
    concept = diagnostics.get("concept_assessment", {})
    return render(
        trace.findings_export(result) or [],
        limitations,
        concept.get("summary") if concept.get("status") == "verified" else None,
        backport=result["output"]["exports"].get("backport_assessment"),
        review_memory=trace.review_memory_export(result),
        verification_failed=result["run"].get("status") == "failed",
        comment_format=comment_format,
        review_tier=diagnostics.get("routing", {}).get("selected", {}).get("tier"),
        report_url=result["run"].get("report_url"),
        head_sha=result["head_sha"],
        repository_url=repository_url,
        base_sha=result["merge_base"],
    )
