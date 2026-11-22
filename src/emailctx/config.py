"""Settings loaded from config.yaml. Model names and thresholds live there, not in code."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Generic, Literal, TypeVar

import yaml
from pydantic import BaseModel, Field, field_validator

Task = Literal["gate", "extract", "judge"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]
T = TypeVar("T")


class PerTask(BaseModel, Generic[T]):
    gate: T
    extract: T
    judge: T

    def for_task(self, task: Task) -> T:
        return getattr(self, task)


DEFAULT_PERSONAL_PROVIDERS = [
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
    "yahoo.com", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com",
]


DEFAULT_MEETING_BOTS = [
    "fireflies.ai", "otter.ai", "fathom.video", "read.ai", "tldv.io", "meetgeek.ai",
    "avoma.com", "gong.io", "grain.com", "krisp.ai", "tactiq.io",
]


class RoutingSettings(BaseModel):
    personal_providers: list[str] = Field(
        default_factory=lambda: list(DEFAULT_PERSONAL_PROVIDERS)
    )
    # Meeting-summary bots: their domain says nothing about the client, so their emails
    # are matched on company/project names instead.
    meeting_bot_domains: list[str] = Field(default_factory=lambda: list(DEFAULT_MEETING_BOTS))
    require_dmarc_pass: bool = False


class PipelineSettings(BaseModel):
    max_extract_retries: int = 1
    min_evidence_chars: int = 5
    judge_top_k: int = 20
    max_attachment_chars: int = 100_000  # per email, across readable attachments


DEFAULT_CATEGORIES = [
    "functional",
    "non-functional",
    "data",
    "integration",
    "ui",
    "constraint",
    "other",
]


class S3Settings(BaseModel):
    bucket: str | None = None
    prefix: str = ""
    region: str | None = None
    profile: str | None = None


class DriveSettings(BaseModel):
    enabled: bool = False
    root_folder: str = "Client Context"  # created by the app in My Drive


class OutputSettings(BaseModel):
    local_dir: Path | None = Path("companies")
    git_commit: bool = False  # make local_dir a git repo and commit each changed file
    s3: S3Settings = Field(default_factory=S3Settings)
    drive: DriveSettings = Field(default_factory=DriveSettings)


GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
DRIVE_READONLY = "https://www.googleapis.com/auth/drive.readonly"
# The one write scope allowed: access only to files and folders this app created.
DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"
ALLOWED_SCOPES = frozenset({GMAIL_READONLY, DRIVE_READONLY, DRIVE_FILE})
READONLY_SCOPES = ALLOWED_SCOPES  # kept for callers; Gmail is always read-only


class GoogleSettings(BaseModel):
    client_secrets: Path = Path("client_secret.json")
    scopes: list[str] = Field(default_factory=lambda: [GMAIL_READONLY])

    @field_validator("scopes")
    @classmethod
    def _read_only(cls, scopes: list[str]) -> list[str]:
        extra = set(scopes) - ALLOWED_SCOPES
        if extra:
            raise ValueError(
                "only read-only Google scopes (plus drive.file for the app's own Drive "
                f"folders) are allowed, not: {sorted(extra)}"
            )
        if not scopes:
            raise ValueError("at least one Google scope is required")
        return scopes


class GmailSettings(BaseModel):
    # Gmail search for tracked mail. None = built from the registry (company domains,
    # contacts, keywords, meeting bots). "" = all mail.
    query: str | None = None
    backfill_max: int | None = 200


class BootstrapSettings(BaseModel):
    """Building a company's first context.md from recent mail."""

    days: int = 100
    max_per_company: int | None = 500


class Settings(BaseModel):
    db_path: Path = Path("emailctx.db")
    google: GoogleSettings = Field(default_factory=GoogleSettings)
    gmail: GmailSettings = Field(default_factory=GmailSettings)
    bootstrap: BootstrapSettings = Field(default_factory=BootstrapSettings)
    output: OutputSettings = Field(default_factory=OutputSettings)
    models: PerTask[str] = PerTask[str](
        gate="claude-haiku-5-5", extract="claude-opus-5-5", judge="claude-haiku-5-5"
    )
    effort: PerTask[Effort] = PerTask[Effort](gate="low", extract="high", judge="low")
    max_tokens: PerTask[int] = PerTask[int](gate=1024, extract=16000, judge=1024)
    fallbacks: PerTask[str | None] = PerTask[str | None](
        gate=None, extract="default", judge=None
    )
    routing: RoutingSettings = Field(default_factory=RoutingSettings)
    pipeline: PipelineSettings = Field(default_factory=PipelineSettings)
    categories: list[str] = Field(default_factory=lambda: list(DEFAULT_CATEGORIES))

    def model_post_init(self, __context: object) -> None:
        if "other" not in self.categories:
            self.categories.append("other")

    def resolve_paths(self, base: Path) -> Settings:
        """Return a copy with relative paths resolved against `base`."""
        local = self.output.local_dir
        if local is not None and not local.is_absolute():
            local = base / local
        secrets = self.google.client_secrets
        if not secrets.is_absolute():
            secrets = base / secrets
        return self.model_copy(
            update={
                "db_path": self.db_path if self.db_path.is_absolute() else base / self.db_path,
                "output": self.output.model_copy(update={"local_dir": local}),
                "google": self.google.model_copy(update={"client_secrets": secrets}),
            }
        )


def load_settings(path: Path | None = None) -> Settings:
    """Load settings from `path`, $EMAILCTX_CONFIG, or ./config.yaml (in that order).

    With no config file, defaults apply and paths resolve against the working directory.
    """
    if path is None:
        env = os.environ.get("EMAILCTX_CONFIG")
        path = Path(env) if env else Path("config.yaml")
    if not path.exists():
        return Settings().resolve_paths(Path.cwd())
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Settings.model_validate(data).resolve_paths(path.resolve().parent)
