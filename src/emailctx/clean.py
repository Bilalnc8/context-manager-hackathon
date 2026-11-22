"""Parse raw emails and clean their bodies.

Cleaning = MIME decode, HTML to text, strip quoted replies and signatures. The cleaned
text is what extraction sees and what the evidence check runs against, so anything
left in it can become a requirement.

Quote stripping rules (English clients):
- everything from an attribution line on: "On <date>, <name> wrote:" (may wrap over
  up to three lines), "-----Original Message-----", or an Outlook header block
  (From:/Sent:/To:/Subject: within a few lines, optionally preceded by a ____ rule)
- any line starting with ">"
- HTML: <blockquote>, .gmail_quote, Outlook #appendonsend / #divRplyFwdMsg and
  everything after them

Explicit forwards ("---------- Forwarded message ---------", "Begin forwarded
message:") are kept: forwarded content is new to this thread and may carry requirements.

Signature rules: cut at "-- " / "--", at "Sent from my ..." style mobile footers, and at
the last sign-off line ("Thanks,", "Best regards") when what follows looks like a
signature block (at most 6 short lines).
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime

from bs4 import BeautifulSoup, Comment, NavigableString

from emailctx.textnorm import normalize_requirement

# --- Parsed email ----------------------------------------------------------------


@dataclass(frozen=True)
class Attachment:
    filename: str
    content_type: str
    data: bytes = field(repr=False)


@dataclass(frozen=True)
class ParsedEmail:
    msg_id: str  # RFC Message-ID without <>, or "sha256:<hex>" fallback
    rfc_message_id: str | None
    thread_root: str  # first References id, else In-Reply-To, else msg_id
    references: list[str]  # References + In-Reply-To ids, oldest first
    sender: str | None  # address only, lowercased
    sender_name: str | None = None  # From display name, e.g. "Gurinder Gill"
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    reply_to: list[str] = field(default_factory=list)
    subject: str = ""
    date: datetime | None = None  # naive UTC
    body_text: str = ""  # decoded body before stripping
    cleaned_text: str = ""
    dmarc_pass: bool | None = None  # None: no Authentication-Results dmarc verdict
    labels: tuple[str, ...] = ()  # Gmail labels (filled by Gmail sync)
    attachments: tuple[Attachment, ...] = ()


_MSG_ID = re.compile(r"<([^<>\s]+)>")


def _clean_msg_id(value: object | None) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    m = _MSG_ID.search(s)
    if m:
        return m.group(1)
    return s or None


def _addresses(msg: EmailMessage, header: str) -> list[str]:
    values = [str(v) for v in msg.get_all(header, [])]
    return [addr.lower() for _name, addr in getaddresses(values) if addr]


def _sender_name(msg: EmailMessage) -> str | None:
    values = [str(v) for v in msg.get_all("From", [])]
    for name, addr in getaddresses(values):
        if addr:
            name = " ".join(name.split()).strip("\"' ")
            return name if name and name.lower() != addr.lower() else None
    return None


def _parse_date(msg: EmailMessage) -> datetime | None:
    raw = msg.get("Date")
    if raw is None:
        return None
    dt = getattr(raw, "datetime", None)
    if dt is None:
        try:
            dt = parsedate_to_datetime(str(raw))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


_DMARC = re.compile(r"\bdmarc=(\w+)", re.IGNORECASE)


def _dmarc_pass(msg: EmailMessage) -> bool | None:
    verdicts = [
        m.group(1).lower()
        for h in msg.get_all("Authentication-Results", [])
        for m in _DMARC.finditer(str(h))
    ]
    if not verdicts:
        return None
    return all(v == "pass" for v in verdicts)


def _decode_part(part: EmailMessage) -> str:
    try:
        content = part.get_content()
    except (LookupError, UnicodeDecodeError):
        payload = part.get_payload(decode=True) or b""
        content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    return content


def extract_body(msg: EmailMessage) -> str:
    """Return the body as plain text. Prefers text/plain, falls back to HTML."""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = _decode_part(part)
    if part.get_content_type() == "text/html":
        return html_to_text(content)
    return content


def parse_eml(raw: bytes) -> ParsedEmail:
    msg = BytesParser(policy=policy.default).parsebytes(raw)

    rfc_id = _clean_msg_id(msg.get("Message-ID"))
    refs = _MSG_ID.findall(str(msg.get("References", "") or ""))
    in_reply_to = _clean_msg_id(msg.get("In-Reply-To"))
    if in_reply_to and in_reply_to not in refs:
        refs.append(in_reply_to)

    from_addrs = _addresses(msg, "From")
    sender = from_addrs[0] if from_addrs else None
    subject = str(msg.get("Subject", "") or "")
    body = extract_body(msg)
    cleaned = clean_body(body)

    msg_id = rfc_id
    if not msg_id:
        basis = "\n".join([sender or "", str(msg.get("Date", "") or ""), subject, cleaned])
        msg_id = "sha256:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()

    return ParsedEmail(
        msg_id=msg_id,
        rfc_message_id=rfc_id,
        thread_root=refs[0] if refs else msg_id,
        references=refs,
        sender=sender,
        sender_name=_sender_name(msg),
        to=_addresses(msg, "To"),
        cc=_addresses(msg, "Cc"),
        reply_to=_addresses(msg, "Reply-To"),
        subject=subject,
        date=_parse_date(msg),
        body_text=body,
        cleaned_text=cleaned,
        dmarc_pass=_dmarc_pass(msg),
        attachments=_attachments(msg),
    )


# --- Attachments ------------------------------------------------------------------


def _attachments(msg: EmailMessage) -> tuple[Attachment, ...]:
    out: list[Attachment] = []
    for i, part in enumerate(msg.iter_attachments(), start=1):
        ctype = part.get_content_type()
        if ctype == "message/rfc822":
            inner = part.get_payload()
            inner = inner[0] if isinstance(inner, list) and inner else inner
            data = inner.as_bytes() if hasattr(inner, "as_bytes") else b""
        else:
            data = part.get_payload(decode=True) or b""
        name = part.get_filename()
        if not name:
            ext = mimetypes.guess_extension(ctype) or (".eml" if ctype == "message/rfc822" else ".bin")
            name = f"attachment-{i}{ext}"
        out.append(Attachment(_safe_filename(name), ctype, data))
    return tuple(out)


_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def _safe_filename(name: str, limit: int = 120) -> str:
    name = _UNSAFE.sub("-", name).strip(" .") or "attachment"
    if len(name) <= limit:
        return name
    stem, dot, ext = name.rpartition(".")
    return (stem[: limit - len(ext) - 1] + "." + ext) if dot and len(ext) <= 8 else name[:limit]


def attachment_text(att: Attachment) -> str | None:
    """Readable text of a PDF or text attachment, or None for anything else."""
    lower = att.filename.lower()
    if att.content_type == "application/pdf" or lower.endswith(".pdf"):
        try:
            from io import BytesIO

            from pypdf import PdfReader

            reader = PdfReader(BytesIO(att.data))
            return "\n".join((page.extract_text() or "") for page in reader.pages).strip() or None
        except Exception:
            return None  # encrypted, scanned or broken PDF: stored, not read
    if att.content_type in ("text/plain", "text/markdown", "text/csv") or lower.endswith(
        (".txt", ".md", ".csv")
    ):
        return att.data.decode("utf-8", errors="replace").strip() or None
    return None


def with_attachment_text(
    cleaned: str, attachments: tuple[Attachment, ...], max_chars: int
) -> tuple[str, list[str]]:
    """Append readable attachment text to the cleaned body (the text extraction sees and
    evidence is checked against). Returns the text and the attachments that were read."""
    parts, used, budget = [cleaned], [], max_chars
    for att in attachments:
        text = attachment_text(att)
        if not text or budget <= 0:
            continue
        if len(text) > budget:
            text = text[:budget] + "\n[... attachment text cut at the configured limit ...]"
        budget -= len(text)
        parts.append(f"--- Attachment: {att.filename} ---\n{text}")
        used.append(att.filename)
    return "\n\n".join(p for p in parts if p), used


def email_copy_text(parsed: ParsedEmail) -> str:
    """The readable copy of an email stored in the company's Drive folder."""
    date = parsed.date.strftime("%Y-%m-%d %H:%M UTC") if parsed.date else "unknown"
    lines = [
        f"From: {parsed.sender or ''}",
        f"To: {', '.join(parsed.to)}",
    ]
    if parsed.cc:
        lines.append(f"Cc: {', '.join(parsed.cc)}")
    lines += [f"Date: {date}", f"Subject: {parsed.subject}"]
    if parsed.rfc_message_id:
        lines.append(f"Message-ID: <{parsed.rfc_message_id}>")
    if parsed.attachments:
        lines.append(f"Attachments: {', '.join(a.filename for a in parsed.attachments)}")
    body = parsed.body_text.replace("\r\n", "\n").strip()
    return "\n".join(lines) + "\n\n" + body + "\n"


