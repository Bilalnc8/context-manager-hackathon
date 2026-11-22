from __future__ import annotations

from conftest import EML, LLM
from typer.testing import CliRunner

import emailctx.llm
from emailctx.cli import app
from emailctx.llm import RecordedLLM


def test_ingest_file_cli(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        "db_path: state/emailctx.db\noutput:\n  local_dir: out\n", encoding="utf-8"
    )
    # One LLM client per invocation: missing-company run, first ingest, re-ingest.
    recordings = iter([None, "atlas_01_initial", None])

    def fake_llm(settings):
        name = next(recordings)
        return RecordedLLM.from_file(LLM / f"{name}.json") if name else RecordedLLM({})

    monkeypatch.setattr(emailctx.llm, "AnthropicLLM", fake_llm)
    runner = CliRunner()
    eml = str(EML / "atlas_01_initial.eml")
    base = ["ingest-file", eml, "--project", "atlas", "--config", str(config)]

    missing_company = runner.invoke(app, base)
    assert missing_company.exit_code == 2
    assert "give its company" in missing_company.output

    first = runner.invoke(app, [*base, "--company", "acme"])
    assert first.exit_code == 0, first.output
    assert "outcome: applied" in first.output
    assert "applied  add    REQ-001" in first.output
    md = tmp_path / "out" / "acme" / "context.md"
    assert f"wrote {md}" in first.output
    assert (tmp_path / "state" / "emailctx.db").exists()  # paths resolve against config dir

    second = runner.invoke(app, base)  # known project: no --company needed
    assert second.exit_code == 0, second.output
    assert "outcome: already_processed" in second.output

    rendered = runner.invoke(app, ["render", "--company", "acme", "--config", str(config)])
    assert rendered.exit_code == 0 and f"unchanged {md}" in rendered.output


def test_routing_review_and_conflict_commands(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("db_path: state.db\noutput:\n  local_dir: out\n", encoding="utf-8")
    registry = tmp_path / "registry.yaml"
    registry.write_text(  # acme's domain isn't listed yet: its mail is an unknown company
        "own_domains: [ourstudio.io]\n"
        "companies:\n"
        "  - id: acme\n"
        "    projects:\n"
        "      - id: atlas\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(emailctx.llm, "AnthropicLLM", lambda settings: RecordedLLM(
        {"gate": [{"has_change": False}] * 5}
    ))
    runner = CliRunner()
    cfg = ["--config", str(config)]

    loaded = runner.invoke(app, ["registry", "load", str(registry), *cfg])
    assert loaded.exit_code == 0, loaded.output
    assert "companies 1" in loaded.output and "projects 1" in loaded.output

    # jane@acme.com: acme.com is an unknown domain, so the email waits for review.
    routed = runner.invoke(app, ["ingest-file", str(EML / "atlas_01_initial.eml"), *cfg])
    assert routed.exit_code == 0, routed.output
    assert "outcome: review" in routed.output and "queued for review as item 1" in routed.output

    listed = runner.invoke(app, ["review", "list", *cfg])
    assert "[1] 2026-11-16  Atlas export requirements" in listed.output
    assert "pending companies: acme.com" in listed.output

    no_target = runner.invoke(app, ["review", "assign", "1", *cfg])
    assert no_target.exit_code == 2 and "--company" in no_target.output

    assigned = runner.invoke(app, ["review", "assign", "1", "--company", "acme", *cfg])
    assert assigned.exit_code == 0, assigned.output
    assert "route:   assigned via manual -> atlas" in assigned.output

    empty = runner.invoke(app, ["review", "list", *cfg])
    assert "review queue is empty" in empty.output
    again = runner.invoke(app, ["review", "assign", "1", "--company", "acme", *cfg])
    assert again.exit_code == 2 and "already assigned" in again.output

    # acme.com now belongs to acme: the next acme email routes by domain on its own.
    nxt = runner.invoke(app, ["ingest-file", str(EML / "atlas_05_meeting.eml"), *cfg])
    assert "route:   assigned via company -> atlas" in nxt.output

    conflicts = runner.invoke(app, ["conflicts", "list", *cfg])
    assert "nothing needs confirmation" in conflicts.output
