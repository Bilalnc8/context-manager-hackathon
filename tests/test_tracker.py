"""Delivery status, client questions and pasted client input, through the MCP server.

These cover the three things to show on Sunday:
1. a fresh session sees what is done, open and verified (get_company_context);
2. new client feedback is captured and finished items stay finished;
3. a question only the client can answer is logged once, asked once, and closed when
   the answer comes back.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from emailctx import tracker
from emailctx.ingest import Runtime
from emailctx.llm import RecordedLLM
from emailctx.mcp_server import build_server
from emailctx.pipeline.ops import AnswerOp
from emailctx.pipeline.types import QuestionView
from emailctx.pipeline.validate import validate_ops
from emailctx.store.models import Question, Requirement, StatusChange

USER = "dev@ourstudio.io"


def text_of(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


class Harness:
    def __init__(self, env):
        self.env = env
        self.llm = RecordedLLM({})
        self.server = build_server(
            lambda: Runtime(env.settings, env.sessions, self), current_user=lambda: USER
        )

    def structured(self, *args):  # the runtime's LLM: replays whatever was queued last
        return self.llm.structured(*args)

    def call(self, tool: str, **args) -> str:
        return text_of(asyncio.run(self.server.call_tool(tool, args)))

    def md(self) -> str:
        return self.env.md_path().read_text(encoding="utf-8")

    def req(self, req_id: str) -> Requirement:
        with self.env.sessions() as s:
            return s.get(Requirement, ("atlas", req_id))


@pytest.fixture
def h(seeded) -> Harness:
    return Harness(seeded)


def test_server_lists_its_tools(h):
    tools = {t.name for t in asyncio.run(h.server.list_tools())}
    assert tools == {
        "list_companies", "get_company_context", "list_items", "list_questions", "get_source",
        "set_item_status", "log_question", "mark_questions_asked", "record_answer",
        "withdraw_question", "add_client_input",
    }


def test_fresh_session_sees_status_of_every_item(h):
    assert "REQ-001 is now done (was open)" in h.call(
        "set_item_status", company="acme", req_id="1", status="done", note="PR #12"
    )
    h.call("set_item_status", company="Acme", req_id="REQ-002", status="in progress")

    context = h.call("get_company_context", company="acm")  # a part of the name works
    assert "**Status:** deliverables 0 open · 1 in progress · 1 done · 0 verified" in context
    assert "- **REQ-001** [done] The export must support CSV.[^1]" in context
    assert "- **REQ-002** [in progress] Reports must load in under 2 seconds.[^1]" in context
    assert context == h.md()  # the published file is the same view

    listed = h.call("list_items", company="acme", status="done")
    assert "REQ-001 [done]" in listed and "REQ-002" not in listed
    assert f"by {USER}" in listed and "PR #12" in listed
    assert "1 done" in h.call("list_companies")

    with h.env.sessions() as s:
        log = s.scalars(select(StatusChange).order_by(StatusChange.id)).all()
    assert [(c.req_id, c.old_status, c.new_status, c.changed_by) for c in log] == [
        ("REQ-001", "open", "done", USER), ("REQ-002", "open", "in_progress", USER),
    ]


def test_bad_status_and_unknown_company_are_explained(h):
    assert h.call("set_item_status", company="acme", req_id="1", status="shipped").startswith(
        "Error: status must be one of open, in_progress, done, verified"
    )
    assert "Error: no company matches 'nope'. Known companies: acme" in h.call(
        "get_company_context", company="nope"
    )
    assert "REQ-999 is not an active requirement" in h.call(
        "set_item_status", company="acme", req_id="REQ-999", status="done"
    )


def test_question_is_logged_once_and_asked_once(h):
    q = "Should exported timestamps be in UTC or the user's local time?"
    assert h.call("log_question", company="acme", question=q, req_id="REQ-001") == f"Logged Q-001: {q}"
    again = h.call("log_question", company="acme", question="should exported timestamps be in UTC or the user's local time")
    assert again.startswith("Already logged, not added again:\n- Q-001 (about REQ-001)")

    assert "**Q-001** (about REQ-001)" in h.md() and "_(not asked yet)_" in h.md()
    assert h.call("mark_questions_asked", company="acme", question_ids=["Q-001"]) == "Marked as asked: Q-001"
    assert "already asked; it won't be asked again" in h.call(
        "mark_questions_asked", company="acme", question_ids=["1"]
    )
    assert "waiting for the answer" in h.md()
    assert "1 question for the client" in h.md()


def test_answer_recorded_by_hand_is_returned_instead_of_asking_again(h):
    h.call("log_question", company="acme", question="Which SSO provider do you use?")
    assert h.call("record_answer", company="acme", question_id="Q-001", answer="Okta.") == "Q-001 answered: Okta."
    again = h.call("log_question", company="acme", question="Which SSO provider do you use?")
    assert "Already logged" in again and "Answer (recorded by dev@ourstudio.io): Okta." in again
    assert f"**Answer:** Okta. _(recorded by {USER})_" in h.md()
    assert "## Questions for the client" not in h.md()


def test_client_email_answers_a_question_and_updates_the_item(h):
    h.call("log_question", company="acme", question="Should exported timestamps be in UTC?", req_id="REQ-001")
    h.call("mark_questions_asked", company="acme", question_ids=["Q-001"])
    h.llm = RecordedLLM({
        "gate": [{"has_change": True, "reason": "Answers Q-001."}],
        "extract": [{"ops": [
            {"op": "answer", "question_id": "Q-001", "answer": "Exported timestamps must be in UTC.",
             "evidence": "timestamps should be in UTC"},
            {"op": "modify", "req_id": "REQ-001", "text": "The export must support CSV, with timestamps in UTC.",
             "evidence": "timestamps should be in UTC"},
        ]}],
        "judge": [{"duplicate_of": "REQ-001", "same_meaning": False}],
    })
    paste = dict(
        company="acme", subject="Re: export questions", date="2026-11-21T10:00:00",
        text="Hi team,\n\nTo answer your question: the timestamps should be in UTC.\n",
    )
    got = h.call("add_client_input", **paste)
    assert got.splitlines() == [
        "acme: applied.",
        "- REQ-001 modify: The export must support CSV, with timestamps in UTC.",
        "- Q-001 answered",
        "Every sentence was captured or skipped with a reason.",
    ]
    gate_prompt = h.llm.calls[0][1]
    assert "<open_questions>\nQ-001 (about REQ-001): Should exported timestamps be in UTC?" in gate_prompt

    md = h.md()
    assert "**Q-001** (about REQ-001) Should exported timestamps be in UTC? **Answer:** Exported timestamps must be in UTC.[^2]" in md
    assert '[^2]: Email "Re: export questions"' in md
    assert "0 questions for the client" in md
    # The same paste again (same details) is processed once, with no LLM call.
    assert h.call("add_client_input", **paste) == "This exact input was already added; nothing changed."


def test_new_work_on_finished_item_is_a_new_item(h):
    h.call("set_item_status", company="acme", req_id="REQ-001", status="verified")
    h.llm = RecordedLLM({
        "gate": [{"has_change": True, "reason": "Asks for PDF export."}],
        "extract": [{"ops": [{"op": "add", "text": "The export must support PDF.", "category": "functional",
                              "evidence": "we also need PDF export"}]}],
        # The judge links it to REQ-001 (same feature, more scope): normally a modify.
        "judge": [{"duplicate_of": "REQ-001", "same_meaning": False, "contradicts": False}],
    })
    got = h.call("add_client_input", company="acme", text="The CSV export works great, but we also need PDF export.")
    assert "- REQ-003 add: The export must support PDF." in got
    assert h.req("REQ-001").delivery == "verified"  # finished stays finished
    assert h.req("REQ-001").text == "The export must support CSV."
    assert "- **REQ-003** [open] The export must support PDF." in h.md()


def test_client_change_to_finished_work_reopens_it(h):
    h.call("set_item_status", company="acme", req_id="REQ-001", status="done")
    h.llm = RecordedLLM({
        "gate": [{"has_change": True, "reason": "Changes the export."}],
        "extract": [{"ops": [{"op": "modify", "req_id": "REQ-001", "text": "The export must support CSV with semicolons as separators.",
                              "evidence": "use semicolons instead of commas"}]}],
        "judge": [{"duplicate_of": "REQ-001", "same_meaning": False}],
    })
    h.call("add_client_input", company="acme", kind="meeting_notes",
           text="Decisions:\n- CSV export: use semicolons instead of commas (Excel in DE).\n")
    req = h.req("REQ-001")
    assert (req.delivery, req.text) == ("open", "The export must support CSV with semicolons as separators.")
    with h.env.sessions() as s:
        last = s.scalars(select(StatusChange).order_by(StatusChange.id.desc())).first()
    assert (last.old_status, last.new_status) == ("done", "open")
    assert last.changed_by.startswith("email sha256:")


def test_answer_for_unknown_question_is_rejected(env):
    ops = [AnswerOp(op="answer", question_id="Q-009", answer="UTC.", evidence="should be in UTC")]
    result = validate_ops(
        ops, source_text="It should be in UTC.", msg_id="m1", current=[], llm=RecordedLLM({}),
        embedder=None, settings=env.settings, questions=[QuestionView("Q-001", "UTC or local?")],
    )
    assert result.answers == [] and "unknown or already answered question id Q-009" in result.rejected[0].reason


def test_withdrawn_question_can_be_logged_again(h):
    h.call("log_question", company="acme", question="Do you need dark mode?")
    assert h.call("withdraw_question", company="acme", question_id="Q-001") == "Q-001 withdrawn."
    assert h.call("log_question", company="acme", question="Do you need dark mode?") == "Logged Q-002: Do you need dark mode?"
    with h.env.sessions() as s:
        assert [q.status for q in s.scalars(select(Question).order_by(Question.seq))] == ["withdrawn", "open"]


def test_any_citation_can_be_opened_including_pasted_input(h):
    first = h.call("get_source", company="acme", source="[^1]")
    assert first.splitlines()[:4] == [
        "Subject: Atlas export requirements", "From: Jane Doe (jane@acme.com)",
        "Date: 2026-11-16 09:00 UTC", "Source: eml",
    ]
    assert first.endswith("the export must support CSV. Reports should load in under 2 seconds.")
    assert h.call("get_source", company="acme", source="atlas-001@acme.com") == first

    h.llm = RecordedLLM({
        "gate": [{"has_change": True}],
        "extract": [{"ops": [{"op": "add", "text": "Users sign in with Okta SSO.", "category": "integration",
                              "evidence": "sign in with our Okta"}]}],
        "judge": [{"duplicate_of": None, "same_meaning": False}],
    })
    h.call("add_client_input", company="acme", kind="meeting_notes", sender="jane@acme.com",
           date="2026-11-20T15:00:00", text="Call with Jane:\n- Users should sign in with our Okta.\n")
    pasted = h.call("get_source", company="acme", source="2")
    assert "Subject: Meeting notes" in pasted and "Source: pasted" in pasted
    assert pasted.endswith("- Users should sign in with our Okta.")  # kept: it exists nowhere else
    assert "has no footnote 9" in h.call("get_source", company="acme", source="9")


def test_writes_queue_while_an_email_is_processed(h):
    """A teammate's write during a slow LLM call waits its turn instead of failing."""
    import threading
    import time

    entered = threading.Event()

    class SlowLLM(RecordedLLM):
        def structured(self, task, *args):
            if task == "gate":
                entered.set()
                time.sleep(1.0)  # the pipeline is mid-call, holding the write lock
            return super().structured(task, *args)

    h.llm = SlowLLM({
        "gate": [{"has_change": True}],
        "extract": [{"ops": [{"op": "add", "text": "The export must support PDF.", "category": "functional",
                              "evidence": "we need PDF export"}]}],
        "judge": [{"duplicate_of": None, "same_meaning": False}],
    })
    results = {}
    worker = threading.Thread(target=lambda: results.setdefault(
        "paste", h.call("add_client_input", company="acme", text="Also, we need PDF export.")))
    worker.start()
    assert entered.wait(5)
    results["status"] = h.call("set_item_status", company="acme", req_id="REQ-001", status="done")
    worker.join(10)

    assert results["status"].startswith("REQ-001 is now done")
    assert "- REQ-003 add: The export must support PDF." in results["paste"]
    md = h.md()  # the file reflects both writes: rendering happens inside the lock
    assert "- **REQ-001** [done]" in md and "- **REQ-003** [open] The export must support PDF." in md


def test_prompt_loads_a_company(h):
    result = asyncio.run(h.server.get_prompt("company", {"name": "Acme"}))
    text = "".join(getattr(m.content, "text", "") for m in result.messages)
    assert "get_company_context" in text and "'Acme'" in text


def test_tracker_requires_a_question(env):
    with env.sessions() as s, pytest.raises(tracker.TrackerError, match="empty"):
        tracker.log_question(s, "atlas", "   ", by=USER)
