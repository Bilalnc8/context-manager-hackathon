"""Ingest one email: parse, clean, route to a project, run the segment pipeline, publish.

The DB gets the email's metadata (ID, thread, subject, date, sender), the cleaned text
the pipeline read (so every citation can be opened later), paragraph hashes for the
thread backstop, routing decisions, and whatever requirement changes passed validation.

Idempotent: a msg_id already in processed_messages is skipped before any LLM call.
Everything for one message is one transaction; the company's context.md is rendered
after commit and published only where its hash changed. With a write_lock (the MCP
server's), the whole ingest, render included, holds it, so writers take turns and the
published file always reflects the latest commit.
"""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

if TYPE_CHECKING:
    from emailctx.drive import DriveStore

from emailctx.clean import (
    ParsedEmail,
    paragraph_hashes,
    parse_eml,
    remove_seen_paragraphs,
    with_attachment_text,
)
from emailctx.config import Settings
from emailctx.llm import LLMClient
from emailctx.pipeline.graph import PipelineDeps, build_graph
from emailctx.pipeline.ops import Skipped
from emailctx.pipeline.types import AcceptedOp, AppliedChange, OpOutcome
from emailctx.render.publish import Publisher, PublishResult, publishers_from_settings
from emailctx.render.render import render_and_publish
from emailctx.routing.cascade import RouteDecision, log_decision, pin_thread, route
from emailctx.routing.embeddings import Embedder, NoOpEmbedder
from emailctx.store.db import utcnow
from emailctx.store.models import (
    Company,
    Message,
    ProcessedMessage,
    Project,
    ReviewQueue,
    Segment,
    ThreadParagraph,
)


class IngestError(ValueError):
    """Bad input, e.g. a new project without a company."""


@dataclass
class IngestResult:
    msg_id: str
    status: Literal["skipped", "processed"]
    # already_processed | review | unassigned | empty | gated_out | no_change | applied
    outcome: str
    route: RouteDecision | None = None
    project_id: str | None = None
    company_id: str | None = None
    review_id: int | None = None
    accepted: list[AcceptedOp] = field(default_factory=list)
    dropped: list[OpOutcome] = field(default_factory=list)
    rejected: list[OpOutcome] = field(default_factory=list)
    applied: list[AppliedChange] = field(default_factory=list)
    answered: list[str] = field(default_factory=list)  # question ids the email answered
    published: list[PublishResult] = field(default_factory=list)
    # Coverage (pipeline/coverage.py): sentences extraction said change nothing, and
    # sentences nothing accounted for (a person should look at these).
    skipped: list[Skipped] = field(default_factory=list)
    unaccounted: list[str] = field(default_factory=list)
    gate_reason: str | None = None  # why the gate let the email through or stopped it


@dataclass
class Runtime:
    """What processing needs besides the email itself."""

    settings: Settings
    sessions: sessionmaker[Session]
    llm: LLMClient
    embedder: Embedder = field(default_factory=NoOpEmbedder)
    publishers: Sequence[Publisher] | None = None  # None: from settings
    drive: DriveStore | None = None  # set when Drive output is on: copies + .md go there
    # Every write in one process (email sync, pasted input, a teammate's status change)
    # takes this lock, rendering and publishing included: writers queue instead of
    # failing on SQLite's lock, and an older render can't overwrite a newer one.
    write_lock: threading.RLock = field(default_factory=threading.RLock)

    def company_name(self, company_id: str) -> str:
        with self.sessions() as s:
            company = s.get(Company, company_id)
            return company.name if company is not None else company_id

    def targets(self) -> Sequence[Publisher]:
        if self.publishers is not None:
            return self.publishers
        return publishers_from_settings(
            self.settings, drive_store=self.drive, company_name=self.company_name
        )


# --- Registry helpers -----------------------------------------------------------------


def ensure_company(session: Session, company_id: str) -> Company:
    company = session.get(Company, company_id)
    if company is None:
        company = Company(company_id=company_id, name=company_id, status="active")
        session.add(company)
        session.flush()
    elif company.status != "active":
        company.status = "active"  # naming it explicitly approves it
    return company


def company_project(session: Session, company_id: str) -> Project | None:
    """A company's one project, if it has one yet."""
    return session.scalar(
        select(Project).where(Project.company_id == company_id).order_by(Project.project_id)
    )


def _refuse_second_project(session: Session, company_id: str, project_id: str) -> None:
    existing = company_project(session, company_id)
    if existing is not None and existing.project_id != project_id:
        raise IngestError(
            f"company {company_id!r} already has project {existing.project_id!r}; "
            "each company has exactly one project"
        )


