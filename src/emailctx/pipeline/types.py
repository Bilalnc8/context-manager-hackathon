"""Plain data passed between pipeline steps."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from emailctx.pipeline.ops import AddOp, ModifyOp, NoneOp, RemoveOp

AnyOp = AddOp | ModifyOp | RemoveOp | NoneOp


@dataclass(frozen=True)
class ReqView:
    """An active requirement as the pipeline sees it."""

    req_id: str
    text: str
    normalized_text: str
    category: str


@dataclass(frozen=True)
class AcceptedOp:
    kind: Literal["add", "modify", "remove"]
    req_id: str | None  # None for add; assigned on apply
    text: str | None
    category: str | None
    evidence: str  # as written by the LLM; validated in memory, never persisted
    msg_id: str
    converted_from: str | None = None  # "add" when a near-duplicate add became a modify


@dataclass(frozen=True)
class OpOutcome:
    op: AnyOp
    status: Literal["dropped", "rejected"]
    reason: str


@dataclass
class ValidationResult:
    accepted: list[AcceptedOp] = field(default_factory=list)
    dropped: list[OpOutcome] = field(default_factory=list)  # valid but no change (reword, dup)
    rejected: list[OpOutcome] = field(default_factory=list)  # invalid (evidence, unknown id)


@dataclass(frozen=True)
class AppliedChange:
    req_id: str
    op: Literal["add", "modify", "remove"]
    old_text: str | None
    new_text: str | None
