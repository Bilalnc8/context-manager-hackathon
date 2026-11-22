"""Phase 3: company resolution, the routing cascade, the review queue and the registry."""

from __future__ import annotations

import dataclasses
from email.message import EmailMessage
from pathlib import Path

import pytest
from sqlalchemy import select

from emailctx.clean import parse_eml
from emailctx.config import Settings
from emailctx.ingest import Runtime, ingest_file
from emailctx.llm import RecordedLLM
from emailctx.registry import RegistrySpec, load_registry
from emailctx.review import ReviewError, assign, dismiss, list_open
from emailctx.routing.cascade import route
from emailctx.routing.company import Registry, registrable_domain, resolve_company
from emailctx.store.models import (
    Company,
    CompanyContact,
    CompanyDomain,
    ProjectRule,
    RoutingLog,
)

REGISTRY = {
    "own_domains": ["ourstudio.io"],
    "companies": [  # each company has exactly one project
        {
            "id": "acme", "name": "Acme", "domains": ["acme.com", "acme.co.uk"],
            "contacts": ["jane.acme@gmail.com"],
            "projects": [{"id": "atlas", "name": "Atlas", "keywords": ["Atlas"]}],
        },
        {
            "id": "beaconco", "name": "Beacon Co", "domains": ["beacon.io"],
            "projects": [{"id": "beacon", "name": "Beacon", "keywords": ["Beacon", "PO-4471"],
                          "participants": ["bob.contractor@gmail.com"],
                          "drive_folder_id": "1BeaconFolderXyz"}],
        },
        {"id": "globex", "domains": ["globex.com"],
         "projects": [{"id": "gx-portal", "label": "Clients/Globex"}]},
        {"id": "initech", "domains": ["initech.com"]},  # no project yet
    ],
}


def quiet_llm() -> RecordedLLM:
    """Gate says "nothing here" for every email: routing tests don't care about extraction."""
    return RecordedLLM({"gate": [{"has_change": False, "reason": "test"}] * 50})


_counter = iter(range(1, 10_000))


def build(frm: str, to=("team@ourstudio.io",), cc=(), reply_to=(), subject="Hello",
          body="Some text.", msg_id: str | None = None, in_reply_to: str | None = None,
          headers: dict | None = None) -> bytes:
    n = next(_counter)
    msg = EmailMessage()
    msg["From"] = frm
    if to:
        msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if reply_to:
        msg["Reply-To"] = ", ".join(reply_to)
    msg["Subject"] = subject
    msg["Date"] = f"Sat, 21 Nov 2026 {n // 60 % 24:02d}:{n % 60:02d}:00 +0000"
    msg["Message-ID"] = f"<{msg_id or f'm{n}'}@test>"
    if in_reply_to:
        msg["In-Reply-To"] = f"<{in_reply_to}@test>"
        msg["References"] = f"<{in_reply_to}@test>"
    for k, v in (headers or {}).items():
        msg[k] = v
    msg.set_content(body)
    return msg.as_bytes()


@pytest.fixture
def reg(env):
    with env.sessions() as s:
        load_registry(s, RegistrySpec.model_validate(REGISTRY))
        s.commit()
    return env


def resolve(env, raw: bytes, settings: Settings | None = None):
    with env.sessions() as s:
        return resolve_company(parse_eml(raw), Registry.load(s), settings or env.settings)


def ingest(env, tmp_path: Path, raw: bytes, llm=None):
    path = tmp_path / f"mail{next(_counter)}.eml"
    path.write_bytes(raw)
    result = ingest_file(path, settings=env.settings, sessions=env.sessions, llm=llm or quiet_llm())
    return result, path


# --- Domain resolution rules -------------------------------------------------------------


def test_registrable_domain_normalization():
    assert registrable_domain("mail.eu.acme.co.uk") == "acme.co.uk"
    assert registrable_domain("Mail.ACME.com.") == "acme.com"
    assert registrable_domain("localhost") == "localhost"


