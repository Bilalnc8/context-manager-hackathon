"""In-memory stand-ins for the Gmail v1 and Drive v3 googleapiclient services.

They implement only the calls emailctx makes, with the same call shapes
(service.users().messages().list(...).execute(num_retries=...)), so the real sync,
bootstrap and Drive code runs unchanged against them.
"""

from __future__ import annotations

import base64
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from email import message_from_bytes, policy
from email.utils import getaddresses

import httplib2
from googleapiclient.errors import HttpError


class _Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self, num_retries: int = 0):
        return self.fn()


# --- Gmail ------------------------------------------------------------------------------------


class FakeGmail:
    """Gmail with history, labels, pagination and a small search evaluator.

    Queries containing from:/to:/cc:/subject: terms are evaluated against the stored
    messages' headers (plus meeting-bot clauses `from:(bot.ai OR ...) ("Name" OR ...)`).
    Any other query (e.g. "label:clients") matches the IDs added with matches=True.
    """

    PAGE = 2  # small pages so pagination is exercised

    def __init__(self, account: str = "sam@ourstudio.io") -> None:
        self.account = account
        self.store: dict[str, dict] = {}
        self.records: list[tuple[int, str, list[str]]] = []
        self.history_id = 1000
        self.matching: set[str] = set()
        self.expired_before: int | None = None
        self.calls: Counter = Counter()
        self.label_list = [{"id": "INBOX", "name": "INBOX"}, {"id": "Label_7", "name": "Clients/Globex"}]
        self._t = datetime(2026, 11, 1, tzinfo=UTC)

    def add(self, gmail_id: str, raw: bytes, *, labels=("INBOX",), matches: bool = True) -> None:
        self.history_id += 1
        self._t += timedelta(hours=1)
        self.store[gmail_id] = {"raw": raw, "labelIds": list(labels),
                                "internalDate": int(self._t.timestamp() * 1000)}
        if matches:
            self.matching.add(gmail_id)
        self.records.append((self.history_id, gmail_id, list(labels)))

    # --- search evaluation ---

    def _headers(self, gid: str) -> dict:
        m = message_from_bytes(self.store[gid]["raw"], policy=policy.default)
        addr = lambda h: " ".join(a for _, a in getaddresses([str(v) for v in m.get_all(h, [])])).lower()
        body = m.get_body(preferencelist=("plain", "html"))
        return {
            "from": addr("From"), "to": addr("To"), "cc": addr("Cc"),
            "subject": str(m.get("Subject", "")),
            "text": str(m.get("Subject", "")) + "\n" + (body.get_content() if body else ""),
        }

    def _structured_match(self, gid: str, q: str) -> bool:
        h = self._headers(gid)
        for field, value in re.findall(r"\b(from|to|cc):([^\s()\"]+)", q):
            if value.lower() in h[field]:
                return True
        for kw in re.findall(r'subject:"([^"]+)"', q):
            if kw.lower() in h["subject"].lower():
                return True
        bots = re.search(r"from:\(([^)]*)\)\s*\(([^)]*)\)", q)
        if bots:
            domains = [d.strip() for d in bots.group(1).split(" OR ")]
            names = re.findall(r'"([^"]+)"', bots.group(2))
            if any(h["from"].endswith(d) for d in domains) and any(
                n.lower() in h["text"].lower() for n in names
            ):
                return True
        return False

    def _search(self, q: str | None) -> list[str]:
        ids = sorted(self.store, key=lambda i: self.store[i]["internalDate"], reverse=True)
        if not q:
            return ids
        if re.search(r"\b(from|to|cc|subject):", q):
            ids = [i for i in ids if self._structured_match(i, q)]
        else:
            ids = [i for i in ids if i in self.matching]
        after = re.search(r"after:(\d+)", q)
        before = re.search(r"before:(\d+)", q)
        if after:
            ids = [i for i in ids if self.store[i]["internalDate"] // 1000 > int(after.group(1))]
        if before:
            ids = [i for i in ids if self.store[i]["internalDate"] // 1000 < int(before.group(1))]
        return ids

    # --- service surface ---

    def users(self):
        return self

    def getProfile(self, userId):
        return _Req(lambda: {"emailAddress": self.account, "historyId": str(self.history_id)})

    def messages(self):
        return _GmailMessages(self)

    def history(self):
        return _GmailHistory(self)

    def labels(self):
        return _GmailLabels(self)


class _GmailMessages:
    def __init__(self, g: FakeGmail):
        self.g = g

    def list(self, userId, q=None, maxResults=100, pageToken=None):
        g = self.g
        g.calls["list"] += 1
        g.calls[f"q:{q}"] += 1
        ids = g._search(q)
        start = int(pageToken or 0)
        size = min(maxResults, g.PAGE)
        page = ids[start:start + size]
        out = {"messages": [{"id": i} for i in page]} if page else {}
        if start + size < len(ids):
            out["nextPageToken"] = str(start + size)
        return _Req(lambda: out)

    def get(self, userId, id, format):
        g = self.g
        g.calls["get"] += 1
        m = g.store[id]
        raw = base64.urlsafe_b64encode(m["raw"]).decode().rstrip("=")
        return _Req(lambda: {"id": id, "raw": raw, "labelIds": m["labelIds"],
                             "internalDate": str(m["internalDate"])})


class _GmailHistory:
    def __init__(self, g: FakeGmail):
        self.g = g

    def list(self, userId, startHistoryId, historyTypes, pageToken=None):
        g = self.g
        g.calls["history"] += 1
        if g.expired_before is not None and int(startHistoryId) < g.expired_before:
            def boom():
                raise HttpError(httplib2.Response({"status": 404}), b'{"error": "expired"}')
            return _Req(boom)
        recs = [r for r in g.records if r[0] > int(startHistoryId)]
        start = int(pageToken or 0)
        page = recs[start:start + g.PAGE]
        out = {
            "history": [{"id": str(h), "messagesAdded": [{"message": {"id": i, "labelIds": l}}]}
                        for h, i, l in page],
            "historyId": str(g.history_id),
        }
        if start + g.PAGE < len(recs):
            out["nextPageToken"] = str(start + g.PAGE)
        return _Req(lambda: out)


class _GmailLabels:
    def __init__(self, g: FakeGmail):
        self.g = g

    def list(self, userId):
        return _Req(lambda: {"labels": self.g.label_list})


# --- Drive ------------------------------------------------------------------------------------


class FakeDrive:
    """Drive v3 files.list/create/update with appProperties search and trash."""

    def __init__(self) -> None:
        self.store: dict[str, dict] = {}
        self.calls: Counter = Counter()
        self._n = 0
        self._clock = 0

    def files(self):
        return _DriveFiles(self)

    # --- helpers for assertions ---

    def path_of(self, fid: str) -> str:
        f = self.store[fid]
        parents = f.get("parents") or []
        return f["name"] if not parents else f"{self.path_of(parents[0])}/{f['name']}"

    def tree(self, include_trashed: bool = False) -> dict[str, dict]:
        return {
            self.path_of(fid): f for fid, f in self.store.items()
            if include_trashed or not f.get("trashed")
        }

    def read(self, path: str) -> bytes:
        return self.tree()[path]["data"]

    def trash(self, path: str) -> None:
        self.tree()[path]["trashed"] = True


def _meta(f: dict) -> dict:
    return {k: f[k] for k in ("id", "name", "parents", "appProperties", "modifiedTime") if k in f}


def _stamp(d: FakeDrive) -> str:
    d._clock += 1
    return f"2026-11-21T10:{d._clock // 60:02d}:{d._clock % 60:02d}.000Z"


class _DriveFiles:
    def __init__(self, d: FakeDrive):
        self.d = d

    def get_media(self, fileId):
        self.d.calls["get_media"] += 1
        return _Req(lambda: self.d.store[fileId]["data"])

    def list(self, q, spaces="drive", pageSize=100, fields=None, pageToken=None):
        self.d.calls["list"] += 1
        pairs = [
            (k, v.replace("\\'", "'").replace("\\\\", "\\"))
            for k, v in re.findall(r"key='([^']*)' and value='((?:[^'\\]|\\.)*)'", q)
        ]
        found = [
            _meta(f) for f in self.d.store.values()
            if not f.get("trashed") and all((f.get("appProperties") or {}).get(k) == v for k, v in pairs)
        ]
        return _Req(lambda: {"files": found[:pageSize]})

    def create(self, body, fields=None, media_body=None):
        self.d.calls["create"] += 1
        self.d._n += 1
        fid = f"f{self.d._n}"
        f = {"id": fid, "name": body["name"], "mimeType": body.get("mimeType"),
             "parents": list(body.get("parents", [])),
             "appProperties": dict(body.get("appProperties", {})), "trashed": False,
             "modifiedTime": _stamp(self.d),
             "data": media_body.getbytes(0, media_body.size()) if media_body else None}
        self.d.store[fid] = f
        return _Req(lambda: _meta(f))

    def update(self, fileId, body=None, fields=None, media_body=None):
        self.d.calls["update"] += 1
        f = self.d.store[fileId]
        body = body or {}
        if "name" in body:
            f["name"] = body["name"]
        f["appProperties"].update(body.get("appProperties", {}))
        if media_body is not None:
            f["data"] = media_body.getbytes(0, media_body.size())
        f["modifiedTime"] = _stamp(self.d)
        return _Req(lambda: _meta(f))
