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
