"""Text normalization shared by the evidence check and the reword check."""

from __future__ import annotations

import re
import unicodedata

_ZERO_WIDTH = dict.fromkeys(
    map(ord, "​‌‍‎‏⁠﻿­"), None
)

_ASCII_PUNCT = str.maketrans(
    {
        # single quotes / apostrophes / primes
        "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
        # double quotes
        "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
        "«": '"', "»": '"',
        # hyphens and dashes
        "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
        "―": "-", "−": "-",
    }
)

_WS = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^\w\s]")


def normalize_match(text: str) -> str:
    """Normalization for the evidence substring check. Case-sensitive.

    NFKC, strip zero-width characters, map curly quotes and dashes to ASCII,
    collapse all whitespace (including newlines) to single spaces.
    """
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_ZERO_WIDTH).translate(_ASCII_PUNCT)
    return _WS.sub(" ", text).strip()


def normalize_requirement(text: str) -> str:
    """Normalization for the cheap "unchanged text" check.

    Everything `normalize_match` does, plus casefolding and dropping punctuation, so
    "Export must support CSV." and "export must support csv" compare equal.
    """
    text = normalize_match(text).casefold()
    text = _NON_WORD.sub(" ", text)
    return _WS.sub(" ", text).strip()


def clean_requirement_text(text: str) -> str:
    """Canonical stored form of requirement text: one line, trimmed."""
    return _WS.sub(" ", text).strip()


def evidence_in_source(evidence: str, source: str) -> bool:
    needle = normalize_match(evidence)
    return bool(needle) and needle in normalize_match(source)
