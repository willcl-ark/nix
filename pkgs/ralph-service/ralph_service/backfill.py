"""Explicit, offline public report recovery from completed durable jobs."""

import argparse
import json
from pathlib import Path
import re
import sqlite3

from . import config, report


def backfill(
    state_dir,
    report_dir,
    *,
    apply=False,
    repositories=None,
    report_base_url="/report",
    archive_base_url="/traces",
):
    """Recover paired pages from public snapshots or completed jobs, without inference."""
    repositories = repositories or {}
    report_dir = Path(report_dir)
    database = (Path(state_dir) / "jobs.sqlite3").resolve()
    counts = dict(existing=0, eligible=0, written=0, skipped=0, unrecoverable=0)
    errors = []
    saved = set()
    # Public snapshots are authoritative even if the durable job is gone.
    for path in sorted([*report_dir.glob("*.json"), *report_dir.glob("*/*.json")]):
        if path.parent != report_dir and path.parent.name in ("reports", "stats"):
            continue
        try:
            public = json.loads(path.read_text())
            if not isinstance(public, dict) or "report_id" not in public:
                continue
            repository_id = "core" if path.parent == report_dir else path.parent.name
            report_id = path.stem
            saved.add((repository_id, report_id))
            if (
                public["report_id"] != report_id
                or public.get("repository_id", "core") != repository_id
                or not report.REPORT_ID.fullmatch(report_id)
                or not re.fullmatch(r"[a-z][a-z0-9_-]*", repository_id)
                or not isinstance(public.get("pr"), int)
            ):
                raise ValueError("inconsistent public report identity")
            relative = path.relative_to(report_dir).with_suffix(".html")
            if (
                path.with_suffix(".html").is_file()
                and (report_dir / "reports" / relative).is_file()
            ):
                counts["existing"] += 1
                continue
            report._html_report(public, archive_base_url, archive_base_url)
            report._html_archive(public, path.name, report_base_url)
            counts["eligible"] += 1
            if apply:
                report.save_report(
                    report_dir,
                    public["pr"],
                    {},
                    report_id,
                    repository_id=repository_id,
                    report_base_url=report_base_url,
                    archive_base_url=archive_base_url,
                    refresh=True,
                )
                counts["written"] += 1
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            counts["unrecoverable"] += 1
            errors.append(f"Snapshot {path.name}: {type(exc).__name__}")
    # Do not instantiate JobStore: its constructor performs migrations.
    if database.is_file():
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
            rows = db.execute(
                "SELECT id, repo_id, number, generation, head, review_result FROM jobs "
                "WHERE status = 'complete' ORDER BY id"
            )
            for job_id, repository_id, number, generation, head, raw in rows:
                report_id = f"{number}-{head}-{job_id}-{generation}"
                if (repository_id, report_id) in saved:
                    continue
                if not re.fullmatch(
                    r"[a-z][a-z0-9_-]*", repository_id
                ) or not re.fullmatch(r"[0-9]+-[0-9a-f]{40}-[0-9]+-[0-9]+", report_id):
                    counts["unrecoverable"] += 1
                    errors.append(f"Job {job_id}: invalid report identity")
                    continue
                root = (
                    report_dir
                    if repository_id == "core"
                    else report_dir / repository_id
                )
                html_path = root / f"{report_id}.html"
                json_path = root / f"{report_id}.json"
                try:
                    result = json.loads(raw) if raw else None
                    if not isinstance(result, dict):
                        raise ValueError("no saved result")
                    run = result.get("run")
                    if not isinstance(run, dict):
                        raise ValueError("unsupported saved result schema")
                    if run.get("repository_id", repository_id) != repository_id:
                        raise ValueError("saved result belongs to another repository")
                    metadata = repositories.get(repository_id) or {}
                    scope = run.get("review_scope") or {}
                    identity = {
                        "repository_id": repository_id,
                        "repository": run.get("repository")
                        or metadata.get("repository")
                        or repository_id,
                        "repository_url": scope.get("repository")
                        or metadata.get("repository_url"),
                    }
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
                    public = report._public_record(
                        number, result, report_id, **identity
                    )
                    report._html_report(public, archive_base_url, archive_base_url)
                    report._html_archive(public, json_path.name, report_base_url)
                    counts["eligible"] += 1
                    if apply:
                        report.save_report(
                            report_dir,
                            number,
                            result,
                            report_id,
                            **identity,
                            report_base_url=report_base_url,
                            archive_base_url=archive_base_url,
                        )
                        counts["written"] += 1
                except (
                    OSError,
                    ValueError,
                    TypeError,
                    KeyError,
                    AttributeError,
                ) as exc:
                    counts["unrecoverable"] += 1
                    errors.append(f"Job {job_id}: {type(exc).__name__}")
    if apply:
        from .history import save_history

        save_history(
            report_dir,
            report_base_url=report_base_url,
            archive_base_url=archive_base_url,
        )
    return {"apply": apply, **counts, "errors": errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument(
        "--repositories", type=Path, help="Repository registry for historical metadata"
    )
    parser.add_argument("--report-base-url", default="/report")
    parser.add_argument("--archive-base-url", default="/traces")
    parser.add_argument("--apply", action="store_true", help="Write missing reports")
    args = parser.parse_args()
    summary = backfill(
        args.state_dir,
        args.report_dir,
        apply=args.apply,
        report_base_url=args.report_base_url,
        archive_base_url=args.archive_base_url,
        repositories=config.load_repositories(args.repositories)
        if args.repositories
        else None,
    )
    print(json.dumps(summary, indent=2))
    return 1 if summary["unrecoverable"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