# --- HTML to text ----------------------------------------------------------------

_BLOCK_TAGS = [
    "p", "div", "li", "tr", "table", "ul", "ol", "hr", "pre", "section", "article",
    "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
]
_WS = re.compile(r"\s+")


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "head", "title"]):
        tag.decompose()
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()

    # Gmail wraps both replies and forwards in .gmail_quote; keep forwards.
    for quote in soup.select(".gmail_quote"):
        attr = quote.select_one(".gmail_attr")
        if attr is not None and "Forwarded message" in attr.get_text():
            quote.unwrap()
        else:
            quote.decompose()
    for sel in (
        "blockquote",
        ".gmail_signature",
        "[data-smartmail=gmail_signature]",
        ".moz-cite-prefix",
        ".moz-signature",
    ):
        for tag in soup.select(sel):
            tag.decompose()
    # Outlook: the reply header and everything after it is history.
    marker = soup.select_one("#appendonsend, #divRplyFwdMsg")
    while marker is not None:
        for sib in marker.find_next_siblings():
            sib.decompose()
        marker.decompose()
        marker = soup.select_one("#appendonsend, #divRplyFwdMsg")

    for s in soup.find_all(string=True):
        if s.find_parent("pre") is None:
            s.replace_with(NavigableString(_WS.sub(" ", str(s))))
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert(0, NavigableString("- "))
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")

    lines = [ln.strip() for ln in soup.get_text().split("\n")]
    return "\n".join(lines)


