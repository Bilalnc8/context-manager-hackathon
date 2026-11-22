"""Build a company's first <Company>.md from recent mail.

When a company's Drive folder has no .md yet, Gmail is searched for the last
`bootstrap.days` days (default 100) of mail related to that company:

- from, to or cc any of its domains or its known contacts,
- its project's subject keywords,
- meeting-bot emails (fireflies.ai, otter.ai, ...) that mention its name.

Those emails go through the normal pipeline oldest first (so "latest wins" holds and
every requirement is evidence-checked and cited), their copies and attachments land in
the company's folder, and the .md is created even if no requirement was found yet.
Emails that turn out to belong to another company are left for that company.
Already-processed emails are skipped without being downloaded, so re-running is cheap.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from emailctx.config import Settings
from emailctx.ingest import IngestResult, Runtime, ingest_eml
from emailctx.render.publish import PublishResult
from emailctx.render.render import render_and_publish
from emailctx.store.models import (
    Company,
    CompanyContact,
    CompanyDomain,
    Message,
    ProcessedMessage,
    Project,
    ProjectRule,
)

SKIP_LABELS = {"DRAFT", "SPAM", "TRASH"}


def _quote(term: str) -> str:
    return '"' + term.replace('"', "") + '"'


def _company_terms(session: Session, company_id: str, settings: Settings) -> list[str]:
    """Gmail search clauses that pick out one company's mail."""
    company = session.get(Company, company_id)
    if company is None or company.status != "active":
        return []
    domains = list(session.scalars(select(CompanyDomain.domain).where(CompanyDomain.company_id == company_id)))
    contacts = list(session.scalars(select(CompanyContact.email).where(CompanyContact.company_id == company_id)))
    projects = list(session.scalars(select(Project).where(Project.company_id == company_id)))
    keywords = list(
        session.scalars(
            select(ProjectRule.value).where(
                ProjectRule.project_id.in_([p.project_id for p in projects]),
                ProjectRule.rule_type == "keyword",
            )
        )
    )
    parts = [f"{field}:{a}" for a in [*domains, *contacts] for field in ("from", "to", "cc")]
    parts += [f"subject:{_quote(k)}" for k in keywords]
    bots = settings.routing.meeting_bot_domains
    names = {company.name, *(p.name for p in projects)} - {""}
    if bots and names:
        parts.append(
            f"(from:({' OR '.join(bots)}) ({' OR '.join(_quote(n) for n in sorted(names))}))"
        )
    return parts


def company_query(session: Session, company_id: str, settings: Settings) -> str | None:
    parts = _company_terms(session, company_id, settings)
    if not parts:
        return None
    return f"newer_than:{settings.bootstrap.days}d ({' OR '.join(parts)})"


def auto_query(session: Session, settings: Settings) -> str | None:
    """The incremental-sync query when gmail.query is null: every registered company."""
    company_ids = session.scalars(
        select(Project.company_id).where(Project.status == "active", Project.company_id.is_not(None))
    )
    parts: list[str] = []
    for cid in sorted(set(company_ids)):
        parts += [p for p in _company_terms(session, cid, settings) if p not in parts]
    return f"({' OR '.join(parts)})" if parts else None


def companies_missing_context(session: Session, has_context: Callable[[str], bool]) -> list[str]:
    """Active companies with a project whose .md doesn't exist yet."""
    company_ids = sorted(
        set(
            session.scalars(
                select(Project.company_id)
                .join(Company, Company.company_id == Project.company_id)
                .where(Project.status == "active", Company.status == "active")
            )
        )
    )
    return [c for c in company_ids if not has_context(c)]


@dataclass
class BootstrapReport:
    company_id: str
    query: str | None = None
    candidates: int = 0
    known: int = 0  # already processed: not downloaded
    other_company: int = 0  # belonged to another company: left for its run
    skipped_labels: int = 0
    results: list[tuple[str, IngestResult]] = field(default_factory=list)
    published: list[PublishResult] = field(default_factory=list)


def bootstrap_company(
    service: Any,
    rt: Runtime,
    company_id: str,
    *,
    on_result: Callable[[str, IngestResult], None] | None = None,
) -> BootstrapReport:
    """Process the company's recent mail oldest first, then publish its .md."""
    from emailctx.sources.gmail_sync import fetch_raw, list_ids

    with rt.sessions() as s:
        query = company_query(s, company_id, rt.settings)
    report = BootstrapReport(company_id, query)

    if query is not None:
        ids = list_ids(service, query, rt.settings.bootstrap.max_per_company)
        report.candidates = len(ids)
        labels = None
        for gmail_id in ids:
            with rt.sessions() as s:
                known = s.scalar(select(Message).where(Message.gmail_id == gmail_id))
                if known is not None and s.get(ProcessedMessage, known.msg_id) is not None:
                    report.known += 1
                    continue
            raw, meta = fetch_raw(service, gmail_id)
            if SKIP_LABELS & set(meta.get("labelIds", [])):
                report.skipped_labels += 1
                continue
            if labels is None:
                labels = {
                    l["id"]: l.get("name", l["id"])
                    for l in service.users().labels().list(userId="me").execute(num_retries=3).get("labels", [])
                }
            result = ingest_eml(
                raw,
                source="gmail",
                source_ref=f"gmail:{gmail_id}",
                gmail_id=gmail_id,
                labels=[labels.get(l, l) for l in meta.get("labelIds", [])],
                settings=rt.settings,
                sessions=rt.sessions,
                llm=rt.llm,
                embedder=rt.embedder,
                publishers=rt.publishers,
                drive=rt.drive,
                only_companies={company_id},
            )
            if result.outcome == "other_company":
                report.other_company += 1
            report.results.append((gmail_id, result))
            if on_result:
                on_result(gmail_id, result)

    # Create (or refresh) the .md even when nothing was found: the folder now has one.
    with rt.sessions() as s:
        report.published = render_and_publish(
            s, company_id, rt.settings, rt.targets(),
            message=f"{company_id}: built from the last {rt.settings.bootstrap.days} days of mail",
        )
    return report
