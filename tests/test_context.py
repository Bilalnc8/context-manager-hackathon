"""Fetching a company's .md from Drive by name: lookup, CLI, and the MCP server."""

from __future__ import annotations

import asyncio

import pytest
from fakes import FakeDrive
from typer.testing import CliRunner

from emailctx.auth import AuthError
from emailctx.context import CompanyNotFound, fetch_context, find_company, list_company_files
from emailctx.drive import DrivePublisher, DriveStore
from emailctx.mcp_server import build_server

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


def text_of(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


def test_mcp_server_tools(store):
    server = build_server(lambda: store)
    tools = {t.name for t in asyncio.run(server.list_tools())}
    assert {"list_companies", "get_company_context"} <= tools

    got = text_of(asyncio.run(server.call_tool("get_company_context", {"company": "acme"})))
    assert got.startswith("<!-- Acme Corp.md from Google Drive, last updated 2026-11-21")
    assert "- **REQ-001** Something for Acme Corp." in got

    listed = text_of(asyncio.run(server.call_tool("list_companies", {})))
    assert listed.splitlines() == [
        "- Acme Corp (updated 2026-11-21)", "- Globe Tech (updated 2026-11-21)",
        "- Globex (updated 2026-11-21)",
    ]
    unknown = text_of(asyncio.run(server.call_tool("get_company_context", {"company": "nope"})))
    assert "no company file matches 'nope'" in unknown


def test_mcp_prompt_loads_a_company(store):
    server = build_server(lambda: store)
    result = asyncio.run(server.get_prompt("company", {"name": "Acme"}))
    text = "".join(getattr(m.content, "text", "") for m in result.messages)
    assert "get_company_context" in text and "'Acme'" in text


def test_mcp_server_starts_signed_out_and_says_what_to_do():
    calls = []

    def not_signed_in():
        calls.append(1)
        raise AuthError("not signed in to Google: run `emailctx auth`")

    server = build_server(not_signed_in)
    assert calls == []  # building the server doesn't touch Google
    got = text_of(asyncio.run(server.call_tool("get_company_context", {"company": "acme"})))
    assert got == "Could not reach Google Drive: not signed in to Google: run `emailctx auth`"
