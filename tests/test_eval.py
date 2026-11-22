"""The company's sample project through the pipeline, and the eval that scores it."""

from __future__ import annotations

from email import message_from_bytes, policy
from pathlib import Path

import pytest

from emailctx.config import OutputSettings, Settings
from emailctx.evaluate import EvalError, prepare_workdir, read_facts, run_eval
from emailctx.ingest import Runtime
from emailctx.sample import load_sample, read_sample
from emailctx.store.db import make_engine, make_sessionmaker

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "dummy_data"
FACTS = ROOT / "eval" / "facts"
# The sample project (dummy_data/, expected_context/, eval/facts/) lives on the
# sample-data branch, not main; these tests run only where it is checked out.
needs_sample = pytest.mark.skipif(
    not (SAMPLE.exists() and FACTS.exists()),
    reason="sample data lives on the sample-data branch",
)


class ScriptedLLM:
    """Gate: only email-001 has a change. Extract: two items from it. Grade: two real
    quotes for Acme and one quote that isn't in the file."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def structured(self, task, system, prompt, schema):
        self.calls.append(task)
        if task == "gate":
            return schema.model_validate({
                "has_change": "Subject: Customer portal kickoff\n" in prompt, "reason": "scripted",
            })
        if task == "extract":
            return schema.model_validate({"ops": [
                {"op": "add", "category": "functional", "text": "Account login is in phase-one scope.",
                 "evidence": "We want account login"},
                {"op": "add", "category": "out-of-scope", "text": "Payment processing is excluded from phase one.",
                 "evidence": "payment processing is not part of phase one"},
            ]})
        if task == "grade":
            if "scope-login:" not in prompt:  # Nova and Orion: empty files, nothing present
                return schema.model_validate({"grades": []})
            return schema.model_validate({"grades": [
                {"id": "scope-login", "verdict": "present", "quote": "Account login is in phase-one scope."},
                {"id": "oos-payments", "verdict": "present", "quote": "Payment processing is excluded"},
                {"id": "pilot-date", "verdict": "present", "quote": "Pilot on December 5"},  # not in the file
                {"id": "scope-support", "verdict": "missing"},
            ]})
        raise AssertionError(f"unexpected {task} call")


@needs_sample
def test_sample_reads_as_real_email():
    sample = read_sample(SAMPLE)
    assert len(sample.inputs) == 42
    assert {i.kind for i in sample.inputs} == {"email", "meeting"}
    dates = [i.date for i in sample.inputs]
    assert dates == sorted(dates)
    ids = [i.source_id for i in sample.inputs]
    assert ids.index("email-006") < ids.index("meeting-001") < ids.index("email-007")  # Nov 4, midday

    reply = message_from_bytes(next(i for i in sample.inputs if i.source_id == "email-010").raw, policy=policy.default)
    assert reply["Message-ID"] == "<email-010@fixtures.contextlab.example>"
    assert reply["In-Reply-To"] == "<email-007@fixtures.contextlab.example>"
    assert reply["References"] == "<email-007@fixtures.contextlab.example>"
    assert reply["From"] == "alex@contextlab.example" and reply["To"] == "mia@acme.example"

    notes = message_from_bytes(next(i for i in sample.inputs if i.source_id == "meeting-004").raw, policy=policy.default)
    assert notes["From"].addresses[0].domain == "notetaker.example"
    assert notes["Subject"] == "Meeting notes: Pilot readiness check (meeting-004)"
    assert "December 5 pilot is conditionally approved" in notes.get_body().get_content()

    assert [c.id for c in sample.registry.companies] == ["acme", "nova", "orion"]
    assert sample.registry.own_domains == ["contextlab.example"]


@needs_sample
def test_every_sample_input_reaches_its_project_once(tmp_path):
    settings = Settings(db_path=tmp_path / "s.db", output=OutputSettings(local_dir=tmp_path / "out"))
    llm = ScriptedLLM()
    rt = Runtime(settings, make_sessionmaker(make_engine(settings.db_path)), llm)
    sample = read_sample(SAMPLE)
    results = load_sample(sample, rt, tmp_path / "eml")
    assert [(i.source_id, r.company_id) for i, r in results] == [(i.source_id, i.expected_project) for i in sample.inputs]
    steps = {r.route.step for i, r in results if i.kind == "meeting"}
    assert steps == {"meeting_bot"}  # routed by the company the notes name

    calls = len(llm.calls)
    again = load_sample(sample, rt, tmp_path / "eml")
    assert all(r.status == "skipped" for _, r in again)
    assert len(llm.calls) == calls  # nothing processed twice


@needs_sample
def test_eval_scores_the_generated_files(tmp_path):
    workdir = tmp_path / "run"
    report = run_eval(SAMPLE, FACTS, workdir, Settings(), ScriptedLLM())

    assert report.misrouted == [] and report.unaccounted == [] and report.rejected == []
    assert report.rerun_processed == 0 and report.rerun_changed == []
    graded = {g.fact.id: g for g in report.grades["acme"]}
    assert graded["scope-login"].verdict == "present"
    assert graded["oos-payments"].verdict == "present"
    assert graded["pilot-date"].verdict == "missing"  # its quote is not in the file
    assert "not in the file" in graded["pilot-date"].note
    assert graded["owner-mia"].note == "no grade returned"
    assert report.score() == (2, sum(len(f.facts) for f in read_facts(FACTS).values()))

    md = (workdir / "report.md").read_text(encoding="utf-8")
    assert "Sample `" in md and "36 emails, 6 meeting notes" in md
    assert "| acme | 2/14 (14%) | 12 | 0 |" in md
    assert "- Routing: 42/42 inputs reached the expected project." in md
    assert "- Coverage: every sentence was captured or skipped with a reason." in md
    assert "- Rerun: processing the sample again changed nothing" in md
    assert '- [present] Account login is in phase-one scope. — "Account login is in phase-one scope."' in md
    assert "- **[missing]** The pilot is on December 5" in md
    generated = (workdir / "companies" / "acme" / "context.md").read_text(encoding="utf-8")
    assert "- **REQ-002** Payment processing is excluded from phase one.[^1]" in generated
    assert "eval.db" in {p.name for p in workdir.iterdir()}  # the real store is never touched


@needs_sample
def test_eval_reports_a_rerun_that_changes_files(tmp_path, monkeypatch):
    import emailctx.evaluate as ev

    reads = iter([b"first", None, None, b"second", None, None])  # acme's file differs the 2nd time
    monkeypatch.setattr(ev, "_read", lambda settings, project: next(reads))
    report = run_eval(SAMPLE, None, tmp_path / "run", Settings(), ScriptedLLM())
    assert report.rerun_changed == ["acme"]
    assert "- Rerun: NOT idempotent: 0 inputs processed again; files changed: acme." in report.to_markdown()


def test_eval_only_clears_its_own_folder(tmp_path):
    mine = tmp_path / "mine"
    (mine / "keep.txt").parent.mkdir()
    (mine / "keep.txt").write_text("not ours", encoding="utf-8")
    with pytest.raises(EvalError, match="not created by `emailctx eval`"):
        prepare_workdir(mine)
    assert (mine / "keep.txt").exists()

    ours = tmp_path / "ours"
    prepare_workdir(ours)
    (ours / "old.txt").write_text("previous run", encoding="utf-8")
    prepare_workdir(ours)
    assert [p.name for p in ours.iterdir()] == [".emailctx-eval"]


@needs_sample
def test_facts_cover_every_expected_file():
    facts = read_facts(FACTS)
    _, expected = read_sample(SAMPLE).registry, read_sample(SAMPLE).expected_files
    assert set(facts) == set(expected) == {"acme", "nova", "orion"}
    for project, fs in facts.items():
        assert (ROOT / fs.expected).exists()
        assert len({f.id for f in fs.facts}) == len(fs.facts)  # unique ids
