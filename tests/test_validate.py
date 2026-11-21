from __future__ import annotations

from emailctx.config import Settings
from emailctx.llm import RecordedLLM
from emailctx.pipeline.ops import ExtractResult
from emailctx.pipeline.types import ReqView
from emailctx.pipeline.validate import coerce_category, validate_ops
from emailctx.routing.embeddings import NoOpEmbedder
from emailctx.textnorm import normalize_requirement

SOURCE = "Please add XLSX export. Also drop the PDF export, nobody uses it."
CURRENT = [
    ReqView("REQ-001", "Export must support CSV.", normalize_requirement("Export must support CSV."), "functional"),
    ReqView("REQ-002", "Export must support PDF.", normalize_requirement("Export must support PDF."), "functional"),
]


def run(ops: list[dict], judge: list[dict] | None = None, current=CURRENT):
    llm = RecordedLLM({"judge": judge or []})
    parsed = ExtractResult.model_validate({"ops": ops}).ops
    result = validate_ops(
        parsed,
        source_text=SOURCE,
        msg_id="m1",
        current=current,
        llm=llm,
        embedder=NoOpEmbedder(),
        settings=Settings(),
    )
    assert llm.remaining() == {}, "unused judge responses"
    return result


def test_evidence_not_in_source_rejected():
    r = run([{"op": "add", "text": "Export must support XML.", "category": "functional",
              "evidence": "add XML export"}])
    assert [o.reason for o in r.rejected] == ["evidence not found in the email"]
    assert r.accepted == []


def test_too_short_evidence_rejected():
    r = run([{"op": "remove", "req_id": "REQ-002", "evidence": "PDF"}])
    assert r.rejected[0].reason.startswith("evidence shorter than")


def test_unknown_req_id_rejected():
    r = run([{"op": "remove", "req_id": "REQ-099", "evidence": "drop the PDF export"}])
    assert r.rejected[0].reason == "unknown or removed requirement id REQ-099"


def test_remove_accepted():
    r = run([{"op": "remove", "req_id": "REQ-002", "evidence": "drop the PDF export"}])
    assert [(a.kind, a.req_id) for a in r.accepted] == [("remove", "REQ-002")]


def test_add_judged_distinct_is_accepted_with_coerced_category():
    r = run(
        [{"op": "add", "text": "Export must support XLSX.", "category": "Reporting",
          "evidence": "Please add XLSX export"}],
        judge=[{"duplicate_of": None, "same_meaning": False}],
    )
    assert [(a.kind, a.text, a.category) for a in r.accepted] == [
        ("add", "Export must support XLSX.", "other")
    ]


def test_add_that_changes_existing_requirement_becomes_modify():
    r = run(
        [{"op": "add", "text": "Export must support CSV and XLSX.", "category": "functional",
          "evidence": "Please add XLSX export"}],
        judge=[{"duplicate_of": "REQ-001", "same_meaning": False}],
    )
    assert [(a.kind, a.req_id, a.converted_from) for a in r.accepted] == [
        ("modify", "REQ-001", "add")
    ]


def test_add_with_hallucinated_duplicate_id_is_kept_as_add():
    r = run(
        [{"op": "add", "text": "Export must support XLSX.", "category": "functional",
          "evidence": "Please add XLSX export"}],
        judge=[{"duplicate_of": "REQ-777", "same_meaning": True}],
    )
    assert [a.kind for a in r.accepted] == ["add"]


def test_duplicate_adds_in_one_email_collapse():
    op = {"op": "add", "text": "Export must support XLSX.", "category": "functional",
          "evidence": "Please add XLSX export"}
    r = run([op, op], current=[])  # nothing to compare against: no judge call
    assert len(r.accepted) == 1
    assert r.dropped[0].reason == "duplicate of an earlier add in this email"


def test_second_op_on_same_requirement_dropped():
    r = run(
        [
            {"op": "remove", "req_id": "REQ-002", "evidence": "drop the PDF export"},
            {"op": "modify", "req_id": "REQ-002", "text": "PDF export is optional.",
             "evidence": "drop the PDF export"},
        ]
    )
    assert [a.kind for a in r.accepted] == ["remove"]
    assert "already changed" in r.dropped[0].reason


def test_none_ops_ignored():
    r = run([{"op": "none", "reason": "nothing"}])
    assert (r.accepted, r.dropped, r.rejected) == ([], [], [])


def test_coerce_category():
    allowed = Settings().categories
    assert coerce_category("Non Functional", allowed) == "non-functional"
    assert coerce_category("UI", allowed) == "ui"
    assert coerce_category("performance", allowed) == "other"
    assert coerce_category(None, allowed) == "other"
