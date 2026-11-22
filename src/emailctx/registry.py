"""Load the company/project registry from a YAML file (idempotent upsert).

    own_domains: [ourstudio.io]
    companies:
      - id: acme
        name: Acme Corp
        domains: [acme.com, mail.acme.co.uk]     # normalized to registrable domains
        contacts: [jane.acme@gmail.com]          # people on personal providers
        projects:                                # exactly one project per company
          - id: atlas
            name: Atlas
            description: Reporting portal
            label: Clients/Atlas                 # Gmail label (explicit signal)
            keywords: [Atlas, PO-4471]           # in the subject (explicit signal)
            participants: [jane@acme.com]        # address or domain
            drive_folder_id: 1AbC...
            status: active                       # or archived

Loading never deletes rules learned from review assignments. A domain that was queued
as a pending company is moved to the company that now lists it.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from emailctx.routing.company import registrable_domain
from emailctx.store.models import (
    Company,
    CompanyContact,
    CompanyDomain,
    OwnDomain,
    Project,
    ProjectRule,
)


class ProjectSpec(BaseModel):
    id: str
    name: str | None = None
    description: str | None = None
    label: str | None = None
    keywords: list[str] = Field(default_factory=list)
    participants: list[str] = Field(default_factory=list)
    drive_folder_id: str | None = None
    status: str = "active"


class CompanySpec(BaseModel):
    id: str
    name: str | None = None
    domains: list[str] = Field(default_factory=list)
    contacts: list[str] = Field(default_factory=list)
    projects: list[ProjectSpec] = Field(default_factory=list)  # at most one

    @field_validator("projects")
    @classmethod
    def _one_project(cls, projects: list[ProjectSpec]) -> list[ProjectSpec]:
        if len(projects) > 1:
            raise ValueError("each company has exactly one project")
        return projects


class RegistrySpec(BaseModel):
    own_domains: list[str] = Field(default_factory=list)
    companies: list[CompanySpec] = Field(default_factory=list)


def read_registry(path: Path) -> RegistrySpec:
    return RegistrySpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def add_rule(session: Session, project_id: str, rule_type: str, value: str) -> bool:
    exists = session.scalar(
        select(ProjectRule).where(
            ProjectRule.project_id == project_id,
            ProjectRule.rule_type == rule_type,
            ProjectRule.value == value,
        )
    )
    if exists is None:
        session.add(ProjectRule(project_id=project_id, rule_type=rule_type, value=value))
        return True
    return False


def _drop_if_empty_pending(session: Session, company_id: str) -> None:
    company = session.get(Company, company_id)
    if company is None or company.status != "pending":
        return
    has_any = (
        session.scalar(select(CompanyDomain).where(CompanyDomain.company_id == company_id))
        or session.scalar(select(CompanyContact).where(CompanyContact.company_id == company_id))
        or session.scalar(select(Project).where(Project.company_id == company_id))
    )
    if not has_any:
        session.delete(company)


def load_registry(session: Session, spec: RegistrySpec) -> dict[str, int]:
    counts = {"own_domains": 0, "companies": 0, "domains": 0, "contacts": 0, "projects": 0, "rules": 0}

    for d in spec.own_domains:
        domain = registrable_domain(d)
        if session.get(OwnDomain, domain) is None:
            session.add(OwnDomain(domain=domain))
            counts["own_domains"] += 1

    for c in spec.companies:
        company = session.get(Company, c.id)
        if company is None:
            company = Company(company_id=c.id, name=c.name or c.id, status="active")
            session.add(company)
            counts["companies"] += 1
        else:
            company.name = c.name or company.name
            company.status = "active"
        session.flush()

        for d in c.domains:
            domain = registrable_domain(d)
            row = session.get(CompanyDomain, domain)
            if row is None:
                session.add(CompanyDomain(domain=domain, company_id=c.id))
                counts["domains"] += 1
            elif row.company_id != c.id:
                previous = row.company_id
                row.company_id = c.id
                session.flush()
                _drop_if_empty_pending(session, previous)
                counts["domains"] += 1

        for email in c.contacts:
            email = email.strip().lower()
            row = session.get(CompanyContact, email)
            if row is None:
                session.add(CompanyContact(email=email, company_id=c.id))
                counts["contacts"] += 1
            elif row.company_id != c.id:
                row.company_id = c.id
                counts["contacts"] += 1

        for p in c.projects:
            other = session.scalar(
                select(Project).where(Project.company_id == c.id, Project.project_id != p.id)
            )
            if other is not None:
                raise ValueError(
                    f"company {c.id!r} already has project {other.project_id!r}; "
                    "each company has exactly one project"
                )
            project = session.get(Project, p.id)
            if project is None:
                project = Project(project_id=p.id, name=p.name or p.id, company_id=c.id)
                session.add(project)
                counts["projects"] += 1
            elif project.company_id not in (None, c.id):
                raise ValueError(
                    f"project {p.id!r} belongs to company {project.company_id!r}, not {c.id!r}"
                )
            project.company_id = c.id
            project.name = p.name or project.name
            project.status = p.status
            project.description = p.description if p.description is not None else project.description
            project.label = p.label if p.label is not None else project.label
            project.drive_folder_id = (
                p.drive_folder_id if p.drive_folder_id is not None else project.drive_folder_id
            )
            session.flush()
            for kw in p.keywords:
                counts["rules"] += add_rule(session, p.id, "keyword", kw)
            for who in p.participants:
                counts["rules"] += add_rule(session, p.id, "participant", who.strip().lower())

    session.flush()
    return counts
