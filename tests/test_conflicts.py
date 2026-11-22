"""Phase 5: conflicts under "Needs confirmation", latest-wins by email date, git history."""

from __future__ import annotations

import subprocess
from email.message import EmailMessage
from pathlib import Path

import pytest
from conftest import FakeS3

from emailctx.config import OutputSettings, S3Settings
from emailctx.conflicts import ConflictError, confirm, list_open
from emailctx.ingest import Runtime, ingest_file
from emailctx.llm import RecordedLLM
from emailctx.render.publish import publishers_from_settings


def mail(tmp_path: Path, name: str, date: str, body: str, subject="Re: Atlas export requirements") -> Path:
    msg = EmailMessage()
    msg["From"] = "jane@acme.com"
    msg["To"] = "sam@ourstudio.io"
    msg["Subject"] = subject
    msg["Date"] = date
    msg["Message-ID"] = f"<{name}@acme.com>"
    msg.set_content(body)
    path = tmp_path / f"{name}.eml"
    path.write_bytes(msg.as_bytes())
    return path


def run(env, path: Path, llm: dict | None, publishers=None):
    return ingest_file(path, "atlas", settings=env.settings, sessions=env.sessions,
                       llm=RecordedLLM(llm or {}), publishers=publishers)


def modify(evidence: str, text: str, contradicts: bool) -> dict:
    return {
        "gate": [{"has_change": True}],
        "extract": [{"ops": [{"op": "modify", "req_id": "REQ-001", "text": text, "evidence": evidence}]}],
        "judge": [{"duplicate_of": "REQ-001", "same_meaning": False, "contradicts": contradicts}],
    }


def section(md: str, title: str) -> str:
    if f"## {title}" not in md:
        return ""
    return md.split(f"## {title}", 1)[1].split("\n## ", 1)[0]


# --- Contradictions -----------------------------------------------------------------------


def test_contradiction_applies_latest_and_flags_it(seeded, tmp_path):
    path = mail(tmp_path, "pdf", "Fri, 20 Nov 2026 10:00:00 +0000",
                "Change of plan: drop CSV, the export must be PDF only.")
    result = run(seeded, path, modify("drop CSV, the export must be PDF only",
                                      "The export must be PDF only.", contradicts=True))
    assert result.outcome == "applied"
    assert seeded.requirements()[0].text == "The export must be PDF only."  # latest wins

    md = seeded.md_bytes().decode()
    needs = section(md, "Needs confirmation")
    assert ('**C1** · **REQ-001**: now "The export must be PDF only."[^2], '
            'which contradicts the earlier "The export must support CSV."[^1]. The newer email won.') in needs
    assert md.index("## Needs confirmation") < md.index("## Changelog")

    with seeded.sessions() as s:
        [c] = list_open(s)
    assert (c.kind, c.other_source_id, c.kept_source_id) == ("contradiction", "atlas-001@acme.com", "pdf@acme.com")

    confirm(c.conflict_id, Runtime(seeded.settings, seeded.sessions, llm=None))
    md2 = seeded.md_bytes().decode()
    assert "Needs confirmation" not in md2
    assert seeded.requirements()[0].text == "The export must be PDF only."


def test_extension_is_not_a_conflict(seeded):
    result, _ = seeded.ingest("atlas_04_real_change", "atlas_04_real_change")
    assert result.outcome == "applied"
    assert "Needs confirmation" not in seeded.md_bytes().decode()
    with seeded.sessions() as s:
        assert list_open(s) == []


# --- Latest wins by email date (out-of-order emails) ----------------------------------------


def test_older_email_does_not_overwrite_newer_change(seeded, tmp_path):
    seeded.ingest("atlas_04_real_change", "atlas_04_real_change")  # 2026-11-19: CSV and XLSX
    before = seeded.requirements()[0]

    late = mail(tmp_path, "json", "Tue, 17 Nov 2026 12:00:00 +0000",  # older, arrives later
                "We need the export to support CSV and JSON.")
    result = run(seeded, late, modify("export to support CSV and JSON",
                                      "The export must support CSV and JSON.", contradicts=False))
    assert result.outcome == "flagged"
    assert result.applied == []
    after = seeded.requirements()[0]
    assert (after.text, after.version, after.source_ids) == (before.text, before.version, before.source_ids)

    md = seeded.md_bytes().decode()
    assert ('**C1** · **REQ-001**: an older email says "The export must support CSV and JSON."[^2], '
            'but a newer one set "The export must support CSV and XLSX."[^3]. The newer email won.') in md
    assert '[^2]: Email "Re: Atlas export requirements", 2026-11-17, ID `json@acme.com`' in md

    # Re-ingesting the late email changes nothing.
    snapshot, md_bytes = seeded.snapshot(), seeded.md_bytes()
    assert run(seeded, late, None).status == "skipped"
    assert seeded.snapshot() == snapshot and seeded.md_bytes() == md_bytes


