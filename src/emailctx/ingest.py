"""Ingest one email into one project: parse, clean, run the segment pipeline, publish.

The email is processed in memory and never stored: the DB gets its metadata (ID,
thread, subject, date), paragraph hashes for the thread backstop, and whatever
requirement changes passed validation.

Idempotent: a msg_id already in processed_messages is skipped before any LLM call.
Everything for one message is one transaction; the company's context.md is rendered
after commit and published only where its hash changed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from emailctx.clean import ParsedEmail, paragraph_hashes, parse_eml, remove_seen_paragraphs
from emailctx.config import Settings
from emailctx.llm import LLMClient
from emailctx.pipeline.graph import PipelineDeps, build_graph
from emailctx.pipeline.types import AcceptedOp, AppliedChange, OpOutcome
from emailctx.render.publish import Publisher, PublishResult, publishers_from_settings
from emailctx.render.render import render_and_publish
from emailctx.routing.embeddings import Embedder, NoOpEmbedder
from emailctx.store.db import utcnow
from emailctx.store.models import (
    Company,
    Message,
    ProcessedMessage,
    Project,
    Segment,
    ThreadParagraph,
)


class IngestError(ValueError):
    """Bad input, e.g. a new project without a company."""


@dataclass
class IngestResult:
    msg_id: str
    status: Literal["skipped", "processed"]
    outcome: str  # already_processed | empty | gated_out | no_change | applied
    company_id: str | None = None
    accepted: list[AcceptedOp] = field(default_factory=list)
    dropped: list[OpOutcome] = field(default_factory=list)
    rejected: list[OpOutcome] = field(default_factory=list)
    applied: list[AppliedChange] = field(default_factory=list)
    published: list[PublishResult] = field(default_factory=list)


def ensure_company(session: Session, company_id: str) -> Company:
    company = session.get(Company, company_id)
    if company is None:
        company = Company(company_id=company_id, name=company_id, status="active")
        session.add(company)
        session.flush()
    return company


def ensure_project(session: Session, project_id: str, company_id: str | None) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        if not company_id:
            raise IngestError(f"project {project_id!r} is new: give its company (--company)")
        ensure_company(session, company_id)
        project = Project(project_id=project_id, name=project_id, company_id=company_id)
        session.add(project)
        session.flush()
    elif company_id and project.company_id != company_id:
        if project.company_id is not None:
            raise IngestError(
                f"project {project_id!r} belongs to company {project.company_id!r}, "
                f"not {company_id!r}"
            )
        ensure_company(session, company_id)
        project.company_id = company_id
    elif project.company_id is None:
        raise IngestError(f"project {project_id!r} has no company: give it one (--company)")
    return project


def resolve_thread(session: Session, parsed: ParsedEmail) -> str:
    """Inherit the thread of any referenced message we already have, else the RFC root."""
    for ref in parsed.references:
        known = session.get(Message, ref)
        if known is not None and known.thread_id:
            return known.thread_id
    return parsed.thread_root


def _seen_in_thread(session: Session, thread_id: str, parsed: ParsedEmail) -> set[str]:
    """Paragraph hashes from earlier-dated emails in the thread."""
    if parsed.date is None:
        return set()
    rows = session.scalars(
        select(ThreadParagraph.para_hash).where(
            ThreadParagraph.thread_id == thread_id,
            ThreadParagraph.first_seen.is_not(None),
            ThreadParagraph.first_seen < parsed.date,
        )
    )
    return set(rows)


def _record_paragraphs(session: Session, thread_id: str, parsed: ParsedEmail) -> None:
    for h in paragraph_hashes(parsed.cleaned_text):
        row = session.get(ThreadParagraph, (thread_id, h))
        if row is None:
            session.add(ThreadParagraph(thread_id=thread_id, para_hash=h, first_seen=parsed.date))
        elif parsed.date and (row.first_seen is None or parsed.date < row.first_seen):
            row.first_seen = parsed.date  # an older email arrived late


def ingest_eml(
    raw: bytes,
    project_id: str,
    *,
    company_id: str | None = None,
    settings: Settings,
    sessions: sessionmaker[Session],
    llm: LLMClient,
    embedder: Embedder | None = None,
    publishers: Sequence[Publisher] | None = None,
) -> IngestResult:
    parsed = parse_eml(raw)
    embedder = embedder or NoOpEmbedder()

    with sessions() as session:
        if session.get(ProcessedMessage, parsed.msg_id) is not None:
            return IngestResult(parsed.msg_id, "skipped", "already_processed")

        project = ensure_project(session, project_id, company_id)
        thread_id = resolve_thread(session, parsed)
        text = remove_seen_paragraphs(
            parsed.cleaned_text, _seen_in_thread(session, thread_id, parsed)
        )

        if session.get(Message, parsed.msg_id) is None:
            session.add(
                Message(
                    msg_id=parsed.msg_id,
                    thread_id=thread_id,
                    subject=parsed.subject,
                    date=parsed.date,
                    source="eml",
                    ingested_at=utcnow(),
                )
            )
        _record_paragraphs(session, thread_id, parsed)
        session.add(Segment(msg_id=parsed.msg_id, project_id=project_id))
        session.flush()

        graph = build_graph(PipelineDeps(session, llm, embedder, settings))
        state = graph.invoke(
            {
                "project_id": project_id,
                "msg_id": parsed.msg_id,
                "subject": parsed.subject,
                "text": text,  # in memory only
                "source_date": parsed.date,
            }
        )

        outcome = state.get("outcome", "no_change")
        session.add(
            ProcessedMessage(msg_id=parsed.msg_id, outcome=outcome, processed_at=utcnow())
        )
        session.commit()

        validation = state.get("validation")
        result = IngestResult(
            msg_id=parsed.msg_id,
            status="processed",
            outcome=outcome,
            company_id=project.company_id,
            accepted=validation.accepted if validation else [],
            dropped=validation.dropped if validation else [],
            rejected=validation.rejected if validation else [],
            applied=state.get("applied", []),
        )
        if result.applied:
            targets = publishers if publishers is not None else publishers_from_settings(settings)
            result.published = render_and_publish(session, project.company_id, settings, targets)
        return result


def ingest_file(path: Path, project_id: str, **kwargs) -> IngestResult:
    return ingest_eml(path.read_bytes(), project_id, **kwargs)