# --- Quote and signature stripping -------------------------------------------------

_ON_WROTE = re.compile(r"^On\b.{0,400}\bwrote:$", re.IGNORECASE | re.DOTALL)
_ORIGINAL = re.compile(r"^-{2,}\s*Original Message\s*-{2,}$", re.IGNORECASE)
_FORWARD = re.compile(
    r"^(-{2,}\s*Forwarded message\s*-{2,}|Begin forwarded message:)$", re.IGNORECASE
)
_HDR_FROM = re.compile(r"^\*?From:\*?\s", re.IGNORECASE)
_HDR_SENT = re.compile(r"^\*?(Sent|Date):\*?\s", re.IGNORECASE)
_HDR_TO_SUBJ = re.compile(r"^\*?(To|Subject):\*?\s", re.IGNORECASE)
_RULE = re.compile(r"^_{5,}$")
_QUOTED = re.compile(r"^\s*>")
_SIG_DELIM = re.compile(r"^--\s?$")
_MOBILE = re.compile(
    r"^(Sent from my \w+.*|Sent from (Outlook|Mail|Yahoo Mail)\b.*|Get Outlook for \w+.*)$",
    re.IGNORECASE,
)
_SIGNOFF = re.compile(
    r"^(thanks|thank you|many thanks|thanks again|thanks so much|best|all the best|"
    r"best regards|best wishes|kind regards|warm regards|regards|cheers|sincerely|thx)"
    r"[,.!]?$",
    re.IGNORECASE,
)
_MAX_SIG_LINES = 6
_MAX_SIG_LINE_LEN = 80


