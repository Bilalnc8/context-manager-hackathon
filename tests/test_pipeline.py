"""End-to-end pipeline tests: the five required Phase 1 behaviours, plus a few around them."""

from __future__ import annotations

import pytest

from sqlalchemy import select

from emailctx.llm import RecordedLLM
from emailctx.store.models import Message


def test_initial_email_creates_requirements(seeded):
    reqs = seeded.requirements()
    assert [(r.req_id, r.text, r.category, r.version) for r in reqs] == [
        ("REQ-001", "The export must support CSV.", "functional", 1),
        ("REQ-002", "Reports must load in under 2 seconds.", "non-functional", 1),
    ]
    assert all(r.source_ids == ["atlas-001@acme.com"] for r in reqs)
    assert [c.op for c in seeded.change_log()] == ["add", "add"]
    md = seeded.md_bytes().decode()
    assert "## atlas" not in md  # one project per company: no project heading
    assert "- **REQ-001** [open] The export must support CSV.[^1]" in md
    assert "\n## Deliverables\n\n### Functional\n" in md
    assert "\n### Non-functional\n" in md
    assert "- 2026-11-16 · **REQ-001** added: The export must support CSV.[^1]" in md
    assert '[^1]: Email "Atlas export requirements", 2026-11-16, ID `atlas-001@acme.com`' in md


# 1. A reply quoting an old requirement does not re-add it ---------------------------


def test_reply_quoting_old_requirement_does_not_readd_it(seeded):
    before_md, before = seeded.md_bytes(), seeded.snapshot()

    result, llm = seeded.ingest("atlas_02_reply_quote", "atlas_02_reply_quote")

    # The quote never reaches extraction...
    extract_prompt = next(p for task, p in llm.calls if task == "extract")
    assert "Sounds good" in extract_prompt
    assert "must support CSV" not in extract_prompt.split("<email>")[1]
    # ...so an op citing it fails the evidence check, and the retry returns none.
    assert result.outcome == "no_change"
    assert [o.reason for o in result.rejected] == []  # final attempt is clean
    assert [task for task, _ in llm.calls] == ["gate", "extract", "extract"]
    assert "evidence not found" in llm.calls[2][1]  # retry carried the rejection reason

    assert seeded.md_bytes() == before_md
    after = seeded.snapshot()
    assert after["requirements"] == before["requirements"]
    assert after["counts"]["change_log"] == before["counts"]["change_log"]


# 2. A reworded requirement does not change context.md ------------------------------


@pytest.mark.parametrize(
    "recording, judge_calls",
    [
        ("atlas_03_reworded_modify", 1),  # modify with new wording -> judge: same meaning
        ("atlas_03_reworded_add", 1),  # add duplicating REQ-001 -> judge: same meaning
        ("atlas_03_normalized_only", 0),  # case/punctuation only -> cheap exit, no judge
    ],
)
def test_reworded_requirement_leaves_md_byte_identical(seeded, recording, judge_calls):
    md_path = seeded.md_path()
    before_md, before_mtime = md_path.read_bytes(), md_path.stat().st_mtime_ns
    before = seeded.snapshot()

    result, llm = seeded.ingest("atlas_03_reworded", recording)

    assert result.outcome == "no_change"
    assert result.applied == [] and result.rejected == []
    assert len(result.dropped) == 1
    assert sum(1 for task, _ in llm.calls if task == "judge") == judge_calls
    assert llm.remaining() == {}

    assert md_path.read_bytes() == before_md
    assert md_path.stat().st_mtime_ns == before_mtime  # not even rewritten
    after = seeded.snapshot()
    assert after["requirements"] == before["requirements"]
    assert after["counts"]["change_log"] == before["counts"]["change_log"]


# 3. An op with evidence not in the email is rejected -------------------------------


def test_op_with_evidence_not_in_email_is_rejected(seeded):
    before_md, before = seeded.md_bytes(), seeded.snapshot()

    result, llm = seeded.ingest("atlas_05_meeting", "atlas_05_hallucinated")

    assert result.outcome == "no_change"
    assert len(result.rejected) == 1  # final attempt
    assert result.rejected[0].reason == "evidence not found in the email"
    assert [task for task, _ in llm.calls] == ["gate", "extract", "extract"]  # one retry
    assert llm.remaining() == {}

    assert seeded.md_bytes() == before_md
    assert seeded.snapshot()["requirements"] == before["requirements"]


