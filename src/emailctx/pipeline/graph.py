"""Per-segment pipeline as a LangGraph: gate -> extract -> validate -> apply.

validate can send rejected ops and unaccounted sentences (see coverage.py) back to
extract, up to max_extract_retries times.
Rendering happens after the caller commits, so the md never reflects uncommitted state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

from emailctx.config import Settings
from emailctx.llm import LLMClient
from emailctx.pipeline.apply import (
    apply_answers,
    apply_ops,
    load_active,
    load_waiting_questions,
    record_stale,
)
from emailctx.pipeline.extract import run_extract
from emailctx.pipeline.gate import run_gate
from emailctx.pipeline.ops import Skipped
from emailctx.pipeline.types import AnyOp, AppliedChange, ValidationResult
from emailctx.pipeline.validate import validate_ops
from emailctx.routing.embeddings import Embedder


class SegmentState(TypedDict, total=False):
    # inputs
    project_id: str
    msg_id: str
    subject: str
    text: str
    source_date: datetime | None
    # working state
    attempt: int
    ops: list[AnyOp]
    skipped: list[Skipped]
    validation: ValidationResult
    gate_reason: str
    # outputs
    applied: list[AppliedChange]
    answered: list[str]  # question ids the email answered
    conflicts: int  # conflicts recorded for "Needs confirmation"
    outcome: str  # empty | gated_out | no_change | applied | flagged


@dataclass
class PipelineDeps:
    session: Session
    llm: LLMClient
    embedder: Embedder
    settings: Settings


def build_graph(deps: PipelineDeps) -> Any:
    max_retries = deps.settings.pipeline.max_extract_retries

    def gate(state: SegmentState) -> SegmentState:
        if not state["text"].strip():
            return {"outcome": "empty"}
        questions = load_waiting_questions(deps.session, state["project_id"])
        verdict = run_gate(deps.llm, state["text"], state.get("subject", ""), questions)
        update: SegmentState = {"gate_reason": verdict.reason}
        if not verdict.has_change:
            update["outcome"] = "gated_out"
        return update

    def extract(state: SegmentState) -> SegmentState:
        previous = state.get("validation")
        result = run_extract(
            deps.llm,
            text=state["text"],
            subject=state.get("subject", ""),
            current=load_active(deps.session, state["project_id"]),
            categories=deps.settings.categories,
            rejected_feedback=previous.rejected if previous else (),
            unaccounted_feedback=previous.unaccounted if previous else (),
            questions=load_waiting_questions(deps.session, state["project_id"]),
        )
        return {
            "ops": list(result.ops),
            "skipped": list(result.skipped),
            "attempt": state.get("attempt", 0) + 1,
        }

    def validate(state: SegmentState) -> SegmentState:
        result = validate_ops(
            state["ops"],
            source_text=state["text"],
            msg_id=state["msg_id"],
            current=load_active(deps.session, state["project_id"]),
            llm=deps.llm,
            embedder=deps.embedder,
            settings=deps.settings,
            source_date=state.get("source_date"),
            questions=load_waiting_questions(deps.session, state["project_id"]),
            skipped=state.get("skipped", []),
        )
        return {"validation": result}

    def apply(state: SegmentState) -> SegmentState:
        v = state["validation"]
        applied = apply_ops(
            deps.session, state["project_id"], v.accepted, source_date=state.get("source_date")
        )
        answered = apply_answers(
            deps.session, state["project_id"], v.answers, source_date=state.get("source_date")
        )
        conflicts = record_stale(deps.session, state["project_id"], v.stale)
        conflicts += sum(1 for op in v.accepted if op.contradicts)
        return {
            "applied": applied,
            "answered": answered,
            "conflicts": conflicts,
            "outcome": "applied" if applied or answered else "flagged",
        }

    def no_change(state: SegmentState) -> SegmentState:
        return {"outcome": "no_change"}

    def after_gate(state: SegmentState) -> str:
        return END if state.get("outcome") else "extract"

    def after_validate(state: SegmentState) -> str:
        v = state["validation"]
        if (v.rejected or v.unaccounted) and state["attempt"] <= max_retries:
            return "extract"
        return "apply" if (v.accepted or v.stale or v.answers) else "no_change"

    g = StateGraph(SegmentState)
    g.add_node("gate", gate)
    g.add_node("extract", extract)
    g.add_node("validate", validate)
    g.add_node("apply", apply)
    g.add_node("no_change", no_change)
    g.add_edge(START, "gate")
    g.add_conditional_edges("gate", after_gate, ["extract", END])
    g.add_edge("extract", "validate")
    g.add_conditional_edges("validate", after_validate, ["extract", "apply", "no_change"])
    g.add_edge("apply", END)
    g.add_edge("no_change", END)
    return g.compile()
