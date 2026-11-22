"""Deterministic rendering of one context.md per company from the store.

Layout (one project per company), most actionable first:

    # <Company>: project context
    Project, client domain, status counts, last change
    ## Key dates                  milestones
    ## Open concerns              concerns not yet resolved
    ## Questions for the client   not asked yet / asked and waiting
    ## Needs confirmation         contradictions and stale emails
    ## Deliverables               ### per deliverable category, each with its status
    ## Next actions               open actions ("Owner (Org): what, by when")
    ## Out of scope               excluded or deferred work
    ## Decisions
    ## Answered by the client
    ## Resolved and completed     resolved concerns, done actions
    ## Changelog
    footnotes

Within a section items are sorted by numeric ID; deliverable categories follow config
order. (Should a company ever hold several projects, each gets its own section and
company-level lines name the project.) Each item and changelog entry cites its source
emails as markdown footnotes:

    [^1]: Email "Atlas export requirements", 2026-11-16, from Jane Doe

The sender is the From display name (the address only when there is no name; the file
may be published, so addresses stay out of it otherwise). Footnotes are numbered by
source date, so new emails get new numbers and existing numbers stay put.

Same store contents -> same bytes: no wall-clock times, "\n" line endings. The header's
"Last change" comes from the cited sources only, so an email that changes nothing leaves
the file byte-identical.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from sqlalchemy import select
from sqlalchemy.orm import Session

from emailctx.config import RESOLVABLE_CATEGORIES, TRACKING_CATEGORIES, Settings
from emailctx.render.publish import Publisher, PublishResult
from emailctx.store.models import (
    FINISHED_STATES,
    ChangeLog,
    Company,
    CompanyDomain,
    Conflict,
    Message,
    Project,
    Question,
    Requirement,
)

_TEMPLATES = Path(__file__).parent / "templates"
_VERBS = {"add": "added", "modify": "modified", "remove": "removed", "resolve": "resolved"}
_DELIVERY = {"open": "open", "in_progress": "in progress", "done": "done", "verified": "verified"}

# Tracking categories -> section title, before and after Deliverables.
_BEFORE = (("milestone", "Key dates"), ("concern", "Open concerns"))
_AFTER = (("action", "Next actions"), ("out-of-scope", "Out of scope"), ("decision", "Decisions"))

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


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def sender_label(message: Message | None) -> str | None:
    """'Gurinder Gill (gill@gmail.com)', or just the address when there is no name."""
    if message is None or not message.sender:
        return None
    name = getattr(message, "sender_name", None)
    return f"{_one_line(name)} ({message.sender})" if name else message.sender


def _footnote_sender(message: Message | None) -> str | None:
    """The sender's display name for a citation. The file may be published (Drive,
    S3, git), so the address appears only when the email had no name."""
    if message is None:
        return None
    name = getattr(message, "sender_name", None)
    return _one_line(name) if name else message.sender


def _footnote_text(msg_id: str, message: Message | None) -> str:
    subject = _one_line(message.subject) if message and message.subject else "(no subject)"
    parts = [f'Email "{subject}"', _day(message.date if message else None)]
    who = _footnote_sender(message)
    if who:
        parts.append(f"from {who}")
    if message is not None and message.attachments:
        parts.append("attachments: " + ", ".join(message.attachments))
    return ", ".join(parts)


@dataclass
class _Loaded:
    company: Company
    projects: Sequence[Project]
    reqs: Sequence[Requirement]
    changes: Sequence[ChangeLog]
    conflicts: Sequence[Conflict]
    questions: Sequence[Question]
    messages: dict[str, Message]
    numbers: dict[str, int]  # cited msg_id -> footnote number


def _load(session: Session, company_id: str) -> _Loaded:
    company = session.get(Company, company_id)
    if company is None:
        raise KeyError(f"unknown company {company_id}")

    projects = session.scalars(
        select(Project).where(Project.company_id == company_id).order_by(Project.project_id)
    ).all()
    project_ids = [p.project_id for p in projects]

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

    questions = session.scalars(
        select(Question)
        .where(Question.project_id.in_(project_ids), Question.status != "withdrawn")
        .order_by(Question.project_id, Question.seq)
    ).all()

    # Footnotes: every cited email, numbered by (date, msg_id).
    cited = (
        {q.answer_source_id for q in questions if q.answer_source_id}
        | {m for r in reqs for m in r.source_ids}
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
    return _Loaded(company, projects, reqs, changes, conflicts, questions, messages, numbers)


def footnotes(session: Session, company_id: str) -> dict[int, tuple[str, Message | None]]:
    """Footnote number -> (msg_id, its message), numbered exactly as in the rendered file."""
    d = _load(session, company_id)
    return {n: (m, d.messages.get(m)) for m, n in d.numbers.items()}


def item_kind(category: str, settings: Settings) -> str:
    """A tracking category, a configured deliverable category, or "other"."""
    if category in TRACKING_CATEGORIES or category in settings.categories:
        return category
    return "other"


def is_deliverable(category: str, settings: Settings) -> bool:
    return item_kind(category, settings) not in TRACKING_CATEGORIES


def _section(title: str, lines: list[str] | None = None, groups: list[dict] | None = None) -> dict:
    return {"title": title, "groups": groups if groups is not None else [{"title": None, "lines": lines}]}


def render_company(session: Session, company_id: str, settings: Settings) -> str:
    d = _load(session, company_id)
    company, projects, reqs, changes = d.company, d.projects, d.reqs, d.changes
    conflicts, questions, messages, numbers = d.conflicts, d.questions, d.messages, d.numbers
    project_names = {p.project_id: p.name for p in projects}

    def refs(msg_ids: Sequence[str]) -> str:
        return "".join(f"[^{n}]" for n in sorted(numbers[m] for m in set(msg_ids)))

    deliverable_cats = [c for c in settings.categories if c not in TRACKING_CATEGORIES]
    if "other" not in deliverable_cats:
        deliverable_cats.append("other")

    def kind(r: Requirement) -> str:
        return item_kind(r.category, settings)

    def finished(r: Requirement) -> bool:
        return r.delivery in FINISHED_STATES

    def status(r: Requirement) -> str:
        return _DELIVERY.get(r.delivery, r.delivery)

    def line(r: Requirement, tag: str | None) -> str:
        return f"**{r.req_id}**{f' [{tag}]' if tag else ''} {r.text}{refs(r.source_ids)}"

    def project_sections(mine: list[Requirement]) -> list[dict]:
        by_kind: dict[str, list[Requirement]] = {}
        for r in mine:
            by_kind.setdefault(kind(r), []).append(r)
        out = []
        for cat, title in _BEFORE:
            items = by_kind.get(cat, [])
            if cat == "milestone":  # the approval state is in the text
                lines = [line(r, "done" if finished(r) else None) for r in items]
            else:
                lines = [line(r, status(r)) for r in items if not finished(r)]
            if lines:
                out.append(_section(title, lines))
        groups = [
            {"title": category_title(cat), "lines": [line(r, status(r)) for r in by_kind[cat]]}
            for cat in deliverable_cats
            if by_kind.get(cat)
        ]
        if groups:
            out.append(_section("Deliverables", groups=groups))
        for cat, title in _AFTER:
            items = by_kind.get(cat, [])
            if cat == "action":
                lines = [line(r, status(r)) for r in items if not finished(r)]
            else:
                lines = [line(r, None) for r in items]
            if lines:
                out.append(_section(title, lines))
        closed = [r for r in mine if kind(r) in RESOLVABLE_CATEGORIES and finished(r)]
        if closed:
            out.append(_section(
                "Resolved and completed",
                [line(r, "resolved" if kind(r) == "concern" else "done") for r in closed],
            ))
        return out

    blocks = []
    for p in projects:
        mine = [r for r in reqs if r.project_id == p.project_id]
        if not mine and not any(c.project_id == p.project_id for c in changes):
            continue  # nothing to say about this project yet
        blocks.append({"project_id": p.project_id, "name": p.name, "sections": project_sections(mine)})
    single = len(blocks) <= 1  # one project per company: no project headings

    def where(project_id: str) -> str:
        return "" if single else f"{project_names[project_id]} · "

    changelog = []
    for c in changes:
        if c.op == "modify":
            summary = f'"{c.old_text}" → "{c.new_text}"'
        else:
            summary = c.new_text if c.op == "add" else c.old_text
        changelog.append(
            f"{_day(c.timestamp)} · {where(c.project_id)}**{c.req_id}** {_VERBS[c.op]}: "
            f"{summary}{refs([c.source_id])}"
        )

    sources = [
        {"n": n, "text": _footnote_text(msg_id, messages.get(msg_id))}
        for msg_id, n in sorted(numbers.items(), key=lambda kv: kv[1])
    ]

    needs_confirmation = []
    for c in conflicts:
        kept = f'"{c.kept_text}"' + (refs([c.kept_source_id]) if c.kept_source_id else "")
        head = f"**C{c.conflict_id}** · {where(c.project_id)}**{c.req_id}**"
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

    def question_line(q: Question) -> str:
        about = f" (about {q.req_id})" if q.req_id else ""
        return f"{where(q.project_id)}**{q.question_id}**{about} {q.text}"

    waiting = [q for q in questions if q.status in ("open", "asked")]
    open_questions = []
    for q in waiting:
        state = f"asked {_day(q.asked_at)}, waiting for the answer" if q.status == "asked" else "not asked yet"
        open_questions.append(f"{question_line(q)} _({state})_")
    answered = []
    for q in questions:
        if q.status == "answered":
            source = refs([q.answer_source_id]) if q.answer_source_id else f" _(recorded by {q.answered_by})_"
            answered.append(f"{question_line(q)} **Answer:** {q.answer}{source}")

    # Questions and confirmations: with one project they go right before Deliverables,
    # so everything still waiting is at the top; answers go before the resolved items.
    waiting_sections = [
        _section(title, lines)
        for title, lines in (
            ("Questions for the client", open_questions),
            ("Needs confirmation", needs_confirmation),
        )
        if lines
    ]
    answered_sections = [_section("Answered by the client", answered)] if answered else []
    company_sections: list[dict] = []
    if single and blocks:
        secs = blocks[0]["sections"]
        titles = [s["title"] for s in secs]
        at = next((i for i, t in enumerate(titles) if t not in ("Key dates", "Open concerns")), len(secs))
        secs[at:at] = waiting_sections
        titles = [s["title"] for s in secs]
        at = titles.index("Resolved and completed") if "Resolved and completed" in titles else len(secs)
        secs[at:at] = answered_sections
    else:
        company_sections = waiting_sections + answered_sections

    header = []
    if reqs or questions:
        domains = sorted(session.scalars(
            select(CompanyDomain.domain).where(CompanyDomain.company_id == company_id)
        ))
        ident = [f"**Project:** `{blocks[0]['project_id']}`"] if single and blocks else []
        if domains:
            label = "Client domains" if len(domains) > 1 else "Client domain"
            ident.append(f"**{label}:** " + ", ".join(f"`{x}`" for x in domains))
        if ident:
            header.append(" · ".join(ident))
        deliverables = [r for r in reqs if kind(r) not in TRACKING_CATEGORIES]
        counts = " · ".join(
            f"{sum(1 for r in deliverables if r.delivery == k)} {label}" for k, label in _DELIVERY.items()
        )
        open_concerns = sum(1 for r in reqs if kind(r) == "concern" and not finished(r))
        open_actions = sum(1 for r in reqs if kind(r) == "action" and not finished(r))
        header.append(
            f"**Status:** deliverables {counts} · {_plural(open_concerns, 'open concern')} · "
            f"{_plural(open_actions, 'open action')} · "
            f"{_plural(len(waiting), 'question')} for the client"
        )
    change_sources = {c.source_id for c in changes} | {
        q.answer_source_id for q in questions if q.answer_source_id
    }
    dated = [(messages[m].date, m) for m in change_sources if m in messages and messages[m].date]
    if dated:
        when, latest = max(dated)
        subject = _one_line(messages[latest].subject or "(no subject)")
        header.append(f'**Last change:** {_day(when)}, email "{subject}"{refs([latest])}')

    text = _env.get_template("context.md.j2").render(
        company={"company_id": company.company_id, "name": company.name},
        header=header,
        blocks=blocks,
        single=single,
        company_sections=company_sections,
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
