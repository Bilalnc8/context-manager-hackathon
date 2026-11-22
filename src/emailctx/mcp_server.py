"""MCP server: any Claude session (Claude Code, Claude Desktop, claude.ai) can see where a
client project stands and keep it up to date.

It works on the central requirements store (the same SQLite database the email pipeline
writes), so every agent and every teammate sees the same state:

- read:  list_companies, get_company_context, list_items, list_questions, get_source
- write: set_item_status (open / in_progress / done / verified), log_question,
         mark_questions_asked, record_answer, withdraw_question, add_client_input
         (a client email or meeting notes pasted into the session)

Every write takes the runtime's write lock and re-renders the company's <Company>.md
wherever output is configured, so many users can write at once: writes queue, reads
never wait (SQLite WAL), and the published file always reflects the latest commit.

Run it over HTTP for a team (`emailctx serve`; connect with
`claude mcp add --transport http emailctx http://localhost:8000/mcp`) or over stdio
for one machine (`emailctx mcp`).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select

from emailctx import tracker
from emailctx.ingest import Runtime
from emailctx.render.render import render_and_publish, render_company
from emailctx.store.models import CompanyDomain, DELIVERY_STATES, Question
from emailctx.tracker import TrackerError

INSTRUCTIONS = """\
Client project tracker: what each client company asked for (deliverables extracted \
from their emails and meeting notes, each cited to its source) with the delivery status \
of each (open, in_progress, done, verified); where the project stands (key dates and \
their approval, open client concerns and blockers, next actions with owners, decisions, \
out-of-scope work); and the questions only the client can answer.

