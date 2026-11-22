"""The company's sample project (dummy_data/) as real inputs to the pipeline.

    dummy_data/
      domain_project_mapping.yaml   internal domains + one project per client domain
      emails.jsonl                  client and team emails, with Message-ID / In-Reply-To
      meeting-NNN_<project>_<date>.md   AI note-taker write-ups

Each email becomes an .eml with its headers (From, To, Cc, Date, Subject, Message-ID,
In-Reply-To, References) and goes through the normal pipeline, routing included: the
expected project in the data is only used to check routing afterwards. Each meeting
write-up becomes an email from a note-taker address (NOTE_TAKER_DOMAIN, added to the
meeting-bot domains), so it is routed by the company it names, the way a Fireflies or
Otter summary would be. Inputs are processed in date order.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from typing import Literal

import yaml

from emailctx.config import Settings
from emailctx.ingest import IngestResult, Runtime, ingest_file
from emailctx.registry import CompanySpec, ProjectSpec, RegistrySpec, load_registry

NOTE_TAKER_DOMAIN = "notetaker.example"
NOTE_TAKER = f"Meeting Notes <notes@{NOTE_TAKER_DOMAIN}>"
# Meeting write-ups carry a date only; they are placed at midday (EST) on that day.
MEETING_TIME = time(12, 0, tzinfo=timezone(timedelta(hours=-5)))


@dataclass(frozen=True)
class SampleInput:
    source_id: str  # email-001 | meeting-001
    kind: Literal["email", "meeting"]
    date: datetime
    expected_project: str | None
    subject: str
    raw: bytes

    @property
    def msg_id(self) -> str:
        return f"{self.source_id}@fixtures.contextlab.example"


@dataclass
class Sample:
    registry: RegistrySpec
    inputs: list[SampleInput]  # date order
    expected_files: dict[str, Path] = field(default_factory=dict)  # project -> expected .md


def read_mapping(path: Path) -> tuple[RegistrySpec, dict[str, Path]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    companies = []
    expected: dict[str, Path] = {}
    for pid, spec in (data.get("projects") or {}).items():
        name = spec.get("name") or pid
        companies.append(CompanySpec(
            id=pid, name=name, domains=list(spec.get("domains") or []),
            projects=[ProjectSpec(id=pid, name=name)],
        ))
        if spec.get("context_file"):
            expected[pid] = path.parent.parent / spec["context_file"]
    return RegistrySpec(own_domains=list(data.get("internal_domains") or []), companies=companies), expected


def _email(rec: dict, by_id: dict[str, dict]) -> SampleInput:
    msg = EmailMessage()
    msg["From"] = rec["from"]
    msg["To"] = ", ".join(rec.get("to") or [])
    if rec.get("cc"):
        msg["Cc"] = ", ".join(rec["cc"])
    msg["Subject"] = rec["subject"]
    date = datetime.fromisoformat(rec["timestamp"])
    msg["Date"] = format_datetime(date)
    msg["Message-ID"] = rec["message_id"]
    parent = rec.get("in_reply_to")
    if parent:
        msg["In-Reply-To"] = parent
        chain = []
        while parent:  # References: the whole thread, oldest first
            chain.insert(0, parent)
            parent = by_id.get(parent, {}).get("in_reply_to")
        msg["References"] = " ".join(chain)
    msg.set_content(rec["body"])
    return SampleInput(
        rec["id"], "email", date, rec.get("expected_project_id"), rec["subject"], bytes(msg)
    )


def read_emails(path: Path) -> list[SampleInput]:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    by_id = {r["message_id"]: r for r in records}
    return [_email(r, by_id) for r in records]


_FIELD = re.compile(r"^- \*\*(?P<key>[^:*]+):\*\*\s*(?P<value>.+)$", re.M)


def read_meeting(path: Path) -> SampleInput:
    text = path.read_text(encoding="utf-8")
    fields = {m["key"].strip().lower(): m["value"].strip() for m in _FIELD.finditer(text)}
    title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), path.stem)
    meeting_id = fields.get("meeting id") or path.stem.split("_")[0]
    date = datetime.combine(datetime.fromisoformat(fields["date"]).date(), MEETING_TIME)
    subject = f"Meeting notes: {title} ({meeting_id})"
    msg = EmailMessage()
    msg["From"] = NOTE_TAKER
    msg["To"] = "team@contextlab.example"
    msg["Subject"] = subject
    msg["Date"] = format_datetime(date)
    msg["Message-ID"] = f"<{meeting_id}@fixtures.contextlab.example>"
    msg.set_content(text)
    return SampleInput(meeting_id, "meeting", date, fields.get("project id"), subject, bytes(msg))


def read_sample(folder: Path) -> Sample:
    registry, expected = read_mapping(folder / "domain_project_mapping.yaml")
    inputs = read_emails(folder / "emails.jsonl")
    inputs += [read_meeting(p) for p in sorted(folder.glob("meeting-*.md"))]
    inputs.sort(key=lambda i: (i.date, i.source_id))
    return Sample(registry, inputs, expected)


def sample_settings(settings: Settings) -> Settings:
    """The settings with the sample's note taker counted as a meeting bot."""
    bots = settings.routing.meeting_bot_domains
    if NOTE_TAKER_DOMAIN in bots:
        return settings
    routing = settings.routing.model_copy(update={"meeting_bot_domains": [*bots, NOTE_TAKER_DOMAIN]})
    return settings.model_copy(update={"routing": routing})


def load_sample(
    sample: Sample,
    rt: Runtime,
    eml_dir: Path,
    *,
    on_result: Callable[[SampleInput, IngestResult], None] | None = None,
) -> list[tuple[SampleInput, IngestResult]]:
    """Load the registry, write each input as an .eml under eml_dir (so review items can
    be re-read) and ingest them in date order. Running it again changes nothing."""
    settings = sample_settings(rt.settings)
    with rt.sessions() as s:
        load_registry(s, sample.registry)
        s.commit()
    eml_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for item in sample.inputs:
        path = eml_dir / f"{item.source_id}.eml"
        if not path.exists() or path.read_bytes() != item.raw:
            path.write_bytes(item.raw)
        result = ingest_file(
            path, settings=settings, sessions=rt.sessions, llm=rt.llm, embedder=rt.embedder,
            publishers=rt.targets(), drive=rt.drive, write_lock=rt.write_lock,
        )
        results.append((item, result))
        if on_result is not None:
            on_result(item, result)
    return results


__all__ = [
    "NOTE_TAKER_DOMAIN", "Sample", "SampleInput", "load_sample", "read_emails", "read_mapping",
    "read_meeting", "read_sample", "sample_settings",
]
