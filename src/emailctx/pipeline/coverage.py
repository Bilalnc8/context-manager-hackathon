"""Coverage: nothing in an email is missed.

Extraction returns its ops plus `skipped`: every other sentence, quoted, with the reason
it changes nothing. Code then checks each sentence of the email (the same cleaned text
the evidence check uses) against the quotes:

- a sentence is accounted for when an op's evidence or a skipped quote overlaps it
  (both must be verbatim, after the evidence normalization);
- ops that were rejected don't count: their sentence was read but not captured;
- sentences with fewer than `coverage_min_words` words ("Hi team,", "Confirmed.") and
  attachment markers are not checked.

Whatever is left is `unaccounted`: sent back to extraction once (with the rejected ops,
within max_extract_retries) and reported with the result, so a person sees exactly which
sentences produced nothing and no reason.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from emailctx.textnorm import normalize_match

_ATTACHMENT_MARKER = re.compile(r"^-{3} Attachment: .* -{3}$")
_STARTS_ITEM = re.compile(r"^(?:[-*+•]\s|#{1,6}\s|\d+[.)]\s)")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[*_]*[A-Z0-9])")
_WORD = re.compile(r"[^\W_]+")
_ABBREVIATIONS = {
    "jan.", "feb.", "mar.", "apr.", "jun.", "jul.", "aug.", "sep.", "sept.", "oct.", "nov.",
    "dec.", "mon.", "tue.", "wed.", "thu.", "fri.", "sat.", "sun.", "mr.", "mrs.", "ms.",
    "dr.", "no.", "vs.", "etc.", "approx.", "inc.", "ltd.", "co.", "corp.", "st.",
}


def split_sentences(text: str) -> list[str]:
    """Sentences of an email body, in order. Wrapped lines are joined; list items,
    headings and paragraphs start new sentences."""
    pieces: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            pieces.append(" ".join(current))
            current.clear()

    for raw in text.splitlines():
        line = raw.strip()
        if not line or _ATTACHMENT_MARKER.match(line):
            flush()
            continue
        if _STARTS_ITEM.match(line):
            flush()
        current.append(line)
    flush()

    out: list[str] = []
    for piece in pieces:
        start = 0
        for m in _SENTENCE_END.finditer(piece):
            word = piece[:m.start()].rsplit(None, 1)[-1].lower().lstrip("(\"'*_")
            if word in _ABBREVIATIONS or re.fullmatch(r"(?:[a-z]\.)+", word):
                continue  # "Dec. 5", "e.g. the", "J. Smith"
            out.append(piece[start:m.start()].strip())
            start = m.end()
        out.append(piece[start:].strip())
    return [s for s in out if s]


def word_count(sentence: str) -> int:
    return len(_WORD.findall(sentence))


def _spans(haystack: str, needle: str) -> Iterable[tuple[int, int]]:
    start = haystack.find(needle)
    while start != -1:
        yield start, start + len(needle)
        start = haystack.find(needle, start + 1)


def unaccounted(
    text: str, quotes: Iterable[str], *, min_words: int, min_overlap: int = 5
) -> list[str]:
    """Sentences of `text` with at least `min_words` words that no quote overlaps."""
    sentences = split_sentences(text)
    normed = [normalize_match(s) for s in sentences]
    # One normalized string with known sentence offsets, so a quote that spans two
    # sentences covers both.
    offsets: list[tuple[int, int]] = []
    pos = 0
    for n in normed:
        offsets.append((pos, pos + len(n)))
        pos += len(n) + 1
    full = " ".join(normed)

    covered = [False] * len(sentences)
    for q in quotes:
        nq = normalize_match(q)
        if len(nq) < min_overlap:
            continue
        for qs, qe in _spans(full, nq):
            for i, (ss, se) in enumerate(offsets):
                if min(qe, se) - max(qs, ss) >= min(min_overlap, se - ss):
                    covered[i] = True
    return [
        s for s, ok in zip(sentences, covered, strict=True)
        if not ok and word_count(s) >= min_words
    ]


__all__ = ["split_sentences", "unaccounted", "word_count"]
