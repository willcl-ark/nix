"""Token use of finished reviews, as compact monthly files for the Trends page."""

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
    "findings",
    "size",
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
    "timed",
    "seconds",
    "start",
    "end",
)
# One row for each stage that ran, from saved results rather than requests.
STAGE_FIELDS = (
    "review",
    "stage",
    "model",
    "status",
    "refused",
    "turns",
    "tools",
    "candidates",
    "accepted",
    "sole",
)
SUMS = {
    "requests": "requests",
    "unknown": "unknown_usage",
    "input": "input",
    "cached": "cached",
    "cache_input": "cache_input",
    "output": "output",
    "timed": "timed",
    "seconds": "seconds",
}


def months(jobs, requests):
    """Group usage rows by the month in which each finished review started."""
    result = {}
    for job, rows in _finished(jobs, requests):
        day = min(row["day"] for row in rows)
        month = result.setdefault(
            day[:7],
            {
                "review_fields": REVIEW_FIELDS,
                "reviews": [],
                "run_fields": RUN_FIELDS,
                "runs": [],
                "stage_fields": STAGE_FIELDS,
                "stages": [],
                "names": [],
            },
        )
        review = len(month["reviews"])
        began = _earliest(rows)
        runs = defaultdict(list)
        for row in rows:
            runs[row["stage"], row["model"]].append(row)
        for key, group in sorted(runs.items()):
            month["runs"].append(
                [
                    review,
                    *(_index(month["names"], name) for name in key),
                    *(_add(group, source) for source in SUMS.values()),
                    _since(began, _earliest(group)),
                    _since(began, _latest(group)),
                ]
            )
        for stage in job["stage_runs"]:
            month["stages"].append(
                [
                    review,
                    *(
                        _index(month["names"], stage[name])
                        for name in ("stage", "model", "status")
                    ),
                    int(stage["refused"]),
                    *(stage[name] for name in STAGE_FIELDS[5:]),
                ]
            )
        month["reviews"].append(_review(job, day))
    return result


def changes(jobs, requests):
    """List when reviews started to use other stages, models or engine versions.

    Configurations differ between repositories, so only one that no earlier
    review used is a change, and not the first one of a repository.
    """
    started = sorted(
        (
            (_earliest(rows), job["row"]["repo_id"], job["engine"])
            for job, rows in _finished(jobs, requests)
            if job["engine"]["models"]
        ),
        key=lambda item: item[0],
    )
    seen = set()
    repositories = set()
    models = {}
    stages_seen = set()
    result = []
    for time, repository, engine in started:
        versions = {engine["code_sha256"], engine["config_sha256"]}
        changed = [
            [profile, stage, models[profile, stage], setting]
            for profile, stages in engine["models"].items()
            for stage, setting in stages.items()
            if models.get((profile, stage), setting) != setting
        ]
        # Reviews also name the stages they skipped.
        added = sorted(set(engine["stages"]) - stages_seen) if stages_seen else []
        if changed or added or (repository in repositories and versions - seen):
            result.append({"time": time, "models": changed, "stages": added})
        seen |= versions
        stages_seen |= set(engine["stages"])
        repositories.add(repository)
        for profile, stages in engine["models"].items():
            for stage, setting in stages.items():
                models[profile, stage] = setting
    return result


def _finished(jobs, requests):
    usage = defaultdict(list)
    for request in requests:
        if request["day"] is not None:
            usage[request["job_id"]].append(request)
    return [
        (job, usage[job["row"]["id"]])
        for job in sorted(jobs, key=lambda job: job["row"]["id"])
        if job["row"]["status"] in FINISHED and job["row"]["id"] in usage
    ]


def _add(rows, name):
    values = [row[name] for row in rows if row[name] is not None]
    return sum(values) if values else None


def _earliest(rows):
    return min(row["requested_at"] for row in rows)


def _latest(rows):
    return max(
        (row["completed_at"] for row in rows if row["completed_at"] is not None),
        default=None,
    )


def _since(began, time):
    return None if time is None else round(time - began)


def _index(names, name):
    """Rows refer to names by position to keep the file small."""
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
        (saved.get("findings") or {}).get("published"),
        _size(job),
    ]


def _size(job):
    """Only the router always sees the pull request without added context."""
    return next(
        (run["input_bytes"] for run in job["stage_runs"] if run["stage"] == "router"),
        None,
    )
