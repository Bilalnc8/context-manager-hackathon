"""Resolve which company an email is about (DESIGN.md, "Resolving the company from an email").

1. Collect From, Reply-To, To and Cc addresses (never display names).
2. Drop our own organization's domains.
3. Normalize each domain to its registrable domain with the Public Suffix List:
   mail.eu.acme.co.uk -> acme.co.uk. Uses tldextract's bundled list, no network.
4. Personal providers (gmail.com, ...) say nothing about the company; such addresses
   match only through company_contacts on the full address.
5. An external sender's company wins. If we sent the email, use the recipients' companies.
6. Several recipient companies: the one with active projects wins; still tied -> review.
7. Unknown domain: reported as a new domain so the caller can queue a pending company.
8. Optional: trust an external sender only when Authentication-Results says dmarc=pass.

This module only reads the registry; the cascade creates pending companies.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

import tldextract
from sqlalchemy import select
from sqlalchemy.orm import Session

from emailctx.clean import ParsedEmail
from emailctx.config import Settings
from emailctx.store.models import Company, CompanyContact, CompanyDomain, OwnDomain, Project

# Bundled Public Suffix List snapshot: deterministic and works offline.
_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


def registrable_domain(host: str) -> str:
    host = host.strip().lower().rstrip(".")
    return _EXTRACT(host).top_domain_under_public_suffix or host


AddressKind = Literal["own", "company", "pending", "ignored", "personal", "bot", "unknown"]


@dataclass(frozen=True)
class AddressInfo:
    address: str
    domain: str  # registrable domain
    kind: AddressKind
    company_id: str | None = None

    @property
    def external_signal(self) -> bool:
        """Says something about which external company this is."""
        return self.kind in ("company", "pending", "unknown")


@dataclass(frozen=True)
class CompanyResolution:
    status: Literal["company", "none", "review"]
    rule: str  # sender | recipients | active_projects | internal_only | tie | pending | unknown | dmarc
    company_id: str | None = None
    reason: str = ""
    review_kind: Literal["project", "new_company"] = "project"
    tied_companies: tuple[str, ...] = ()
    pending_companies: tuple[str, ...] = ()  # already-pending companies involved
    new_domains: tuple[str, ...] = ()  # unknown domains to queue as pending companies


@dataclass
class Registry:
    own_domains: set[str] = field(default_factory=set)
    domains: dict[str, str] = field(default_factory=dict)  # registrable domain -> company_id
    contacts: dict[str, str] = field(default_factory=dict)  # address -> company_id
    status: dict[str, str] = field(default_factory=dict)  # company_id -> status
    active_projects: Counter = field(default_factory=Counter)  # company_id -> count

    @classmethod
    def load(cls, session: Session) -> Registry:
        reg = cls()
        reg.own_domains = {registrable_domain(d) for d in session.scalars(select(OwnDomain.domain))}
        reg.domains = {d.domain: d.company_id for d in session.scalars(select(CompanyDomain))}
        reg.contacts = {c.email.lower(): c.company_id for c in session.scalars(select(CompanyContact))}
        reg.status = {c.company_id: c.status for c in session.scalars(select(Company))}
        for p in session.scalars(select(Project).where(Project.status == "active")):
            if p.company_id:
                reg.active_projects[p.company_id] += 1
        return reg


def classify(
    address: str,
    reg: Registry,
    personal_providers: Iterable[str],
    bot_domains: Iterable[str] = (),
) -> AddressInfo | None:
    address = address.strip().lower()
    _, at, host = address.rpartition("@")
    if not at or not host:
        return None
    domain = registrable_domain(host)
    if domain in reg.own_domains:
        return AddressInfo(address, domain, "own")
    if domain in set(bot_domains):
        return AddressInfo(address, domain, "bot")  # meeting bot: no company signal
    personal = set(personal_providers)
    if host in personal or domain in personal:
        cid = reg.contacts.get(address)
        if cid is None:
            return AddressInfo(address, domain, "personal")
        return AddressInfo(address, domain, _kind(cid, reg), cid)
    cid = reg.domains.get(domain)
    if cid is None:
        return AddressInfo(address, domain, "unknown")
    return AddressInfo(address, domain, _kind(cid, reg), cid)


def _kind(company_id: str, reg: Registry) -> AddressKind:
    status = reg.status.get(company_id, "active")
    return {"active": "company", "pending": "pending", "ignored": "ignored"}.get(status, "company")


def _unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def resolve_company(parsed: ParsedEmail, reg: Registry, settings: Settings) -> CompanyResolution:
    providers = settings.routing.personal_providers
    bots = settings.routing.meeting_bot_domains

    def infos(addrs: Iterable[str]) -> list[AddressInfo]:
        return [i for a in addrs if (i := classify(a, reg, providers, bots)) is not None]

    from_info = infos([parsed.sender] if parsed.sender else [])
    we_sent = bool(from_info) and from_info[0].kind == "own"

    # Rule 5: an external sender's company wins (Reply-To counts as the sender's side).
    if not we_sent:
        sender = next((i for i in from_info + infos(parsed.reply_to) if i.external_signal), None)
        if sender is not None:
            if settings.routing.require_dmarc_pass and parsed.dmarc_pass is not True:
                return CompanyResolution(
                    "review", "dmarc",
                    reason=f"sender domain {sender.domain} is not DMARC-authenticated",
                )
            if sender.kind == "company":
                return CompanyResolution("company", "sender", company_id=sender.company_id)
            if sender.kind == "pending":
                return CompanyResolution(
                    "review", "pending", review_kind="new_company",
                    reason=f"sender's company {sender.company_id} awaits approval",
                    pending_companies=(sender.company_id,),
                )
            return CompanyResolution(
                "review", "unknown", review_kind="new_company",
                reason=f"unknown sender domain {sender.domain}",
                new_domains=(sender.domain,),
            )

    # We sent it, or the sender says nothing (personal provider, ignored): use recipients.
    recipients = infos([*parsed.to, *parsed.cc])
    companies = _unique(i.company_id for i in recipients if i.kind == "company")
    if len(companies) == 1:
        return CompanyResolution("company", "recipients", company_id=companies[0])
    if len(companies) > 1:
        with_projects = [c for c in companies if reg.active_projects[c] > 0]
        if len(with_projects) == 1:
            return CompanyResolution("company", "active_projects", company_id=with_projects[0])
        tied = tuple(with_projects or companies)
        return CompanyResolution(
            "review", "tie", reason=f"several companies on the email: {', '.join(tied)}",
            tied_companies=tied,
        )

    pending = tuple(_unique(i.company_id for i in recipients if i.kind == "pending"))
    new = tuple(_unique(i.domain for i in recipients if i.kind == "unknown"))
    if pending or new:
        parts = [f"pending company {c}" for c in pending] + [f"unknown domain {d}" for d in new]
        return CompanyResolution(
            "review", "pending" if pending else "unknown", review_kind="new_company",
            reason=", ".join(parts), pending_companies=pending, new_domains=new,
        )
    return CompanyResolution("none", "internal_only")
