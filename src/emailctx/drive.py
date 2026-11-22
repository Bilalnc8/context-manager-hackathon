"""Google Drive storage: one folder per company.

    Client Context/                      (root folder, created by the app)
      Acme/
        Acme.md                          the requirements file (updated in place)
        Emails/
          2026-11-19 Re Atlas export requirements.txt
        Documents/
          2026-11-19 Meeting summary.pdf

Uses the drive.file scope: the app sees and edits only what it created itself. Every
file and folder it creates carries private appProperties (kind + key), which is how it
finds them again: no Drive IDs are kept locally, so the folder can be moved or renamed
by people without breaking anything. Email copies and attachments are written once;
the .md is updated in place only when its SHA-256 changes (Drive keeps its revisions).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from emailctx.clean import ParsedEmail, email_copy_text
from emailctx.render.publish import PublishResult

FOLDER_MIME = "application/vnd.google-apps.folder"
P_KIND = "emailctx_kind"
P_KEY = "emailctx_key"
P_SHA = "emailctx_sha256"
_RETRIES = 3
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def safe_name(name: str, limit: int = 120) -> str:
    name = _UNSAFE.sub("-", " ".join(name.split())).strip(" .-") or "untitled"
    return name[:limit].rstrip(" .-")


def _short_key(*parts: str) -> str:
    """appProperties allow ~124 bytes per key+value; hash anything long or arbitrary."""
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:40]


def _key_for(company_id: str) -> str:
    return company_id if len(company_id.encode()) <= 100 else _short_key(company_id)


def _q(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class DriveStore:
    def __init__(self, service: Any, root_name: str) -> None:
        self.service = service
        self.root_name = root_name
        self._cache: dict[tuple[str, str], dict] = {}

    def _run(self, request: Any) -> dict:
        return request.execute(num_retries=_RETRIES)

    def _files(self) -> Any:
        return self.service.files()

    # --- lookup ------------------------------------------------------------------------

    def find(self, kind: str, key: str, *, fresh: bool = False) -> dict | None:
        if not fresh and (kind, key) in self._cache:
            return self._cache[(kind, key)]
        q = (
            f"appProperties has {{ key='{P_KIND}' and value='{_q(kind)}' }} and "
            f"appProperties has {{ key='{P_KEY}' and value='{_q(key)}' }} and trashed = false"
        )
        found = self._run(
            self._files().list(
                q=q, spaces="drive", pageSize=10,
                fields="files(id, name, parents, appProperties)",
            )
        ).get("files", [])
        f = found[0] if found else None
        if f is None:
            self._cache.pop((kind, key), None)
        else:
            self._cache[(kind, key)] = f
        return f

    def _create(self, kind: str, key: str, name: str, parent: str | None, mime: str,
                data: bytes | None = None, extra: dict[str, str] | None = None) -> dict:
        body: dict[str, Any] = {
            "name": name,
            "mimeType": mime,
            "appProperties": {P_KIND: kind, P_KEY: key, **(extra or {})},
        }
        if parent:
            body["parents"] = [parent]
        kwargs: dict[str, Any] = {"body": body, "fields": "id, name, parents, appProperties"}
        if data is not None:
            from googleapiclient.http import MediaInMemoryUpload

            kwargs["media_body"] = MediaInMemoryUpload(data, mimetype=mime, resumable=False)
        f = self._run(self._files().create(**kwargs))
        self._cache[(kind, key)] = f
        return f

    # --- folders -----------------------------------------------------------------------

    def _folder(self, kind: str, key: str, name: str, parent: str | None) -> str:
        f = self.find(kind, key)
        if f is None:
            f = self._create(kind, key, name, parent, FOLDER_MIME)
        elif f.get("name") != name:
            f = self._run(self._files().update(fileId=f["id"], body={"name": name},
                                               fields="id, name, parents, appProperties"))
            self._cache[(kind, key)] = f
        return f["id"]

    def root(self) -> str:
        return self._folder("root", _short_key(self.root_name), self.root_name, None)

    def company_folder(self, company_id: str, company_name: str) -> str:
        return self._folder("company", _key_for(company_id), safe_name(company_name), self.root())

    def subfolder(self, company_id: str, company_name: str, title: str) -> str:
        parent = self.company_folder(company_id, company_name)
        return self._folder(f"folder:{title.lower()}", _key_for(company_id), title, parent)

    # --- files -------------------------------------------------------------------------

    def put(self, kind: str, key: str, parent: str, name: str, data: bytes, mime: str,
            *, replace: bool) -> tuple[dict, bool]:
        """Create the file, or (replace=True) update it if its content or name changed.
        Returns (file, written)."""
        digest = hashlib.sha256(data).hexdigest()
        existing = self.find(kind, key)
        if existing is None:
            return self._create(kind, key, name, parent, mime, data, {P_SHA: digest}), True
        if not replace:
            return existing, False
        same = (existing.get("appProperties") or {}).get(P_SHA) == digest
        if same and existing.get("name") == name:
            return existing, False
        from googleapiclient.http import MediaInMemoryUpload

        f = self._run(
            self._files().update(
                fileId=existing["id"],
                body={"name": name, "appProperties": {P_SHA: digest}},
                media_body=MediaInMemoryUpload(data, mimetype=mime, resumable=False),
                fields="id, name, parents, appProperties",
            )
        )
        self._cache[(kind, key)] = f
        return f, not same

    def has_context(self, company_id: str) -> bool:
        """Whether the company's .md exists in Drive (checked live, not from cache)."""
        return self.find("context", _key_for(company_id), fresh=True) is not None

    def list_contexts(self) -> list[dict]:
        """Every company .md the app created (not trashed): id, name, key, modifiedTime."""
        out: list[dict] = []
        token = None
        while True:
            kwargs: dict[str, Any] = {
                "q": f"appProperties has {{ key='{P_KIND}' and value='context' }} and trashed = false",
                "spaces": "drive",
                "pageSize": 100,
                "fields": "nextPageToken, files(id, name, appProperties, modifiedTime, webViewLink)",
            }
            if token:
                kwargs["pageToken"] = token
            page = self._run(self._files().list(**kwargs))
            out.extend(page.get("files", []))
            token = page.get("nextPageToken")
            if not token:
                return out

    def read(self, file_id: str) -> bytes:
        return self._run(self._files().get_media(fileId=file_id))


