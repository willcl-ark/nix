"""Token use of finished reviews, as compact monthly files for the Tokens page."""

from collections import defaultdict

# Reviews still running are published once they finish.
FINISHED = ("complete", "superseded", "failed")
REVIEW_FIELDS = (
    "job",
    "repository",
    "number",
    "day",
    "outcome",
    "tier",
    "profile",
    "title",
    "report",
)
RUN_FIELDS = (
    "review",
    "stage",
    "model",
    "requests",
    "unknown",
    "input",
    "cached",
    "cache_input",
    "output",
)
SUMS = {
    "requests": "requests",
    "unknown": "unknown_usage",
    "input": "input",
    "cached": "cached",
    "cache_input": "cache_input",
    "output": "output",
}


def months(jobs, requests):
    """Group usage rows by the month in which each finished review started."""
    finished = {
        job["row"]["id"]: job for job in jobs if job["row"]["status"] in FINISHED
    }
    usage = defaultdict(list)
    for request in requests:
        if request["job_id"] in finished and request["day"] is not None:
            usage[request["job_id"]].append(request)
    result = {}
    for job_id, rows in sorted(usage.items()):
        day = min(row["day"] for row in rows)
        month = result.setdefault(
            day[:7],
            {
                "review_fields": REVIEW_FIELDS,
                "reviews": [],
                "run_fields": RUN_FIELDS,
                "runs": [],
                "names": [],
            },
        )
        runs = {}
        for row in rows:
            run = runs.setdefault((row["stage"], row["model"]), dict.fromkeys(SUMS))
            for name, source in SUMS.items():
                if row[source] is not None:
                    run[name] = (run[name] or 0) + row[source]
        month["runs"].extend(
            [
                len(month["reviews"]),
                *(_index(month["names"], name) for name in key),
                *run.values(),
            ]
            for key, run in sorted(runs.items())
        )
        month["reviews"].append(_review(finished[job_id], day))
    return result


def _index(names, name):
    """Runs refer to stage and model names by position to keep the file small."""
    if name not in names:
        names.append(name)
    return names.index(name)


def _review(job, day):
    row = job["row"]
    saved = job["reviews"][0] if job["reviews"] else {}
    return [
        row["id"],
        row["repo_id"],
        row["number"],
        day,
        row["status"],
        (saved.get("routing") or {}).get("selected_tier"),
        saved.get("profile"),
        saved.get("pr_title"),
        saved.get("report_name"),
    ]
