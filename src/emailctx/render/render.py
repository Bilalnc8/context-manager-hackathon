"""Deterministic rendering of one context.md per company from the store.

Layout: each company has one project, so the file is the project's requirements grouped
by category in config order and sorted by numeric ID, then "Needs confirmation" and the
changelog. (Should a company ever hold several projects, each gets its own section and
changelog lines name the project.) Each requirement
and changelog entry cites its source emails as markdown footnotes:

    [^1]: Email "Atlas export requirements", 2026-11-16, ID `atlas-001@acme.com`

The ID is shown only when the email had a real Message-ID. Footnotes are numbered by
source date, so new emails get new numbers and existing numbers stay put.

Same store contents -> same bytes: no wall-clock times, "\n" line endings.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from sqlalchemy import select
from sqlalchemy.orm import Session

from emailctx.config import Settings
from emailctx.render.publish import Publisher, PublishResult
from emailctx.store.models import ChangeLog, Company, Conflict, Message, Project, Requirement

_TEMPLATES = Path(__file__).parent / "templates"
_VERBS = {"add": "added", "modify": "modified", "remove": "removed"}

_env = Environment(
    loader=FileSystemLoader(_TEMPLATES),
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
    undefined=StrictUndefined,
)


def category_title(category: str) -> str:
    return category.upper() if len(category) <= 2 else category[0].upper() + category[1:]


def _day(d: datetime | None) -> str:
    return d.strftime("%Y-%m-%d") if d else "undated"


def _one_line(s: str) -> str:
    return " ".join(s.split())


def _footnote_text(msg_id: str, message: Message | None) -> str:
    subject = _one_line(message.subject) if message and message.subject else "(no subject)"
    parts = [f'Email "{subject}"', _day(message.date if message else None)]
    if not msg_id.startswith("sha256:"):  # only a real Message-ID is worth showing
        parts.append(f"ID `{msg_id}`")
    if message is not None and message.attachments:
        parts.append("attachments: " + ", ".join(message.attachments))
    return ", ".join(parts)


def render_company(session: Session, company_id: str, settings: Settings) -> str:
    company = session.get(Company, company_id)
    if company is None:
        raise KeyError(f"unknown company {company_id}")

    projects = session.scalars(
        select(Project).where(Project.company_id == company_id).order_by(Project.project_id)
    ).all()
    project_ids = [p.project_id for p in projects]
    project_names = {p.project_id: p.name for p in projects}

    reqs = session.scalars(
        select(Requirement)
        .where(Requirement.project_id.in_(project_ids), Requirement.status == "active")
        .order_by(Requirement.project_id, Requirement.seq)
    ).all()
    changes = session.scalars(
        select(ChangeLog)
        .where(ChangeLog.project_id.in_(project_ids))
        .order_by(ChangeLog.change_id)
    ).all()
    conflicts = session.scalars(
        select(Conflict)
        .where(Conflict.project_id.in_(project_ids), Conflict.status == "open")
        .order_by(Conflict.conflict_id)
    ).all()

    # Footnotes: every cited email, numbered by (date, msg_id).
    cited = (
        {m for r in reqs for m in r.source_ids}
        | {c.source_id for c in changes}
        | {c.other_source_id for c in conflicts}
        | {c.kept_source_id for c in conflicts if c.kept_source_id}
    )
    messages = {
        m.msg_id: m
        for m in session.scalars(select(Message).where(Message.msg_id.in_(cited)))
    }

    def order(msg_id: str) -> tuple:
        d = messages[msg_id].date if msg_id in messages else None
        return (d is None, d or datetime.min, msg_id)

    numbers = {msg_id: n for n, msg_id in enumerate(sorted(cited, key=order), start=1)}

    def refs(msg_ids: Sequence[str]) -> str:
        return "".join(f"[^{n}]" for n in sorted(numbers[m] for m in set(msg_ids)))

    known = set(settings.categories)
    project_blocks = []
    for p in projects:
        mine = [r for r in reqs if r.project_id == p.project_id]
        if not mine and not any(c.project_id == p.project_id for c in changes):
            continue  # nothing to say about this project yet
        groups = []
        for cat in settings.categories:
            members = [r for r in mine if (r.category if r.category in known else "other") == cat]
            if members:
                groups.append(
                    {
                        "title": category_title(cat),
                        "reqs": [
                            {"req_id": r.req_id, "text": r.text, "refs": refs(r.source_ids)}
                            for r in members
                        ],
                    }
                )
        project_blocks.append({"name": p.name, "groups": groups})

    changelog = []
    for c in changes:
        if c.op == "modify":
            summary = f'"{c.old_text}" → "{c.new_text}"'
        else:
            summary = c.new_text if c.op == "add" else c.old_text
        changelog.append(
            {
                "date": _day(c.timestamp),
                "project": project_names[c.project_id],
                "req_id": c.req_id,
                "verb": _VERBS[c.op],
                "summary": summary,
                "ref": refs([c.source_id]),
            }
        )

    sources = [
        {"n": n, "text": _footnote_text(msg_id, messages.get(msg_id))}
        for msg_id, n in sorted(numbers.items(), key=lambda kv: kv[1])
    ]

    single = len(project_blocks) <= 1  # one project per company: no project headings
    needs_confirmation = []
    for c in conflicts:
        kept = f'"{c.kept_text}"' + (refs([c.kept_source_id]) if c.kept_source_id else "")
        where = "" if single else f"{project_names[c.project_id]} · "
        head = f"**C{c.conflict_id}** · {where}**{c.req_id}**"
        if c.kind == "contradiction":
            body = (
                f'now {kept}, which contradicts the earlier "{c.other_text}"'
                f"{refs([c.other_source_id])}. The newer email won."
            )
        elif c.other_text is None:
            body = (
                f"an older email asks to remove it{refs([c.other_source_id])}, "
                f"but a newer one set {kept}. The newer email won."
            )
        else:
            body = (
                f'an older email says "{c.other_text}"{refs([c.other_source_id])}, '
                f"but a newer one set {kept}. The newer email won."
            )
        needs_confirmation.append(f"{head}: {body}")

    text = _env.get_template("context.md.j2").render(
        company={"company_id": company.company_id, "name": company.name},
        projects=project_blocks,
        single=single,
        needs_confirmation=needs_confirmation,
        changelog=changelog,
        sources=sources,
    )
    return text.rstrip("\n") + "\n"


def render_and_publish(
    session: Session,
    company_id: str,
    settings: Settings,
    publishers: Sequence[Publisher],
    message: str = "",
) -> list[PublishResult]:
    """Render and hand the file to every publisher. `message` describes the change."""
    content = render_company(session, company_id, settings)
    return [p.publish(company_id, content, message) for p in publishers]
