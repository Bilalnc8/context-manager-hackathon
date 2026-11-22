"""Drive storage, email copies, PDF attachments, meeting bots, and building a company's
first .md from recent mail."""

from __future__ import annotations

import json
from email.message import EmailMessage

import pytest
from fakes import FakeDrive, FakeGmail

from emailctx.bootstrap import auto_query, company_query
from emailctx.clean import attachment_text, email_copy_text, parse_eml
from emailctx.config import DriveSettings, GoogleSettings, OutputSettings
from emailctx.drive import DriveArchiver, DrivePublisher, DriveStore
from emailctx.ingest import Runtime, ingest_eml
from emailctx.llm import RecordedLLM
from emailctx.registry import RegistrySpec, load_registry
from emailctx.review import assign, list_open
from emailctx.sources.gmail_sync import GmailSync, SyncError
from emailctx.store.models import Company, SyncState

ROOT = "Client Context"
REGISTRY = {
    "own_domains": ["ourstudio.io"],
    "companies": [
        {"id": "acme", "name": "Acme", "domains": ["acme.com"], "contacts": ["jane.acme@gmail.com"],
         "projects": [{"id": "atlas", "name": "Atlas", "keywords": ["PO-4471"]}]},
        {"id": "globex", "name": "Globex", "domains": ["globex.com"],
         "projects": [{"id": "gx-portal", "name": "Globex Portal"}]},
    ],
}


# --- Helpers ----------------------------------------------------------------------------------


def make_pdf(text: str) -> bytes:
    """A minimal one-page PDF with a single line of Helvetica text."""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def mail(n: int, frm: str, to=("sam@ourstudio.io",), cc=(), subject: str | None = None,
         body: str = "Quick update.", pdf: tuple[str, str] | None = None) -> bytes:
    msg = EmailMessage()
    msg["From"] = frm
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject or f"Update {n}"
    msg["Date"] = f"Sun, {n:02d} Nov 2026 09:00:00 +0000"
    msg["Message-ID"] = f"<d{n}@test>"
    msg.set_content(body)
    if pdf:
        name, text = pdf
        msg.add_attachment(make_pdf(text), maintype="application", subtype="pdf", filename=name)
    return msg.as_bytes()


def quiet(n: int = 50) -> RecordedLLM:
    return RecordedLLM({"gate": [{"has_change": False, "reason": "test"}] * n})


@pytest.fixture
def denv(env):
    """Registry loaded, Drive output on (fake Drive), local output off."""
    with env.sessions() as s:
        load_registry(s, RegistrySpec.model_validate(REGISTRY))
        s.commit()
    env.settings = env.settings.model_copy(
        update={"output": OutputSettings(local_dir=None, drive=DriveSettings(enabled=True, root_folder=ROOT))}
    )
    env.drive = FakeDrive()
    env.store = DriveStore(env.drive, ROOT)
    return env


def rt(env, llm=None) -> Runtime:
    return Runtime(env.settings, env.sessions, llm or quiet(), drive=env.store)


def ingest(env, raw: bytes, llm=None, **kw):
    return ingest_eml(raw, settings=env.settings, sessions=env.sessions, llm=llm or quiet(),
                      drive=env.store, **kw)


# --- Drive store ------------------------------------------------------------------------------


def test_publisher_creates_folder_and_updates_md_in_place(denv):
    pub = DrivePublisher(denv.store, lambda cid: {"acme": "Acme"}[cid])
    first = pub.publish("acme", "# Acme\n")
    assert first.written and first.destination == f"drive:{ROOT}/Acme/Acme.md"
    tree = denv.drive.tree()
    assert set(tree) == {ROOT, f"{ROOT}/Acme", f"{ROOT}/Acme/Acme.md"}
    md_id = tree[f"{ROOT}/Acme/Acme.md"]["id"]

    denv.drive.calls.clear()
    assert not pub.publish("acme", "# Acme\n").written
    assert denv.drive.calls["update"] == 0 and denv.drive.calls["create"] == 0

    assert pub.publish("acme", "# Acme v2\n").written
    assert denv.drive.tree()[f"{ROOT}/Acme/Acme.md"]["id"] == md_id  # same file, new revision
    assert denv.drive.read(f"{ROOT}/Acme/Acme.md") == b"# Acme v2\n"