How to use it:
- At the start of work on a client project, call get_company_context for that company \
and treat it as the source of truth for what is done, open and verified.
- When you finish an item, call set_item_status with done (QA sets verified). Use \
in_progress when you start one. A concern or action set to done counts as resolved.
- When you need information only the client can give, call list_questions first: it \
may already be answered or already asked. Only then call log_question. Never ask the \
client something that is already answered.
- To check what the client actually said behind an item, call get_source with the footnote number.
- When the user pastes a client email or meeting notes, call add_client_input so new \
requirements and answers are captured.
- To send questions to the client, draft them from list_questions (open ones), and \
after the user sends them call mark_questions_asked."""


def current_user_default() -> str:
    """Who is calling: the signed-in user when the server has auth, else "agent"."""
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token

        token = get_access_token()
    except Exception:  # pragma: no cover - auth not configured
        token = None
    if token is None:
        return "agent"
    return getattr(token, "subject", None) or token.client_id or "agent"


def _fmt_status(s: str) -> str:
    return s.replace("_", " ")


def _fmt_question(q: Question) -> str:
    about = f" (about {q.req_id})" if q.req_id else ""
    line = f"- {q.question_id}{about}: {q.text}"
    if q.status == "open":
        return line + "  [not asked yet]"
    if q.status == "asked":
        asked = q.asked_at.strftime("%Y-%m-%d") if q.asked_at else "?"
        return line + f"  [asked {asked}, waiting for the client]"
    if q.status == "answered":
        source = "from the client's email" if q.answer_source_id else f"recorded by {q.answered_by}"
        return line + f"\n  Answer ({source}): {q.answer}"
    return line + f"  [{q.status}]"


def coverage_lines(result) -> list[str]:
    """What happened to each sentence that produced no change, so nothing is missed
    silently."""
    lines = []
    if result.skipped:
        lines.append("Read and deliberately skipped:")
        lines += [f'- "{s.quote}" ({s.reason})' for s in result.skipped]
    if result.unaccounted:
        lines.append(
            "NOT captured and no reason given; check these and add them by hand "
            "(set_item_status / log_question) or paste them again:"
        )
        lines += [f'- "{s}"' for s in result.unaccounted]
    elif result.outcome not in ("gated_out", "empty", "review", "unassigned", "already_processed"):
        lines.append("Every sentence was captured or skipped with a reason.")
    return lines


def build_server(
    runtime_factory: Callable[[], Runtime],
    *,
    current_user: Callable[[], str] = current_user_default,
    **server_kwargs,
):
    """runtime_factory is called once, on the first tool call, so the server starts even
    before the database or the API key is ready."""
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("emailctx", instructions=INSTRUCTIONS, **server_kwargs)
    cached: dict[str, Runtime] = {}

    def rt() -> Runtime:
        if "rt" not in cached:
            cached["rt"] = runtime_factory()
        return cached["rt"]

    def publish(session, company_id: str, message: str) -> None:
        r = rt()
        render_and_publish(session, company_id, r.settings, r.targets(), message=message)

    def guarded(fn):
        """Turn expected errors into a message for the model instead of a tool failure."""
        import functools

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except TrackerError as e:
                return f"Error: {e}"

        return wrapper

    # --- read ---------------------------------------------------------------------

    @server.tool()
    @guarded
    def list_companies() -> str:
        """List the client companies with how many deliverables are open, in progress,
        done and verified, how many concerns and actions are open, and how many questions
        are waiting for the client."""
        with rt().sessions() as s:
            rows = tracker.list_companies(s)
        if not rows:
            return "No client companies yet."

        def n_of(n: int, word: str) -> str:
            return f"{n} {word}{'' if n == 1 else 's'}"

        lines = []
        for c in rows:
            counts = ", ".join(f"{n} {_fmt_status(k)}" for k, n in c.counts.items())
            lines.append(
                f"- {c.name} (id {c.company_id}): deliverables {counts}; "
                f"{n_of(c.open_concerns, 'open concern')}, {n_of(c.open_actions, 'open action')}, "
                f"{n_of(c.waiting_questions, 'question')} waiting"
            )
        return "\n".join(lines)

    @server.tool()
    @guarded
    def get_company_context(company: str) -> str:
        """The full current state of a client project as markdown: every requirement with
        its delivery status (open, in progress, done, verified), questions for the client
        and the client's answers, items needing confirmation, the changelog, and footnotes
        citing the emails each item came from. `company` is a name or id; a unique part of
        the name works."""
        r = rt()
        with r.sessions() as s:
            c, _ = tracker.resolve_company(s, company)
            return render_company(s, c.company_id, r.settings)

    @server.tool()
    @guarded
    def list_items(company: str, status: str | None = None, category: str | None = None) -> str:
        """List a client's items with their status, optionally only one status (open,
        in_progress, done or verified; done on a concern means resolved) and/or one
        category: deliverable (all deliverables), milestone, concern, action, decision,
        out-of-scope, or a deliverable category such as functional. Shows the latest
        status note and who set it."""
        with rt().sessions() as s:
            c, p = tracker.resolve_company(s, company)
            reqs = tracker.items(s, p.project_id, status, category)
            if not reqs:
                return f"{c.name}: no items" + (f" with status {status}." if status else ".")
            lines = []
            for req in reqs:
                line = f"- {req.req_id} [{_fmt_status(req.delivery)}] ({req.category}) {req.text}"
                last = tracker.last_status_change(s, p.project_id, req.req_id)
                if last is not None:
                    when = last.changed_at.strftime("%Y-%m-%d")
                    line += f"\n  last status change: {_fmt_status(last.new_status)} by {last.changed_by} on {when}"
                    if last.note:
                        line += f": {last.note}"
                lines.append(line)
        return f"{c.name}:\n" + "\n".join(lines)

    @server.tool()
    @guarded
    def list_questions(company: str, include_answered: bool = True) -> str:
        """Questions for the client: not asked yet, asked and waiting, and (by default)
        already answered with the answer. Check this before logging a new question, so the
        client is never asked the same thing twice."""
        statuses = ("open", "asked", "answered") if include_answered else ("open", "asked")
        with rt().sessions() as s:
            c, p = tracker.resolve_company(s, company)
            qs = tracker.questions(s, p.project_id, statuses)
        if not qs:
            return f"{c.name}: no questions."
        return f"{c.name}:\n" + "\n".join(_fmt_question(q) for q in qs)

    @server.tool()
    @guarded
    def get_source(company: str, source: str) -> str:
        """Open the email or meeting notes behind a citation: `source` is a footnote number
        from get_company_context (e.g. "3" or "[^3]") or a Message-ID. Returns the text the
        pipeline read (quoted replies and signatures removed, attachment text included)."""
        from emailctx.render.render import footnotes
        from emailctx.store.models import Message

        with rt().sessions() as s:
            c, _ = tracker.resolve_company(s, company)
            notes = footnotes(s, c.company_id)
            key = source.strip().strip("[]^<> ")
            if key.isdigit():
                if int(key) not in notes:
                    raise TrackerError(f"{c.name} has no footnote {key} (it has 1-{len(notes)})")
                msg_id, m = notes[int(key)]
            else:
                msg_id, m = key, s.get(Message, key)
                if m is None or msg_id not in {mid for mid, _ in notes.values()}:
                    raise TrackerError(f"no source {source!r} cited in {c.name}'s file")
        if m is None:
            return f"Source {msg_id} has no stored record."
        date = m.date.strftime("%Y-%m-%d %H:%M UTC") if m.date else "undated"
        head_lines = [
            f"Subject: {m.subject or '(no subject)'}",
            f"From: {m.sender or 'unknown'}",
            f"Date: {date}",
            f"Source: {m.source}" + ("" if msg_id.startswith("sha256:") else f", Message-ID {msg_id}"),
        ]
        if m.attachments:
            head_lines.append("Attachments: " + ", ".join(m.attachments))
        body = m.body if m.body is not None else "(text not stored: ingested before sources were kept)"
        return "\n".join(head_lines) + "\n\n" + body

    # --- write --------------------------------------------------------------------

    @server.tool()
    @guarded
    def set_item_status(company: str, req_id: str, status: str, note: str | None = None) -> str:
        """Set a requirement's delivery status: open, in_progress, done or verified (QA
        checked it). `req_id` like REQ-003 (or just 3). Add a short note, e.g. the PR or
        what was tested."""
        with rt().write_lock, rt().sessions() as s:
            c, p = tracker.resolve_company(s, company)
            req, previous = tracker.set_status(s, p.project_id, req_id, status, by=current_user(), note=note)
            s.commit()
            publish(s, c.company_id, f"{p.project_id}: {req.req_id} {previous} -> {req.delivery}")
            return f"{req.req_id} is now {_fmt_status(req.delivery)} (was {_fmt_status(previous)}): {req.text}"

    @server.tool()
    @guarded
    def log_question(company: str, question: str, req_id: str | None = None) -> str:
        """Log a question only the client can answer, optionally tied to the requirement it
        blocks. If the same question was already logged, returns that one instead (with
        the client's answer if there is one), so nothing is asked twice."""
        with rt().write_lock, rt().sessions() as s:
            c, p = tracker.resolve_company(s, company)
            q, created = tracker.log_question(s, p.project_id, question, by=current_user(), req_id=req_id)
            s.commit()
            if created:
                publish(s, c.company_id, f"{p.project_id}: {q.question_id} logged")
                return f"Logged {q.question_id}: {q.text}"
            return "Already logged, not added again:\n" + _fmt_question(q)

    @server.tool()
    @guarded
    def mark_questions_asked(company: str, question_ids: list[str]) -> str:
        """Record that these questions were sent to the client (e.g. ["Q-001", "Q-003"]).
        A question can only be asked once."""
        with rt().write_lock, rt().sessions() as s:
            c, p = tracker.resolve_company(s, company)
            qs = tracker.mark_asked(s, p.project_id, question_ids)
            s.commit()
            ids = ", ".join(q.question_id for q in qs)
            publish(s, c.company_id, f"{p.project_id}: {ids} asked")
        return f"Marked as asked: {ids}"

    @server.tool()
    @guarded
    def record_answer(company: str, question_id: str, answer: str) -> str:
        """Record the client's answer to a question when it came outside email (a call, a
        meeting, a chat). Answers in emails are picked up by add_client_input."""
        with rt().write_lock, rt().sessions() as s:
            c, p = tracker.resolve_company(s, company)
            q = tracker.answer_question(s, p.project_id, question_id, answer, by=current_user())
            s.commit()
            publish(s, c.company_id, f"{p.project_id}: {q.question_id} answered (manual)")
            return f"{q.question_id} answered: {q.answer}"

    @server.tool()
    @guarded
    def withdraw_question(company: str, question_id: str) -> str:
        """Withdraw a question that no longer needs asking."""
        with rt().write_lock, rt().sessions() as s:
            c, p = tracker.resolve_company(s, company)
            q = tracker.withdraw_question(s, p.project_id, question_id)
            s.commit()
            publish(s, c.company_id, f"{p.project_id}: {q.question_id} withdrawn")
            return f"{q.question_id} withdrawn."

    @server.tool()
    @guarded
    def add_client_input(
        company: str,
        text: str,
        kind: str = "email",
        subject: str | None = None,
        sender: str | None = None,
        date: str | None = None,
    ) -> str:
        """Add client input pasted by the user: a client email or meeting notes (kind
        "email" or "meeting_notes"). It goes through the same checks as synced email: new
        and changed requirements are captured with quotes from the text, rewordings are
        ignored, finished items stay finished, and answers to open questions are recorded.
        `date` is ISO (e.g. 2026-11-21); default now."""
        from emailctx.ingest import ingest_eml
        from emailctx.llm import LLMError

        r = rt()
        with r.sessions() as s:
            c, p = tracker.resolve_company(s, company)
            domain = s.scalar(
                select(CompanyDomain.domain)
                .where(CompanyDomain.company_id == c.company_id)
                .order_by(CompanyDomain.domain)
            )
        when = None
        if date:
            try:
                when = datetime.fromisoformat(date)
            except ValueError as e:
                raise TrackerError(f"date must be ISO, e.g. 2026-11-21 (got {date!r})") from e
        raw = tracker.client_input_eml(
            text, company_domain=domain, kind=kind, subject=subject, sender=sender, date=when
        )
        try:
            result = ingest_eml(
                raw, p.project_id, company_id=c.company_id, source_ref=None, source="pasted",
                settings=r.settings, sessions=r.sessions, llm=r.llm, embedder=r.embedder,
                publishers=r.targets(), drive=r.drive, write_lock=r.write_lock,
            )
        except LLMError as e:
            return f"Error: could not process the input ({e}). Nothing was changed; safe to retry."
        if result.status == "skipped":
            return "This exact input was already added; nothing changed."
        lines = [f"{c.name}: {result.outcome.replace('_', ' ')}."]
        lines += [f"- {a.req_id} {a.op}: {a.new_text or a.old_text}" for a in result.applied]
        lines += [f"- {q} answered" for q in result.answered]
        lines += [f"- ignored: {o.reason}" for o in result.dropped]
        lines += [f"- rejected: {o.reason}" for o in result.rejected]
        if result.outcome == "gated_out":
            lines.append(
                "No changes or answers found in this input"
                + (f" ({result.gate_reason})." if result.gate_reason else ".")
            )
        lines += coverage_lines(result)
        return "\n".join(lines)

    # --- prompt -------------------------------------------------------------------

    @server.prompt()
    def company(name: str) -> str:
        """Load a client project into this session."""
        return (
            f"Fetch the current state of the client project for {name!r} with the "
            "get_company_context tool and keep it as background for this session. Then "
            "give me a short overview: what is still open or in progress, what is done "
            "but not yet verified, the questions waiting for the client, and anything "
            "under Needs confirmation. After that, wait for my question."
        )

    return server


def run(runtime_factory: Callable[[], Runtime], transport: str = "stdio", **kwargs) -> None:
    server = build_server(runtime_factory)
    if transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", **kwargs)


__all__ = ["DELIVERY_STATES", "INSTRUCTIONS", "build_server", "run"]