def _prev_nonblank(lines: list[str], i: int) -> int | None:
    j = i - 1
    while j >= 0 and not lines[j].strip():
        j -= 1
    return j if j >= 0 else None


def _reply_header_start(lines: list[str]) -> int | None:
    """Index of the first line of quoted reply history, or None."""
    n = len(lines)
    for i in range(n):
        line = lines[i].strip()
        if not line:
            continue
        if _ORIGINAL.match(line):
            return i
        if line.lower().startswith("on "):
            joined = line
            for k in range(3):
                if _ON_WROTE.match(joined):
                    return i
                if i + k + 1 >= n:
                    break
                joined = f"{joined} {lines[i + k + 1].strip()}"
        if _HDR_FROM.match(line):
            window = [ln.strip() for ln in lines[i + 1 : i + 6]]
            if any(_HDR_SENT.match(w) for w in window) and any(
                _HDR_TO_SUBJ.match(w) for w in window
            ):
                prev = _prev_nonblank(lines, i)
                if prev is not None and _FORWARD.match(lines[prev].strip()):
                    continue  # explicit forward: keep
                if prev is not None and _RULE.match(lines[prev].strip()):
                    return prev
                return i
    return None


def _signature_start(lines: list[str]) -> int | None:
    for i, raw in enumerate(lines):
        if _SIG_DELIM.match(raw.rstrip("\n")) or _MOBILE.match(raw.strip()):
            return i
    for i in range(len(lines) - 1, -1, -1):
        if not _SIGNOFF.match(lines[i].strip()):
            continue
        tail = [ln.strip() for ln in lines[i + 1 :] if ln.strip()]
        if len(tail) <= _MAX_SIG_LINES and all(len(t) <= _MAX_SIG_LINE_LEN for t in tail):
            return i
        break  # only the last sign-off is a candidate
    return None


def _tidy(lines: list[str]) -> str:
    out: list[str] = []
    for ln in lines:
        ln = ln.rstrip()
        if not ln and (not out or not out[-1]):
            continue
        out.append(ln)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def clean_body(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = text.split("\n")

    cut = _reply_header_start(lines)
    if cut is not None:
        lines = lines[:cut]
    lines = [ln for ln in lines if not _QUOTED.match(ln)]
    cut = _signature_start(lines)
    if cut is not None:
        lines = lines[:cut]
    return _tidy(lines)


# --- Thread backstop ----------------------------------------------------------------


def _paragraphs(text: str) -> list[str]:
    return [p for p in re.split(r"\n\s*\n", text) if p.strip()]


def paragraph_hash(paragraph: str) -> str | None:
    """SHA-256 of a paragraph's normalized form (case, punctuation, whitespace ignored)."""
    norm = normalize_requirement(paragraph)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest() if norm else None


def paragraph_hashes(text: str) -> set[str]:
    return {h for p in _paragraphs(text) if (h := paragraph_hash(p))}


def remove_seen_paragraphs(text: str, seen_hashes: set[str]) -> str:
    """Drop paragraphs already seen in earlier messages of the same thread.

    Backstop for quoted history the rules above missed (no ">" markers, unknown
    attribution format). Earlier messages are known only by paragraph hashes; their
    text is never stored.
    """
    if not seen_hashes:
        return text
    kept = [p for p in _paragraphs(text) if paragraph_hash(p) not in seen_hashes]
    return "\n\n".join(p.strip("\n") for p in kept)
