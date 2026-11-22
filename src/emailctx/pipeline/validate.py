"""Validation: where "only change the file when a requirement changes" is enforced.

Per op, in order:
1. evidence: normalized evidence must be a substring of the normalized source text
   (and at least `min_evidence_chars` long). Failure -> rejected.
2. modify/remove must reference an active requirement. Failure -> rejected.
3. one op per requirement per email; later ones are dropped.
4. modify: unchanged normalized text -> dropped. Otherwise the judge decides whether the
   new text means the same; same meaning -> dropped as a rewording.
5. add: normalized text equal to an existing requirement (or an earlier add) -> dropped.
   Otherwise the judge looks at the most similar existing requirements:
   duplicate with the same meaning -> dropped; duplicate with a different meaning ->
   converted to a modify of that requirement.

Conflicts (latest wins, a person confirms):
6. modify/remove from an email dated before the requirement's last change -> stale:
   not applied, recorded under "Needs confirmation".
7. modify the judge says contradicts the current text -> applied (it is the latest)
   and recorded under "Needs confirmation".

Finished work stays finished:
8. an add the judge maps onto a done or verified requirement, with a different meaning
   that doesn't contradict it, stays an add: new work on top of finished work is a new
   item. (A modify of finished work reopens it; see apply.)

Answers to the client questions:
9. answer must pass the evidence check, name a question that is still waiting (open or
   asked), and answer each question at most once per email.

Rejected ops can be sent back to extraction for a retry. Dropped ops are legitimate
no-ops and are not retried.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from emailctx.config import Settings
from emailctx.llm import LLMClient
from emailctx.pipeline.ops import AddOp, AnswerOp, JudgeResult, ModifyOp, NoneOp, RemoveOp
from emailctx.pipeline.types import (
    AcceptedAnswer,
    AcceptedOp,
    AnyOp,
    OpOutcome,
    QuestionView,
    ReqView,
    ValidationResult,
)
from emailctx.store.models import FINISHED_STATES
from emailctx.routing.embeddings import Embedder
from emailctx.textnorm import clean_requirement_text, normalize_match, normalize_requirement

JUDGE_SYSTEM = """\
You compare a proposed requirement with a project's existing requirements.

Set duplicate_of to the ID of the existing requirement that covers the same thing as \
the proposed text, or null if none does. Set same_meaning to true only if the proposed \
text requires exactly what that requirement already requires, just worded differently. \
If it adds, drops or changes anything (scope, values, formats, limits, deadlines), \
same_meaning is false. With duplicate_of null, same_meaning is false.

