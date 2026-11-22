"""Apply validated ops to the requirements store. One change_log row per change.

Also records conflicts for "Needs confirmation":
- an applied modify the judge flagged as contradicting the old text (latest wins),
- a stale op from an email older than the requirement's last change (not applied).

A modify of finished work (done or verified) reopens it: what was delivered no longer
matches what the client asks for. A resolve closes a concern or action (delivery
"done", shown as resolved), logged in status_changes as set by the email. Client answers
to open questions close them.

The evidence quote was checked against the email during validation and is not stored.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from emailctx.pipeline.types import (
    AcceptedAnswer,
    AcceptedOp,
    AppliedChange,
    QuestionView,
    ReqView,
)
from emailctx.store.db import utcnow
from emailctx.store.models import (
    FINISHED_STATES,
    ChangeLog,
    Conflict,
    Question,
    Requirement,
    StatusChange,
)
from emailctx.textnorm import normalize_requirement


def load_active(session: Session, project_id: str) -> list[ReqView]:
    rows = session.scalars(
        select(Requirement)
        .where(Requirement.project_id == project_id, Requirement.status == "active")
        .order_by(Requirement.seq)
    )
    return [
        ReqView(
            r.req_id, r.text, r.normalized_text, r.category, r.updated_at,
            r.source_ids[-1] if r.source_ids else None, r.delivery,
        )
        for r in rows
    ]


def load_waiting_questions(session: Session, project_id: str) -> list[QuestionView]:
    """Questions still waiting for the client's answer (open or asked)."""
    rows = session.scalars(
        select(Question)
        .where(Question.project_id == project_id, Question.status.in_(("open", "asked")))
        .order_by(Question.seq)
    )
    return [QuestionView(q.question_id, q.text, q.req_id) for q in rows]


def apply_answers(
    session: Session, project_id: str, answers: Sequence[AcceptedAnswer], *, source_date: datetime | None
) -> list[str]:
    """Record client answers from an email. Returns the answered question ids."""
    done = []
    for a in answers:
        q = session.get(Question, (project_id, a.question_id))
        if q is None or q.status not in ("open", "asked"):
            raise ValueError(f"{a.question_id} is not waiting for an answer in {project_id}")
        q.status = "answered"
        q.answer = a.answer
        q.answer_source_id = a.msg_id
        q.answered_by = "client (email)"
        q.answered_at = source_date or utcnow()
        done.append(q.question_id)
    session.flush()
    return done


RESOLVED_NOTE = "resolved by email"


def format_req_id(seq: int) -> str:
    return f"REQ-{seq:03d}"


def apply_ops(
    session: Session,
    project_id: str,
    accepted: Sequence[AcceptedOp],
    *,
    source_date: datetime | None,
) -> list[AppliedChange]:
    """Apply ops in order. Caller owns the transaction."""
    applied: list[AppliedChange] = []
    next_seq = (
        session.scalar(
            select(func.max(Requirement.seq)).where(Requirement.project_id == project_id)
        )
        or 0
    ) + 1
    now = utcnow()

    for op in accepted:
        if op.kind == "add":
            assert op.text is not None and op.category is not None
            req_id = format_req_id(next_seq)
            session.add(
                Requirement(
                    project_id=project_id,
                    req_id=req_id,
                    seq=next_seq,
                    text=op.text,
                    normalized_text=normalize_requirement(op.text),
                    category=op.category,
                    status="active",
                    version=1,
                    source_ids=[op.msg_id],
                    updated_at=source_date,
                )
            )
            next_seq += 1
            old_text, new_text = None, op.text
        else:
            assert op.req_id is not None
            req = session.get(Requirement, (project_id, op.req_id))
            if req is None or req.status != "active":
                raise ValueError(f"{op.req_id} is not an active requirement of {project_id}")
            req_id = req.req_id
            old_text = req.text
            previous_source = req.source_ids[-1] if req.source_ids else None
            if op.kind == "resolve":
                session.add(
                    StatusChange(
                        project_id=project_id,
                        req_id=req.req_id,
                        old_status=req.delivery,
                        new_status="done",
                        note=RESOLVED_NOTE,
                        changed_by=f"email {op.msg_id}",
                        changed_at=now,
                    )
                )
                req.delivery = "done"
                req.delivery_note = RESOLVED_NOTE
                new_text = req.text
            elif op.kind == "modify":
                assert op.text is not None
                if req.delivery in FINISHED_STATES:
                    session.add(
                        StatusChange(
                            project_id=project_id,
                            req_id=req.req_id,
                            old_status=req.delivery,
                            new_status="open",
                            note="reopened: the client changed this requirement",
                            changed_by=f"email {op.msg_id}",
                            changed_at=now,
                        )
                    )
                    req.delivery = "open"
                    req.delivery_note = None
                req.text = op.text
                req.normalized_text = normalize_requirement(op.text)
                if op.category:
                    req.category = op.category
                new_text = op.text
            else:
                req.status = "removed"
                new_text = None
            req.version += 1
            if op.msg_id not in req.source_ids:
                req.source_ids = [*req.source_ids, op.msg_id]
            req.updated_at = source_date
            if op.contradicts and previous_source is not None:
                session.add(
                    Conflict(
                        project_id=project_id,
                        req_id=req_id,
                        kind="contradiction",
                        kept_text=new_text,
                        kept_source_id=op.msg_id,
                        other_text=old_text,
                        other_source_id=previous_source,
                        status="open",
                        created_at=now,
                    )
                )

        session.add(
            ChangeLog(
                project_id=project_id,
                req_id=req_id,
                op=op.kind,
                old_text=old_text,
                new_text=new_text,
                source_id=op.msg_id,
                timestamp=source_date,
                applied_at=now,
            )
        )
        applied.append(AppliedChange(req_id, op.kind, old_text, new_text))

    session.flush()
    return applied


def record_stale(session: Session, project_id: str, stale: Sequence[AcceptedOp]) -> int:
    """Record ops from emails older than the requirement's last change. Nothing changes."""
    now = utcnow()
    for op in stale:
        req = session.get(Requirement, (project_id, op.req_id))
        assert req is not None
        session.add(
            Conflict(
                project_id=project_id,
                req_id=req.req_id,
                kind="stale",
                kept_text=req.text,
                kept_source_id=req.source_ids[-1] if req.source_ids else None,
                other_text=op.text if op.kind == "modify" else None,
                other_source_id=op.msg_id,
                status="open",
                created_at=now,
            )
        )
    session.flush()
    return len(stale)
