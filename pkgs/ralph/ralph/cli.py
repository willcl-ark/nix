"""Standalone local review command."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
import uuid

from . import engine, profiles, repository, research, runner, spend


def _read_json(path):
    def invalid_constant(value):
        raise ValueError(f"Invalid JSON constant: {value}")

    return json.loads(path.read_text(encoding="utf-8"), parse_constant=invalid_constant)


def load_context(path):
    if path is None:
        return {"title": "", "rationale": "", "source": None}
    value = _read_json(path)
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or set(value) - {"schema_version", "title", "rationale", "source"}
    ):
        raise ValueError(
            "Context must be a schema_version 1 object with title, rationale, and source"
        )
    for name in ("title", "rationale"):
        if not isinstance(value.get(name, ""), str):
            raise ValueError(f"Context {name} must be text")
    source = value.get("source")
    if source is not None:
        if not isinstance(source, dict) or not isinstance(source.get("kind"), str):
            raise ValueError("Context source must be an object with a kind")
        if source["kind"] == "pull_request":
            url = source.get("url")
            parsed = urlsplit(url) if isinstance(url, str) else None
            if (
                parsed is None
                or parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or type(source.get("number")) is not int
                or source["number"] < 1
            ):
                raise ValueError(
                    "Pull request source requires its actual URL and positive number"
                )
    return {
        "title": value.get("title", ""),
        "rationale": value.get("rationale", ""),
        "source": source,
    }


def resolve_archaeology(mode, source, evidence):
    available = bool(
        source and source.get("kind") == "pull_request" and evidence is not None
    )
    if mode == "on" and not available:
        raise ValueError(
            "Archaeology requires pull request context and frozen discussion evidence"
        )
    enabled = mode == "on" or (mode == "auto" and available)
    if enabled:
        reason = "Frozen pull request discussion evidence supplied"
    elif mode == "off":
        reason = "Archaeology explicitly disabled"
    elif source and source.get("kind") == "pull_request":
        reason = "Discussion evidence was not supplied"
    else:
        reason = "No pull request discussion context supplied"
    return enabled, reason


class Artifact:
    """Prepare a private sibling and publish with an atomic no-clobber link."""

    def __init__(self, destination):
        self.destination = destination
        self.temporary = None
        if destination is not None:
            if os.path.lexists(destination):
                raise ValueError(f"Output destination already exists: {destination}")
            fd, name = tempfile.mkstemp(
                prefix=f".{destination.name}.", dir=destination.parent
            )
            os.close(fd)
            self.temporary = Path(name)

    def write(self, result):
        if self.temporary is None:
            return
        payload = (
            json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        )
        with self.temporary.open("w", encoding="utf-8") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.link(self.temporary, self.destination)
        self.close()

    def close(self):
        if self.temporary is not None:
            self.temporary.unlink(missing_ok=True)
            self.temporary = None


@contextmanager
def _cancellation():
    state = {"signal": None}
    previous = {}

    def cancel(number, frame):
        state["signal"] = number

    for number in (signal.SIGINT, signal.SIGTERM):
        previous[number] = signal.signal(number, cancel)
    try:
        yield state
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _key(path):
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"Credential file is empty: {path}")
    return value


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    review = commands.add_parser("review", help="Review committed local changes")
    review.add_argument("target")
    review.add_argument("repo", type=Path)
    review.add_argument("--base")
    review.add_argument("--context", type=Path)
    review.add_argument("--research-evidence", type=Path)
    review.add_argument("--engine", default="current")
    review.add_argument("--profile", choices=profiles.PROFILE_NAMES)
    review.add_argument("--engine-config", type=Path)
    review.add_argument("--archaeology", choices=("auto", "on", "off"))
    review.add_argument("--openai-key-file", type=Path, required=True)
    review.add_argument("--ppq-key-file", type=Path)
    review.add_argument("--review-budget-usd", type=float, default=1.00)
    review.add_argument("--ppq-review-budget-usd", type=float)
    review.add_argument("--prompt-file", type=Path)
    review.add_argument("--audit-prompt-dir", type=Path)
    review.add_argument("--models-json", type=Path)
    review.add_argument("--routing-mode", choices=("enabled", "shadow", "full"))
    review.add_argument("--output", type=Path)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    with _cancellation() as cancellation:
        code = _review(args, cancellation)
        return 128 + cancellation["signal"] if cancellation["signal"] else code


def _review(args, cancellation):
    artifact = None
    try:
        artifact = Artifact(args.output)
        context = load_context(args.context)
        evidence = (
            research.validate(_read_json(args.research_evidence))
            if args.research_evidence is not None
            else None
        )
        settings = _read_json(args.engine_config) if args.engine_config else {}
        if not isinstance(settings, dict):
            raise ValueError("Engine configuration must be a JSON object")
        overrides = {
            name: getattr(args, name)
            for name in (
                "prompt_file",
                "audit_prompt_dir",
                "models_json",
                "routing_mode",
                "profile",
            )
            if getattr(args, name) is not None
        }
        if args.engine != "current" and (args.archaeology is not None or overrides):
            raise ValueError(
                "Archaeology and prompt/model/profile overrides require the current engine"
            )
        settings.update(
            {
                key: str(value) if isinstance(value, Path) else value
                for key, value in overrides.items()
            }
        )
        archaeology = None
        if args.engine == "current":
            mode = args.archaeology
            if mode is None and "archaeology" in settings:
                if type(settings["archaeology"]) is not bool:
                    raise ValueError("Engine archaeology must be a boolean")
                mode = "on" if settings["archaeology"] else "off"
            enabled, reason = resolve_archaeology(
                mode or "auto", context["source"], evidence
            )
            settings["archaeology"] = enabled
            archaeology = {"enabled": enabled, "reason": reason}
        try:
            selected = engine.load(args.engine, settings)
        except Exception as error:
            raise ValueError(f"Could not configure review engine: {error}") from error
        multiplier = (
            selected.config["profile"]["budget_multiplier"]
            if args.engine == "current"
            else 1
        )
        args.review_budget_usd *= multiplier
        api_key = _key(args.openai_key_file)
        if args.ppq_key_file is not None and args.ppq_review_budget_usd is None:
            raise ValueError(
                "PPQ requires an explicit --ppq-review-budget-usd allowance"
            )
        if args.ppq_key_file is None and args.ppq_review_budget_usd is not None:
            raise ValueError("--ppq-review-budget-usd requires --ppq-key-file")
        ppq_key = _key(args.ppq_key_file) if args.ppq_key_file else None
        with tempfile.TemporaryDirectory(prefix="ralph-review-") as ledger_dir:
            review_id = str(uuid.uuid4())
            budget = spend.RequestBudget(
                spend.Ledger(
                    Path(ledger_dir) / "spend.sqlite3", args.review_budget_usd
                ),
                review_id,
            )
            ppq_budget = (
                spend.RequestBudget(
                    spend.Ledger(
                        Path(ledger_dir) / "ppq-spend.sqlite3",
                        args.ppq_review_budget_usd,
                    ),
                    review_id,
                )
                if ppq_key is not None
                else None
            )
            with repository.local_snapshot(
                args.repo, args.target, base=args.base
            ) as snapshot:
                case = engine.ReviewCase(
                    review="",
                    snapshot=snapshot,
                    source=context["source"],
                    research_evidence=evidence,
                )
                target = {
                    "requested": args.target,
                    "base": args.base,
                    "repository": str(args.repo.resolve()),
                    "comparison": "endpoints" if ".." in args.target else "merge_base",
                }
                print(
                    f"Reviewing {snapshot.diff_base_sha}..{snapshot.head_sha}",
                    file=sys.stderr,
                )
                runtime = engine.Runtime(
                    case=case,
                    api_key=api_key,
                    ppq_api_key=ppq_key,
                    budget=budget,
                    ppq_budget=ppq_budget,
                    is_current=lambda: cancellation["signal"] is None,
                )
                if archaeology is not None:
                    runtime.attempt["archaeology"] = archaeology
                try:
                    runtime.check_current()
                    review, skip = repository.build_review_input(
                        snapshot,
                        title=context["title"],
                        description=context["rationale"],
                    )
                    runtime.check_current()
                    case = engine.ReviewCase(
                        review=review or "",
                        snapshot=snapshot,
                        source=context["source"],
                        research_evidence=evidence,
                    )
                    runtime.case = case
                    if not snapshot.changed_paths:
                        result = runner.skipped_result(
                            case, runtime, selected, "Empty tree diff", no_changes=True
                        )
                        code = 0
                    elif skip:
                        result = runner.skipped_result(case, runtime, selected, skip)
                        code = 3
                    else:
                        result = runner.run_review(case, runtime, selected)
                        code = 0
                except Exception as error:
                    code = 128 + cancellation["signal"] if cancellation["signal"] else 1
                    result = runner.failure_result(
                        case,
                        runtime,
                        selected,
                        error,
                        status="cancelled" if cancellation["signal"] else "failed",
                    )
                    print(
                        f"Review failed: {type(error).__name__}: {error}",
                        file=sys.stderr,
                    )
                result["target"] = target
                result["run"]["allowances"] = {
                    "openai_usd": args.review_budget_usd,
                    "ppq_usd": args.ppq_review_budget_usd,
                }
                if cancellation["signal"]:
                    code = 128 + cancellation["signal"]
                if artifact is not None:
                    try:
                        artifact.write(result)
                    except OSError as error:
                        print(
                            f"Could not publish review artifact: {error}",
                            file=sys.stderr,
                        )
                        return (
                            128 + cancellation["signal"]
                            if cancellation["signal"]
                            else 1
                        )
                if code in (0, 3):
                    print(result["content"])
                return 128 + cancellation["signal"] if cancellation["signal"] else code
    except (
        OSError,
        ValueError,
        TypeError,
        ImportError,
        subprocess.SubprocessError,
    ) as error:
        print(f"ralph: {error}", file=sys.stderr)
        return 128 + cancellation["signal"] if cancellation["signal"] else 2
    finally:
        if artifact is not None:
            artifact.close()