def test_renamed_company_renames_folder_and_file(denv):
    names = {"acme": "Acme"}
    pub = DrivePublisher(denv.store, lambda cid: names[cid])
    pub.publish("acme", "# x\n")
    names["acme"] = "Acme Corp"
    pub.publish("acme", "# x\n")
    assert f"{ROOT}/Acme Corp/Acme Corp.md" in denv.drive.tree()
    assert len([p for p in denv.drive.tree() if p.endswith(".md")]) == 1


def test_trashed_md_counts_as_missing(denv):
    DrivePublisher(denv.store, lambda cid: "Acme").publish("acme", "# x\n")
    assert denv.store.has_context("acme")
    denv.drive.trash(f"{ROOT}/Acme/Acme.md")
    assert not denv.store.has_context("acme")


def test_archiver_stores_email_copy_and_attachments_once(denv):
    parsed = parse_eml(mail(3, "jane@acme.com", subject="Kickoff notes",
                            pdf=("Meeting summary.pdf", "Export must support XLSX.")))
    arch = DriveArchiver(denv.store)
    first = arch.archive("acme", "Acme", parsed)
    assert (first.email_written, first.attachments_written) == (True, 1)
    tree = denv.drive.tree()
    email_path = f"{ROOT}/Acme/Emails/2026-11-03 Kickoff notes.txt"
    assert email_path in tree
    assert f"{ROOT}/Acme/Documents/2026-11-03 Meeting summary.pdf" in tree
    text = denv.drive.read(email_path).decode()
    assert text.startswith("From: jane@acme.com\nTo: sam@ourstudio.io\nDate: 2026-11-03 09:00 UTC")
    assert "Attachments: Meeting summary.pdf" in text and "Quick update." in text

    again = arch.archive("acme", "Acme", parsed)
    assert (again.email_written, again.attachments_written) == (False, 0)
    assert len(denv.drive.tree()) == len(tree)


# --- Attachments ------------------------------------------------------------------------------


def test_pdf_text_is_extracted():
    parsed = parse_eml(mail(1, "x@acme.com", pdf=("s.pdf", "Reports must load in 2 seconds.")))
    [att] = parsed.attachments
    assert att.filename == "s.pdf" and att.content_type == "application/pdf"
    assert "Reports must load in 2 seconds." in attachment_text(att)


def test_requirement_from_meeting_summary_pdf(denv):
    raw = mail(5, "jane@acme.com", subject="Notes from today",
               body="Summary attached.", pdf=("Weekly sync.pdf", "Decision: the export must support XLSX."))
    llm = RecordedLLM({
        "gate": [{"has_change": True}],
        "extract": [{"ops": [{"op": "add", "text": "The export must support XLSX.",
                              "category": "functional", "evidence": "the export must support XLSX"}]}],
    })
    result = ingest(denv, raw, llm)
    assert result.outcome == "applied"
    assert "--- Attachment: Weekly sync.pdf ---" in llm.calls[1][1]  # extraction saw the PDF text
    md = denv.drive.read(f"{ROOT}/Acme/Acme.md").decode()
    assert "- **REQ-001** The export must support XLSX.[^1]" in md
    assert '[^1]: Email "Notes from today", 2026-11-05, ID `d5@test`, attachments: Weekly sync.pdf' in md
    assert f"{ROOT}/Acme/Documents/2026-11-05 Weekly sync.pdf" in denv.drive.tree()