def ensure_project(session: Session, project_id: str | None, company_id: str | None) -> Project:
    """Resolve the target project. Each company has exactly one project.

    With only company_id, that company's project is used (created with the company's
    id and name if it doesn't exist yet).
    """
    if project_id is None:
        if not company_id:
            raise IngestError("give a project (--project) or a company (--company)")
        company = ensure_company(session, company_id)
        project = company_project(session, company_id)
        if project is None:
            project = Project(project_id=company_id, name=company.name, company_id=company_id)
            session.add(project)
            session.flush()
        return project

    project = session.get(Project, project_id)
    if project is None:
        if not company_id:
            raise IngestError(f"project {project_id!r} is new: give its company (--company)")
        ensure_company(session, company_id)
        _refuse_second_project(session, company_id, project_id)
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
        _refuse_second_project(session, company_id, project_id)
        project.company_id = company_id
    elif project.company_id is None:
        raise IngestError(f"project {project_id!r} has no company: give it one (--company)")
    return project


# --- Threading ------------------------------------------------------------------------


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


def _record_message(
    session: Session,
    parsed: ParsedEmail,
    thread_id: str,
    source: str,
    gmail_id: str | None,
    max_attachment_chars: int,
) -> None:
    row = session.get(Message, parsed.msg_id)
    if row is None:
        body, _read = with_attachment_text(
            parsed.cleaned_text, parsed.attachments, max_attachment_chars
        )
        session.add(
            Message(
                msg_id=parsed.msg_id,
                gmail_id=gmail_id,
                thread_id=thread_id,
                subject=parsed.subject,
                date=parsed.date,
                attachments=[a.filename for a in parsed.attachments] or None,
                sender=parsed.sender,
                sender_name=parsed.sender_name,
                body=body,
                source=source,
                ingested_at=utcnow(),
            )
        )
    elif gmail_id and row.gmail_id is None:
        row.gmail_id = gmail_id
    for h in paragraph_hashes(parsed.cleaned_text):
        row = session.get(ThreadParagraph, (thread_id, h))
        if row is None:
            session.add(ThreadParagraph(thread_id=thread_id, para_hash=h, first_seen=parsed.date))
        elif parsed.date and (row.first_seen is None or parsed.date < row.first_seen):
            row.first_seen = parsed.date  # an older email arrived late
    session.flush()


def _mark_processed(session: Session, msg_id: str, outcome: str) -> None:
    row = session.get(ProcessedMessage, msg_id)
    if row is None:
        session.add(ProcessedMessage(msg_id=msg_id, outcome=outcome, processed_at=utcnow()))
    else:
        row.outcome, row.processed_at = outcome, utcnow()


# --- Processing -----------------------------------------------------------------------


def process_for_project(
    session: Session,
    parsed: ParsedEmail,
    thread_id: str,
    project: Project,
    rt: Runtime,
    decision: RouteDecision,
) -> IngestResult:
    """Store the email copy, run gate -> extract -> validate -> apply for one project,
    commit, then publish.

    The copy goes to Drive first: if anything later fails, nothing is committed and the
    next run retries; the copy is keyed by Message-ID, so it is never duplicated.
    """
    if rt.drive is not None:
        from emailctx.drive import DriveArchiver

        company = session.get(Company, project.company_id)
        DriveArchiver(rt.drive).archive(
            project.company_id, company.name if company else project.company_id, parsed
        )

    text = remove_seen_paragraphs(parsed.cleaned_text, _seen_in_thread(session, thread_id, parsed))
    text, _read = with_attachment_text(
        text, parsed.attachments, rt.settings.pipeline.max_attachment_chars
    )
    if session.scalar(
        select(Segment).where(Segment.msg_id == parsed.msg_id, Segment.project_id == project.project_id)
    ) is None:
        session.add(Segment(msg_id=parsed.msg_id, project_id=project.project_id))
    session.flush()

    graph = build_graph(PipelineDeps(session, rt.llm, rt.embedder, rt.settings))
    state = graph.invoke(
        {
            "project_id": project.project_id,
            "msg_id": parsed.msg_id,
            "subject": parsed.subject,
            "text": text,  # in memory only
            "source_date": parsed.date,
        }
    )

    outcome = state.get("outcome", "no_change")
    pin_thread(session, thread_id, project.project_id)
    _mark_processed(session, parsed.msg_id, outcome)
    session.commit()

    validation = state.get("validation")
    result = IngestResult(
        msg_id=parsed.msg_id,
        status="processed",
        outcome=outcome,
        route=decision,
        project_id=project.project_id,
        company_id=project.company_id,
        accepted=validation.accepted if validation else [],
        dropped=validation.dropped if validation else [],
        rejected=validation.rejected if validation else [],
        applied=state.get("applied", []),
        answered=state.get("answered", []),
        skipped=validation.skipped if validation else [],
        unaccounted=validation.unaccounted if validation else [],
        gate_reason=state.get("gate_reason"),
    )
    if result.applied or result.answered or state.get("conflicts"):
        result.published = render_and_publish(
            session, project.company_id, rt.settings, rt.targets(), message=commit_message(result, parsed)
        )
    return result