def test_address_not_display_name(reg):
    r = resolve(reg, build('"jane@acme.com" <attacker@evil-example.com>'))
    assert (r.status, r.rule, r.new_domains) == ("review", "unknown", ("evil-example.com",))


def test_subdomains_resolve_to_registered_company(reg):
    assert resolve(reg, build("ops@mail.eu.acme.co.uk")).company_id == "acme"
    assert resolve(reg, build("ops@notify.acme.com")).company_id == "acme"


def test_own_domain_sender_uses_recipients(reg):
    r = resolve(reg, build("sam@ourstudio.io", to=["jane@acme.com"]))
    assert (r.status, r.rule, r.company_id) == ("company", "recipients", "acme")


def test_internal_only_email_has_no_company(reg):
    r = resolve(reg, build("sam@ourstudio.io", to=["lee@ourstudio.io"], cc=["ops@dev.ourstudio.io"]))
    assert (r.status, r.rule) == ("none", "internal_only")


def test_unknown_gmail_sender_says_nothing(reg):
    r = resolve(reg, build("random.person@gmail.com", to=["team@ourstudio.io"]))
    assert (r.status, r.rule) == ("none", "internal_only")


def test_gmail_sender_matched_through_company_contacts(reg):
    r = resolve(reg, build("Jane.Acme@gmail.com"))
    assert (r.status, r.rule, r.company_id) == ("company", "sender", "acme")


def test_gmail_sender_falls_back_to_recipients(reg):
    r = resolve(reg, build("random.person@gmail.com", to=["ops@globex.com"]))
    assert (r.status, r.rule, r.company_id) == ("company", "recipients", "globex")


def test_external_sender_wins_over_cc(reg):
    r = resolve(reg, build("jane@acme.com", cc=["ops@globex.com", "x@unknown-vendor.com"]))
    assert (r.status, r.rule, r.company_id) == ("company", "sender", "acme")


def test_multi_company_cc_prefers_company_with_active_projects(reg):
    r = resolve(reg, build("sam@ourstudio.io", to=["jane@acme.com"], cc=["ops@initech.com"]))
    assert (r.status, r.rule, r.company_id) == ("company", "active_projects", "acme")


def test_multi_company_tie_goes_to_review(reg):
    r = resolve(reg, build("sam@ourstudio.io", to=["jane@acme.com"], cc=["ops@globex.com"]))
    assert (r.status, r.rule) == ("review", "tie")
    assert set(r.tied_companies) == {"acme", "globex"}


def test_unknown_domain_becomes_new_company_review(reg):
    r = resolve(reg, build("ceo@newclient.io"))
    assert (r.status, r.review_kind, r.new_domains) == ("review", "new_company", ("newclient.io",))
    r = resolve(reg, build("sam@ourstudio.io", to=["ceo@mail.newclient.io"]))
    assert r.new_domains == ("newclient.io",)


def test_reply_to_counts_as_sender_side(reg):
    r = resolve(reg, build("noreply@gmail.com", reply_to=["ops@globex.com"], to=["jane@acme.com"]))
    assert (r.rule, r.company_id) == ("sender", "globex")


def test_dmarc_hardening(reg):
    strict = reg.settings.model_copy(
        update={"routing": reg.settings.routing.model_copy(update={"require_dmarc_pass": True})}
    )
    unauth = build("jane@acme.com")
    passed = build("jane@acme.com", headers={"Authentication-Results": "mx.google.com; spf=pass; dmarc=pass (p=REJECT)"})
    failed = build("jane@acme.com", headers={"Authentication-Results": "mx.google.com; dmarc=fail"})
    assert resolve(reg, unauth).company_id == "acme"  # off by default
    assert (resolve(reg, unauth, strict).status, resolve(reg, unauth, strict).rule) == ("review", "dmarc")
    assert resolve(reg, passed, strict).company_id == "acme"
    assert resolve(reg, failed, strict).rule == "dmarc"


# --- Cascade ----------------------------------------------------------------------------------


