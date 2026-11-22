"""Open conflicts ("Needs confirmation") and confirming them.

Confirming accepts what the file says now and removes the item from the section.
Confirming with use_other switches the requirement to the other email's version instead
(a modify, or a removal when the other email asked for one), cited to that email.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from emailctx.ingest import Runtime
from emailctx.pipeline.apply import apply_ops
from emailctx.pipeline.types import AcceptedOp
from emailctx.render.publish import PublishResult
from emailctx.render.render import render_and_publish
from emailctx.store.models import Conflict, Project, Requirement


class ConflictError(ValueError):
    pass


def list_open(session: Session, company_id: str | None = None) -> list[Conflict]:
    q = select(Conflict).where(Conflict.status == "open").order_by(Conflict.conflict_id)
    if company_id:
        q = q.join(Project, Project.project_id == Conflict.project_id).where(
            Project.company_id == company_id
        )
    return list(session.scalars(q))


def confirm(conflict_id: int, rt: Runtime, *, use_other: bool = False) -> list[PublishResult]:
    with rt.sessions() as session:
        c = session.get(Conflict, conflict_id)
        if c is None:
            raise ConflictError(f"no conflict C{conflict_id}")
        if c.status != "open":
            raise ConflictError(f"conflict C{conflict_id} is already {c.status}")

        if use_other:
            req = session.get(Requirement, (c.project_id, c.req_id))
            if req is None or req.status != "active":
                raise ConflictError(f"{c.req_id} is no longer active")
            kind = "remove" if c.other_text is None else "modify"
            apply_ops(
                session,
                c.project_id,
                [AcceptedOp(kind, c.req_id, c.other_text, None, "", c.other_source_id)],
                source_date=req.updated_at,  # a person decided; keep the latest-wins clock
            )
            action = f"{c.req_id} {kind} (C{conflict_id} resolved to the other email)"
        else:
            action = f"C{conflict_id} confirmed"

        c.status = "confirmed"
        session.commit()
        project = session.get(Project, c.project_id)
        return render_and_publish(
            session, project.company_id, rt.settings, rt.targets(),
            message=f"{c.project_id}: {action} (manual)",
        )
