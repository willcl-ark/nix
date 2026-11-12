"""Operational health of saved stage results, shared by stats and notifications."""

import hashlib
import json
from collections import Counter

from .report import public_limits


def stage_runs(debug, result, row, report_name):
    if row["status"] not in {"complete", "failed"}:
        return []
    stages = debug.get("stages") or {}
    accepted = {
        path
        for item in stages.get("tests", {}).get("test_coverage", [])
        if item.get("status") != "deferred"
        for path in item.get("paths", [])
    }
    runs = []
    config = (result.get("engine") or {}).get("config") or {}
    for name, stage in stages.items():
        if not isinstance(stage, dict) or stage.get("status") not in {
            "completed",
            "failed",
            "invalid",
        }:
            continue
        limits = public_limits(stage)
        reasons = []
        if stage["status"] == "invalid":
            reasons.append("Invalid stage output")
        if stage["status"] == "failed":
            reasons.append("Stage failed")
        if limits.get("output_tokens", {}).get("hit") is True:
            reasons.append("Output truncated")
        rejected = stage.get("rejected_test_coverage") or []
        unresolved = 0
        for item in rejected:
            assessment = item.get("assessment")
            paths = assessment.get("paths") if isinstance(assessment, dict) else None
            if (
                not isinstance(paths, list)
                or not paths
                or any(not isinstance(path, str) for path in paths)
                or not set(paths) <= accepted
            ):
                unresolved += 1
        if unresolved:
            reasons.append("Unrepaired coverage evidence")
        identity = {
            "repository_id": row.get("repo_id", "core"),
            "stage": name,
            "model": stage.get("model") or "unknown",
            "configuration": hashlib.sha256(
                json.dumps(
                    [
                        (result.get("engine") or {}).get("code_sha256"),
                        config,
                        {key: value["limit"] for key, value in limits.items()},
                    ],
                    sort_keys=True,
                ).encode()
            ).hexdigest()[:16],
        }
        runs.append(
            {
                **identity,
                "job_id": row["id"],
                "generation": row["generation"],
                "number": row["number"],
                "report_name": report_name,
                "reasons": reasons,
                "rejected_assessments": len(rejected),
                "unrepaired_assessments": unresolved,
            }
        )
    return runs


def summarize(runs, requests, now):
    timestamps = {}
    for request in requests:
        key = (
            request.get("repository_id"),
            request.get("job_id"),
            request.get("generation"),
            request.get("stage"),
        )
        value = request.get("completed_at")
        if isinstance(value, (int, float)) and value <= now:
            timestamps[key] = max(timestamps.get(key, 0), value)
    groups = {}
    undated = 0
    for run in runs:
        stamp = timestamps.get(
            tuple(
                run[key] for key in ("repository_id", "job_id", "generation", "stage")
            )
        )
        if stamp is None:
            undated += 1
            continue
        if stamp < now - 86400:
            continue
        identity = {
            key: run[key]
            for key in ("repository_id", "stage", "model", "configuration")
        }
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[
            :24
        ]
        group = groups.setdefault(key, {**identity, "key": key, "runs": []})
        group["runs"].append(
            {**run, "timestamp": stamp, "id": f"{run['job_id']}:{run['generation']}"}
        )
    for group in groups.values():
        group["runs"].sort(key=lambda run: (run["timestamp"], run["id"]), reverse=True)
        group["runs"] = group["runs"][:5]
        group["affected"] = sum(bool(run["reasons"]) for run in group["runs"])
        group["alert"] = len(group["runs"]) == 5 and group["affected"] >= 3
        group["reasons"] = dict(
            Counter(reason for run in group["runs"] for reason in run["reasons"])
        )
    return {
        "groups": sorted(
            groups.values(),
            key=lambda group: (
                not group["alert"],
                group["repository_id"],
                group["stage"],
                group["model"],
                group["key"],
            ),
        ),
        "undated_runs": undated,
        "window_hours": 24,
    }