def test_email_domain_decides_the_project(reg, tmp_path):
    for sender, project in [("ops@globex.com", "gx-portal"), ("jane@acme.com", "atlas"),
                            ("x@mail.eu.acme.co.uk", "atlas"), ("ops@beacon.io", "beacon")]:
        result, _ = ingest(reg, tmp_path, build(sender))
        assert (result.route.status, result.route.step, result.project_id) == (
            "assigned", "company", project
        ), sender
        assert result.outcome == "gated_out"


def test_participant_places_email_without_a_company_domain(reg, tmp_path):
    result, _ = ingest(reg, tmp_path, build("bob.contractor@gmail.com", to=["sam@ourstudio.io"]))
    assert (result.route.step, result.project_id) == ("participant", "beacon")


def test_keyword_in_subject_beats_domain(reg, tmp_path):
    result, _ = ingest(reg, tmp_path, build("jane@acme.com", subject="Re: PO-4471 invoice"))
    assert (result.route.step, result.project_id) == ("keyword", "beacon")


def test_keyword_needs_word_boundaries(reg, tmp_path):
    result, _ = ingest(reg, tmp_path, build("carol@acme.com", subject="PO-44712 and Atlassian"))
    assert (result.route.step, result.project_id) == ("company", "atlas")  # no keyword hit


def test_drive_folder_link_is_explicit(reg, tmp_path):
    body = "See https://drive.google.com/drive/folders/1BeaconFolderXyz for the specs."
    result, _ = ingest(reg, tmp_path, build("carol@acme.com", body=body))
    assert (result.route.step, result.project_id) == ("drive_link", "beacon")


def test_gmail_label_is_explicit(reg):
    parsed = dataclasses.replace(parse_eml(build("someone@ourstudio.io")), labels=("Clients/Globex",))
    with reg.sessions() as s:
        d = route(s, parsed, parsed.thread_root, reg.settings)
    assert (d.step, d.project_id) == ("label", "gx-portal")


def test_conflicting_explicit_signals_go_to_review(reg, tmp_path):
    result, _ = ingest(reg, tmp_path, build("carol@acme.com", subject="Atlas vs Beacon"))
    assert result.outcome == "review"
    assert "explicit signals" in result.route.reason


def test_thread_inheritance(reg, tmp_path):
    first, _ = ingest(reg, tmp_path, build("ops@beacon.io", msg_id="t1"))
    assert first.project_id == "beacon"
    # An Acme person replying in Beacon's thread stays in Beacon's thread.
    reply, _ = ingest(reg, tmp_path, build("carol@acme.com", msg_id="t2", in_reply_to="t1"))
    assert (reply.route.step, reply.project_id) == ("thread", "beacon")


def test_internal_only_without_rules_goes_to_review_with_all_projects(reg, tmp_path):
    result, _ = ingest(reg, tmp_path, build("sam@ourstudio.io", to=["lee@ourstudio.io"]))
    assert result.outcome == "review"
    assert set(result.route.candidates) == {"atlas", "beacon", "gx-portal"}


def test_company_without_projects_goes_to_review(reg, tmp_path):
    result, _ = ingest(reg, tmp_path, build("ops@initech.com"))
    assert result.outcome == "review"
    assert "no active projects" in result.route.reason


def test_every_decision_is_logged(reg, tmp_path):
    ingest(reg, tmp_path, build("ops@globex.com"))
    ingest(reg, tmp_path, build("ceo@newclient.io"))
    with reg.sessions() as s:
        steps = [(r.step, r.project_id) for r in s.scalars(select(RoutingLog).order_by(RoutingLog.id))]
    assert steps == [("company", "gx-portal"), ("review", None)]


def test_manual_project_skips_routing(reg, tmp_path):
    path = tmp_path / "x.eml"
    path.write_bytes(build("ceo@newclient.io"))
    result = ingest_file(path, "atlas", settings=reg.settings, sessions=reg.sessions, llm=quiet_llm())
    assert (result.route.step, result.project_id) == ("manual", "atlas")


# --- Review queue -------------------------------------------------------------------------------


def runtime(env, llm=None) -> Runtime:
    return Runtime(env.settings, env.sessions, llm or quiet_llm())


