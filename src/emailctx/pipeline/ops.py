"""Pydantic schemas for everything the LLM returns.

The LLM never writes markdown. It returns these objects; code validates and applies them.
`msg_id` is not part of the LLM output: code attaches the segment's msg_id, so the model
can't cite the wrong email.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class AddOp(BaseModel):
    op: Literal["add"]
    text: str = Field(description="The requirement, one self-contained sentence.")
    category: str
    evidence: str = Field(description="Exact quote from the email supporting this op.")


class ModifyOp(BaseModel):
    op: Literal["modify"]
    req_id: str
    text: str = Field(description="The full new text of the requirement.")
    category: str | None = None
    evidence: str


class RemoveOp(BaseModel):
    op: Literal["remove"]
    req_id: str
    evidence: str


class NoneOp(BaseModel):
    op: Literal["none"]
    reason: str = ""


Op = Annotated[AddOp | ModifyOp | RemoveOp | NoneOp, Field(discriminator="op")]


class ExtractResult(BaseModel):
    ops: list[Op]


class GateResult(BaseModel):
    has_change: bool
    reason: str = ""


class JudgeResult(BaseModel):
    duplicate_of: str | None = Field(
        default=None,
        description="ID of the existing requirement covering the same thing, or null.",
    )
    same_meaning: bool = Field(
        description="True if the proposed text means the same as duplicate_of's text."
    )
