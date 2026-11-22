"""Delivery tracking and client questions, on top of the requirements store.

Requirements come from client email (the pipeline). This module is what the team adds:
- delivery status per requirement: open -> in_progress -> done -> verified, set by a
  coding agent or QA, with every change logged in status_changes;
- questions only the client can answer: logged once (a repeat returns the existing
  question, or its answer if the client already gave one), marked asked when they go
  out, and closed when the answer comes back (from an email via the pipeline, or
  recorded by hand);
- client input pasted into a Claude session (an email or meeting notes), run through
  the same pipeline as a synced email.

Plain functions over a Session, so the MCP server, the CLI and tests share them. Callers
own the transaction and re-render the company's file afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from emailctx.context import CompanyFile, CompanyNotFound, find_company
from emailctx.store.db import utcnow
from emailctx.store.models import (
    DELIVERY_STATES,
    Company,
    Project,
    Question,
    Requirement,
    StatusChange,
)
from emailctx.textnorm import clean_requirement_text, normalize_requirement


class TrackerError(ValueError):
    """Bad input from a person or an agent; the message says what to do instead."""


# --- Companies -------------------------------------------------------------------------


@dataclass(frozen=True)
class CompanySummary:
    company_id: str
    name: str
    project_id: str
    counts: dict[str, int]  # delivery state -> active requirements in it
    waiting_questions: int


def _companies_with_projects(session: Session) -> list[tuple[Company, Project]]:
    rows = session.execute(
        select(Company, Project)
        .join(Project, Project.company_id == Company.company_id)
        .where(Company.status == "active")
        .order_by(Company.name)
    ).all()
    seen: dict[str, tuple[Company, Project]] = {}
    for c, p in rows:  # one project per company; keep the first if a store has more
        seen.setdefault(c.company_id, (c, p))
    return list(seen.values())


def resolve_company(session: Session, query: str) -> tuple[Company, Project]:
    """Find a company by name or id (case-insensitive; a unique part of the name works)."""
    pairs = _companies_with_projects(session)
    files = [CompanyFile(c.company_id, c.name, file_id="") for c, _ in pairs]
    try:
        hit = find_company(files, query)
    except CompanyNotFound as e:
        msg = str(e).replace("no company file matches", "no company matches")
        raise TrackerError(msg.replace("Companies with a file", "Known companies")) from e
    return next((c, p) for c, p in pairs if c.company_id == hit.company_id)


def list_companies(session: Session) -> list[CompanySummary]:
    out = []
    for c, p in _companies_with_projects(session):
        counts = dict.fromkeys(DELIVERY_STATES, 0)
        for state, n in session.execute(
            select(Requirement.delivery, func.count())
            .where(Requirement.project_id == p.project_id, Requirement.status == "active")
            .group_by(Requirement.delivery)
        ):
            counts[state] = n
        waiting = session.scalar(
            select(func.count()).select_from(Question).where(
                Question.project_id == p.project_id, Question.status.in_(("open", "asked"))
            )
        )
        out.append(CompanySummary(c.company_id, c.name, p.project_id, counts, waiting or 0))
    return out


# --- Delivery status -------------------------------------------------------------------


def _req_id(value: str) -> str:
    v = value.strip().upper()
    if v.isdigit():
        return f"REQ-{int(v):03d}"
    return v


def items(session: Session, project_id: str, delivery: str | None = None) -> list[Requirement]:
    q = select(Requirement).where(
        Requirement.project_id == project_id, Requirement.status == "active"
    )
    if delivery:
        if delivery not in DELIVERY_STATES:
            raise TrackerError(f"status must be one of {', '.join(DELIVERY_STATES)}")
        q = q.where(Requirement.delivery == delivery)
    return list(session.scalars(q.order_by(Requirement.seq)))


def last_status_change(session: Session, project_id: str, req_id: str) -> StatusChange | None:
    return session.scalar(
        select(StatusChange)
        .where(StatusChange.project_id == project_id, StatusChange.req_id == req_id)
        .order_by(StatusChange.id.desc())
    )


def set_status(
    session: Session, project_id: str, req_id: str, status: str, *, by: str, note: str | None = None
) -> tuple[Requirement, str]:
    """Set a requirement's delivery status. Returns (requirement, previous status)."""
    status = status.strip().lower().replace(" ", "_").replace("-", "_")
    if status not in DELIVERY_STATES:
        raise TrackerError(f"status must be one of {', '.join(DELIVERY_STATES)}")
    req = session.get(Requirement, (project_id, _req_id(req_id)))
    if req is None or req.status != "active":
        raise TrackerError(f"{req_id} is not an active requirement of this project")
    previous = req.delivery
    if previous != status or (note and note != req.delivery_note):
        session.add(
            StatusChange(
                project_id=project_id, req_id=req.req_id, old_status=previous,
                new_status=status, note=note, changed_by=by, changed_at=utcnow(),
            )
        )
        req.delivery = status
        req.delivery_note = note
    session.flush()
    return req, previous


