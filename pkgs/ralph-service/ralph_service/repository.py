"""Managed remote acquisition before the local review boundary."""
import subprocess
from pathlib import Path

from ralph import repository as local


def prepare_checkout(checkout, bot_config):
    checkout = Path(checkout)
    if not checkout.exists():
        checkout.mkdir(parents=True)
        local.git(checkout, "init", "-q")
        local.git(checkout, "remote", "add", "origin", bot_config.origin)
    else:
        try:
            local.git(checkout, "rev-parse", "--git-dir")
        except subprocess.CalledProcessError:
            local.git(checkout, "init", "-q")
            local.git(checkout, "remote", "add", "origin", bot_config.origin)
    if local.git(checkout, "remote", "get-url", "origin").strip() != bot_config.origin:
        raise ValueError("checkout origin does not match configured repository")


def collect_review(checkout, number, base_ref, expected_head, title, description, bot_config,
                   *, ref_prefix="refs/ralph"):
    prepare_checkout(checkout, bot_config)
    base_fetch_ref = f"{ref_prefix}/base"
    head_fetch_ref = f"{ref_prefix}/head"
    local.git(checkout, "check-ref-format", base_fetch_ref)
    local.git(checkout, "check-ref-format", head_fetch_ref)
    if base_ref.startswith("sha:"):
        base_source = base_ref.removeprefix("sha:")
        if not local.SHA.fullmatch(base_source):
            raise ValueError("invalid historical base")
    else:
        if not local.BRANCH.fullmatch(base_ref):
            raise ValueError("invalid base branch")
        base_source = f"refs/heads/{base_ref}"
    if type(number) is not int or number < 1:
        raise ValueError("invalid pull request number")
    # Refetch also fills blobs absent from repositories created by the old service.
    args = ["fetch", "--refetch", "--no-tags", "--no-filter"]
    if local.git(checkout, "rev-parse", "--is-shallow-repository").strip() == "true":
        args.append("--unshallow")
    local.git(checkout, *args, "origin", f"+{base_source}:{base_fetch_ref}",
              f"+refs/pull/{number}/head:{head_fetch_ref}")
    actual_head = local.resolve_commit(checkout, head_fetch_ref)
    base_sha = local.resolve_commit(checkout, base_fetch_ref)
    if actual_head != expected_head:
        return base_sha, actual_head, None, "PR head changed before review"
    snapshot = local.snapshot_repository(checkout, base_sha, actual_head)
    review, skip = local.build_review_input(snapshot, title, description)
    if review is not None:
        review = review.replace("Title: ", "PR title: ", 1).replace(
            "\nDescription:\n", "\nPR description:\n", 1)
        if len(review.encode()) > local.MAX_REVIEW_BYTES:
            review, skip = None, f"Review input exceeds {local.MAX_REVIEW_BYTES} bytes"
    return base_sha, actual_head, review, skip


def current_head(bot_config, number):
    if type(number) is not int or number < 1:
        raise ValueError("invalid pull request number")
    result = subprocess.run(
        ["git", "ls-remote", bot_config.origin, f"refs/pull/{number}/head"],
        check=True, capture_output=True, text=True, timeout=180,
        env=local.git_environment(),
    ).stdout.strip()
    fields = result.split()
    if (len(fields) != 2 or not local.SHA.fullmatch(fields[0])
            or fields[1] != f"refs/pull/{number}/head"):
        raise ValueError("Git returned invalid PR head")
    return fields[0]
