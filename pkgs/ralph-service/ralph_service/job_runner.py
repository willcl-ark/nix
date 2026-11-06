"""Run exactly one persisted review attempt under its pinned package."""

import argparse
import json
import fcntl
import logging
from pathlib import Path

from ralph import progress, usage
from . import coordinator, service
from .jobs import JobStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run"])
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    progress.LOGGER.setLevel(logging.DEBUG)
    with (args.manifest.parent / "execution.lock").open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return run_manifest(args.manifest)


def run_manifest(path):
    manifest = json.loads(path.read_text())
    if manifest["version"] != 1:
        raise ValueError("unsupported attempt manifest")
    root = Path(manifest["state_dir"])
    jobs = JobStore(root / "jobs.sqlite3", initialize=False)
    job = jobs.get_claim(manifest["job_id"], manifest["token"])
    if job is None:
        return 0
    if job["repo_id"] != manifest["repo_id"]:
        raise ValueError("attempt repository mismatch")
    try:
        contexts = coordinator.load_contexts(
            {job["repo_id"]: manifest["entry"]},
            root,
            Path(manifest["report_dir"]) if manifest["report_dir"] else None,
            manifest["report_base_url"],
        )
        api_key = Path(manifest["openai_key_file"]).read_text().strip()
        if not api_key:
            raise ValueError("OpenAI key must not be empty")
        service.run_job(
            job, jobs, api_key, usage.Ledger(root / "usage.sqlite3"), contexts
        )
    except Exception as exc:
        logging.exception("Could not initialize review job %d", job["id"])
        scoped = jobs.for_repository(job["repo_id"])
        if service.retryable_error(exc):
            scoped.retry(job, exc)
        else:
            scoped.fail(job, exc)
    return 0
