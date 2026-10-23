"""Local committed comparisons and bounded reads from immutable Git objects."""

import os
import re
import select
import time
import tempfile
import uuid
from contextlib import contextmanager
import subprocess
from dataclasses import dataclass
from types import MappingProxyType
from pathlib import Path

MAX_REVIEW_BYTES = 200_000
MAX_FILE_BYTES = 1_000_000
MAX_TOOL_BYTES = 12_000
MAX_FOCUSED_DIFF_BYTES = 80_000
MAX_AUDIT_DOC_BYTES = 100_000
MAX_FOLLOWUP_DIFF_BYTES = 80_000
MAX_FOLLOWUP_PATH_DIFF_BYTES = 20_000
SHA = re.compile(r"^[0-9a-f]{40}$")
BRANCH = re.compile(r"^[A-Za-z0-9._/-]+$")


def git_environment():
    # Never let object reads contact a promisor remote or invoke repository hooks.
    return {
        **os.environ,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "LC_ALL": "C",
    }


def git_command(checkout, *args):
    return [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.attributesFile=/dev/null",
        "-C",
        str(checkout),
        *args,
    ]


def git(checkout, *args):
    return subprocess.run(
        git_command(checkout, *args),
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
        env=git_environment(),
    ).stdout


@contextmanager
def object_git_command(checkout, args, attr_source):
    if attr_source is None:
        yield git_command(checkout, *args), git_environment()
        return
    # --attr-source selects committed attributes but still reads info/attributes.
    # An empty bare metadata directory excludes local overrides and diff drivers.
    objects = git(
        checkout, "rev-parse", "--path-format=absolute", "--git-path", "objects"
    ).strip()
    with tempfile.TemporaryDirectory(prefix="ralph-git-") as directory:
        git(directory, "init", "--bare", "--template=", "-q")
        env = git_environment()
        env.update(
            GIT_DIR=directory, GIT_COMMON_DIR=directory, GIT_OBJECT_DIRECTORY=objects
        )
        env.pop("GIT_WORK_TREE", None)
        env.pop("GIT_INDEX_FILE", None)
        yield git_command(directory, f"--attr-source={attr_source}", *args), env


def bounded_git(checkout, *args, max_bytes, attr_source=None):
    """Read at most the requested bytes plus one truncation sentinel."""
    with object_git_command(checkout, args, attr_source) as (command, env):
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env
        )
        output = bytearray()
        deadline = time.monotonic() + 180
        try:
            while len(output) <= max_bytes:
                remaining = deadline - time.monotonic()
                if (
                    remaining <= 0
                    or not select.select([process.stdout], [], [], remaining)[0]
                ):
                    raise subprocess.TimeoutExpired(process.args, 180)
                chunk = os.read(
                    process.stdout.fileno(), min(65536, max_bytes + 1 - len(output))
                )
                if not chunk:
                    break
                output.extend(chunk)
        except BaseException:
            if process.poll() is None:
                process.kill()
            raise
        finally:
            if process.poll() is None and (
                len(output) > max_bytes or time.monotonic() >= deadline
            ):
                process.kill()
            process.wait(timeout=10)
            process.stdout.close()
        if len(output) <= max_bytes and process.returncode:
            raise subprocess.CalledProcessError(process.returncode, process.args)
        output = bytes(output)
        return output


