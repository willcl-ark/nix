"""Explicit, offline public report recovery from completed durable jobs."""

import argparse
import json
from pathlib import Path
import re
import sqlite3

from . import report


def backfill(state_dir, report_dir, *, apply=False):
    """Project published saved results without changing jobs or existing reports."""
    report_dir = Path(report_dir)
    database = (Path(state_dir) / "jobs.sqlite3").resolve()
    counts = dict(existing=0, eligible=0, written=0, skipped=0, unrecoverable=0)
    errors = []
    # Do not instantiate JobStore: its constructor performs migrations.
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute(
            "SELECT id, number, generation, head, review_result FROM jobs "
            "WHERE status = 'complete' ORDER BY id"
        )
        for job_id, number, generation, head, raw in rows:
            report_id = f"{number}-{head}-{job_id}-{generation}"
            if not re.fullmatch(r"[0-9]+-[0-9a-f]{40}-[0-9]+-[0-9]+", report_id):
                counts["unrecoverable"] += 1
                errors.append(f"Job {job_id}: invalid report identity")
                continue
            html_path = report_dir / f"{report_id}.html"
            json_path = report_dir / f"{report_id}.json"
            if html_path.is_file() and json_path.is_file():
                counts["existing"] += 1
                continue
            try:
                result = json.loads(raw) if raw else None
                if not isinstance(result, dict):
                    raise ValueError("no saved result")
                run = result.get("run")
                if not isinstance(run, dict):
                    raise ValueError("unsupported saved result schema")
                if run.get("skipped"):
                    counts["skipped"] += 1
                    continue
                if (
                    run.get("status") != "completed"
                    or result.get("head_sha") != head
                    or not isinstance(result.get("content"), str)
                    or not result["content"].strip()
                    or not isinstance(result.get("engine"), dict)
                    or not isinstance(result.get("output"), dict)
                ):
                    raise ValueError("incomplete or inconsistent saved review")
                if html_path.exists() and not json_path.exists():
                    raise ValueError("existing HTML has no matching public JSON")
                # A new file mtime is not the date of this historical review.
                run.setdefault("review_completed_at", "unknown")
                # Exercise the same allowlisted projection during the dry run.
                public = report._public_record(number, result, report_id)
                report._html_report(public, json_path.name)
                counts["eligible"] += 1
                if apply:
                    report.save_report(report_dir, number, result, report_id)
                    counts["written"] += 1
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                counts["unrecoverable"] += 1
                errors.append(f"Job {job_id}: {type(exc).__name__}")
    if apply:
        from .history import save_history

        save_history(report_dir)
    return {"apply": apply, **counts, "errors": errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Write missing reports")
    args = parser.parse_args()
    summary = backfill(args.state_dir, args.report_dir, apply=args.apply)
    print(json.dumps(summary, indent=2))
    return 1 if summary["unrecoverable"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