def commit_message(result: IngestResult, parsed: ParsedEmail) -> str:
    """One line describing the change and citing its source email."""
    changes = ", ".join(
        [f"{c.req_id} {c.op}" for c in result.applied] + [f"{q} answered" for q in result.answered]
    ) or "needs confirmation"
    date = parsed.date.strftime("%Y-%m-%d") if parsed.date else "undated"
    source = parsed.msg_id if not parsed.msg_id.startswith("sha256:") else "no Message-ID"
    return f"{result.project_id}: {changes} (email {source}, {date})"


def ingest_eml(
    raw: bytes,
    project_id: str | None = None,
    *,
    company_id: str | None = None,
    source_ref: str | None = None,
    source: str = "eml",
    gmail_id: str | None = None,
    labels: Sequence[str] = (),
    settings: Settings,
    sessions: sessionmaker[Session],
    llm: LLMClient,
    embedder: Embedder | None = None,
    publishers: Sequence[Publisher] | None = None,
    drive: DriveStore | None = None,
    only_companies: set[str] | None = None,
    write_lock: threading.RLock | None = None,
) -> IngestResult:
    """Ingest one raw email. With project_id and/or company_id it skips routing (a manual
    assignment; a company alone means its one project).

    The dedupe key is the RFC Message-ID, so the same email arriving as a file and
    through Gmail is processed once; its Gmail ID is recorded either way.

    only_companies (used when building a company's first file): an email that routes to
    any other company is left untouched, unprocessed, for that company's own run.
    """
    with write_lock or nullcontext():
        return _ingest_eml(
            raw, project_id, company_id=company_id, source_ref=source_ref, source=source,
            gmail_id=gmail_id, labels=labels, settings=settings, sessions=sessions, llm=llm,
            embedder=embedder, publishers=publishers, drive=drive, only_companies=only_companies,
        )


def _ingest_eml(
    raw: bytes,
    project_id: str | None,
    *,
    company_id: str | None,
    source_ref: str | None,
    source: str,
    gmail_id: str | None,
    labels: Sequence[str],
    settings: Settings,
    sessions: sessionmaker[Session],
    llm: LLMClient,
    embedder: Embedder | None,
    publishers: Sequence[Publisher] | None,
    drive: DriveStore | None,
    only_companies: set[str] | None,
) -> IngestResult:
    rt = Runtime(settings, sessions, llm, embedder or NoOpEmbedder(), publishers, drive)
    parsed = dataclasses.replace(parse_eml(raw), labels=tuple(labels))

    with sessions() as session:
        if session.get(ProcessedMessage, parsed.msg_id) is not None:
            known = session.get(Message, parsed.msg_id)
            if gmail_id and known is not None and known.gmail_id is None:
                known.gmail_id = gmail_id
                session.commit()
            return IngestResult(parsed.msg_id, "skipped", "already_processed")

        if project_id is not None or company_id is not None:
            project = ensure_project(session, project_id, company_id)
            decision = RouteDecision(
                "assigned", "manual", project.project_id, project.company_id, 1.0, "given on the command line"
            )
        else:
            decision = None

        thread_id = resolve_thread(session, parsed)
        _record_message(
            session, parsed, thread_id, source, gmail_id, settings.pipeline.max_attachment_chars
        )
        if decision is None:
            decision = route(session, parsed, thread_id, settings)
        if (
            only_companies is not None
            and decision.status == "assigned"
            and decision.company_id not in only_companies
        ):
            session.rollback()  # leave no trace: the other company's run handles it
            return IngestResult(parsed.msg_id, "skipped", "other_company", route=decision)
        log_decision(session, parsed.msg_id, decision)

        if decision.status == "review":
            item = ReviewQueue(
                msg_id=parsed.msg_id,
                kind=decision.review_kind,
                candidates={
                    "projects": decision.candidates,
                    "pending_companies": decision.pending_companies,
                },
                reason=decision.reason,
                source_ref=source_ref,
                status="open",
                created_at=utcnow(),
            )
            session.add(item)
            _mark_processed(session, parsed.msg_id, "review")
            session.commit()
            return IngestResult(
                parsed.msg_id, "processed", "review", route=decision,
                company_id=decision.company_id, review_id=item.id,
            )
        if decision.status == "unassigned":
            _mark_processed(session, parsed.msg_id, "unassigned")
            session.commit()
            return IngestResult(parsed.msg_id, "processed", "unassigned", route=decision)

        project = session.get(Project, decision.project_id)
        assert project is not None
        return process_for_project(session, parsed, thread_id, project, rt, decision)


def ingest_file(path: Path | str, project_id: str | None = None, **kwargs) -> IngestResult:
    path = Path(path).resolve()
    return ingest_eml(path.read_bytes(), project_id, source_ref=f"eml:{path}", **kwargs)
