"""SQLite schema. Mirrors the Data model section of docs/DESIGN.md.

Email content is never stored. Emails are cleaned, gated, extracted and validated in
memory; the DB keeps only what idempotency, threading and citations need:
- `messages`: msg_id, gmail_id, thread_id, subject, date. No body, no addresses.
- `thread_paragraphs`: SHA-256 of each cleaned paragraph, so later replies in the
  thread can drop repeated (quoted) paragraphs without the text being kept.
- `change_log`: what changed in the requirement and which email caused it. The verbatim
  evidence quote is checked against the email in memory and then discarded.

All datetimes are naive UTC.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# --- Company / project registry ------------------------------------------------


class Company(Base):
    __tablename__ = "companies"
    company_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="active")  # active | pending


class CompanyDomain(Base):
    __tablename__ = "company_domains"
    domain: Mapped[str] = mapped_column(String, primary_key=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.company_id"))


class CompanyContact(Base):
    __tablename__ = "company_contacts"
    email: Mapped[str] = mapped_column(String, primary_key=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.company_id"))


class OwnDomain(Base):
    __tablename__ = "own_domains"
    domain: Mapped[str] = mapped_column(String, primary_key=True)


class Project(Base):
    __tablename__ = "projects"
    project_id: Mapped[str] = mapped_column(String, primary_key=True)
    company_id: Mapped[str | None] = mapped_column(
        ForeignKey("companies.company_id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    label: Mapped[str | None] = mapped_column(String, nullable=True)
    drive_folder_id: Mapped[str | None] = mapped_column(String, nullable=True)
    req_summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class ProjectRule(Base):
    __tablename__ = "project_rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"))
    rule_type: Mapped[str] = mapped_column(String)  # participant | keyword | thread
    value: Mapped[str] = mapped_column(String)
    __table_args__ = (UniqueConstraint("project_id", "rule_type", "value"),)


class ThreadAssignment(Base):
    __tablename__ = "thread_assignments"
    thread_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.project_id"), primary_key=True
    )


class RoutingLog(Base):
    __tablename__ = "routing_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    msg_id: Mapped[str] = mapped_column(String, index=True)
    company_id: Mapped[str | None] = mapped_column(String, nullable=True)
    project_id: Mapped[str | None] = mapped_column(String, nullable=True)
    step: Mapped[str] = mapped_column(String)  # thread|label|participant|embedding|llm|manual
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime)


class ReviewQueue(Base):
    __tablename__ = "review_queue"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    msg_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String)  # project | new_company
    candidates: Mapped[list | dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String, default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime)


# --- Messages and segments ------------------------------------------------------


class Message(Base):
    """Metadata only. The email body is never stored."""

    __tablename__ = "messages"
    # RFC Message-ID without angle brackets, or "sha256:<hex>" when the header is missing.
    msg_id: Mapped[str] = mapped_column(String, primary_key=True)
    gmail_id: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    thread_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)  # for citations
    date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source: Mapped[str] = mapped_column(String)  # eml | gmail
    ingested_at: Mapped[datetime] = mapped_column(DateTime)


class ThreadParagraph(Base):
    """Fingerprint of a cleaned paragraph seen in a thread (hash only, never text)."""

    __tablename__ = "thread_paragraphs"
    thread_id: Mapped[str] = mapped_column(String, primary_key=True)
    para_hash: Mapped[str] = mapped_column(String, primary_key=True)
    first_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # email date


class Segment(Base):
    """Which project a message was routed to. The segment text lives in memory only."""

    __tablename__ = "segments"
    segment_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    msg_id: Mapped[str] = mapped_column(ForeignKey("messages.msg_id"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"))
    __table_args__ = (UniqueConstraint("msg_id", "project_id"),)


# --- Requirements store ---------------------------------------------------------


class Requirement(Base):
    __tablename__ = "requirements"
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.project_id"), primary_key=True
    )
    req_id: Mapped[str] = mapped_column(String, primary_key=True)  # REQ-001
    seq: Mapped[int] = mapped_column(Integer)  # numeric part, for sorting
    text: Mapped[str] = mapped_column(Text)
    normalized_text: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="active")  # active | removed
    version: Mapped[int] = mapped_column(Integer, default=1)
    source_ids: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (UniqueConstraint("project_id", "seq"),)


class ChangeLog(Base):
    __tablename__ = "change_log"
    change_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), index=True)
    req_id: Mapped[str] = mapped_column(String)
    op: Mapped[str] = mapped_column(String)  # add | modify | remove
    old_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_id: Mapped[str] = mapped_column(String)  # msg_id
    timestamp: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # source date
    applied_at: Mapped[datetime] = mapped_column(DateTime)  # wall clock, never rendered


# --- Idempotency and sync state -------------------------------------------------


class ProcessedMessage(Base):
    __tablename__ = "processed_messages"
    msg_id: Mapped[str] = mapped_column(String, primary_key=True)
    outcome: Mapped[str] = mapped_column(String)
    processed_at: Mapped[datetime] = mapped_column(DateTime)


class DriveRevision(Base):
    __tablename__ = "drive_revisions"
    file_id: Mapped[str] = mapped_column(String, primary_key=True)
    revision_id: Mapped[str] = mapped_column(String)
    text_hash: Mapped[str] = mapped_column(String)


class SyncState(Base):
    __tablename__ = "sync_state"
    account: Mapped[str] = mapped_column(String, primary_key=True)
    history_id: Mapped[str | None] = mapped_column(String, nullable=True)
    drive_page_token: Mapped[str | None] = mapped_column(String, nullable=True)
    last_run: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