def test_unknown_domain_review_then_assign_learns_rules(reg, tmp_path):
    result, path = ingest(reg, tmp_path, build("ceo@mail.newclient.io", msg_id="n1"))
    assert (result.outcome, result.route.review_kind) == ("review", "new_company")
    with reg.sessions() as s:
        assert s.get(Company, "newclient.io").status == "pending"
        [item] = list_open(s)
    assert item.pending_companies == ["newclient.io"] and item.source_ref == f"eml:{path.resolve()}"

    # Same sender again while pending: review again, no second pending company.
    again, _ = ingest(reg, tmp_path, build("cto@newclient.io"))
    assert again.outcome == "review" and again.route.pending_companies == ["newclient.io"]

    assigned = assign(item.id, "atlas", runtime(reg))
    assert (assigned.route.step, assigned.project_id, assigned.outcome) == ("manual", "atlas", "gated_out")
    with reg.sessions() as s:
        assert s.get(Company, "newclient.io") is None  # pending company folded into acme
        assert s.get(CompanyDomain, "newclient.io").company_id == "acme"
        rules = {(r.project_id, r.rule_type, r.value) for r in s.scalars(select(ProjectRule))}
        assert ("atlas", "participant", "ceo@mail.newclient.io") in rules
        assert [i.id for i in list_open(s)] == [again.review_id]

    # The domain now belongs to acme, so the next email routes on its own.
    later, _ = ingest(reg, tmp_path, build("cfo@newclient.io", subject="Next steps"))
    assert (later.route.step, later.project_id) == ("company", "atlas")
    # And a reply in the assigned thread inherits it.
    reply, _ = ingest(reg, tmp_path, build("someone@ourstudio.io", in_reply_to="n1"))
    assert (reply.route.step, reply.project_id) == ("thread", "atlas")


def test_assign_new_company_to_new_project_activates_it(reg, tmp_path):
    result, _ = ingest(reg, tmp_path, build("ceo@newclient.io"))
    assign(result.review_id, "nc-app", runtime(reg), company_id="newclient.io")
    with reg.sessions() as s:
        assert s.get(Company, "newclient.io").status == "active"
    later, _ = ingest(reg, tmp_path, build("cto@newclient.io"))
    assert (later.route.step, later.project_id) == ("company", "nc-app")


def test_assign_with_company_only_creates_its_project(reg, tmp_path):
    result, _ = ingest(reg, tmp_path, build("ceo@newclient.io"))
    done = assign(result.review_id, None, runtime(reg), company_id="newclient")
    assert (done.project_id, done.company_id) == ("newclient", "newclient")
    with reg.sessions() as s:
        assert s.get(Company, "newclient.io") is None  # pending domain folded into "newclient"
        assert s.get(CompanyDomain, "newclient.io").company_id == "newclient"
    later, _ = ingest(reg, tmp_path, build("cto@newclient.io"))
    assert (later.route.step, later.project_id) == ("company", "newclient")


def test_assign_to_existing_company_uses_its_project(reg, tmp_path):
    result, _ = ingest(reg, tmp_path, build("sam@ourstudio.io", to=["lee@ourstudio.io"]))
    done = assign(result.review_id, None, runtime(reg), company_id="globex")
    assert done.project_id == "gx-portal"


def test_assign_personal_sender_becomes_company_contact(reg, tmp_path):
    result, _ = ingest(reg, tmp_path, build("bob.private@gmail.com", to=["sam@ourstudio.io"]))
    assert result.outcome == "review"
    assign(result.review_id, "beacon", runtime(reg))
    with reg.sessions() as s:
        assert s.get(CompanyContact, "bob.private@gmail.com").company_id == "beaconco"
    later, _ = ingest(reg, tmp_path, build("bob.private@gmail.com"))
    assert (later.route.step, later.project_id) == ("company", "beacon")