def test_confirm_use_other_switches_to_the_other_email(seeded, tmp_path):
    seeded.ingest("atlas_04_real_change", "atlas_04_real_change")
    late = mail(tmp_path, "json", "Tue, 17 Nov 2026 12:00:00 +0000", "We need the export to support CSV and JSON.")
    run(seeded, late, modify("export to support CSV and JSON", "The export must support CSV and JSON.", False))

    confirm(1, Runtime(seeded.settings, seeded.sessions, llm=None), use_other=True)
    req = seeded.requirements()[0]
    assert req.text == "The export must support CSV and JSON."
    last = seeded.change_log()[-1]
    assert (last.op, last.source_id, last.old_text) == ("modify", "json@acme.com", "The export must support CSV and XLSX.")
    md = seeded.md_bytes().decode()
    assert "Needs confirmation" not in md
    assert "- **REQ-001** The export must support CSV and JSON.[^1][^2][^3]" in md


def test_stale_removal_is_flagged(seeded, tmp_path):
    seeded.ingest("atlas_04_real_change", "atlas_04_real_change")
    late = mail(tmp_path, "drop", "Tue, 17 Nov 2026 12:00:00 +0000", "Actually, forget about the export for now.")
    result = run(seeded, late, {
        "gate": [{"has_change": True}],
        "extract": [{"ops": [{"op": "remove", "req_id": "REQ-001", "evidence": "forget about the export"}]}],
    })
    assert result.outcome == "flagged"
    assert seeded.requirements()[0].status == "active"
    assert "an older email asks to remove it[^2], but a newer one set" in seeded.md_bytes().decode()


def test_confirm_errors(seeded):
    rt = Runtime(seeded.settings, seeded.sessions, llm=None)
    with pytest.raises(ConflictError, match="no conflict C9"):
        confirm(9, rt)


# --- Git history and S3 change metadata ------------------------------------------------------


def git_log(repo: Path) -> list[str]:
    out = subprocess.run(["git", "log", "--format=%s"], cwd=repo, capture_output=True, text=True, check=True)
    return out.stdout.splitlines()


def test_git_commit_per_changed_file(env, tmp_path):
    repo = tmp_path / "history"
    env.settings = env.settings.model_copy(
        update={"output": OutputSettings(local_dir=repo, git_commit=True)}
    )
    env.ingest("atlas_01_initial", "atlas_01_initial")
    env.ingest("atlas_03_reworded", "atlas_03_reworded_modify")  # no change: no commit
    env.ingest("atlas_04_real_change", "atlas_04_real_change")
    env.ingest("atlas_04_real_change", llm=None)  # re-ingest: no commit

    assert git_log(repo) == [
        "atlas: REQ-001 modify (email atlas-004@acme.com, 2026-11-19)",
        "atlas: REQ-001 add, REQ-002 add (email atlas-001@acme.com, 2026-11-16)",
    ]
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True)
    assert status.stdout == ""  # everything committed


def test_s3_object_carries_change_message(env):
    s3 = FakeS3()
    env.settings = env.settings.model_copy(
        update={"output": OutputSettings(local_dir=None, s3=S3Settings(bucket="b"))}
    )
    from conftest import EML, LLM

    ingest_file(EML / "atlas_01_initial.eml", "atlas", company_id="acme", settings=env.settings,
                sessions=env.sessions, llm=RecordedLLM.from_file(LLM / "atlas_01_initial.json"),
                publishers=publishers_from_settings(env.settings, s3_client=s3))
    meta = s3.objects[("b", "acme/context.md")]["Metadata"]
    assert meta["change"] == "atlas: REQ-001 add, REQ-002 add (email atlas-001@acme.com, 2026-11-16)"
