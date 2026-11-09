"""Service deployment configuration."""

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


def default_repository_url(forgejo_api):
    api_marker = "/api/v1/repos/"
    forgejo_api = forgejo_api.rstrip("/")
    if api_marker not in forgejo_api:
        return None
    base_url, repository = forgejo_api.split(api_marker, 1)
    return f"{base_url.rstrip('/')}/{repository}".rstrip("/")


def pull_request_url(repository_url, number):
    path = "pull" if urlsplit(repository_url).hostname == "github.com" else "pulls"
    return f"{repository_url.rstrip('/')}/{path}/{number}"


def default_comment_marker(repository):
    return f"<!-- ralph:{repository} -->"


@dataclass(frozen=True)
class BotConfig:
    origin: str
    repository: str
    forgejo_api: str
    repository_url: str | None = None
    comment_marker: str | None = None
    report_dir: Path | None = None
    report_base_url: str | None = None
    archive_base_url: str | None = None
    github_repository: str | None = None
    repo_id: str = "core"
    submodules: dict = field(default_factory=dict)
    experiment_shell: str | None = None

    def __post_init__(self):
        if self.github_repository is not None:
            if not re.fullmatch(
                r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.github_repository
            ):
                raise ValueError("github_repository must be owner/repo")
        if not self.origin or not self.repository or not self.forgejo_api:
            raise ValueError("origin, repository, and forgejo_api are required")
        object.__setattr__(self, "forgejo_api", self.forgejo_api.rstrip("/"))
        url = self.repository_url or default_repository_url(self.forgejo_api)
        if not url:
            raise ValueError(
                "repository_url is required when forgejo_api is not a repository API URL"
            )
        object.__setattr__(self, "repository_url", url)
        object.__setattr__(
            self,
            "comment_marker",
            self.comment_marker or default_comment_marker(self.repository),
        )
        if not (
            (self.report_dir is None)
            == (self.report_base_url is None)
            == (self.archive_base_url is None)
        ):
            raise ValueError(
                "report_dir, report_base_url and archive_base_url must be set together"
            )
        for name in ("report_base_url", "archive_base_url"):
            value = getattr(self, name)
            if value is None:
                continue
            public_url = urlsplit(value)
            if (
                public_url.scheme not in {"http", "https"}
                or not public_url.netloc
                or public_url.username
                or public_url.password
                or public_url.query
                or public_url.fragment
            ):
                raise ValueError(
                    f"{name} must be an HTTP URL without credentials, query or fragment"
                )
            object.__setattr__(self, name, value.rstrip("/"))


def repository_config(
    entry, report_dir=None, report_base_url=None, archive_base_url=None
):
    """Construct review context without reading runtime credentials."""
    return BotConfig(
        **{
            key: entry[key]
            for key in (
                "origin",
                "repository",
                "forgejo_api",
                "repository_url",
                "comment_marker",
                "github_repository",
                "repo_id",
                "submodules",
                "experiment_shell",
            )
            if key in entry
        },
        report_dir=report_dir,
        report_base_url=report_base_url,
        archive_base_url=archive_base_url,
    )


def repository_state_dir(state_dir, repo_id):
    return Path(state_dir) if repo_id == "core" else Path(state_dir) / "repos" / repo_id


def load_repositories(path):
    """Load the explicit repository registry shared by service and operators."""
    entries = json.loads(Path(path).read_text())
    if not isinstance(entries, dict) or not entries:
        raise ValueError("repositories must be a nonempty object")
    sources = set()
    result = {}
    for repo_id, raw in entries.items():
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", repo_id):
            raise ValueError(f"invalid repository ID: {repo_id}")
        if not isinstance(raw, dict):
            raise ValueError(f"invalid repository configuration: {repo_id}")
        if "github_post_major_findings" in raw:
            raise ValueError(
                "replace github_post_major_findings with github_publication"
            )
        if raw.get("github_publication", "off") not in ("off", "major-only", "full"):
            raise ValueError("github_publication must be off, major-only or full")
        entry = {**raw, "repo_id": repo_id}
        bot = repository_config(entry)
        source = bot.forgejo_api
        if source in sources:
            raise ValueError("a source repository cannot have multiple IDs")
        sources.add(source)
        entry["repository_url"] = bot.repository_url
        if (
            type(entry.get("max_workers", 1)) is not int
            or entry.get("max_workers", 1) < 1
        ):
            raise ValueError("repository max_workers must be positive")
        if entry.get("strategy", "core") not in {"core", "gui"}:
            raise ValueError("unknown repository review strategy")
        if not isinstance(entry.get("engine_config", {}), dict):
            raise ValueError("engine_config must be an object")
        if not isinstance(entry.get("submodules", {}), dict):
            raise ValueError("submodules must map paths to trusted remote URLs")
        if entry.get("github_publication", "off") != "off" and not (
            entry.get("github_repository") and entry.get("github_token_file")
        ):
            raise ValueError("GitHub publication requires a repository and credentials")
        result[repo_id] = entry
    return result