def test_email_copies_for_every_company_email_but_not_review_items(denv):
    ingest(denv, mail(1, "jane@acme.com"))  # gated out: still a company email, still copied
    queued = ingest(denv, mail(2, "ceo@newclient.io"))
    assert queued.outcome == "review"
    paths = set(denv.drive.tree())
    assert f"{ROOT}/Acme/Emails/2026-11-01 Update 1.txt" in paths
    assert not any("Update 2" in p for p in paths)  # company unknown: nowhere to put it yet

    with denv.sessions() as s:
        [item] = list_open(s)
    assign(item.id, None, rt(denv), company_id="globex", reader=lambda ref: mail(2, "ceo@newclient.io"))
    assert f"{ROOT}/Globex/Emails/2026-11-02 Update 2.txt" in denv.drive.tree()


# --- Meeting bots -------------------------------------------------------------------------------


def test_meeting_bot_email_routes_by_company_name(denv):
    raw = mail(4, "notes@fireflies.ai", subject="Recap: Acme weekly sync",
               pdf=("Summary.pdf", "Attendees from Acme agreed on the export."))
    result = ingest(denv, raw)
    assert (result.route.step, result.project_id) == ("meeting_bot", "atlas")
    with denv.sessions() as s:
        assert s.get(Company, "fireflies.ai") is None  # a bot is never a pending company


def test_meeting_bot_email_matches_names_inside_the_pdf(denv):
    raw = mail(4, "recap@otter.ai", subject="Your meeting notes",
               pdf=("notes.pdf", "Discussed the Globex Portal rollout."))
    result = ingest(denv, raw)
    assert (result.route.step, result.project_id) == ("meeting_bot", "gx-portal")


def test_meeting_bot_email_naming_no_company_is_not_a_new_company(denv):
    result = ingest(denv, mail(4, "notes@fireflies.ai", subject="Recap: internal standup"))
    assert result.outcome == "review"
    assert result.route.review_kind == "project"  # not "new_company"
    assert result.route.pending_companies == []
    with denv.sessions() as s:
        assert s.get(Company, "fireflies.ai") is None


def test_meeting_bot_email_naming_two_companies_goes_to_review(denv):
    result = ingest(denv, mail(4, "notes@fireflies.ai", subject="Acme and Globex joint call"))
    assert result.outcome == "review"
    assert "several companies" in result.route.reason


# --- Queries --------------------------------------------------------------------------------------


def test_company_query(denv):
    with denv.sessions() as s:
        q = company_query(s, "acme", denv.settings)
        auto = auto_query(s, denv.settings)
    assert q.startswith("newer_than:100d (")
    for term in ("from:acme.com", "to:acme.com", "cc:acme.com", "from:jane.acme@gmail.com",
                 'subject:"PO-4471"', '"Acme"', '"Atlas"', "fireflies.ai"):
        assert term in q, term
    assert "newer_than" not in auto and "from:globex.com" in auto and "from:acme.com" in auto


# --- Building a company's first file -------------------------------------------------------------


def sync(env, gmail, llm=None):
    return GmailSync(gmail, rt(env, llm)).run()


def mailbox() -> FakeGmail:
    g = FakeGmail()
    g.add("a1", mail(1, "jane@acme.com"))
    g.add("x1", mail(2, "ops@globex.com", cc=["jane@acme.com"]))  # matches acme's query, is globex's
    g.add("a2", mail(3, "notes@fireflies.ai", subject="Recap: Acme sync",
                     pdf=("Acme recap.pdf", "Acme wants weekly exports.")))
    g.add("n1", mail(4, "news@shop.example"))  # nobody's
    return g


