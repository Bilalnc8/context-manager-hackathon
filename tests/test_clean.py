from __future__ import annotations

from datetime import datetime
from email.message import EmailMessage

from conftest import EML

from emailctx.clean import (
    clean_body,
    html_to_text,
    paragraph_hashes,
    parse_eml,
    remove_seen_paragraphs,
)


def parse(name: str):
    return parse_eml((EML / f"{name}.eml").read_bytes())


def test_headers_and_ids():
    p = parse("atlas_03_reworded")
    assert p.msg_id == "atlas-003@acme.com"
    assert p.rfc_message_id == "atlas-003@acme.com"
    assert p.thread_root == "atlas-001@acme.com"
    assert p.references == ["atlas-001@acme.com", "atlas-002@ourstudio.io"]
    assert p.sender == "jane@acme.com"  # address, not display name
    assert p.to == ["sam@ourstudio.io"]
    assert p.date == datetime(2026, 11, 18, 8, 15)


def test_plain_signature_stripped():
    p = parse("atlas_01_initial")
    assert p.cleaned_text == (
        "Hi team,\n\nFor Atlas, the export must support CSV. "
        "Reports should load in under 2 seconds."
    )


def test_gmail_quote_with_wrapped_attribution_stripped():
    p = parse("atlas_02_reply_quote")
    assert p.cleaned_text == "Sounds good, we'll plan around that."


def test_quoted_printable_decoded_and_quote_stripped():
    p = parse("atlas_04_real_change")
    assert p.cleaned_text == (
        "One change after talking to finance: we also need XLSX export alongside CSV."
    )


def test_mobile_footer_stripped():
    assert parse("atlas_05_meeting").cleaned_text == (
        "Can we meet Thursday to go over the export design?"
    )


def test_gmail_html_quote_and_signature_stripped():
    p = parse("gmail_html_reply")
    assert p.cleaned_text == "The dashboard must show weekly totals."


def test_outlook_plain_header_block_stripped():
    p = parse("outlook_plain_reply")
    assert p.cleaned_text == "Login must support SSO via Okta."


def test_outlook_html_reply_stripped():
    p = parse("outlook_html_reply")
    assert p.cleaned_text == "Audit logs must be kept for 90 days."


def test_explicit_forward_kept_and_missing_message_id_hashed():
    p = parse("forward_no_message_id")
    assert "Deleted projects must be recoverable for 30 days." in p.cleaned_text
    assert p.rfc_message_id is None
    assert p.msg_id.startswith("sha256:") and len(p.msg_id) == len("sha256:") + 64
    assert parse("forward_no_message_id").msg_id == p.msg_id  # stable


def test_base64_utf8_body_decoded():
    msg = EmailMessage()
    msg["From"] = "jane@acme.com"
    msg["Message-ID"] = "<b64@acme.com>"
    msg.set_content("Le rapport doit être exporté en “CSV” — chaque nuit.\n", cte="base64")
    p = parse_eml(msg.as_bytes())
    assert p.cleaned_text == "Le rapport doit être exporté en “CSV” — chaque nuit."


def test_signoff_only_cut_when_tail_looks_like_signature():
    body = "Thanks!\n\nWe also need audit logs.\n\nBest,\nJane"
    assert clean_body(body) == "Thanks!\n\nWe also need audit logs."
    long_tail = "Cheers\n" + "\n".join(f"Requirement line {i} is long enough" for i in range(8))
    assert clean_body(long_tail) == long_tail  # 8 lines after: not a signature


def test_rfc_signature_delimiter():
    assert clean_body("Use Postgres.\n-- \nJane\nAcme") == "Use Postgres."


def test_html_to_text_keeps_inline_text_on_one_line():
    html = "<p>We need <b>CSV</b>\n   and <i>XLSX</i>.</p><ul><li>one</li><li>two</li></ul>"
    text = clean_body(html_to_text(html))
    assert text.splitlines()[0] == "We need CSV and XLSX."
    assert "- one" in text and "- two" in text


def test_remove_seen_paragraphs_by_hash():
    seen = paragraph_hashes("Hi,\n\nThe export must support CSV.")
    assert all(len(h) == 64 for h in seen)  # hashes, not text
    text = "New: add XLSX.\n\nthe export must support csv"
    assert remove_seen_paragraphs(text, seen) == "New: add XLSX."
    assert remove_seen_paragraphs(text, set()) == text
