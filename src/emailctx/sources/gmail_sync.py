"""Gmail sync: backfill once, then incremental from a stored historyId.

Backfill: users.getProfile gives the mailbox's current historyId *before* listing, so
anything arriving during the backfill is picked up by the next incremental run.
messages.list with the configured query (newest first, capped at backfill_max) gives
the IDs; they are processed oldest first so "latest wins" sees them in order.

Incremental: history.list from the stored historyId (messageAdded only). History has no
search filter, so each new message is checked against the query with one
messages.list call narrowed to its own timestamp. Drafts and spam are skipped.
If Google has expired the historyId (HTTP 404), sync falls back to a backfill; IDs
already processed are skipped without being downloaded.

Each email is fetched as raw RFC 822 bytes and processed by ingest_eml (route, gate,
extract, validate, apply; the cleaned text is kept with the message for citations). The stored cursor only advances after
every message in the batch went through, so a failure never skips mail: the next run
starts from the same cursor and processed messages are skipped.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from emailctx.ingest import IngestResult, Runtime, ingest_eml
from emailctx.store.db import utcnow
from emailctx.store.models import Message, ProcessedMessage, SyncState

SKIP_LABELS = {"DRAFT", "SPAM", "TRASH"}
_PAGE = 500
_MATCH_WINDOW_S = 86_400  # see GmailSync._matches_query
_RETRIES = 3


class SyncError(RuntimeError):
    def __init__(self, message: str, report: SyncReport) -> None:
        super().__init__(message)
        self.report = report


@dataclass
class SyncReport:
    account: str = ""
    mode: str = ""  # backfill | incremental | backfill_after_expired_history
    candidates: int = 0
    known: int = 0  # already processed: not downloaded
    not_matching: int = 0  # new mail outside the query
    skipped_labels: int = 0  # drafts, spam, trash
    gone: int = 0  # listed in history, then deleted before we fetched it (e.g. a draft)
    results: list[tuple[str, IngestResult]] = field(default_factory=list)  # (gmail id, result)
    history_id: str | None = None
    bootstraps: list[Any] = field(default_factory=list)  # BootstrapReport per new company


def _run(request: Any) -> dict:
    return request.execute(num_retries=_RETRIES)


def _http_status(error: Exception) -> int | None:
    resp = getattr(error, "resp", None)
    status = getattr(resp, "status", None)
    return int(status) if status is not None else None


def fetch_raw(service: Any, gmail_id: str) -> tuple[bytes, dict]:
    msg = _run(service.users().messages().get(userId="me", id=gmail_id, format="raw"))
    data = msg["raw"]
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    return raw, msg


def list_ids(service: Any, query: str, cap: int | None) -> list[str]:
    """IDs of messages matching `query` (most recent `cap`), returned oldest first."""
    ids: list[str] = []
    token = None
    while True:
        want = _PAGE if cap is None else min(_PAGE, cap - len(ids))
        if want <= 0:
            break
        kwargs: dict[str, Any] = {"userId": "me", "maxResults": want}
        if query:
            kwargs["q"] = query
        if token:
            kwargs["pageToken"] = token
        page = _run(service.users().messages().list(**kwargs))
        ids.extend(m["id"] for m in page.get("messages", []))
        token = page.get("nextPageToken")
        if not token:
            break
    return list(reversed(ids))


class GmailSync:
    def __init__(
        self,
        service: Any,
        rt: Runtime,
        *,
        query: str | None = None,
        backfill_max: int | None = None,
        on_result: Callable[[str, IngestResult], None] | None = None,
    ) -> None:
        self.service = service
        self.rt = rt
        configured = rt.settings.gmail.query if query is None else query
        # None: built from the registry when the sync runs (see bootstrap.auto_query).
        self.query: str | None = configured.strip() if configured is not None else None
        self.backfill_max = backfill_max if backfill_max is not None else rt.settings.gmail.backfill_max
        self.on_result = on_result
        self._label_names: dict[str, str] | None = None

    # --- Gmail calls -------------------------------------------------------------

    def _users(self) -> Any:
        return self.service.users()

    def label_names(self) -> dict[str, str]:
        if self._label_names is None:
            labels = _run(self._users().labels().list(userId="me")).get("labels", [])
            self._label_names = {l["id"]: l.get("name", l["id"]) for l in labels}
        return self._label_names

    def _backfill_ids(self) -> list[str]:
        return list_ids(self.service, self.query or "", self.backfill_max)  # oldest first

    def _history_ids(self, start: str) -> tuple[list[str], str, int]:
        ids: list[str] = []
        skipped = 0
        token = None
        latest = start
        while True:
            kwargs: dict[str, Any] = {
                "userId": "me", "startHistoryId": start, "historyTypes": ["messageAdded"],
            }
            if token:
                kwargs["pageToken"] = token
            page = _run(self._users().history().list(**kwargs))
            latest = page.get("historyId", latest)
            for record in page.get("history", []):
                for added in record.get("messagesAdded", []):
                    m = added["message"]
                    if SKIP_LABELS & set(m.get("labelIds", [])):
                        skipped += 1
                    elif m["id"] not in ids:
                        ids.append(m["id"])
            token = page.get("nextPageToken")
            if not token:
                break
        return ids, latest, skipped

    def _matches_query(self, gmail_id: str, internal_ms: int) -> bool:
        if not self.query:
            return True
        secs = internal_ms // 1000
        # Gmail's after:/before: aren't second-precise: a +-60 s window misses real
        # matches (seen 2026-11-22: +-60 s missed, +-10 min found). The id check below
        # makes the result exact, so the window only has to bound the search.
        q = f"({self.query}) after:{secs - _MATCH_WINDOW_S} before:{secs + _MATCH_WINDOW_S}"
        page = _run(self._users().messages().list(userId="me", q=q, maxResults=_PAGE))
        return any(m["id"] == gmail_id for m in page.get("messages", []))

    # --- Processing ------------------------------------------------------------------

    def _already_processed(self, gmail_id: str) -> bool:
        with self.rt.sessions() as s:
            msg = s.scalar(select(Message).where(Message.gmail_id == gmail_id))
            return msg is not None and s.get(ProcessedMessage, msg.msg_id) is not None

    def _process(self, gmail_id: str, check_query: bool, report: SyncReport) -> None:
        if self._already_processed(gmail_id):
            report.known += 1
            return
        try:
            raw, meta = fetch_raw(self.service, gmail_id)
        except Exception as e:
            # Deleted since history listed it (drafts are replaced on every save). It will
            # never come back, so skip it: stopping here would block the sync forever.
            if _http_status(e) == 404:
                report.gone += 1
                return
            raise
        if SKIP_LABELS & set(meta.get("labelIds", [])):
            report.skipped_labels += 1
            return
        if check_query and not self._matches_query(gmail_id, int(meta.get("internalDate", 0))):
            report.not_matching += 1
            return
        names = self.label_names()
        result = ingest_eml(
            raw,
            source="gmail",
            source_ref=f"gmail:{gmail_id}",
            gmail_id=gmail_id,
            labels=[names.get(l, l) for l in meta.get("labelIds", [])],
            settings=self.rt.settings,
            sessions=self.rt.sessions,
            llm=self.rt.llm,
            embedder=self.rt.embedder,
            publishers=self.rt.publishers,
            drive=self.rt.drive,
            write_lock=self.rt.write_lock,
        )
        report.results.append((gmail_id, result))
        if self.on_result:
            self.on_result(gmail_id, result)

    def run(self) -> SyncReport:
        from googleapiclient.errors import HttpError

        profile = _run(self._users().getProfile(userId="me"))
        report = SyncReport(account=profile["emailAddress"])
        current = str(profile["historyId"])

        with self.rt.sessions() as s:
            state = s.get(SyncState, report.account)
            start = state.history_id if state else None
            if self.query is None:
                from emailctx.bootstrap import auto_query

                self.query = auto_query(s, self.rt.settings)
        if self.query is None:
            raise SyncError(
                "nothing to sync: no companies are registered (run `emailctx registry load`) "
                "and gmail.query is not set",
                report,
            )

        # Companies whose Drive folder has no .md yet: build it from recent mail first.
        if self.rt.drive is not None:
            from emailctx.bootstrap import bootstrap_company, companies_missing_context

            with self.rt.sessions() as s:
                missing = companies_missing_context(s, self.rt.drive.has_context)
            for company_id in missing:
                try:
                    report.bootstraps.append(
                        bootstrap_company(self.service, self.rt, company_id, on_result=self.on_result)
                    )
                except Exception as e:
                    raise SyncError(
                        f"stopped while building {company_id}'s file: {e}. Nothing was "
                        "skipped; the next run continues it.",
                        report,
                    ) from e

        check_query = False
        if start is None and self.rt.drive is not None:
            # The bootstraps above covered each company's recent mail; from here on, only
            # new mail. The cursor was taken before they ran, so nothing slips through.
            report.mode, ids, new_cursor = "bootstrap", [], current
        elif start is None:
            report.mode, ids, new_cursor = "backfill", self._backfill_ids(), current
        else:
            try:
                ids, new_cursor, report.skipped_labels = self._history_ids(start)
                report.mode, check_query = "incremental", True
            except HttpError as e:
                if _http_status(e) != 404:
                    raise
                report.mode, ids, new_cursor = "backfill_after_expired_history", self._backfill_ids(), current
        report.candidates = len(ids)

        for gmail_id in ids:
            try:
                self._process(gmail_id, check_query, report)
            except Exception as e:
                raise SyncError(
                    f"stopped at Gmail message {gmail_id}: {e}. The sync cursor was not "
                    "advanced; the next run resumes here.",
                    report,
                ) from e

        with self.rt.sessions() as s:
            state = s.get(SyncState, report.account)
            if state is None:
                state = SyncState(account=report.account)
                s.add(state)
            state.history_id = str(new_cursor)
            state.last_run = utcnow()
            s.commit()
        report.history_id = str(new_cursor)
        return report


def gmail_reader(service: Any) -> Callable[[str], bytes]:
    """Read a review item's email again from Gmail (it was never stored)."""

    def read(gmail_id: str) -> bytes:
        return fetch_raw(service, gmail_id)[0]

    return read
