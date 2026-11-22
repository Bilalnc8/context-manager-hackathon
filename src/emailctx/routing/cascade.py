"""Routing cascade: stop at the first confident hit (DESIGN.md, "Routing cascade").

1. Thread inheritance: the thread is pinned to exactly one project.
2. Explicit signals: Gmail label, keyword/codename in the subject, a link to the
   project's Drive folder. Treated as certain.
3. Company match: a company with exactly one active project assigns it; otherwise its
   projects become the candidates. No company (internal-only): all projects stay candidates.
2b. Meeting bots (fireflies.ai, otter.ai, ...): the sender's domain says nothing, so
   the subject, body and attachments are searched for each company's name, its
   project's name and keywords, and its domains. Exactly one company named assigns it;
   several go to review.
4. Participants: an address (or a whole domain) that maps to exactly one candidate
   project assigns it.
7. Review queue: everything else, plus ties, pending companies and unknown domains.

Steps 5-6 of the design (embeddings, LLM classifier) are intentionally not built:
routing is decided by domains and rules only, never by a model.

Every rule hit is certain (confidence 1.0). Anything ambiguous goes to review: a wrong
route pollutes another project's requirements, a missed one only waits for a person.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from emailctx.clean import ParsedEmail
from emailctx.config import Settings
from emailctx.routing.company import Registry, registrable_domain, resolve_company
from emailctx.store.db import utcnow
from emailctx.store.models import (
    Company,
    CompanyDomain,
    Project,
    ProjectRule,
    RoutingLog,
    ThreadAssignment,
)


@dataclass
class RouteDecision:
    status: Literal["assigned", "review", "unassigned"]
    step: str  # thread | label | keyword | drive_link | company | participant | review | manual
    project_id: str | None = None
    company_id: str | None = None
    confidence: float | None = None
    reason: str = ""
    review_kind: Literal["project", "new_company"] = "project"
    candidates: list[str] = field(default_factory=list)  # project ids
    pending_companies: list[str] = field(default_factory=list)


def _assigned(project: Project, step: str, reason: str) -> RouteDecision:
    return RouteDecision(
        "assigned", step, project.project_id, project.company_id, 1.0, reason
    )


def _review(reason: str, candidates: list[Project], **kw) -> RouteDecision:
    return RouteDecision(
        "review", "review", reason=reason, candidates=[p.project_id for p in candidates], **kw
    )


def _keyword_in(keyword: str, text: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(keyword)}(?![\w-])", text, re.IGNORECASE) is not None


def _participant_matches(value: str, address: str) -> bool:
    value = value.strip().lower()
    if "@" in value:
        return value == address
    host = address.rpartition("@")[2]
    return host == value or registrable_domain(host) == value


_MIN_NAME = 3  # shorter names match too much text


def _companies_named(
    session: Session, parsed: ParsedEmail, projects: list[Project], rules: list[ProjectRule]
) -> list[Project]:
    """Projects (one per company) whose company is named in a meeting-bot email."""
    from emailctx.clean import attachment_text

    haystack = "\n".join(
        [parsed.subject, parsed.body_text]
        + [a.filename for a in parsed.attachments]
        + [t for a in parsed.attachments if (t := attachment_text(a))]
    )
    hits: dict[str, Project] = {}
    for p in projects:
        if not p.company_id:
            continue
        company = session.get(Company, p.company_id)
        terms = {p.name, p.project_id}
        if company is not None and company.status == "active":
            terms |= {company.name, company.company_id}
        terms |= {r.value for r in rules if r.project_id == p.project_id and r.rule_type == "keyword"}
        terms |= set(
            session.scalars(select(CompanyDomain.domain).where(CompanyDomain.company_id == p.company_id))
        )
        if any(len(t) >= _MIN_NAME and _keyword_in(t, haystack) for t in terms if t):
            hits.setdefault(p.company_id, p)
    return list(hits.values())


def _queue_pending_companies(session: Session, domains: tuple[str, ...]) -> list[str]:
    """Create a pending company per unknown domain (company_id = the domain)."""
    created = []
    for domain in domains:
        if session.get(CompanyDomain, domain) is not None:
            continue
        if session.get(Company, domain) is None:
            session.add(Company(company_id=domain, name=domain, status="pending"))
            session.flush()
        session.add(CompanyDomain(domain=domain, company_id=domain))
        created.append(domain)
    session.flush()
    return created


def route(session: Session, parsed: ParsedEmail, thread_id: str, settings: Settings) -> RouteDecision:
    projects = list(
        session.scalars(select(Project).where(Project.status == "active").order_by(Project.project_id))
    )
    by_id = {p.project_id: p for p in projects}
    rules = list(session.scalars(select(ProjectRule).where(ProjectRule.project_id.in_(by_id))))

    # 1. Thread inheritance
    pinned = [
        by_id[pid]
        for pid in session.scalars(
            select(ThreadAssignment.project_id).where(ThreadAssignment.thread_id == thread_id)
        )
        if pid in by_id
    ]
    if len(pinned) == 1:
        return _assigned(pinned[0], "thread", f"thread {thread_id} belongs to {pinned[0].project_id}")
    if len(pinned) > 1:
        return _review("thread is pinned to several projects", pinned)

    # 2. Explicit signals
    hits: dict[str, tuple[Project, str, str]] = {}
    for p in projects:
        if p.label and p.label in parsed.labels:
            hits.setdefault(p.project_id, (p, "label", f"Gmail label {p.label}"))
        for r in rules:
            if r.project_id == p.project_id and r.rule_type == "keyword" and _keyword_in(r.value, parsed.subject):
                hits.setdefault(p.project_id, (p, "keyword", f"subject mentions {r.value!r}"))
        if p.drive_folder_id and p.drive_folder_id in parsed.body_text:
            hits.setdefault(p.project_id, (p, "drive_link", "links the project's Drive folder"))
    if len(hits) == 1:
        p, step, why = next(iter(hits.values()))
        return _assigned(p, step, why)
    if len(hits) > 1:
        return _review("explicit signals point to several projects", [h[0] for h in hits.values()])

    # 2b. Meeting-bot summaries
    sender_host = (parsed.sender or "").rpartition("@")[2]
    if sender_host and registrable_domain(sender_host) in set(settings.routing.meeting_bot_domains):
        named = _companies_named(session, parsed, projects, rules)
        if len(named) == 1:
            p = named[0]
            return _assigned(p, "meeting_bot", f"meeting summary names {p.company_id}")
        if len(named) > 1:
            return _review("meeting summary names several companies", named)

    # 3. Company match
    resolution = resolve_company(parsed, Registry.load(session), settings)
    if resolution.status == "review":
        created = _queue_pending_companies(session, resolution.new_domains)
        pending = [*resolution.pending_companies, *created]
        tied = [p for p in projects if p.company_id in resolution.tied_companies]
        return _review(
            resolution.reason, tied, review_kind=resolution.review_kind, pending_companies=pending
        )
    if resolution.status == "company":
        cid = resolution.company_id
        candidates = [p for p in projects if p.company_id == cid]
        if len(candidates) == 1:
            return _assigned(candidates[0], "company", f"{cid} has one active project")
        if not candidates:
            return RouteDecision(
                "review", "review", company_id=cid, reason=f"company {cid} has no active projects"
            )
    else:
        candidates = projects  # internal-only or no company signal

    # 4. Participants
    addresses = [a for a in [parsed.sender, *parsed.reply_to, *parsed.to, *parsed.cc] if a]
    unique: dict[str, str] = {}
    for address in addresses:
        matched = {
            r.project_id
            for r in rules
            if r.rule_type == "participant"
            and r.project_id in {c.project_id for c in candidates}
            and _participant_matches(r.value, address)
        }
        if len(matched) == 1:
            unique.setdefault(matched.pop(), address)
    if len(unique) == 1:
        pid, address = next(iter(unique.items()))
        return _assigned(by_id[pid], "participant", f"{address} works only on {pid}")
    if len(unique) > 1:
        return _review("participants point to several projects", [by_id[p] for p in unique])

    # 7. Review queue (no classifier: rules only)
    if not projects:
        return RouteDecision("unassigned", "review", reason="no active projects registered")
    decision = _review("no routing rule matched", candidates)
    decision.company_id = resolution.company_id
    return decision


def log_decision(session: Session, msg_id: str, decision: RouteDecision) -> None:
    session.add(
        RoutingLog(
            msg_id=msg_id,
            company_id=decision.company_id,
            project_id=decision.project_id,
            step=decision.step,
            confidence=decision.confidence,
            timestamp=utcnow(),
        )
    )


def pin_thread(session: Session, thread_id: str, project_id: str) -> None:
    if session.get(ThreadAssignment, (thread_id, project_id)) is None:
        session.add(ThreadAssignment(thread_id=thread_id, project_id=project_id))
