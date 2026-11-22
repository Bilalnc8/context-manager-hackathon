"""Coverage: every sentence of an email is captured by an op or skipped with a reason."""

from __future__ import annotations

import pytest
from test_tracker import Harness

from emailctx.llm import RecordedLLM
from emailctx.pipeline.coverage import split_sentences, unaccounted

NEW = {"duplicate_of": None, "same_meaning": False}


def test_split_sentences():
    text = (
        "Hi team,\n\n"
        "The export must support CSV. Reports should load in under 2\n"  # wrapped line
        "seconds! Can we add PDF?\n"
        "- **Date:** 2026-11-20\n"
        "- Sam: run tests by Dec 7; Ella: confirm attendees.\n"
        "## Action items\n"
        "--- Attachment: notes.pdf ---\n"
        "Pilot is on Dec. 5 for e.g. the West region.\n"
    )
    assert split_sentences(text) == [
        "Hi team,",
        "The export must support CSV.",
        "Reports should load in under 2 seconds!",
        "Can we add PDF?",
        "- **Date:** 2026-11-20",
        "- Sam: run tests by Dec 7; Ella: confirm attendees.",
        "## Action items",
        "Pilot is on Dec. 5 for e.g. the West region.",  # "Dec. 5" and "e.g. the" don't split
    ]


def test_unaccounted():
    text = "Hi team,\n\nWe need “CSV” export. Reports must load fast. Thanks for the demo today, it went well."
    # Short sentences ("Hi team,") are never checked; quotes match after normalization
    # (curly quotes), and a quote that spans two sentences covers both.
    assert unaccounted(text, ['We need "CSV" export.'], min_words=4) == [
        "Reports must load fast.", "Thanks for the demo today, it went well.",
    ]
    assert unaccounted(text, ["export. Reports must"], min_words=4) == [
        "Thanks for the demo today, it went well.",
    ]
    assert unaccounted(text, ["export. Reports must", "Thanks for the demo"], min_words=4) == []
    assert unaccounted(text, ["not in the email at all"], min_words=4) == [
        "We need “CSV” export.", "Reports must load fast.", "Thanks for the demo today, it went well.",
    ]


@pytest.fixture
def h(seeded) -> Harness:
    return Harness(seeded)


EMAIL = (
    "We also need XLSX export for finance. "
    "The weekly sync moves to Thursdays from next week. "
    "Please also make the report title editable by admins."
)


def test_missed_sentence_is_sent_back_then_reported(h):
    h.llm = RecordedLLM({
        "gate": [{"has_change": True, "reason": "New export format."}],
        "extract": [
            # First attempt: captures one sentence, forgets the other two.
            {"ops": [{"op": "add", "category": "functional", "text": "The export must support XLSX.",
                      "evidence": "We also need XLSX export for finance."}]},
            # Retry: skips the meeting logistics, still misses the editable title.
            {"ops": [{"op": "add", "category": "functional", "text": "The export must support XLSX.",
                      "evidence": "We also need XLSX export for finance."}],
             "skipped": [{"quote": "The weekly sync moves to Thursdays from next week.",
                          "reason": "meeting logistics"}]},
        ],
        "judge": [NEW, NEW],
    })
    got = h.call("add_client_input", company="acme", text=EMAIL)

    extract_prompts = [p for task, p in h.llm.calls if task == "extract"]
    assert len(extract_prompts) == 2
    feedback = extract_prompts[1]
    assert "got no op and are not in skipped" in feedback
    assert "- The weekly sync moves to Thursdays from next week." in feedback
    assert "- Please also make the report title editable by admins." in feedback

    assert "- REQ-003 add: The export must support XLSX." in got
    assert ('Read and deliberately skipped:\n'
            '- "The weekly sync moves to Thursdays from next week." (meeting logistics)') in got
    assert got.endswith('NOT captured and no reason given; check these and add them by hand '
                        '(set_item_status / log_question) or paste them again:\n'
                        '- "Please also make the report title editable by admins."')


def test_a_skipped_quote_must_be_verbatim(h):
    h.llm = RecordedLLM({
        "gate": [{"has_change": True, "reason": "x"}],
        "extract": [{"ops": [{"op": "none", "reason": "nothing new"}],
                     "skipped": [{"quote": "The demo went great", "reason": "pleasantry"}]}] * 2,
    })
    got = h.call("add_client_input", company="acme", text="Thanks, the demo went really well today.")
    assert 'NOT captured and no reason given' in got
    assert '- "Thanks, the demo went really well today."' in got
    assert "deliberately skipped" not in got and "pleasantry" not in got  # not reported as read


def test_every_sentence_accounted_for_needs_no_retry(h):
    h.llm = RecordedLLM({
        "gate": [{"has_change": True, "reason": "x"}],
        "extract": [{"ops": [{"op": "none"}],
                     "skipped": [{"quote": "Thanks, the demo went really well today.", "reason": "pleasantry"}]}],
    })
    got = h.call("add_client_input", company="acme", text="Thanks, the demo went really well today.")
    assert got.splitlines()[-1] == "Every sentence was captured or skipped with a reason."
    assert h.llm.remaining() == {}


def test_gated_out_input_says_why(h):
    h.llm = RecordedLLM({"gate": [{"has_change": False, "reason": "a thank-you note"}]})
    got = h.call("add_client_input", company="acme", text="Thanks for the call today, talk soon everyone.")
    assert got == "acme: gated out.\nNo changes or answers found in this input (a thank-you note)."