# 4. Ingesting the same file twice changes nothing ----------------------------------


def test_ingesting_same_file_twice_changes_nothing(env):
    env.ingest("atlas_01_initial", "atlas_01_initial")
    md_path = env.md_path()
    before_md, before_mtime = md_path.read_bytes(), md_path.stat().st_mtime_ns
    before = env.snapshot()

    result, llm = env.ingest("atlas_01_initial", llm=None)  # any LLM call would fail

    assert result.status == "skipped"
    assert result.outcome == "already_processed"
    assert llm.calls == []
    assert env.snapshot() == before
    assert md_path.read_bytes() == before_md
    assert md_path.stat().st_mtime_ns == before_mtime


def test_reingesting_after_later_changes_still_changes_nothing(seeded):
    seeded.ingest("atlas_04_real_change", "atlas_04_real_change")
    before, before_md = seeded.snapshot(), seeded.md_bytes()
    for name in ("atlas_01_initial", "atlas_04_real_change"):
        result, llm = seeded.ingest(name, llm=None)
        assert result.status == "skipped" and llm.calls == []
    assert seeded.snapshot() == before
    assert seeded.md_bytes() == before_md


# 5. A real change produces exactly one modified requirement plus a change_log row --


def test_real_change_modifies_exactly_one_requirement(seeded):
    before_reqs = {r.req_id: r for r in seeded.requirements()}
    before_log = seeded.change_log()
    before_md = seeded.md_bytes()

    result, llm = seeded.ingest("atlas_04_real_change", "atlas_04_real_change")

    assert result.outcome == "applied"
    assert [(c.req_id, c.op) for c in result.applied] == [("REQ-001", "modify")]
    assert llm.remaining() == {}

    after_reqs = {r.req_id: r for r in seeded.requirements()}
    assert after_reqs.keys() == before_reqs.keys()
    changed = [k for k in after_reqs if after_reqs[k].text != before_reqs[k].text]
    assert changed == ["REQ-001"]
    req = after_reqs["REQ-001"]
    assert req.text == "The export must support CSV and XLSX."
    assert req.version == 2
    assert req.source_ids == ["atlas-001@acme.com", "atlas-004@acme.com"]
    assert after_reqs["REQ-002"].version == 1

    new_log = seeded.change_log()[len(before_log):]
    assert len(new_log) == 1
    row = new_log[0]
    assert (row.req_id, row.op) == ("REQ-001", "modify")
    assert row.old_text == "The export must support CSV."
    assert row.new_text == "The export must support CSV and XLSX."
    assert row.source_id == "atlas-004@acme.com"

    md = seeded.md_bytes().decode()
    assert seeded.md_bytes() != before_md
    assert "- **REQ-001** [open] The export must support CSV and XLSX.[^1][^2]" in md
    assert '[^2]: Email "Re: Atlas export requirements", 2026-11-19, ID `atlas-004@acme.com`' in md
    assert "we also need XLSX export alongside CSV" not in md  # quote checked, not kept


# Around the edges --------------------------------------------------------------------


def test_gate_stops_noise_before_extraction(seeded):
    before_md = seeded.md_bytes()
    llm = RecordedLLM({"gate": [{"has_change": False, "reason": "Scheduling."}]})
    result, _ = seeded.ingest("atlas_05_meeting", llm)
    assert result.outcome == "gated_out"
    assert [task for task, _ in llm.calls] == ["gate"]
    assert seeded.md_bytes() == before_md


def test_llm_failure_leaves_message_unprocessed(env):
    """If extraction blows up, nothing is committed, so a later run retries the email."""
    llm = RecordedLLM({"gate": [{"has_change": True}]})  # no extract response -> raises
    with pytest.raises(AssertionError, match="unexpected extract call"):
        env.ingest("atlas_01_initial", llm)
    assert env.snapshot()["counts"]["processed_messages"] == 0
    assert env.snapshot()["counts"]["messages"] == 0

    result, _ = env.ingest("atlas_01_initial", "atlas_01_initial")
    assert result.outcome == "applied"


