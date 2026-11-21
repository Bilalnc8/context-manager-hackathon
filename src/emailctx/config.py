"""Settings loaded from config.yaml. Model names and thresholds live there, not in code."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Generic, Literal, TypeVar

import yaml
from pydantic import BaseModel, Field

Task = Literal["gate", "extract", "judge"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]
T = TypeVar("T")


class PerTask(BaseModel, Generic[T]):
    gate: T
    extract: T
    judge: T

    def for_task(self, task: Task) -> T:
        return getattr(self, task)


class PipelineSettings(BaseModel):
    max_extract_retries: int = 1
    min_evidence_chars: int = 5
    judge_top_k: int = 20


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


class OutputSettings(BaseModel):
    local_dir: Path | None = Path("companies")
    s3: S3Settings = Field(default_factory=S3Settings)


class Settings(BaseModel):
    db_path: Path = Path("emailctx.db")
    output: OutputSettings = Field(default_factory=OutputSettings)
    models: PerTask[str] = PerTask[str](
        gate="claude-haiku-5-5", extract="claude-opus-5-5", judge="claude-haiku-5-5"
    )
    effort: PerTask[Effort] = PerTask[Effort](gate="low", extract="high", judge="low")
    max_tokens: PerTask[int] = PerTask[int](gate=1024, extract=16000, judge=1024)
    fallbacks: PerTask[str | None] = PerTask[str | None](
        gate=None, extract="default", judge=None
    )
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
        return self.model_copy(
            update={
                "db_path": self.db_path if self.db_path.is_absolute() else base / self.db_path,
                "output": self.output.model_copy(update={"local_dir": local}),
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
