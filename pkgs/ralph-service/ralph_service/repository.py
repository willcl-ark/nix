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


def collect_review(
    checkout,
    number,
    base_ref,
    expected_head,
    title,
    description,
    bot_config,
    *,
    ref_prefix="refs/ralph",
):
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
    local.git(
        checkout,
        *args,
        "origin",
        f"+{base_source}:{base_fetch_ref}",
        f"+refs/pull/{number}/head:{head_fetch_ref}",
    )
    actual_head = local.resolve_commit(checkout, head_fetch_ref)
    base_sha = local.resolve_commit(checkout, base_fetch_ref)
    if actual_head != expected_head:
        return base_sha, actual_head, None, "PR head changed before review"
    snapshot = local.snapshot_repository(checkout, base_sha, actual_head)
    review, skip = local.build_review_input(snapshot, title, description)
    if review is not None:
        review = review.replace("Title: ", "PR title: ", 1).replace(
            "\nDescription:\n", "\nPR description:\n", 1
        )
        if len(review.encode()) > local.MAX_REVIEW_BYTES:
            review, skip = None, f"Review input exceeds {local.MAX_REVIEW_BYTES} bytes"
    return base_sha, actual_head, review, skip


def current_head(bot_config, number):
    if type(number) is not int or number < 1:
        raise ValueError("invalid pull request number")
    result = subprocess.run(
        ["git", "ls-remote", bot_config.origin, f"refs/pull/{number}/head"],
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
        env=local.git_environment(),
    ).stdout.strip()
    fields = result.split()
    if (
        len(fields) != 2
        or not local.SHA.fullmatch(fields[0])
        or fields[1] != f"refs/pull/{number}/head"
    ):
        raise ValueError("Git returned invalid PR head")
    return fields[0]


def collect_backport_evidence(snapshot, metadata, bot_config, ref_prefix, token=None):
    """Acquire bounded source history before entering the local review boundary."""
    import re
    import urllib.error

    from ralph import backports

    from . import forgejo

    if backports.classify(snapshot, metadata)["mode"] != "backport":
        return None
    requests = backports.source_requests(snapshot, metadata)
    origins = {}
    repository = bot_config.github_repository or bot_config.repository
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("invalid configured source repository")
    allowed = {repository}
    if repository == "bitcoin/bitcoin":
        allowed.add("bitcoin-core/gui")
    errors = (
        ValueError,
        urllib.error.URLError,
        TimeoutError,
        subprocess.SubprocessError,
    )

    def request(source, path):
        if source not in allowed:
            raise ValueError("source repository is not allowed")
        return forgejo._github_api(f"/repos/{source}" + path, token)

    verified = set()

    def pin(sha, source):
        if not isinstance(sha, str) or not local.SHA.fullmatch(sha):
            raise ValueError("invalid upstream commit SHA")
        if sha in verified:
            return
        ref = f"{ref_prefix}/originals/{sha}"
        local.git(snapshot.checkout, "check-ref-format", ref)
        try:
            local.resolve_commit(snapshot.checkout, sha)
            local.verify_local_objects(snapshot.checkout, sha, sha)
        except ValueError:
            # Only configured upstreams are eligible; PR text cannot choose a URL.
            local.git(
                snapshot.checkout,
                "fetch",
                "--refetch",
                "--no-filter",
                "--no-tags",
                f"https://github.com/{source}.git",
                f"+{sha}:{ref}",
            )
            local.verify_local_objects(snapshot.checkout, sha, sha)
        local.git(snapshot.checkout, "update-ref", ref, sha)
        verified.add(sha)

    # Restrict reads to configured sources and Bitcoin's GUI companion repo.
    requested = set(requests["prs"])
    resolution_errors = []
    for item in requests["originals"][:500]:
        raw = item["original"]
        if not isinstance(raw, str) or not re.fullmatch(r"[0-9a-fA-F]{7,40}", raw):
            continue
        source = (item.get("pr") or f"{repository}#0").rsplit("#", 1)[0]
        if source not in allowed:
            source = repository
        try:
            commit = request(source, f"/commits/{raw}")
            if not isinstance(commit, dict):
                raise ValueError("invalid upstream commit")
            sha = commit.get("sha")
            pin(sha, source)
            # Github-Pull can name the backport PR itself. Resolve membership
            # from the original SHA as well as from the trailer.
            for page in range(1, 4):
                pulls = request(
                    source, f"/commits/{sha}/pulls?per_page=100&page={page}"
                )
                if not isinstance(pulls, list):
                    raise ValueError("invalid upstream commit associations")
                for pull in pulls:
                    if not isinstance(pull, dict):
                        raise ValueError("invalid upstream commit association")
                    number = pull.get("number")
                    # Bitcoin release PRs also contain these SHAs. Only master
                    # PRs identify the upstream implementation being ported.
                    if (
                        type(number) is int
                        and number > 0
                        and (
                            source not in {"bitcoin/bitcoin", "bitcoin-core/gui"}
                            or (pull.get("base") or {}).get("ref") == "master"
                        )
                    ):
                        requested.add(f"{source}#{number}")
                if len(pulls) < 100:
                    break
            else:
                raise ValueError(
                    "upstream commit associations exceed acquisition limit"
                )
        except errors as exc:
            resolution_errors.append(
                f"Could not acquire original {raw}: {type(exc).__name__}"
            )
    if len(requests["originals"]) > 500:
        resolution_errors.append("Original commit count exceeds acquisition limit")
    if len(requested) > 50:
        resolution_errors.append("Source PR count exceeds acquisition limit")
    for canonical in sorted(requested)[:50]:
        match = re.fullmatch(
            r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)", canonical
        )
        if match is None or match[1] not in allowed:
            origins[canonical] = {
                "error": "Source PR is outside the configured repositories"
            }
            continue
        source, number = match[1], int(match[2])
        try:
            pull = request(source, f"/pulls/{number}")
            if not isinstance(pull, dict):
                raise ValueError("invalid upstream PR")
            commits = []
            for page in range(1, 4):
                batch = request(
                    source, f"/pulls/{number}/commits?per_page=100&page={page}"
                )
                if not isinstance(batch, list):
                    raise ValueError("invalid upstream PR commits")
                for commit in batch:
                    if not isinstance(commit, dict):
                        raise ValueError("invalid upstream PR commit")
                    sha = commit.get("sha")
                    pin(sha, source)
                    commits.append(sha)
                if len(batch) < 100:
                    break
            # GitHub's PR commits endpoint exposes at most 250 commits.
            if (
                len(commits) >= 250
                or pull.get("commits") != len(commits)
                or len(set(commits)) != len(commits)
            ):
                raise ValueError("upstream PR commit list is incomplete")
            origins[canonical] = {
                "commits": commits,
                "base_ref": (pull.get("base") or {}).get("ref"),
                "title": pull.get("title", ""),
                "body": pull.get("body") or "",
                "url": f"https://github.com/{source}/pull/{number}",
            }
        except errors as exc:
            origins[canonical] = {
                "error": f"Source PR unavailable: {type(exc).__name__}"
            }
    evidence = backports.collect(snapshot, metadata, origins)
    if evidence is not None and resolution_errors:
        evidence["complete"] = False
        evidence["limitations"].extend(resolution_errors)
    return evidence


def release_source_refs(checkout, ref_prefix):
    prefix = f"{ref_prefix}/originals/"
    local.git(checkout, "check-ref-format", prefix + "check")
    refs = local.git(
        checkout, "for-each-ref", "--format=%(refname)", prefix
    ).splitlines()
    for ref in refs:
        local.git(checkout, "update-ref", "-d", ref)