def test_thread_backstop_drops_unmarked_quotes(seeded):
    """Paragraphs repeated from an earlier email in the thread never reach extraction."""
    raw = (
        "From: Sam Lee <sam@ourstudio.io>\n"
        "To: Jane Doe <jane@acme.com>\n"
        "Subject: Re: Atlas export requirements\n"
        "Date: Tue, 17 Nov 2026 12:00:00 +0000\n"
        "Message-ID: <atlas-unmarked@ourstudio.io>\n"
        "In-Reply-To: <atlas-001@acme.com>\n"
        "Content-Type: text/plain; charset=utf-8\n\n"
        "Noted, thanks.\n\n"
        "For Atlas, the export must support CSV. Reports should load in under 2 seconds.\n"
    )
    from emailctx.ingest import ingest_eml

    llm = RecordedLLM({"gate": [{"has_change": False, "reason": "Ack."}]})
    ingest_eml(raw.encode(), "atlas", settings=seeded.settings, sessions=seeded.sessions, llm=llm)
    gate_prompt = llm.calls[0][1]
    assert "Noted, thanks." in gate_prompt
    assert "must support CSV" not in gate_prompt


# Source text is kept, cleaned ------------------------------------------------------------


def test_source_text_is_kept_cleaned(seeded):
    """Every input's cleaned text is stored with its message, so any citation can be
    opened later. Quoted replies and signatures are not: they were removed before the
    pipeline read the email."""
    seeded.ingest("atlas_02_reply_quote", "atlas_02_reply_quote")
    seeded.ingest("atlas_05_meeting", RecordedLLM({"gate": [{"has_change": False}]}))

    with seeded.sessions() as s:
        msgs = {m.msg_id: m for m in s.scalars(select(Message))}
    first, reply = msgs["atlas-001@acme.com"], msgs["atlas-002@ourstudio.io"]
    assert first.body == "Hi team,\n\nFor Atlas, the export must support CSV. Reports should load in under 2 seconds."
    assert (first.sender, reply.sender) == ("jane@acme.com", "sam@ourstudio.io")
    assert reply.body == "Sounds good, we'll plan around that."  # the quoted original is gone
    assert all("Product Lead, Acme" not in (m.body or "") for m in msgs.values())  # signature
    assert any("Can we meet Thursday" in (m.body or "") for m in msgs.values())  # gated out, still kept
    # The rendered file holds requirement text and citations, never email bodies.
    md = seeded.md_bytes()
    assert b"Hi team" not in md and b"jane@acme.com" not in md


# Companies -------------------------------------------------------------------------------


def test_one_file_per_company(env):
    llm = RecordedLLM(
        {
            "gate": [{"has_change": True}],
            "extract": [{"ops": [{"op": "add", "text": "Dashboard must show weekly totals.",
                                  "category": "ui", "evidence": "must show weekly totals"}]}],
        }
    )
    env.ingest("atlas_01_initial", "atlas_01_initial")
    result, _ = env.ingest("gmail_html_reply", llm, project="beacon", company="globex")
    assert result.outcome == "applied"
    assert [p.destination for p in result.published] == [str(env.md_path("globex"))]
    assert sorted(p.name for p in env.settings.output.local_dir.iterdir()) == ["acme", "globex"]
    assert "Dashboard must show weekly totals" in env.md_bytes("globex").decode()
    assert "Dashboard" not in env.md_bytes("acme").decode()


def test_second_project_for_a_company_is_refused(seeded):
    from emailctx.ingest import IngestError

    with pytest.raises(IngestError, match="already has project 'atlas'"):
        seeded.ingest("gmail_html_reply", llm=None, project="beacon", company="acme")
    assert seeded.snapshot()["counts"]["messages"] == 1  # nothing recorded


def test_company_alone_means_its_project(seeded):
    llm = RecordedLLM({"gate": [{"has_change": False}]})
    result, _ = seeded.ingest("atlas_05_meeting", llm, project=None, company="acme")
    assert (result.route.step, result.project_id) == ("manual", "atlas")


def test_new_project_needs_a_company(env):
    from emailctx.ingest import IngestError

    with pytest.raises(IngestError, match="give its company"):
        env.ingest("atlas_01_initial", llm=None, company=None)
    assert env.snapshot()["counts"]["messages"] == 0


def test_project_cannot_move_between_companies(seeded):
    from emailctx.ingest import IngestError

    with pytest.raises(IngestError, match="belongs to company 'acme'"):
        seeded.ingest("atlas_05_meeting", llm=None, company="globex")


def test_known_project_needs_no_company_flag(seeded):
    llm = RecordedLLM({"gate": [{"has_change": False}]})
    result, _ = seeded.ingest("atlas_05_meeting", llm, company=None)
    assert result.outcome == "gated_out"
