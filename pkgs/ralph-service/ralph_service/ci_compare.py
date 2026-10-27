"""Compare approved PR agent data with its merge base using trusted Ralph code."""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from final_comment import load_format
from ralph import engine, spend
from ralph.repository import git_command, git_environment

from . import config, evaluate

MAX_FILE_BYTES = 1_000_000
MAX_DATA_BYTES = 8_000_000
DATA_ROOTS = ("pkgs/ralph/agents", "pkgs/ralph/final_comment")


def _git(repository, *args):
    return subprocess.run(
        git_command(repository, *args),
        check=True,
        capture_output=True,
        timeout=300,
        env=git_environment(),
    ).stdout


def _proxy_url(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith("/v1")
    ):
        raise ValueError(
            "Proxy URL must be HTTPS and end in /v1 without credentials or query"
        )
    return value.rstrip("/")


def _pull_request(repository, number):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "ralph-ci"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/pulls/{number}", headers=headers
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("GitHub PR metadata is too large")
    return json.loads(data)


def _validate_pull(pull, repository, number, head_sha):
    if (
        pull.get("number") != number
        or pull.get("state") != "open"
        or (pull.get("base") or {}).get("repo", {}).get("full_name", "").lower()
        != repository.lower()
    ):
        raise ValueError(
            "Comparison requires an open PR in the configured GitHub repository"
        )
    if (pull.get("head") or {}).get("sha") != head_sha:
        raise ValueError("Approved HEAD SHA does not match the open PR")


def _export_data(repository, commit, destination):
    """Read Git blobs directly; never check out or execute candidate files."""
    entries = _git(
        repository, "ls-tree", "-rz", "--long", "--full-tree", commit, "--", *DATA_ROOTS
    )
    total = 0
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, kind, object_id, size = metadata.split()
        path = PurePosixPath(raw_path.decode("utf-8"))
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Candidate data path escapes its directory")
        if path.is_relative_to(DATA_ROOTS[0]):
            relative = path.relative_to("pkgs/ralph")
            selected = path.suffix in {".toml", ".md"}
        elif path.parent == PurePosixPath(DATA_ROOTS[1]):
            relative = path.relative_to("pkgs/ralph")
            selected = path.suffix == ".md" and path.name != "README.md"
        else:
            continue
        if not selected:
            continue
        if mode not in {b"100644", b"100755"} or kind != b"blob":
            raise ValueError(f"Candidate data must be a regular Git blob: {path}")
        length = int(size)
        total += length
        if length > MAX_FILE_BYTES or total > MAX_DATA_BYTES:
            raise ValueError(f"Candidate data exceeds the size limit: {path}")
        content = _git(repository, "cat-file", "blob", object_id.decode("ascii"))
        if len(content) != length:
            raise ValueError(f"Candidate blob size changed: {path}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with target.open("xb") as output:
            output.write(content)


def prepare(args):
    repository = args.repository_dir.resolve()
    work = args.work_dir
    if not work.is_absolute() or work.resolve().is_relative_to(repository):
        raise ValueError(
            "Work directory must be absolute and outside the trusted checkout"
        )
    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", args.github_repository
    ):
        raise ValueError("GitHub repository must be owner/repo")
    if not re.fullmatch(r"[0-9a-f]{40}", args.head_sha):
        raise ValueError("HEAD SHA must be a full 40-character lowercase Git SHA")
    if args.pull_request < 1 or args.bitcoin_pr < 1:
        raise ValueError("PR numbers must be positive")
    proxy_url = _proxy_url(args.proxy_url)
    if (
        Path(
            _git(repository, "rev-parse", "--show-toplevel").decode().strip()
        ).resolve()
        != repository
    ):
        raise ValueError("Repository directory must be the trusted checkout root")
    runtime_sha = _git(repository, "rev-parse", "HEAD").decode().strip()
    _validate_pull(
        _pull_request(args.github_repository, args.pull_request),
        args.github_repository,
        args.pull_request,
        args.head_sha,
    )
    work.mkdir(parents=True, mode=0o700)
    work = work.resolve()
    candidate_ref = f"refs/ralph-ci/{args.pull_request}/{uuid.uuid4().hex}"
    try:
        _git(
            repository,
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            "--no-write-fetch-head",
            f"https://github.com/{args.github_repository}.git",
            f"+refs/pull/{args.pull_request}/head:{candidate_ref}",
        )
        fetched = _git(repository, "rev-parse", candidate_ref).decode().strip()
        if fetched != args.head_sha:
            raise ValueError("Fetched PR HEAD differs from the approved SHA")
        base_sha = _git(repository, "merge-base", runtime_sha, fetched).decode().strip()
        resolved = {}
        for side, commit in (("base", base_sha), ("head", fetched)):
            directory = work / "data" / side
            _export_data(repository, commit, directory)
            resolved[side] = engine.load(
                "current",
                {
                    "agents_dir": directory / "agents",
                    "comment_format": load_format(directory / "final_comment"),
                    "openai_api_base": proxy_url,
                    "archaeology": True,
                },
            ).identity
    finally:
        _git(repository, "update-ref", "-d", candidate_ref)

    bot = config.BotConfig(
        "https://github.com/bitcoin/bitcoin.git",
        "bitcoin/bitcoin",
        "https://api.github.com/repos/bitcoin/bitcoin",
        repository_url="https://github.com/bitcoin/bitcoin",
        github_repository="bitcoin/bitcoin",
    )
    case = evaluate.capture_case(
        None,
        work / "state" / "evaluation-checkout",
        work / "cases",
        args.bitcoin_pr,
        bot,
        engine.load("current", resolved["base"]["config"], configured=True),
        state_dir=work / "state",
    )
    frozen = json.loads(case.read_text())
    prepared = {
        "schema_version": 1,
        "github_repository": args.github_repository,
        "pull_request": args.pull_request,
        "runtime_sha": runtime_sha,
        "base_sha": base_sha,
        "head_sha": fetched,
        "bitcoin_pr": args.bitcoin_pr,
        "proxy_url": proxy_url,
        "case": str(case.relative_to(work)),
        "case_id": frozen["case_id"],
        "case_sha256": frozen["manifest_sha256"],
        "engines": resolved,
    }
    evaluate.write_private_json(work / "prepared.json", prepared)
    print(work / "prepared.json")
    return 0


