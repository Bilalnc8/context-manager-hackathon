"""Publish a rendered company context.md to its destinations: a local folder (optionally
a git repo with one commit per change) and/or S3.

Every destination compares SHA-256 hashes first and writes only when the content
differs, so an unchanged render never creates a new file version, commit or S3 object
version. The change message ("atlas: REQ-001 modify (email <id>, 2026-11-19)") becomes
the git commit message and the S3 object's "change" metadata.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from emailctx.config import S3Settings, Settings

FILENAME = "context.md"


@dataclass(frozen=True)
class PublishResult:
    destination: str  # file path or s3:// URI
    sha256: str
    written: bool


class Publisher(Protocol):
    def publish(self, company_id: str, content: str, message: str = "") -> PublishResult: ...


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LocalPublisher:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, company_id: str) -> Path:
        return self.root / company_id / FILENAME

    def publish(self, company_id: str, content: str, message: str = "") -> PublishResult:
        path = self.path(company_id)
        data = content.encode("utf-8")
        digest = sha256_hex(data)
        if path.exists() and sha256_hex(path.read_bytes()) == digest:
            return PublishResult(str(path), digest, written=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)  # bytes: no platform newline translation
        return PublishResult(str(path), digest, written=True)


class GitPublisher(LocalPublisher):
    """LocalPublisher whose root is its own git repo: one commit per changed file.

    The root is initialized as a repo if it isn't one. Keep it out of any other repo's
    working tree (this project's .gitignore excludes companies/).
    """

    AUTHOR = ["-c", "user.name=emailctx", "-c", "user.email=emailctx@localhost"]

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        root.mkdir(parents=True, exist_ok=True)
        if not (root / ".git").exists():
            self._git("init", "--quiet")

    def _git(self, *args: str) -> str:
        done = subprocess.run(
            ["git", *self.AUTHOR, *args],
            cwd=self.root, capture_output=True, text=True, check=False,
        )
        if done.returncode != 0:
            raise RuntimeError(f"git {args[0]} failed: {done.stderr.strip()}")
        return done.stdout

    def publish(self, company_id: str, content: str, message: str = "") -> PublishResult:
        result = super().publish(company_id, content, message)
        if result.written:
            rel = f"{company_id}/{FILENAME}"
            self._git("add", "--", rel)
            self._git("commit", "--quiet", "-m", message or f"{company_id}: update", "--", rel)
        return result


class S3Publisher:
    """Writes s3://<bucket>/<prefix><company_id>/context.md.

    The rendered file's SHA-256 is stored in the object's user metadata and compared
    on the next publish (an S3 ETag is not a SHA-256, and isn't even an MD5 for
    multipart or KMS-encrypted uploads). Credentials come from boto3's normal chain
    (env vars, ~/.aws, the configured profile) and are never logged.
    """

    META_KEY = "sha256"

    def __init__(self, s3: S3Settings, client: Any | None = None) -> None:
        if not s3.bucket:
            raise ValueError("S3 output needs output.s3.bucket in config")
        self.bucket = s3.bucket
        self.prefix = s3.prefix
        if client is None:
            import boto3

            session = boto3.Session(profile_name=s3.profile, region_name=s3.region)
            client = session.client("s3")
        self.client = client

    def key(self, company_id: str) -> str:
        return f"{self.prefix}{company_id}/{FILENAME}"

    def _current_hash(self, key: str) -> str | None:
        from botocore.exceptions import ClientError

        try:
            head = self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return head.get("Metadata", {}).get(self.META_KEY)

    def publish(self, company_id: str, content: str, message: str = "") -> PublishResult:
        key = self.key(company_id)
        uri = f"s3://{self.bucket}/{key}"
        data = content.encode("utf-8")
        digest = sha256_hex(data)
        if self._current_hash(key) == digest:
            return PublishResult(uri, digest, written=False)
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType="text/markdown; charset=utf-8",
            Metadata=self._metadata(digest, message),
        )
        return PublishResult(uri, digest, written=True)

    def _metadata(self, digest: str, message: str) -> dict[str, str]:
        meta = {self.META_KEY: digest}
        if message:
            # S3 user metadata must be ASCII and fits in 2 KB with room to spare here.
            meta["change"] = message.encode("ascii", "replace").decode("ascii")[:1024]
        return meta


def publishers_from_settings(
    settings: Settings,
    s3_client: Any | None = None,
    *,
    drive_store: Any | None = None,
    company_name: Any | None = None,
) -> list[Publisher]:
    """Destinations from config. Drive needs a signed-in DriveStore and a callable that
    returns a company's display name (folder and file are named after it)."""
    out: list[Publisher] = []
    if settings.output.drive.enabled:
        if drive_store is None or company_name is None:
            raise ValueError("Drive output is enabled but there is no signed-in Drive client")
        from emailctx.drive import DrivePublisher

        out.append(DrivePublisher(drive_store, company_name))
    if settings.output.local_dir is not None:
        local_cls = GitPublisher if settings.output.git_commit else LocalPublisher
        out.append(local_cls(settings.output.local_dir))
    elif settings.output.git_commit:
        raise ValueError("output.git_commit needs output.local_dir")
    if settings.output.s3.bucket:
        out.append(S3Publisher(settings.output.s3, client=s3_client))
    return out