def test_assign_runs_the_pipeline_on_the_reread_email(reg, tmp_path):
    body = "Atlas reports must be exportable as CSV."
    result, _ = ingest(reg, tmp_path, build("sam@ourstudio.io", to=["lee@ourstudio.io"], body=body))
    llm = RecordedLLM({
        "gate": [{"has_change": True}],
        "extract": [{"ops": [{"op": "add", "text": "Reports must be exportable as CSV.",
                              "category": "functional", "evidence": "must be exportable as CSV"}]}],
    })
    done = assign(result.review_id, "atlas", runtime(reg, llm))
    assert done.outcome == "applied"
    assert reg.md_path("acme").exists()


def test_assign_fails_cleanly_when_source_is_gone(reg, tmp_path):
    result, path = ingest(reg, tmp_path, build("ceo@newclient.io"))
    path.unlink()
    with pytest.raises(ReviewError, match="source file is gone"):
        assign(result.review_id, "atlas", runtime(reg))
    with reg.sessions() as s:
        assert [i.id for i in list_open(s)] == [result.review_id]  # still open


def test_reingesting_a_queued_email_does_not_requeue(reg, tmp_path):
    raw = build("ceo@newclient.io")
    path = tmp_path / "q.eml"
    path.write_bytes(raw)
    first = ingest_file(path, settings=reg.settings, sessions=reg.sessions, llm=quiet_llm())
    second = ingest_file(path, settings=reg.settings, sessions=reg.sessions, llm=RecordedLLM({}))
    assert first.outcome == "review" and second.status == "skipped"
    with reg.sessions() as s:
        assert len(list_open(s)) == 1


def test_dismiss_with_ignore_domains(reg, tmp_path):
    result, _ = ingest(reg, tmp_path, build("promo@spammy-vendor.com"))
    dismiss(result.review_id, runtime(reg), ignore_domains=True)
    with reg.sessions() as s:
        assert s.get(Company, "spammy-vendor.com").status == "ignored"
        assert list_open(s) == []
    # The ignored domain no longer produces new-company reviews.
    later, _ = ingest(reg, tmp_path, build("promo@spammy-vendor.com", to=["ops@globex.com"]))
    assert (later.route.step, later.project_id) == ("company", "gx-portal")


def test_assigning_twice_is_refused(reg, tmp_path):
    result, _ = ingest(reg, tmp_path, build("ceo@newclient.io"))
    assign(result.review_id, "atlas", runtime(reg))
    with pytest.raises(ReviewError, match="already assigned"):
        assign(result.review_id, "beacon", runtime(reg))


# --- Registry --------------------------------------------------------------------------------


def test_registry_load_is_idempotent_and_keeps_learned_rules(reg):
    with reg.sessions() as s:
        s.add(ProjectRule(project_id="atlas", rule_type="participant", value="learned@acme.com"))
        s.commit()
        counts = load_registry(s, RegistrySpec.model_validate(REGISTRY))
        s.commit()
        assert all(v == 0 for v in counts.values()), counts
        values = set(s.scalars(select(ProjectRule.value)))
    assert "learned@acme.com" in values


def test_registry_allows_one_project_per_company(reg):
    with pytest.raises(ValueError, match="exactly one project"):
        RegistrySpec.model_validate(
            {"companies": [{"id": "x", "projects": [{"id": "p1"}, {"id": "p2"}]}]}
        )
    spec = {"companies": [{"id": "acme", "projects": [{"id": "atlas-2"}]}]}
    with reg.sessions() as s, pytest.raises(ValueError, match="already has project 'atlas'"):
        load_registry(s, RegistrySpec.model_validate(spec))


def test_registry_adopts_pending_domain(reg, tmp_path):
    ingest(reg, tmp_path, build("ceo@newclient.io"))
    spec = {"companies": [{"id": "newco", "name": "New Co", "domains": ["newclient.io"],
                           "projects": [{"id": "nc"}]}]}
    with reg.sessions() as s:
        load_registry(s, RegistrySpec.model_validate(spec))
        s.commit()
        assert s.get(Company, "newclient.io") is None
        assert s.get(CompanyDomain, "newclient.io").company_id == "newco"