Set contradicts to true when the proposed text reverses or conflicts with that \
requirement (e.g. "CSV only" vs "no CSV, use PDF"; "90 days" vs "30 days"), and false \
when it only extends or refines it (e.g. "CSV" vs "CSV and XLSX")."""


def coerce_category(category: str | None, allowed: Sequence[str]) -> str:
    """Map to the configured category set; anything unknown becomes "other"."""
    if not category:
        return "other"
    c = category.strip().lower().replace("_", "-").replace(" ", "-")
    return c if c in allowed else "other"


def run_judge(llm: LLMClient, proposed: str, candidates: Sequence[ReqView]) -> JudgeResult:
    existing = "\n".join(f"{r.req_id}: {r.text}" for r in candidates)
    prompt = (
        f"<proposed>\n{proposed}\n</proposed>\n\n"
        f"<existing_requirements>\n{existing}\n</existing_requirements>"
    )
    return llm.structured("judge", JUDGE_SYSTEM, prompt, JudgeResult)


def validate_ops(
    ops: Sequence[AnyOp],
    *,
    source_text: str,
    msg_id: str,
    current: Sequence[ReqView],
    llm: LLMClient,
    embedder: Embedder,
    settings: Settings,
    source_date: datetime | None = None,
    questions: Sequence[QuestionView] = (),
) -> ValidationResult:
    result = ValidationResult()
    waiting = {q.question_id for q in questions}
    answered: set[str] = set()

    def is_stale(req: ReqView) -> bool:
        return source_date is not None and req.updated_at is not None and source_date < req.updated_at

    def keep(op: AcceptedOp, target: ReqView) -> None:
        (result.stale if is_stale(target) else result.accepted).append(op)
    source_norm = normalize_match(source_text)
    min_chars = settings.pipeline.min_evidence_chars
    active = {r.req_id: r for r in current}
    by_norm = {r.normalized_text: r.req_id for r in current}
    targeted: set[str] = set()
    added_norms: set[str] = set()

    def reject(op: AnyOp, reason: str) -> None:
        result.rejected.append(OpOutcome(op, "rejected", reason))

    def drop(op: AnyOp, reason: str) -> None:
        result.dropped.append(OpOutcome(op, "dropped", reason))

    for op in ops:
        if isinstance(op, NoneOp):
            continue

        evidence = normalize_match(op.evidence)
        if len(evidence) < min_chars:
            reject(op, f"evidence shorter than {min_chars} characters")
            continue
        if evidence not in source_norm:
            reject(op, "evidence not found in the email")
            continue

        if isinstance(op, AnswerOp):
            qid = op.question_id.strip().upper()
            answer = clean_requirement_text(op.answer)
            if qid not in waiting:
                reject(op, f"unknown or already answered question id {op.question_id}")
            elif qid in answered:
                drop(op, f"{qid} already answered by an earlier op in this email")
            elif not answer:
                reject(op, "empty answer")
            else:
                answered.add(qid)
                result.answers.append(AcceptedAnswer(qid, answer, op.evidence, msg_id))
            continue

        if isinstance(op, (ModifyOp, RemoveOp)):
            if op.req_id not in active:
                reject(op, f"unknown or removed requirement id {op.req_id}")
                continue
            if op.req_id in targeted:
                drop(op, f"{op.req_id} already changed by an earlier op in this email")
                continue

        if isinstance(op, RemoveOp):
            targeted.add(op.req_id)
            keep(AcceptedOp("remove", op.req_id, None, None, op.evidence, msg_id), active[op.req_id])
            continue

        text = clean_requirement_text(op.text)
        if not text:
            reject(op, "empty requirement text")
            continue
        norm = normalize_requirement(text)

        if isinstance(op, ModifyOp):
            cur = active[op.req_id]
            if norm == cur.normalized_text:
                drop(op, "rewording: normalized text unchanged")
                continue
            verdict = run_judge(llm, text, [cur])
            if verdict.same_meaning:
                drop(op, f"rewording of {cur.req_id} (judge)")
                continue
            category = coerce_category(op.category, settings.categories) if op.category else cur.category
            targeted.add(op.req_id)
            keep(
                AcceptedOp("modify", op.req_id, text, category, op.evidence, msg_id,
                           contradicts=verdict.contradicts),
                cur,
            )
            continue

        assert isinstance(op, AddOp)
        category = coerce_category(op.category, settings.categories)
        if norm in by_norm:
            drop(op, f"duplicate of {by_norm[norm]}: normalized text equal")
            continue
        if norm in added_norms:
            drop(op, "duplicate of an earlier add in this email")
            continue

        if current:
            candidates = embedder.top_k(
                text, list(current), [r.text for r in current], settings.pipeline.judge_top_k
            )
            verdict = run_judge(llm, text, candidates)
            dup = verdict.duplicate_of
            if dup is not None and dup in {c.req_id for c in candidates}:
                if verdict.same_meaning:
                    drop(op, f"duplicate of {dup} (judge)")
                    continue
                finished = active[dup].delivery in FINISHED_STATES and not verdict.contradicts
                if finished:  # new work on top of finished work: a new item
                    added_norms.add(norm)
                    result.accepted.append(
                        AcceptedOp("add", None, text, category, op.evidence, msg_id)
                    )
                    continue
                if dup in targeted:
                    drop(op, f"{dup} already changed by an earlier op in this email")
                    continue
                targeted.add(dup)
                keep(
                    AcceptedOp("modify", dup, text, category, op.evidence, msg_id, "add",
                               contradicts=verdict.contradicts),
                    active[dup],
                )
                continue

        added_norms.add(norm)
        result.accepted.append(AcceptedOp("add", None, text, category, op.evidence, msg_id))

    return result
