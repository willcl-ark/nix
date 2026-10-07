"""Background runner for addressed-finding assessments."""

import argparse
import json
import logging
import sqlite3
import subprocess
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from . import config, followup, spend


DEFAULT_BATCH_SIZE = 5
FOLLOWUP_DIR = "followup"
ASSESSMENTS_DB = "assessments.sqlite3"
SPEND_DB = "spend.sqlite3"
CHECKOUT_DIR = "checkout"


@contextmanager
def _read_only_db(path):
    uri = f"file:{quote(str(path), safe='/:')}?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


@contextmanager
def _write_db(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def _read_db(path):
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def _decode_result(text):
    if text is None:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _job(row):
    item = dict(row)
    item["force"] = bool(item["force"])
    item["review_result"] = _decode_result(item["review_result"])
    return item


def completed_jobs(jobs_db):
    if not jobs_db.exists():
        return []
    with _read_only_db(jobs_db) as db:
        rows = db.execute("""
            SELECT id, number, generation, base_ref, head, action, force,
                status, attempts, review_result
            FROM jobs
            WHERE status = 'complete' AND review_result IS NOT NULL
            ORDER BY id
        """).fetchall()
    return [job for job in (_job(row) for row in rows)
            if job["review_result"] is not None]


def init_assessments(path, jobs):
    with _write_db(path) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS assessments (
            job_id INTEGER PRIMARY KEY,
            result TEXT
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
        initialized = db.execute(
            "SELECT 1 FROM metadata WHERE key = 'initialized'").fetchone()
        if initialized:
            return False
        db.executemany(
            "INSERT OR IGNORE INTO assessments (job_id, result) VALUES (?, NULL)",
            [(job["id"],) for job in jobs],
        )
        db.execute(
            "INSERT INTO metadata (key, value) VALUES ('initialized', '1')")
        return True


def pending_jobs(path, jobs):
    with _read_db(path) as db:
        seen = {
            row["job_id"] for row in db.execute(
                "SELECT job_id FROM assessments")
        }
    return [job for job in jobs if job["id"] not in seen]


def save_assessment(path, job_id, result):
    value = None if result is None else json.dumps(result, sort_keys=True)
    with _write_db(path) as db:
        db.execute(
            "INSERT OR IGNORE INTO assessments (job_id, result) VALUES (?, ?)",
            (job_id, value),
        )


def assessment_results(path):
    with _read_db(path) as db:
        rows = db.execute(
            "SELECT job_id, result FROM assessments WHERE result IS NOT NULL"
        ).fetchall()
    results = {}
    for row in rows:
        try:
            result = json.loads(row["result"])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(result, dict):
            results[row["job_id"]] = result
    return results


def history_before(jobs, job):
    return [
        candidate for candidate in reversed(jobs)
        if (candidate["number"] == job["number"]
            and candidate["generation"] < job["generation"])
    ]


def _git(checkout, *args):
    return subprocess.run(
        ["git", "-C", str(checkout), *args], check=True, capture_output=True,
        text=True, timeout=180,
    ).stdout


def prepare_checkout(checkout, origin):
    if not (checkout / ".git").exists():
        checkout.mkdir(mode=0o700, parents=True, exist_ok=True)
        _git(checkout, "init", "-q")
        _git(checkout, "remote", "add", "origin", origin)
    if _git(checkout, "remote", "get-url", "origin").strip() != origin:
        raise ValueError("follow-up checkout origin does not match configured origin")


def fetch_heads(checkout, origin, job_id, old_head, new_head):
    prepare_checkout(checkout, origin)
    ref_prefix = f"refs/ralph/followup/{job_id}"
    _git(checkout, "fetch", "--no-tags", "--filter=blob:none", "origin",
         f"+{old_head}:{ref_prefix}/old", f"+{new_head}:{ref_prefix}/new")


def assess_job(api_key, state_dir, origin, prompt_config, ledger, jobs, job,
               prior_assessments):
    history = history_before(jobs, job)
    checkout = state_dir / FOLLOWUP_DIR / CHECKOUT_DIR
    if history:
        old_head = followup._head(history[0])
        new_head = followup._head(job)
        if old_head != new_head:
            try:
                fetch_heads(checkout, origin, job["id"], old_head, new_head)
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                logging.warning("Could not fetch follow-up commits for job %d: %s",
                                job["id"], type(exc).__name__)
    return followup.assess_completed_review(
        api_key, checkout, job, history, prior_assessments, prompt_config,
        spend.RequestBudget(ledger, _review_id(job)))


def _review_id(job):
    return f'pr:{job["number"]}:{job["id"]}:{job["generation"]}'


def run_once(state_dir, origin, api_key, prompt_config, budget_usd,
             batch_size=DEFAULT_BATCH_SIZE):
    state_dir = Path(state_dir)
    followup_dir = state_dir / FOLLOWUP_DIR
    assessments_path = followup_dir / ASSESSMENTS_DB
    jobs = completed_jobs(state_dir / "jobs.sqlite3")
    first_run = init_assessments(assessments_path, jobs)
    if first_run:
        logging.info("follow-up seeded %d existing completed jobs", len(jobs))
        return 0

    pending = pending_jobs(assessments_path, jobs)
    if not pending:
        return 0

    prior_assessments = assessment_results(assessments_path)
    ledger = spend.Ledger(followup_dir / SPEND_DB, review_limit_usd=budget_usd)
    processed = 0
    assessed = 0
    for job in pending:
        if job["action"] not in {"synchronize", "synchronized"}:
            save_assessment(assessments_path, job["id"], None)
            processed += 1
            continue
        if assessed >= batch_size:
            break
        try:
            result = assess_job(api_key, state_dir, origin, prompt_config, ledger,
                                jobs, job, prior_assessments)
        except Exception as exc:
            logging.warning("Could not assess follow-up job %d: %s",
                            job["id"], type(exc).__name__)
            result = {
                "assessed_at": followup._now(),
                "model": prompt_config.models["addressed_findings"],
                "status": "complete",
                "error_type": type(exc).__name__,
                "findings": [],
            }
        save_assessment(assessments_path, job["id"], result)
        prior_assessments[job["id"]] = result
        processed += 1
        assessed += 1
    logging.info("follow-up processed %d jobs", processed)
    return processed


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Assess whether later PR updates addressed Ralph findings")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--openai-key-file", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path, default=config.DEFAULT_PROMPT_FILE)
    parser.add_argument("--audit-prompt-dir", type=Path,
                        default=config.DEFAULT_AUDIT_DIR)
    parser.add_argument("--models-json", type=Path)
    parser.add_argument("--budget-usd", type=float, default=0.10)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args(argv)
    if args.batch_size < 1:
        parser.error("batch-size must be positive")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    api_key = args.openai_key_file.read_text().strip()
    if not api_key:
        parser.error("openai key file must not be empty")
    prompt_config = config.PromptConfig.load(
        args.prompt_file, args.audit_prompt_dir, args.models_json)
    run_once(args.state_dir, args.origin, api_key, prompt_config,
             args.budget_usd, batch_size=args.batch_size)
    return 0