def run(args):
    work = args.work_dir
    if not work.is_absolute():
        raise ValueError("Work directory must be absolute")
    prepared = json.loads((work / "prepared.json").read_text())
    if prepared["schema_version"] != 1:
        raise ValueError("Unsupported prepared comparison schema")
    proxy_url = _proxy_url(prepared["proxy_url"])
    selected = {
        side: engine.load(
            "current",
            {**prepared["engines"][side]["config"], "openai_api_base": proxy_url},
        )
        for side in ("base", "head")
    }
    results = work / "results"
    key_path = args.openai_key_file.resolve()
    if key_path.is_relative_to(results.resolve()) or key_path.is_relative_to(
        (work / "cases").resolve()
    ):
        raise ValueError("Model key must stay outside results and captured cases")
    # Refuse a second invocation rather than accidentally buying duplicate reviews.
    results.mkdir(mode=0o700)
    api_key = evaluate.read_secret(args.openai_key_file, "OpenAI key")
    ledger = spend.Ledger(
        work / "state" / "spend.sqlite3", review_limit_usd=args.review_budget_usd
    )
    summary = {
        key: value
        for key, value in prepared.items()
        if key not in {"engines", "case", "proxy_url"}
    }
    summary["review_budget_usd"] = args.review_budget_usd
    summary["sides"] = {}
    for side in ("base", "head"):
        directory = evaluate.private_dir(results / side)
        identity = selected[side].identity
        try:
            artifact_path = evaluate.run_case(
                work / prepared["case"],
                api_key,
                directory,
                ledger,
                engine_name="current",
                engine_config=selected[side].config,
            )
            artifact = json.loads(artifact_path.read_text())
        except Exception as error:  # noqa: BLE001 - HEAD must still run if BASE fails.
            artifact = {
                "engine": identity,
                "run": {"status": "failed"},
                "error": {
                    "type": type(error).__name__,
                    "message": str(error).replace(api_key, "[redacted]"),
                },
            }
            artifact_path = directory / "failure.json"
            evaluate.write_private_json(artifact_path, artifact)
        (directory / "review.md").write_text(artifact.get("content") or "")
        summary["sides"][side] = {
            "status": artifact["run"]["status"],
            "source_sha": prepared[f"{side}_sha"],
            "engine": {
                key: identity[key] for key in ("name", "code_sha256", "config_sha256")
            },
            "artifact": str(artifact_path.relative_to(results)),
            "markdown": f"{side}/review.md",
            "effective_review_budget_usd": args.review_budget_usd
            * selected[side].config["profile"]["budget_multiplier"],
            "evaluation": evaluate.summarize_runs([artifact_path]),
        }
    evaluate.write_private_json(results / "summary.json", summary)
    rows = [
        "# Ralph agent comparison",
        "",
        f"PR: {prepared['github_repository']}#{prepared['pull_request']}",
        f"Runtime: `{prepared['runtime_sha']}`",
        f"Bitcoin PR: bitcoin/bitcoin#{prepared['bitcoin_pr']}",
        f"Frozen case: `{prepared['case_id']}`",
        "",
        "Files are in the workflow's downloadable comparison artifact.",
        "",
        "| Side | Commit | Status | Review | Artifact |",
        "| --- | --- | --- | --- | --- |",
    ]
    for side, result in summary["sides"].items():
        rows.append(
            f"| {side.upper()} | `{result['source_sha']}` | {result['status']} | "
            f"`{result['markdown']}` | `{result['artifact']}` |"
        )
    (results / "summary.md").write_text("\n".join(rows) + "\n")
    print(results / "summary.md")
    return int(
        any(result["status"] != "completed" for result in summary["sides"].values())
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser(
        "prepare", help="Validate and capture without model credentials"
    )
    capture.add_argument("--repository-dir", type=Path, required=True)
    capture.add_argument("--pull-request", type=int, required=True)
    capture.add_argument("--head-sha", required=True)
    capture.add_argument("--github-repository", required=True)
    capture.add_argument("--bitcoin-pr", type=int, required=True)
    capture.add_argument("--proxy-url", required=True)
    replay = commands.add_parser(
        "run", help="Run both prepared sides with trusted code"
    )
    replay.add_argument("--openai-key-file", type=Path, required=True)
    replay.add_argument("--review-budget-usd", type=float, default=5.00)
    for command in (capture, replay):
        command.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        return prepare(args) if args.command == "prepare" else run(args)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        print(f"ralph-ci: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
