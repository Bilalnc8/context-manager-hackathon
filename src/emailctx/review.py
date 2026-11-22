"""The review queue: emails the router couldn't place with confidence.

Assigning an item re-reads the email from its source (it was never stored), processes
it for the chosen project, and turns the decision into rules where it can:
- the thread is pinned to the project (replies inherit it),
- the external sender becomes a participant of the project,
- a sender on a personal provider becomes a contact of the project's company,
- a single pending company on the item is approved: activated if it is the project's
  company, otherwise its domain becomes an alias of the project's company.

Dismissing an item marks the email unassigned; with ignore_domains its pending
companies are set to "ignored" so their domains stop sending emails to review.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from emailctx.clean import ParsedEmail, parse_eml
from emailctx.ingest import (
    IngestError,
    IngestResult,
    Runtime,
    _mark_processed,
    ensure_project,
    process_for_project,
)
from emailctx.registry import add_rule
from emailctx.routing.cascade import RouteDecision, log_decision
from emailctx.routing.company import Registry, classify
from emailctx.store.db import utcnow
from emailctx.store.models import (
    Company,
    CompanyContact,
    CompanyDomain,
    Message,
    Project,
    ReviewQueue,
)


class ReviewError(ValueError):
    pass


@dataclass(frozen=True)
class ReviewItem:
    id: int
    kind: str
    msg_id: str | None
    subject: str | None
    date: datetime | None
    reason: str
    candidates: list[str]
    pending_companies: list[str]
    source_ref: str | None


def list_open(session: Session) -> list[ReviewItem]:
    rows = session.execute(
        select(ReviewQueue, Message.subject, Message.date)
        .outerjoin(Message, Message.msg_id == ReviewQueue.msg_id)
        .where(ReviewQueue.status == "open")
        .order_by(ReviewQueue.id)
    ).all()
    return [
        ReviewItem(
            id=item.id,
            kind=item.kind,
            msg_id=item.msg_id,
            subject=subject,
            date=date,
            reason=item.reason,
            candidates=list((item.candidates or {}).get("projects", [])),
            pending_companies=list((item.candidates or {}).get("pending_companies", [])),
            source_ref=item.source_ref,
        )
        for item, subject, date in rows
    ]


def make_reader(
    gmail: Callable[[str], bytes] | None = None,
) -> Callable[[str | None], bytes]:
    """Build a reader for review source refs: "eml:<path>" or "gmail:<id>".

    `gmail` fetches raw bytes by Gmail ID; it can be lazy (e.g. sign in on first use).
    """

    def read(source_ref: str | None) -> bytes:
        if not source_ref:
            raise ReviewError("item has no source to read the email from")
        scheme, _, ref = source_ref.partition(":")
        if scheme == "eml":
            path = Path(ref)
            if not path.exists():
                raise ReviewError(f"source file is gone: {path}")
            return path.read_bytes()
        if scheme == "gmail":
            if gmail is None:
                raise ReviewError("this item comes from Gmail: sign in with `emailctx auth` first")
            try:
                return gmail(ref)
            except ReviewError:
                raise
            except Exception as e:
                raise ReviewError(f"could not read Gmail message {ref}: {e}") from e
        raise ReviewError(f"unknown source {scheme!r}")

    return read


read_source = make_reader()


def _open_item(session: Session, item_id: int) -> ReviewQueue:
    item = session.get(ReviewQueue, item_id)
    if item is None:
        raise ReviewError(f"no review item {item_id}")
    if item.status != "open":
        raise ReviewError(f"review item {item_id} is already {item.status}")
    return item


def _approve_pending(session: Session, pending: list[str], project: Project) -> None:
    live = [c for c in pending if (co := session.get(Company, c)) is not None and co.status == "pending"]
    if len(live) != 1:
        return  # none, or ambiguous which one the assignment approves
    cid = live[0]
    if cid == project.company_id:
        session.get(Company, cid).status = "active"
        return
    for row in session.scalars(select(CompanyDomain).where(CompanyDomain.company_id == cid)):
        row.company_id = project.company_id
    for row in session.scalars(select(CompanyContact).where(CompanyContact.company_id == cid)):
        row.company_id = project.company_id
    session.flush()
    session.delete(session.get(Company, cid))


def _learn_from(session: Session, parsed: ParsedEmail, project: Project, rt: Runtime) -> None:
    if not parsed.sender:
        return
    info = classify(
        parsed.sender, Registry.load(session), rt.settings.routing.personal_providers,
        rt.settings.routing.meeting_bot_domains,
    )
    if info is not None and info.kind == "bot":
        return  # never learn a meeting bot as a project participant
    if info is None or info.kind == "own":
        return
    add_rule(session, project.project_id, "participant", info.address)
    if (
        info.kind == "personal"
        and project.company_id
        and session.get(CompanyContact, info.address) is None
    ):
        session.add(CompanyContact(email=info.address, company_id=project.company_id))


def assign(
    item_id: int,
    project_id: str | None,
    rt: Runtime,
    *,
    company_id: str | None = None,
    reader: Callable[[str | None], bytes] = read_source,
) -> IngestResult:
    with rt.sessions() as session:
        item = _open_item(session, item_id)
        parsed = parse_eml(reader(item.source_ref))
        if parsed.msg_id != item.msg_id:
            raise ReviewError(f"source no longer holds message {item.msg_id}")
        message = session.get(Message, parsed.msg_id)
        if message is None or not message.thread_id:
            raise ReviewError(f"message {parsed.msg_id} was never recorded")

        try:
            project = ensure_project(session, project_id, company_id)
        except IngestError as e:
            raise ReviewError(str(e)) from e
        _approve_pending(session, list((item.candidates or {}).get("pending_companies", [])), project)
        _learn_from(session, parsed, project, rt)

        decision = RouteDecision(
            "assigned", "manual", project.project_id, project.company_id, 1.0,
            f"review item {item_id} assigned by a person",
        )
        log_decision(session, parsed.msg_id, decision)
        item.status = "assigned"
        item.resolved_project_id = project.project_id
        item.resolved_at = utcnow()
        session.flush()
        return process_for_project(session, parsed, message.thread_id, project, rt, decision)


def dismiss(item_id: int, rt: Runtime, *, ignore_domains: bool = False) -> None:
    with rt.sessions() as session:
        item = _open_item(session, item_id)
        if ignore_domains:
            for cid in (item.candidates or {}).get("pending_companies", []):
                company = session.get(Company, cid)
                if company is not None and company.status == "pending":
                    company.status = "ignored"
        if item.msg_id:
            log_decision(
                session, item.msg_id,
                RouteDecision("unassigned", "manual", reason=f"review item {item_id} dismissed"),
            )
            _mark_processed(session, item.msg_id, "unassigned")
        item.status = "dismissed"
        item.resolved_at = utcnow()
        session.commit()