def test_first_sync_builds_every_company_file(denv):
    g = mailbox()
    report = sync(denv, g)
    assert report.mode == "bootstrap"
    built = {b.company_id: b for b in report.bootstraps}
    assert set(built) == {"acme", "globex"}
    assert [gid for gid, r in built["acme"].results if r.status == "processed"] == ["a1", "a2"]
    assert built["acme"].other_company == 1  # x1 left for globex...
    assert [gid for gid, r in built["globex"].results if r.status == "processed"] == ["x1"]  # ...which took it

    tree = denv.drive.tree()
    assert f"{ROOT}/Acme/Acme.md" in tree and f"{ROOT}/Globex/Globex.md" in tree
    assert "_No requirements yet._" in denv.drive.read(f"{ROOT}/Acme/Acme.md").decode()
    assert f"{ROOT}/Acme/Emails/2026-11-01 Update 1.txt" in tree
    assert f"{ROOT}/Acme/Documents/2026-11-03 Acme recap.pdf" in tree
    assert f"{ROOT}/Globex/Emails/2026-11-02 Update 2.txt" in tree
    assert not any("Update 4" in p for p in tree)  # unrelated mail is never fetched
    with denv.sessions() as s:
        assert s.get(SyncState, "sam@ourstudio.io").history_id == str(g.history_id)


def test_later_syncs_only_process_new_mail(denv):
    g = mailbox()
    sync(denv, g)
    g.add("a3", mail(5, "jane@acme.com", subject="New info"))
    g.calls.clear()
    report = sync(denv, g)
    assert (report.mode, report.bootstraps) == ("incremental", [])
    assert [gid for gid, _ in report.results] == ["a3"]
    assert f"{ROOT}/Acme/Emails/2026-11-05 New info.txt" in denv.drive.tree()


def test_deleted_md_is_rebuilt_without_reprocessing(denv):
    g = mailbox()
    sync(denv, g)
    denv.drive.trash(f"{ROOT}/Acme/Acme.md")
    g.calls.clear()
    report = sync(denv, g, llm=RecordedLLM({}))  # no LLM calls expected
    [b] = report.bootstraps
    # a1, a2 and globex's x1 (acme in Cc) all matched acme's search and are all known.
    assert b.company_id == "acme" and b.known == 3 and b.results == []
    assert g.calls["get"] == 0
    assert f"{ROOT}/Acme/Acme.md" in denv.drive.tree()


def test_bootstrap_failure_resumes_next_run(denv):
    g = mailbox()
    with pytest.raises(SyncError, match="building acme's file"):
        sync(denv, g, llm=quiet(1))  # second email has no gate response
    with denv.sessions() as s:
        assert s.get(SyncState, "sam@ourstudio.io") is None
    report = sync(denv, g)
    acme = next(b for b in report.bootstraps if b.company_id == "acme")
    assert acme.known == 1
    assert f"{ROOT}/Acme/Acme.md" in denv.drive.tree()


def test_auto_query_needs_a_registry(env):
    with pytest.raises(SyncError, match="no companies are registered"):
        GmailSync(FakeGmail(), Runtime(env.settings, env.sessions, quiet())).run()


# --- Scopes and copy format ------------------------------------------------------------------------


def test_drive_file_is_the_only_write_scope_allowed():
    GoogleSettings(scopes=["https://www.googleapis.com/auth/gmail.readonly",
                           "https://www.googleapis.com/auth/drive.file"])
    with pytest.raises(ValueError, match="read-only"):
        GoogleSettings(scopes=["https://www.googleapis.com/auth/drive"])


def test_email_copy_text_has_headers_then_full_body():
    parsed = parse_eml(mail(7, "jane@acme.com", cc=["ops@globex.com"], body="Line one.\n\n> quoted"))
    assert email_copy_text(parsed) == (
        "From: jane@acme.com\nTo: sam@ourstudio.io\nCc: ops@globex.com\n"
        "Date: 2026-11-07 09:00 UTC\nSubject: Update 7\nMessage-ID: <d7@test>\n\n"
        "Line one.\n\n> quoted\n"
    )


def test_db_still_holds_no_email_bodies(denv):
    ingest(denv, mail(1, "jane@acme.com", body="Secret pricing: 40k per seat."))
    assert b"Secret pricing" not in denv.settings.db_path.read_bytes()
    assert b"Secret pricing" in denv.drive.read(f"{ROOT}/Acme/Emails/2026-11-01 Update 1.txt")
