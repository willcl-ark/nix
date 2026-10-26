"""Classify backports and compare their immutable local Git objects."""

import re
import subprocess

from . import repository

MAX_COMMITS = 200
MAX_PACKET_BYTES = 160_000
MAX_PATCH_BYTES = 12_000
PR = re.compile(
    r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)"
    r"|\b([\w.-]+/[\w.-]+)#(\d+)"
    r"|\bgui#(\d+)|#gui(\d+)|(?<![\w/])#(\d+)",
    re.IGNORECASE,
)
TRAILER = re.compile(
    r"^(Rebased-From|Github-Pull):\s*(.*?)\s*$", re.IGNORECASE | re.MULTILINE
)


def _prs(text, repo):
    result = []
    for match in PR.finditer(text):
        owner, number, qualified, qnumber, gui, gui2, short = match.groups()
        key = f"{owner or qualified or ('bitcoin-core/gui' if gui or gui2 else repo)}#{number or qnumber or gui or gui2 or short}"
        if len(key) <= 256 and key not in result:
            result.append(key)
    return result


def _commits(snapshot):
    shas = repository.git(
        snapshot.checkout,
        "rev-list",
        "--reverse",
        f"{snapshot.base_sha}..{snapshot.head_sha}",
    ).splitlines()
    rows = []
    for sha in shas[:MAX_COMMITS]:
        message = repository.bounded_git(
            snapshot.checkout, "show", "-s", "--format=%B", sha, max_bytes=16_000
        ).decode(errors="replace")
        trailers = {}
        for name, value in TRAILER.findall(message):
            trailers.setdefault(name.lower(), []).append(value)
        rows.append((sha, message, trailers))
    return rows, len(shas) > MAX_COMMITS


def _declared(metadata):
    # Only explicit backport prose and its following list declare expected PRs.
    result = []
    active = False
    repo = metadata.get("repository", "bitcoin/bitcoin")
    text = metadata.get("title", "") + "\n" + metadata.get("body", "")
    for line in text[:MAX_PACKET_BYTES].splitlines():
        mentions = bool(re.search(r"\bbackport(?:s|ed|ing)?\b", line, re.IGNORECASE))
        if mentions:
            active = True
        elif line.strip() and not re.match(
            r"\s*(?:(?:[-*]|\d+\.)\s|https://github\.com/|#\d|gui#|#gui)", line
        ):
            active = False
        if active:
            result.extend(_prs(line, repo))
    return list(dict.fromkeys(result))[:MAX_COMMITS]


def classify(snapshot, metadata=None):
    metadata = metadata or {}
    provenance = repository.git(
        snapshot.checkout,
        "log",
        "--max-count=1",
        "--format=%H",
        "--regexp-ignore-case",
        "--grep=^Rebased-From:",
        f"{snapshot.base_sha}..{snapshot.head_sha}",
    ).strip()
    labels = [
        item.get("name", "") if isinstance(item, dict) else item
        for item in metadata.get("labels", [])
    ]
    reasons = []
    if any(label.lower() == "backport" for label in labels):
        reasons.append("Backport label")
    if provenance:
        reasons.append("Rebased-From commit trailer")
    if re.fullmatch(r"\d+\.\d*(?:x)?", metadata.get("base_ref", "")) and re.search(
        r"\bbackport(?:s|ed|ing)?\b",
        metadata.get("title", "") + "\n" + metadata.get("body", ""),
        re.IGNORECASE,
    ):
        reasons.append("Release branch with explicit backport intent")
    return {
        "mode": "backport" if reasons else "normal",
        "reasons": reasons,
        "declared_prs": _declared(metadata),
    }


def source_requests(snapshot, metadata=None):
    metadata = metadata or {}
    prs = list(_declared(metadata))
    originals = []
    for sha, _, trailers in _commits(snapshot)[0]:
        refs = _prs(
            "\n".join(trailers.get("github-pull", [])),
            metadata.get("repository", "bitcoin/bitcoin"),
        )
        if "rebased-from" in trailers:
            prs.extend(refs)
            for original in trailers["rebased-from"]:
                originals.append(
                    {
                        "commit": sha,
                        "original": original,
                        "pr": refs[0] if len(refs) == 1 else None,
                    }
                )
    return {"prs": list(dict.fromkeys(prs)), "originals": originals}