# --- Client questions ------------------------------------------------------------------


def _question_id(value: str) -> str:
    v = value.strip().upper()
    if v.isdigit():
        return f"Q-{int(v):03d}"
    return v


def questions(
    session: Session, project_id: str, statuses: tuple[str, ...] = ("open", "asked", "answered")
) -> list[Question]:
    return list(
        session.scalars(
            select(Question)
            .where(Question.project_id == project_id, Question.status.in_(statuses))
            .order_by(Question.seq)
        )
    )


def get_question(session: Session, project_id: str, question_id: str) -> Question:
    q = session.get(Question, (project_id, _question_id(question_id)))
    if q is None:
        raise TrackerError(f"no question {question_id} in this project")
    return q


def log_question(
    session: Session, project_id: str, text: str, *, by: str, req_id: str | None = None
) -> tuple[Question, bool]:
    """Log a question for the client. Returns (question, created).

    The same question (normalized text) is never logged twice: if it is already waiting,
    or the client already answered it, that question comes back with created=False.
    """
    text = clean_requirement_text(text)
    if not text:
        raise TrackerError("the question is empty")
    if req_id is not None:
        req = session.get(Requirement, (project_id, _req_id(req_id)))
        if req is None or req.status != "active":
            raise TrackerError(f"{req_id} is not an active requirement of this project")
        req_id = req.req_id
    norm = normalize_requirement(text)
    existing = session.scalar(
        select(Question).where(
            Question.project_id == project_id,
            Question.normalized_text == norm,
            Question.status != "withdrawn",
        )
    )
    if existing is not None:
        return existing, False
    seq = (
        session.scalar(select(func.max(Question.seq)).where(Question.project_id == project_id)) or 0
    ) + 1
    q = Question(
        project_id=project_id, question_id=f"Q-{seq:03d}", seq=seq, text=text,
        normalized_text=norm, req_id=req_id, status="open", created_by=by, created_at=utcnow(),
    )
    session.add(q)
    session.flush()
    return q, True


def mark_asked(session: Session, project_id: str, question_ids: list[str]) -> list[Question]:
    """Mark questions as sent to the client. Already asked or answered ones are an error,
    so nothing is asked twice."""
    qs = [get_question(session, project_id, qid) for qid in question_ids]
    for q in qs:
        if q.status != "open":
            raise TrackerError(f"{q.question_id} is already {q.status}; it won't be asked again")
    now = utcnow()
    for q in qs:
        q.status = "asked"
        q.asked_at = now
    session.flush()
    return qs


def answer_question(
    session: Session, project_id: str, question_id: str, answer: str, *, by: str
) -> Question:
    """Record an answer the client gave outside email (a call, a meeting, a chat)."""
    q = get_question(session, project_id, question_id)
    if q.status not in ("open", "asked"):
        raise TrackerError(f"{q.question_id} is {q.status}, not waiting for an answer")
    answer = clean_requirement_text(answer)
    if not answer:
        raise TrackerError("the answer is empty")
    q.status = "answered"
    q.answer = answer
    q.answer_source_id = None
    q.answered_by = by
    q.answered_at = utcnow()
    session.flush()
    return q


def withdraw_question(session: Session, project_id: str, question_id: str) -> Question:
    q = get_question(session, project_id, question_id)
    if q.status == "answered":
        raise TrackerError(f"{q.question_id} is already answered")
    q.status = "withdrawn"
    session.flush()
    return q


# --- Client input pasted into a session --------------------------------------------------


def client_input_eml(
    text: str,
    *,
    company_domain: str | None,
    kind: str = "email",
    subject: str | None = None,
    sender: str | None = None,
    date: datetime | None = None,
) -> bytes:
    """Wrap pasted text (a client email, meeting notes) as an email the pipeline reads.

    No Message-ID is set, so the pipeline keys it on a hash of sender, date, subject and
    body: the same text with the same details is processed once. The date defaults to
    now, so pasted input counts as the latest word under "latest wins".
    """
    if not text.strip():
        raise TrackerError("the client input is empty")
    msg = EmailMessage()
    default_sender = f"client@{company_domain}" if company_domain else "client@unknown.invalid"
    msg["From"] = sender or default_sender
    msg["Subject"] = subject or ("Meeting notes" if kind == "meeting_notes" else "Client feedback")
    date = date or datetime.now().astimezone()
    msg["Date"] = format_datetime(date if date.tzinfo else date.astimezone())
    msg.set_content(text)
    return bytes(msg)


__all__ = [
    "CompanySummary",
    "TrackerError",
    "answer_question",
    "client_input_eml",
    "get_question",
    "items",
    "last_status_change",
    "list_companies",
    "log_question",
    "mark_asked",
    "questions",
    "resolve_company",
    "set_status",
    "withdraw_question",
]
