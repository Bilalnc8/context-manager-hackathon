"""Embedder interface.

Only the no-op prefilter exists: every requirement is a candidate for the dedupe judge.
The interface stays so a real embedder (e.g. sentence-transformers) can narrow the
judge's input to the top-k most similar requirements if projects grow large.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, TypeVar

C = TypeVar("C")


class Embedder(Protocol):
    def top_k(self, query: str, candidates: Sequence[C], texts: Sequence[str], k: int) -> list[C]:
        """Return up to k candidates most similar to `query`. texts[i] describes candidates[i]."""
        ...


class NoOpEmbedder:
    """No prefilter: returns the first k candidates in their given order."""

    def top_k(self, query: str, candidates: Sequence[C], texts: Sequence[str], k: int) -> list[C]:
        return list(candidates[:k])
