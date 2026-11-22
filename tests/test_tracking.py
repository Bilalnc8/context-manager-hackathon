"""Where the project stands: key dates, open concerns, next actions, decisions and
out-of-scope work, each in its own section; concerns and actions closed by email."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from test_tracker import Harness

from emailctx.llm import RecordedLLM
from emailctx.pipeline.ops import ResolveOp
from emailctx.pipeline.types import ReqView
from emailctx.pipeline.validate import validate_ops
from emailctx.routing.embeddings import NoOpEmbedder
from emailctx.store.models import StatusChange

NEW = {"duplicate_of": None, "same_meaning": False}

KICKOFF = (
    "The December 5 pilot is approved, on the condition that security sign-off is complete by "
    "December 3. Payment processing is not part of phase one. Alex will send the "
    "penetration-test summary by December 2. We agreed to keep the support form to three fields."
)


@pytest.fixture
def h(seeded) -> Harness:
    return Harness(seeded)


def add_tracking_items(h: Harness) -> str:
    """REQ-003 milestone, REQ-004 concern, REQ-005 out-of-scope, REQ-006 action, REQ-007 decision."""
    h.llm = RecordedLLM({
        "gate": [{"has_change": True, "reason": "Pilot approval and scope."}],
        "extract": [{"ops": [
            {"op": "add", "category": "milestone",
             "text": "Pilot on December 5: conditionally approved; final go/no-go pending.",
             "evidence": "The December 5 pilot is approved, on the condition"},
            {"op": "add", "category": "concern",
             "text": "Security sign-off is pending and must be complete by December 3.",
             "evidence": "security sign-off is complete by December 3"},
            {"op": "add", "category": "out_of_scope",  # coerced to out-of-scope
             "text": "Online payment processing is excluded from phase one.",
             "evidence": "Payment processing is not part of phase one."},
            {"op": "add", "category": "action",
             "text": "Alex (ContextLab): send the penetration-test summary by December 2.",
             "evidence": "Alex will send the penetration-test summary by December 2."},
            {"op": "add", "category": "decision",
             "text": "The support form has three fields.",
             "evidence": "keep the support form to three fields"},
        ]}],
        "judge": [NEW] * 5,
    })
    return h.call("add_client_input", company="acme", subject="Pilot approval",
                  date="2026-11-16T09:00:00", text=KICKOFF)


def test_tracking_items_get_their_own_sections(h):
    got = add_tracking_items(h)
    assert "Every sentence was captured or skipped with a reason." in got
    md = h.md()
    order = ["## Key dates", "## Open concerns", "## Deliverables", "## Next actions",
             "## Out of scope", "## Decisions", "## Changelog"]
    positions = [md.index(f"\n{s}\n") for s in order]
    assert positions == sorted(positions)
    assert "- **REQ-003** Pilot on December 5: conditionally approved; final go/no-go pending.[^2]" in md
    assert "- **REQ-004** [open] Security sign-off is pending" in md
    assert "- **REQ-005** Online payment processing is excluded from phase one.[^2]" in md
    assert "- **REQ-006** [open] Alex (ContextLab): send the penetration-test summary" in md
    assert "- **REQ-007** The support form has three fields.[^2]" in md
    assert ("**Status:** deliverables 2 open · 0 in progress · 0 done · 0 verified · "
            "1 open concern · 1 open action · 0 questions for the client") in md
    assert '**Last change:** 2026-11-16, email "Pilot approval"[^2]' in md

    listed = h.call("list_companies")
    assert "deliverables 2 open, 0 in progress, 0 done, 0 verified; 1 open concern, 1 open action" in listed
    concerns = h.call("list_items", company="acme", category="concern")
    assert "REQ-004" in concerns and "REQ-001" not in concerns
    deliverables = h.call("list_items", company="acme", category="deliverable")
    assert "REQ-001" in deliverables and "REQ-002" in deliverables and "REQ-004" not in deliverables


def test_email_resolves_a_concern(h):
    add_tracking_items(h)
    h.llm = RecordedLLM({
        "gate": [{"has_change": True, "reason": "Security approved."}],
        "extract": [{"ops": [
            {"op": "resolve", "req_id": "REQ-004", "evidence": "Jordan signed off on security today"},
        ]}],
    })
    paste = dict(company="acme", subject="Security sign-off", date="2026-11-20T09:00:00",
                 text="Jordan signed off on security today.")
    got = h.call("add_client_input", **paste)
    assert "- REQ-004 resolve: Security sign-off is pending" in got

    md = h.md()
    assert "## Open concerns" not in md
    assert md.index("\n## Resolved and completed\n") < md.index("\n## Changelog\n")
    assert "- **REQ-004** [resolved] Security sign-off is pending and must be complete by December 3.[^2][^3]" in md
    assert "- 2026-11-20 · **REQ-004** resolved: Security sign-off is pending" in md
    assert "0 open concerns" in md
    with h.env.sessions() as s:
        change = s.scalars(select(StatusChange).order_by(StatusChange.id.desc())).first()
    assert (change.req_id, change.old_status, change.new_status) == ("REQ-004", "open", "done")
    assert change.changed_by.startswith("email sha256:")
    assert h.call("add_client_input", **paste) == "This exact input was already added; nothing changed."


def test_team_can_close_an_action(h):
    add_tracking_items(h)
    h.call("set_item_status", company="acme", req_id="REQ-006", status="done", note="sent")
    md = h.md()
    assert "## Next actions" not in md
    assert "- **REQ-006** [done] Alex (ContextLab): send the penetration-test summary" in md
    assert "0 open actions" in md


def test_client_reopens_a_resolved_concern(h):
    add_tracking_items(h)
    h.call("set_item_status", company="acme", req_id="REQ-004", status="done")
    h.llm = RecordedLLM({
        "gate": [{"has_change": True, "reason": "Security reopened."}],
        "extract": [{"ops": [{"op": "modify", "req_id": "REQ-004",
                              "text": "Security sign-off is pending again: two findings need mitigations.",
                              "evidence": "two findings need mitigations"}]}],
        "judge": [{"duplicate_of": "REQ-004", "same_meaning": False}],
    })
    h.call("add_client_input", company="acme", text="Sorry, two findings need mitigations before we sign off.")
    assert h.req("REQ-004").delivery == "open"
    assert "- **REQ-004** [open] Security sign-off is pending again" in h.md()


def _concern(req_id: str, category: str = "concern", delivery: str = "open", updated=None) -> ReqView:
    return ReqView(req_id, "Text.", "text", category, updated, "m0", delivery)


@pytest.mark.parametrize("current, outcome, reason", [
    ([_concern("REQ-001", "functional")], "rejected", "only concerns and actions can be resolved"),
    ([_concern("REQ-001", delivery="done")], "dropped", "already resolved"),
    ([_concern("REQ-001", updated=datetime(2026, 11, 20))], "dropped", "older than REQ-001's last change"),
    ([], "rejected", "unknown or removed requirement id REQ-001"),
    ([_concern("REQ-001", "action")], "accepted", None),
])
def test_resolve_rules(env, current, outcome, reason):
    op = ResolveOp(op="resolve", req_id="REQ-001", evidence="It is fixed now")
    result = validate_ops(
        [op], source_text="It is fixed now.", msg_id="m1", current=current, llm=RecordedLLM({}),
        embedder=NoOpEmbedder(), settings=env.settings, source_date=datetime(2026, 11, 19),
    )
    if outcome == "accepted":
        assert [(a.kind, a.req_id) for a in result.accepted] == [("resolve", "REQ-001")]
    else:
        (o,) = getattr(result, outcome)
        assert reason in o.reason
