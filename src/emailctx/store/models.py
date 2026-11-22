"""SQLite schema. Mirrors the Data model section of docs/DESIGN.md.

The database is the single source of truth, including the text of every client input:
- `messages`: msg_id, gmail_id, thread_id, subject, date, attachment file names, the
  sender, and `body`: the text the pipeline read (cleaned: quoted replies and
  signatures removed, attachment text appended). Every citation can be opened from it,
  including input pasted into a Claude session, which exists nowhere else.
- `thread_paragraphs`: SHA-256 of each cleaned paragraph, so later replies in the
  thread can drop repeated (quoted) paragraphs.
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
    # active | pending (unknown domain awaiting approval) | ignored (dismissed: no signal)
    status: Mapped[str] = mapped_column(String, default="active")


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
    status: Mapped[str] = mapped_column(String, default="active")  # active | archived
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
    """An email waiting for a person to pick its project.

    The email itself is not stored: `source_ref` says where to read it again on assign
    ("eml:<absolute path>" now, "gmail:<id>" once Gmail sync exists).
    """

    __tablename__ = "review_queue"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    msg_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String)  # project | new_company
    # {"projects": [project_id, ...], "pending_companies": [company_id, ...]}
    candidates: Mapped[list | dict | None] = mapped_column(JSON, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="open")  # open | assigned | dismissed
    resolved_project_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# --- Messages and segments ------------------------------------------------------


class Message(Base):
    """An email or other client input, with the cleaned text the pipeline read."""

    __tablename__ = "messages"
    # RFC Message-ID without angle brackets, or "sha256:<hex>" when the header is missing.
    msg_id: Mapped[str] = mapped_column(String, primary_key=True)
    gmail_id: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    thread_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)  # for citations
    date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attachments: Mapped[list | None] = mapped_column(JSON, nullable=True)  # file names only
    sender: Mapped[str | None] = mapped_column(String, nullable=True)  # From header
    body: Mapped[str | None] = mapped_column(Text, nullable=True)  # cleaned text + attachments
    source: Mapped[str] = mapped_column(String)  # eml | gmail | pasted
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
    # Delivery progress, set by the team (coding agent, QA), never by the email pipeline,
    # except that a client change to finished work reopens it.
    delivery: Mapped[str] = mapped_column(String, default="open")  # see DELIVERY_STATES
    delivery_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (UniqueConstraint("project_id", "seq"),)


DELIVERY_STATES = ("open", "in_progress", "done", "verified")
FINISHED_STATES = ("done", "verified")


class StatusChange(Base):
    """Every delivery status change: who set it (a person, an agent, or an email that
    reopened finished work) and why."""

    __tablename__ = "status_changes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), index=True)
    req_id: Mapped[str] = mapped_column(String)
    old_status: Mapped[str] = mapped_column(String)
    new_status: Mapped[str] = mapped_column(String)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_by: Mapped[str] = mapped_column(String)  # user email, "agent", or "email <msg_id>"
    changed_at: Mapped[datetime] = mapped_column(DateTime)


QUESTION_STATES = ("open", "asked", "answered", "withdrawn")


class Question(Base):
    """A question only the client can answer.

    open: logged, not sent yet. asked: sent to the client (asked once, never again).
    answered: the client's answer, from an email (answer_source_id = its msg_id) or
    recorded by a person (answer_source_id None). withdrawn: no longer needed.
    """

    __tablename__ = "questions"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), primary_key=True)
    question_id: Mapped[str] = mapped_column(String, primary_key=True)  # Q-001
    seq: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    normalized_text: Mapped[str] = mapped_column(Text)
    req_id: Mapped[str | None] = mapped_column(String, nullable=True)  # the item it blocks
    status: Mapped[str] = mapped_column(String, default="open")
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    asked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer_source_id: Mapped[str | None] = mapped_column(String, nullable=True)
    answered_by: Mapped[str | None] = mapped_column(String, nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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


class Conflict(Base):
    """A requirement change a person should confirm. Rendered under "Needs confirmation".

    kind:
    - contradiction: a newer email contradicted the requirement. Latest wins, so
      kept_text is the new text and other_text the one it replaced.
    - stale: an email older than the requirement's last change tried to change it.
      Latest wins, so nothing was applied; other_text is what the older email said.
    """

    __tablename__ = "conflicts"
    conflict_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), index=True)
    req_id: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)  # contradiction | stale
    kept_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    other_text: Mapped[str | None] = mapped_column(Text, nullable=True)  # None: a removal
    kept_source_id: Mapped[str | None] = mapped_column(String, nullable=True)
    other_source_id: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="open")  # open | confirmed
    created_at: Mapped[datetime] = mapped_column(DateTime)  # wall clock, never rendered


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


# --- MCP server login (emailctx serve --auth google) ----------------------------
# Claude registers itself, the user signs in with Google, and emailctx issues its own
# tokens. Codes and tokens are stored as SHA-256 hashes, never in the clear. Times are
# Unix seconds, as the MCP SDK compares them with time.time().

class OAuthClient(Base):
    """A Claude client that registered itself (dynamic client registration)."""

    __tablename__ = "oauth_clients"
    client_id: Mapped[str] = mapped_column(String, primary_key=True)
    info_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float)


class OAuthPending(Base):
    """An /authorize request waiting for the user to finish Google sign-in."""

    __tablename__ = "oauth_pending"
    key_hash: Mapped[str] = mapped_column(String, primary_key=True)  # Google's `state`
    client_id: Mapped[str] = mapped_column(String)
    params_json: Mapped[str] = mapped_column(Text)  # Claude's redirect URI, PKCE challenge, state
    google_verifier: Mapped[str] = mapped_column(String)  # our PKCE verifier toward Google
    expires_at: Mapped[float] = mapped_column(Float)


class OAuthCode(Base):
    """A one-time emailctx authorization code, handed to Claude after sign-in."""

    __tablename__ = "oauth_codes"
    code_hash: Mapped[str] = mapped_column(String, primary_key=True)
    client_id: Mapped[str] = mapped_column(String)
    data_json: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[float] = mapped_column(Float)


class OAuthToken(Base):
    """An access or refresh token. Both tokens from one grant share a `family`, so
    revoking or rotating one removes the other."""

    __tablename__ = "oauth_tokens"
    token_hash: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String)  # access | refresh
    family: Mapped[str] = mapped_column(String, index=True)
    client_id: Mapped[str] = mapped_column(String)
    subject: Mapped[str] = mapped_column(String)  # the user's verified Google email
    scopes: Mapped[str] = mapped_column(String, default="")  # space-separated
    resource: Mapped[str | None] = mapped_column(String, nullable=True)
    expires_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
