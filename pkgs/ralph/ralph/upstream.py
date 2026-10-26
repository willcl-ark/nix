"""Bounded object reads restricted to the backport's resolved upstream evidence."""

from contextlib import contextmanager
import uuid

from .repository import (
    SHA,
    MAX_TOOL_BYTES,
    bounded_git,
    git,
    has_commit,
    read_file,
    search_code,
    tracked_files_at,
)


def original_commits(evidence):
    if not isinstance(evidence, dict):
        return set()
    allowed = {
        item.get("original")
        for item in evidence.get("commits", [])
        if isinstance(item.get("original"), str)
    }
    for source in evidence.get("sources", {}).values():
        allowed.update(source.get("commits", []))
    return {
        commit
        for commit in allowed
        if isinstance(commit, str) and SHA.fullmatch(commit)
    }


@contextmanager
def pin(checkout, evidence):
    """Keep locally resolved originals alive while source tools can read them."""
    prefix = f"refs/ralph/upstream/{uuid.uuid4().hex}"
    refs = []
    try:
        for commit in sorted(original_commits(evidence)):
            if not has_commit(checkout, commit):
                continue
            ref = f"{prefix}/{commit}"
            git(checkout, "update-ref", ref, commit)
            refs.append(ref)
        yield
    finally:
        for ref in refs:
            git(checkout, "update-ref", "-d", ref)


def lookup(checkout, evidence, name, args):
    commit = args.get("commit")
    before = False if name == "read_upstream_commit" else args.get("before")
    if (
        not isinstance(commit, str)
        or not SHA.fullmatch(commit)
        or commit not in original_commits(evidence)
        or type(before) is not bool
    ):
        return "Invalid upstream commit. Use a resolved original SHA from the evidence."
    ref = git(
        checkout, "rev-parse", "--verify", f"{commit}^" if before else commit
    ).strip()
    if name == "read_upstream_commit":
        output = bounded_git(
            checkout,
            "show",
            "--format=fuller",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            ref,
            "--",
            max_bytes=MAX_TOOL_BYTES,
            attr_source=ref,
        )
        return output[:MAX_TOOL_BYTES].decode(errors="replace") + (
            "\n[Patch truncated; inspect original files and parents with upstream tools.]"
            if len(output) > MAX_TOOL_BYTES
            else ""
        )
    if name == "read_upstream_file":
        return read_file(
            checkout,
            tracked_files_at(checkout, ref),
            args.get("path"),
            args.get("start_line"),
        )
    if name == "search_upstream_code":
        return search_code(checkout, ref, args.get("query"))
    return "Invalid upstream tool."