def has_commit(checkout, commit):
    if not isinstance(commit, str) or not SHA.fullmatch(commit):
        return False
    return (
        subprocess.run(
            git_command(checkout, "cat-file", "-e", f"{commit}^{{commit}}"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            env=git_environment(),
        ).returncode
        == 0
    )


def diff_between_heads(checkout, old_head, new_head, max_bytes=MAX_FOLLOWUP_DIFF_BYTES):
    if (
        not isinstance(old_head, str)
        or not SHA.fullmatch(old_head)
        or not isinstance(new_head, str)
        or not SHA.fullmatch(new_head)
    ):
        raise ValueError("invalid head SHA")
    output = bounded_git(
        checkout,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        f"{old_head}..{new_head}",
        max_bytes=max_bytes,
        attr_source=new_head,
    )
    return output[:max_bytes].decode(errors="replace") + (
        "\n[Diff truncated]" if len(output) > max_bytes else ""
    )


def diff_path_between_heads(
    checkout, old_head, new_head, path, max_bytes=MAX_FOLLOWUP_PATH_DIFF_BYTES
):
    if (
        not isinstance(old_head, str)
        or not SHA.fullmatch(old_head)
        or not isinstance(new_head, str)
        or not SHA.fullmatch(new_head)
        or not isinstance(path, str)
        or "\x00" in path
    ):
        return "Invalid path diff request."
    output = bounded_git(
        checkout,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        f"{old_head}..{new_head}",
        "--",
        path,
        max_bytes=max_bytes,
        attr_source=new_head,
    )
    return output[:max_bytes].decode(errors="replace") + (
        "\n[Path diff truncated]" if len(output) > max_bytes else ""
    )


def file_excerpt_at(checkout, commit, path, line, context=30):
    if (
        not isinstance(commit, str)
        or not SHA.fullmatch(commit)
        or not isinstance(path, str)
        or "\x00" in path
        or type(line) is not int
        or line < 1
    ):
        return "Invalid file excerpt request."
    try:
        files = tracked_files_at(checkout, commit)
    except subprocess.CalledProcessError:
        return f"{path} is unavailable at {commit}."
    return f"{path} at {commit} near line {line}:\n" + read_file(
        checkout, files, path, max(1, line - context)
    )


def release_review_refs(checkout, ref_prefix):
    """Delete the base and head fetch refs for one finished review attempt."""
    base_ref = f"{ref_prefix}/base"
    head_ref = f"{ref_prefix}/head"
    git(checkout, "check-ref-format", base_ref)
    git(checkout, "check-ref-format", head_ref)
    subprocess.run(
        git_command(checkout, "update-ref", "--stdin"),
        input=f"start\ndelete {base_ref}\ndelete {head_ref}\nprepare\ncommit\n",
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
        env=git_environment(),
    )


def tracked_files(checkout, ref="HEAD"):
    """Map tracked regular paths at a Git object to blob IDs."""
    return tracked_files_at(checkout, ref)


def tracked_files_at(checkout, ref):
    entries = git(checkout, "ls-tree", "-r", "-z", "--full-tree", ref).split("\x00")
    files = {}
    for entry in entries:
        if not entry:
            continue
        metadata, path = entry.split("\t", 1)
        mode, kind, blob = metadata.split()
        if kind == "blob" and mode in {"100644", "100755"}:
            files[path] = blob
    return files


def find_paths(files, query):
    if (
        not isinstance(query, str)
        or not 1 <= len(query) <= 100
        or "\n" in query
        or "\r" in query
        or "\x00" in query
    ):
        return "Path query must be 1 to 100 characters on one line."
    result = ""
    for path in files:
        if query.casefold() not in path.casefold():
            continue
        line = f"{path}\n"
        if len((result + line).encode()) > MAX_TOOL_BYTES:
            return result + "[Results truncated]"
        result += line
    return result or "No matching tracked files."


def read_file(checkout, files, path, start_line):
    if (
        not isinstance(path, str)
        or path not in files
        or not isinstance(start_line, int)
        or isinstance(start_line, bool)
        or start_line < 1
    ):
        return "Invalid path or line. Only tracked regular files can be read."
    size = int(git(checkout, "cat-file", "-s", files[path]).strip())
    if size > MAX_FILE_BYTES:
        return f"File exceeds {MAX_FILE_BYTES} bytes."
    content = subprocess.run(
        git_command(checkout, "cat-file", "blob", files[path]),
        check=True,
        capture_output=True,
        timeout=30,
        env=git_environment(),
    ).stdout.decode(errors="replace")
    if "\x00" in content:
        return "Binary file cannot be read as text."
    lines = content.splitlines()
    if start_line > len(lines):
        return f"{path} has {len(lines)} lines."
    result = f"{path} ({len(lines)} lines):\n"
    for number in range(start_line, min(start_line + 150, len(lines) + 1)):
        line = f"{number}: {lines[number - 1]}\n"
        if len((result + line).encode()) > MAX_TOOL_BYTES:
            break
        result += line
    return result


def read_diff(checkout, changed_paths, base, head, path, start_line):
    if (
        not isinstance(path, str)
        or path not in changed_paths
        or not isinstance(start_line, int)
        or isinstance(start_line, bool)
        or not 1 <= start_line <= 100_000
    ):
        return "Invalid changed path or diff line."
    args = (
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        f"{base}..{head}",
        "--",
        path,
    )
    with object_git_command(checkout, args, head) as (command, env):
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        result = f"{path} (diff lines from {start_line}):\n"
        found = False
        stopped_early = False
        number, pending = 1, b""
        oversized = False
        deadline = time.monotonic() + 180
        try:
            while True:
                remaining = deadline - time.monotonic()
                if (
                    remaining <= 0
                    or not select.select([process.stdout], [], [], remaining)[0]
                ):
                    raise subprocess.TimeoutExpired(process.args, 180)
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                for piece in chunk.splitlines(keepends=True):
                    if number >= start_line and not oversized:
                        pending += piece
                        oversized = len(pending) > MAX_TOOL_BYTES
                    if not piece.endswith(b"\n"):
                        continue
                    if number >= start_line:
                        found = True
                        item = f"{number}: " + pending.decode(errors="replace")
                        if oversized or len((result + item).encode()) > MAX_TOOL_BYTES:
                            result += (
                                f"[Diff line {number} exceeds output limit; "
                                f"continue at {number + 1}]\n"
                            )
                            stopped_early = True
                            break
                        result += item
                        if number >= start_line + 149:
                            stopped_early = True
                            break
                    number += 1
                    pending, oversized = b"", False
                if stopped_early:
                    break
        except BaseException:
            if process.poll() is None:
                process.kill()
            raise
        finally:
            if process.poll() is None and (
                stopped_early or time.monotonic() >= deadline
            ):
                process.kill()
            process.wait(timeout=10)
            process.stdout.close()
        if not stopped_early and process.returncode:
            raise subprocess.CalledProcessError(process.returncode, process.args)
        return result if found else result + "No diff lines."


def search_code(checkout, head, query):
    if (
        not isinstance(query, str)
        or not 3 <= len(query) <= 100
        or "\n" in query
        or "\r" in query
        or "\x00" in query
    ):
        return "Search query must be 3 to 100 characters on one line."
    try:
        output = bounded_git(
            checkout,
            "grep",
            "--no-recurse-submodules",
            "-n",
            "-I",
            "-F",
            "-e",
            query,
            head,
            "--",
            max_bytes=MAX_TOOL_BYTES,
        )
    except subprocess.CalledProcessError as exc:
        if exc.returncode != 1:
            raise
        output = b""
    if not output:
        return "No matches in tracked text files."
    return output[:MAX_TOOL_BYTES].decode(errors="replace") + (
        "\n[Results truncated]" if len(output) > MAX_TOOL_BYTES else ""
    )


def blame_base(checkout, base_files, base, path, start_line):
    if (
        not isinstance(path, str)
        or path not in base_files
        or not isinstance(start_line, int)
        or isinstance(start_line, bool)
        or not 1 <= start_line <= 100_000
    ):
        return "Invalid path or line at the comparison base."
    try:
        output = bounded_git(
            checkout,
            "blame",
            "--no-textconv",
            "--no-progress",
            "--line-porcelain",
            "-L",
            f"{start_line},+20",
            base,
            "--",
            path,
            max_bytes=MAX_FILE_BYTES,
            attr_source=base,
        ).decode(errors="replace")
    except subprocess.CalledProcessError:
        return "No lines at that location in the comparison base."
    summaries = {}
    lines = []
    current = None
    for line in output.splitlines():
        header = re.match(r"^([0-9a-f]{40}) \d+ (\d+)(?: \d+)?$", line)
        if header:
            current = (header.group(1), int(header.group(2)))
        elif line.startswith("summary ") and current:
            summaries[current[0]] = line[8:]
        elif line.startswith("\t") and current:
            lines.append((current[0], current[1], line[1:]))
    result = f"{path} at comparison base {base}:\n"
    for commit, number, content in lines:
        item = f"{number}: {commit} {summaries.get(commit, '')}: {content}\n"
        if len((result + item).encode()) > MAX_TOOL_BYTES:
            return result + "[Results truncated]"
        result += item
    return result if lines else "No blame results."


def read_commit(checkout, base_files, base, commit, path):
    if (
        not isinstance(commit, str)
        or not SHA.fullmatch(commit)
        or not isinstance(path, str)
        or path not in base_files
    ):
        return "Invalid commit or path at the comparison base."
    ancestor = subprocess.run(
        git_command(checkout, "merge-base", "--is-ancestor", commit, base),
        capture_output=True,
        timeout=30,
        env=git_environment(),
    )
    if ancestor.returncode:
        return "Commit is not an ancestor of the comparison base."
    output = bounded_git(
        checkout,
        "show",
        "--format=fuller",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        commit,
        "--",
        path,
        max_bytes=MAX_TOOL_BYTES,
        attr_source=commit,
    )
    return output[:MAX_TOOL_BYTES].decode(errors="replace") + (
        "\n[Commit output truncated]" if len(output) > MAX_TOOL_BYTES else ""
    )


def audit_developer_notes(snapshot):
    checkout, base = snapshot.checkout, snapshot.diff_base_sha
    try:
        notes = bounded_git(
            checkout,
            "show",
            f"{base}:doc/developer-notes.md",
            max_bytes=MAX_AUDIT_DOC_BYTES,
        ).decode(errors="replace")
    except subprocess.CalledProcessError:
        return "Developer notes are unavailable at the comparison base."
    content = notes.encode()[:MAX_AUDIT_DOC_BYTES].decode(errors="replace")
    return content + (
        "\n[Developer notes truncated]"
        if len(notes.encode()) > MAX_AUDIT_DOC_BYTES
        else ""
    )


def is_test_path(path):
    return (
        path.startswith(("test/", "tests/", "qa/", "src/test/", "src/wallet/test/"))
        or "/test/" in path
        or "/tests/" in path
    )


def focused_review_input(review, snapshot, name):
    if f"Patch exceeds {MAX_REVIEW_BYTES} input bytes. Use read_diff" not in review:
        return review
    checkout, base, head = snapshot.checkout, snapshot.diff_base_sha, snapshot.head_sha
    paths = sorted(snapshot.changed_paths)
    if name == "tests":
        paths.sort(key=lambda path: not is_test_path(path))
    elif name == "design":
        paths.sort(key=lambda path: is_test_path(path) or path.startswith("doc/"))
    excerpt = []
    size = 0
    for path in paths:
        patch = bounded_git(
            checkout,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            f"{base}..{head}",
            "--",
            path,
            max_bytes=MAX_FOCUSED_DIFF_BYTES,
            attr_source=head,
        ).decode(errors="replace")
        if size + len(patch.encode()) > MAX_FOCUSED_DIFF_BYTES:
            excerpt.append(
                patch.encode()[: MAX_FOCUSED_DIFF_BYTES - size].decode(errors="replace")
            )
            excerpt.append(
                "\n[Diff excerpt incomplete. Use read_diff for the remaining "
                "content of this file and later paths.]\n"
            )
            break
        excerpt.append(patch)
        size += len(patch.encode())
    return review + "\nRelevant diff excerpt:\n" + "".join(excerpt)


@dataclass(frozen=True)
class RepositorySnapshot:
    checkout: Path
    base_sha: str
    head_sha: str
    merge_base: str | None
    diff_base_sha: str
    head_files: object
    base_files: object
    changed_paths: frozenset[str]


def resolve_commit(checkout, revision):
    if not isinstance(revision, str) or not revision or revision.startswith("-"):
        raise ValueError("Revision must name an explicit commit")
    result = subprocess.run(
        git_command(
            checkout,
            "-c",
            "core.warnAmbiguousRefs=true",
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{revision}^{{commit}}",
        ),
        capture_output=True,
        text=True,
        timeout=30,
        env=git_environment(),
    )
    if result.returncode or "ambiguous" in result.stderr.lower():
        raise ValueError(
            f"Revision is unavailable, ambiguous, or not a commit: {revision}"
        )
    commit = result.stdout.strip()
    if not SHA.fullmatch(commit):
        raise ValueError("Unsupported Git object ID")
    return commit


def verify_local_objects(checkout, base_sha, head_sha):
    """Require complete reachable trees and history without any object fetching."""
    try:
        if git(checkout, "rev-parse", "--is-shallow-repository").strip() == "true":
            raise ValueError(
                "Local Git history is shallow; acquire complete history first"
            )
        subprocess.run(
            git_command(
                checkout,
                "rev-list",
                "--objects",
                "--quiet",
                "--missing=error",
                base_sha,
                head_sha,
            ),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=300,
            env=git_environment(),
        )
    except subprocess.CalledProcessError as exc:
        raise ValueError(
            "Local Git objects or history are missing; acquire them first"
        ) from exc


def snapshot_repository(checkout, base_sha, head_sha, *, exact_endpoints=False):
    """Build immutable evidence after the caller has retained the endpoint objects."""
    checkout = Path(checkout).resolve()
    base_sha = resolve_commit(checkout, base_sha)
    head_sha = resolve_commit(checkout, head_sha)
    verify_local_objects(checkout, base_sha, head_sha)
    try:
        bases = git(checkout, "merge-base", "--all", base_sha, head_sha).splitlines()
    except subprocess.CalledProcessError as exc:
        if exc.returncode != 1:
            raise
        bases = []
    merge_base = bases[0] if len(bases) == 1 else None
    if not exact_endpoints and merge_base is None:
        raise ValueError("Branch comparison requires a unique merge base")
    diff_base = base_sha if exact_endpoints else merge_base
    head_files = MappingProxyType(tracked_files_at(checkout, head_sha))
    base_files = MappingProxyType(tracked_files_at(checkout, diff_base))
    changed = git(
        checkout,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--name-only",
        "-z",
        diff_base,
        head_sha,
        "--",
    )
    return RepositorySnapshot(
        checkout,
        base_sha,
        head_sha,
        merge_base,
        diff_base,
        head_files,
        base_files,
        frozenset(path for path in changed.split("\x00") if path),
    )


@contextmanager
def pin_snapshot(snapshot):
    """Retain endpoint histories until all work using this snapshot has stopped."""
    prefix = f"refs/ralph/local/{uuid.uuid4().hex}"
    subprocess.run(
        git_command(snapshot.checkout, "update-ref", "--stdin"),
        input=(
            f"start\ncreate {prefix}/base {snapshot.base_sha}\n"
            f"create {prefix}/head {snapshot.head_sha}\nprepare\ncommit\n"
        ),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env=git_environment(),
    )
    try:
        yield snapshot
    finally:
        release_review_refs(snapshot.checkout, prefix)


@contextmanager
def local_snapshot(checkout, target, *, base=None):
    """Resolve local branch/range input once and retain it for a review lifetime."""
    checkout = Path(checkout).resolve()
    try:
        git(checkout, "rev-parse", "--git-dir")
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise ValueError("Review path is not a Git repository") from exc
    if not isinstance(target, str) or "..." in target:
        raise ValueError("Use an explicit base..head range or a branch with --base")
    if ".." in target:
        if base is not None or target.count("..") != 1:
            raise ValueError("A range cannot be combined with --base")
        base_ref, head_ref = target.split("..")
        if not base_ref or not head_ref:
            raise ValueError("Both range endpoints must be explicit commits")
        exact = True
    else:
        if base is None:
            raise ValueError("Branch comparison requires --base")
        base_ref, head_ref, exact = base, target, False
    base_sha = resolve_commit(checkout, base_ref)
    head_sha = resolve_commit(checkout, head_ref)
    # Pin before traversing history, so concurrent branch movement cannot unretain it.
    endpoints = RepositorySnapshot(
        checkout,
        base_sha,
        head_sha,
        None,
        base_sha,
        MappingProxyType({}),
        MappingProxyType({}),
        frozenset(),
    )
    with pin_snapshot(endpoints):
        yield snapshot_repository(checkout, base_sha, head_sha, exact_endpoints=exact)


def build_review_input(snapshot, title="", description=""):
    """Return bounded committed review evidence and an optional skip reason."""
    checkout, base, head = snapshot.checkout, snapshot.diff_base_sha, snapshot.head_sha
    commits = bounded_git(
        checkout,
        "log",
        "--reverse",
        "--format=%H%n%B%n%x00",
        f"{base}..{head}",
        max_bytes=MAX_REVIEW_BYTES,
    ).decode(errors="replace")
    prelude = f"Title: {title}\nDescription:\n{description}\nCommits:\n{commits}\n"
    patch = bounded_git(
        checkout,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--binary",
        base,
        head,
        "--",
        max_bytes=MAX_REVIEW_BYTES,
        attr_source=head,
    ).decode(errors="replace")
    review = f"{prelude}Patch:\n{patch}"
    if len(review.encode()) > MAX_REVIEW_BYTES:
        changed = bounded_git(
            checkout,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--name-status",
            base,
            head,
            "--",
            max_bytes=MAX_REVIEW_BYTES,
            attr_source=head,
        ).decode(errors="replace")
        review = (
            f"{prelude}Patch exceeds {MAX_REVIEW_BYTES} input bytes. "
            "Use read_diff to inspect changed files.\nChanged files:\n" + changed
        )
        if len(review.encode()) > MAX_REVIEW_BYTES:
            return None, f"Review input exceeds {MAX_REVIEW_BYTES} bytes"
    return review, None
