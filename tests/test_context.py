"""Fetching a company's .md from Drive by name: lookup and the `emailctx context` CLI.

The MCP server works on the requirements store instead: see test_tracker.py."""

from __future__ import annotations


import pytest
from fakes import FakeDrive
from typer.testing import CliRunner

from emailctx.context import CompanyNotFound, fetch_context, find_company, list_company_files
from emailctx.drive import DrivePublisher, DriveStore

ROOT = "Client Context"
COMPANIES = {"acme": "Acme Corp", "globex": "Globex", "globe-tech": "Globe Tech"}


@pytest.fixture
def store():
    drive = FakeDrive()
    s = DriveStore(drive, ROOT)
    pub = DrivePublisher(s, COMPANIES.__getitem__)
    for cid, name in COMPANIES.items():
        pub.publish(cid, f"# {name}: requirements\n\n- **REQ-001** Something for {name}.\n")
    s.fake = drive
    return s


# --- Lookup ----------------------------------------------------------------------------------


@pytest.mark.parametrize("query, expected", [
    ("Acme Corp", "acme"),        # exact name
    ("acme corp.md", "acme"),     # case and .md don't matter
    ("  ACME   corp ", "acme"),   # extra spaces
    ("acme", "acme"),             # id / unique prefix
    ("corp", "acme"),             # unique part of the name
    ("globex", "globex"),         # exact id wins over the "glob" prefix shared with Globe Tech
    ("globe tech", "globe-tech"),
])
def test_find_company(store, query, expected):
    assert find_company(list_company_files(store), query).company_id == expected


def test_ambiguous_and_unknown_names_explain_themselves(store):
    files = list_company_files(store)
    with pytest.raises(CompanyNotFound, match="several companies: Globe Tech, Globex"):
        find_company(files, "glob")
    with pytest.raises(CompanyNotFound, match="Companies with a file: Acme Corp, Globe Tech, Globex"):
        find_company(files, "initech")


def test_fetch_context_reads_the_file(store):
    found, text = fetch_context(store, "acme")
    assert found.name == "Acme Corp"
    assert text == "# Acme Corp: requirements\n\n- **REQ-001** Something for Acme Corp.\n"


def test_trashed_files_are_not_offered(store):
    store.fake.trash(f"{ROOT}/Globex/Globex.md")
    assert [c.name for c in list_company_files(store)] == ["Acme Corp", "Globe Tech"]


# --- CLI ---------------------------------------------------------------------------------------


def test_context_cli(store, monkeypatch):
    import emailctx.cli
    from emailctx.cli import app

    monkeypatch.setattr(emailctx.cli, "_drive_store", lambda settings: store)
    runner = CliRunner()
    shown = runner.invoke(app, ["context", "acme"])
    assert shown.exit_code == 0
    assert shown.output.startswith("# Acme Corp: requirements")

    listed = runner.invoke(app, ["context"])
    assert listed.output.splitlines()[0].startswith("Acme Corp  (updated 2026-11-21)")

    missing = runner.invoke(app, ["context", "initech"])
    assert missing.exit_code == 1


# --- MCP server ---------------------------------------------------------------------------------

