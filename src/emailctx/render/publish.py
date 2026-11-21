"""Publish a rendered company context.md to its destinations: a local folder and/or S3.

Every destination compares SHA-256 hashes first and writes only when the content
differs, so an unchanged render never creates a new file version or S3 object version.
"""

from __future__ import annotations

import hashlib
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
    def publish(self, company_id: str, content: str) -> PublishResult: ...


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LocalPublisher:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, company_id: str) -> Path:
        return self.root / company_id / FILENAME

    def publish(self, company_id: str, content: str) -> PublishResult:
        path = self.path(company_id)
        data = content.encode("utf-8")
        digest = sha256_hex(data)
        if path.exists() and sha256_hex(path.read_bytes()) == digest:
            return PublishResult(str(path), digest, written=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)  # bytes: no platform newline translation
        return PublishResult(str(path), digest, written=True)


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

    def publish(self, company_id: str, content: str) -> PublishResult:
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
            Metadata={self.META_KEY: digest},
        )
        return PublishResult(uri, digest, written=True)


def publishers_from_settings(settings: Settings, s3_client: Any | None = None) -> list[Publisher]:
    out: list[Publisher] = []
    if settings.output.local_dir is not None:
        out.append(LocalPublisher(settings.output.local_dir))
    if settings.output.s3.bucket:
        out.append(S3Publisher(settings.output.s3, client=s3_client))
    return out