def collect(snapshot, metadata=None, origins=None):
    classification = classify(snapshot, metadata)
    if classification["mode"] == "normal":
        return None
    metadata, origins = metadata or {}, origins or {}
    rows, truncated = _commits(snapshot)
    limitations = []
    if (
        len(metadata.get("body", "")) > MAX_PACKET_BYTES
        or len(classification["declared_prs"]) == MAX_COMMITS
    ):
        limitations.append("Declared backport inventory reached its size limit")
    if truncated:
        limitations.append(f"Commit inventory exceeds {MAX_COMMITS} commits")
    evidence = {
        "classification": classification,
        "base_sha": snapshot.base_sha,
        "head_sha": snapshot.head_sha,
        "commits": [],
        "sources": {},
        "untagged": [],
        "limitations": limitations,
    }
    budget = MAX_PACKET_BYTES

    def read(*args, attr_source=None):
        nonlocal budget
        limit = min(MAX_PATCH_BYTES, budget)
        if limit <= 0:
            if "Evidence byte budget exhausted" not in limitations:
                limitations.append("Evidence byte budget exhausted")
            return "[Evidence omitted]"
        try:
            output = repository.bounded_git(
                snapshot.checkout,
                *args,
                max_bytes=limit,
                attr_source=attr_source or snapshot.head_sha,
            )
        except (subprocess.SubprocessError, OSError) as error:
            limitations.append(f"Git evidence unavailable: {type(error).__name__}")
            return "[Evidence unavailable]"
        budget -= min(len(output), limit)
        if len(output) > limit:
            limitations.append(f"Truncated Git evidence: {' '.join(args[:3])}")
        return output[:limit].decode(errors="replace")

    def resolve(raw):
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", raw):
            raise ValueError("invalid original SHA")
        return repository.resolve_commit(snapshot.checkout, raw.lower())

    def patch(sha):
        return read(
            "show",
            "--format=fuller",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            sha,
            "--",
            attr_source=sha,
        )

    mapped = set()
    requested = source_requests(snapshot, metadata)["prs"]
    used_sources = set(classification["declared_prs"])
    for sha, message, trailers in rows:
        row = {
            "commit": sha,
            "subject": message.splitlines()[0][:500] if message else "",
            "patch": patch(sha),
        }
        budget -= 1_000  # Reserve space for provenance, subject and object IDs.
        evidence["commits"].append(row)
        raw = trailers.get("rebased-from", [])
        if not raw:
            evidence["untagged"].append(sha)
            continue
        if len(raw) != 1:
            limitations.append(f"{sha}: multiple Rebased-From trailers")
            continue
        row["original_raw"] = raw[0][:128]
        try:
            original = resolve(raw[0])
        except (ValueError, subprocess.SubprocessError):
            limitations.append(
                f"{sha}: unavailable, ambiguous or malformed original {raw[0][:128]}"
            )
            continue
        row["original"] = original
        mapped.add(original)
        candidates = [
            key for key, data in origins.items() if original in data.get("commits", [])
        ]
        upstream_candidates = [
            key for key in candidates if origins[key].get("base_ref") == "master"
        ]
        candidates = upstream_candidates or candidates
        if len(candidates) == 1:
            row["source_pr"] = candidates[0]
            used_sources.add(candidates[0])
        else:
            limitations.append(
                f"{sha}: source PR membership is {'ambiguous' if candidates else 'unverified'}"
            )
        row["original_patch"] = patch(original)
        parents = repository.git(
            snapshot.checkout, "rev-list", "--parents", "-n", "1", original
        ).split()[1:]
        backport_parents = repository.git(
            snapshot.checkout, "rev-list", "--parents", "-n", "1", sha
        ).split()[1:]
        if len(parents) == len(backport_parents) == 1:
            row["original_parent"] = parents[0]
            row["range_diff"] = read(
                "range-diff",
                "--creation-factor=100",
                "--no-color",
                "--no-ext-diff",
                "--no-textconv",
                f"{parents[0]}..{original}",
                f"{backport_parents[0]}..{sha}",
                "--",
            )
        else:
            limitations.append(
                f"{sha}: root or merge commit requires manual comparison"
            )

    # A wrong Github-Pull trailer may name the backport PR. Only require that
    # trailer's PR when it could not be resolved through original membership.
    if not mapped:
        limitations.append("No resolved Rebased-From provenance")
    if not evidence["commits"]:
        limitations.append("No commits in the pinned comparison")
    for key in requested:
        if key not in origins or origins[key].get("error"):
            limitations.append(f"{key}: source metadata unavailable")
    if len(used_sources) > MAX_COMMITS:
        limitations.append("Source PR inventory exceeds its size limit")
    for key in sorted(used_sources)[:MAX_COMMITS]:
        data = origins.get(key, {})
        source = {}
        for field in ("title", "body", "url"):
            value = data.get(field, "")
            limit = min(4_000, max(0, budget))
            source[field] = value[:limit]
            budget -= len(source[field].encode())
            if len(value) > limit:
                limitations.append(f"{key}: truncated {field}")
        original_commits = data.get("commits", [])
        commit_limit = min(MAX_COMMITS, max(0, budget // 200))
        source.update(
            commits=original_commits[:commit_limit], missing=[], already_in_base=[]
        )
        budget -= 200 * len(source["commits"])
        if len(original_commits) > commit_limit:
            limitations.append(f"{key}: truncated original commit list")
        evidence["sources"][key] = source
        if not source["commits"]:
            limitations.append(f"{key}: no verified original commit list")
        for original in source["commits"]:
            if original in mapped:
                continue
            if not repository.has_commit(snapshot.checkout, original):
                limitations.append(
                    f"{key}: original object unavailable: {str(original)[:128]}"
                )
                source["missing"].append(original)
                continue
            try:
                repository.git(
                    snapshot.checkout,
                    "merge-base",
                    "--is-ancestor",
                    original,
                    snapshot.base_sha,
                )
                present = True
            except subprocess.CalledProcessError:
                # Earlier cherry-picks may have different object IDs. Their
                # provenance is evidence of presence, subject to semantic review.
                present = False
                if repository.SHA.fullmatch(original):
                    matches = read(
                        "log",
                        "--format=%H%n%B",
                        "--extended-regexp",
                        "--regexp-ignore-case",
                        f"--grep=^Rebased-From: *{original[:7]}",
                        snapshot.base_sha,
                    )
                    for _, value in TRAILER.findall(matches):
                        try:
                            present |= resolve(value) == original
                        except (ValueError, subprocess.SubprocessError):
                            pass
            source["already_in_base" if present else "missing"].append(original)

    notes = (
        read("show", f"{snapshot.head_sha}:doc/release-notes.md")
        if "doc/release-notes.md" in snapshot.head_files
        else ""
    )
    evidence["release_notes"] = {
        "changed_paths": sorted(
            path
            for path in snapshot.changed_paths
            if path.startswith("doc/release-notes")
        ),
        "mentions": {
            key: bool(
                re.search(r"(?<!\d)" + re.escape(key.split("#")[1]) + r"(?!\d)", notes)
            )
            for key in used_sources
        },
    }
    evidence["complete"] = not limitations
    return evidence
