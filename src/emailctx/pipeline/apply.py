"""Apply validated ops to the requirements store. One change_log row per change.

The evidence quote was checked against the email during validation and is not stored.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from emailctx.pipeline.types import AcceptedOp, AppliedChange, ReqView
from emailctx.store.db import utcnow
from emailctx.store.models import ChangeLog, Requirement
from emailctx.textnorm import normalize_requirement


def load_active(session: Session, project_id: str) -> list[ReqView]:
    rows = session.scalars(
        select(Requirement)
        .where(Requirement.project_id == project_id, Requirement.status == "active")
        .order_by(Requirement.seq)
    )
    return [ReqView(r.req_id, r.text, r.normalized_text, r.category) for r in rows]


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
            if op.kind == "modify":
                assert op.text is not None
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
