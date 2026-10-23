"""Service deployment configuration."""

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def default_repository_url(forgejo_api):
    api_marker = "/api/v1/repos/"
    forgejo_api = forgejo_api.rstrip("/")
    if api_marker not in forgejo_api:
        return None
    base_url, repository = forgejo_api.split(api_marker, 1)
    return f"{base_url.rstrip('/')}/{repository}".rstrip("/")


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

    def __post_init__(self):
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
        if (self.report_dir is None) != (self.report_base_url is None):
            raise ValueError("report_dir and report_base_url must be set together")
        if self.report_base_url is not None:
            report_url = urlsplit(self.report_base_url)
            if (
                report_url.scheme not in {"http", "https"}
                or not report_url.netloc
                or report_url.username
                or report_url.password
                or report_url.query
                or report_url.fragment
            ):
                raise ValueError(
                    "report_base_url must be an HTTP URL without credentials, query or fragment"
                )
            object.__setattr__(
                self, "report_base_url", self.report_base_url.rstrip("/")
            )