class DrivePublisher:
    """Publisher for <root>/<Company>/<Company>.md."""

    def __init__(self, store: DriveStore, company_name: Callable[[str], str]) -> None:
        self.store = store
        self.company_name = company_name

    def publish(self, company_id: str, content: str, message: str = "") -> PublishResult:
        name = safe_name(self.company_name(company_id))
        folder = self.store.company_folder(company_id, name)
        data = content.encode("utf-8")
        _, written = self.store.put(
            "context", _key_for(company_id), folder, f"{name}.md", data, "text/markdown",
            replace=True,
        )
        return PublishResult(
            f"drive:{self.store.root_name}/{name}/{name}.md",
            hashlib.sha256(data).hexdigest(),
            written,
        )


@dataclass(frozen=True)
class ArchiveResult:
    email_written: bool
    attachments_written: int


class DriveArchiver:
    """Stores a readable copy of each company email and its attachments, once."""

    def __init__(self, store: DriveStore) -> None:
        self.store = store

    def archive(self, company_id: str, company_name: str, parsed: ParsedEmail) -> ArchiveResult:
        date = parsed.date.strftime("%Y-%m-%d") if parsed.date else "undated"
        emails = self.store.subfolder(company_id, company_name, "Emails")
        title = safe_name(f"{date} {parsed.subject or '(no subject)'}", limit=110)
        _, email_written = self.store.put(
            "email", _short_key(parsed.msg_id), emails, f"{title}.txt",
            email_copy_text(parsed).encode("utf-8"), "text/plain", replace=False,
        )
        written = 0
        if parsed.attachments:
            docs = self.store.subfolder(company_id, company_name, "Documents")
            for i, att in enumerate(parsed.attachments):
                _, new = self.store.put(
                    "attachment", _short_key(parsed.msg_id, str(i), att.filename), docs,
                    safe_name(f"{date} {att.filename}", limit=150), att.data,
                    att.content_type or "application/octet-stream", replace=False,
                )
                written += new
        return ArchiveResult(email_written, written)
