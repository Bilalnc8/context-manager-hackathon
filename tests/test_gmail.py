"""Phase 2: Gmail sync (backfill, incremental, expired history, resume) and OAuth handling."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest
from fakes import FakeGmail
from pydantic import ValidationError
from sqlalchemy import select

from emailctx.auth import AuthError, MemoryTokenStore, load_credentials, run_auth_flow
from emailctx.config import GoogleSettings, Settings
from emailctx.ingest import Runtime
from emailctx.llm import RecordedLLM
from emailctx.registry import RegistrySpec, load_registry
from emailctx.review import ReviewError, assign, list_open, make_reader
from emailctx.sources.gmail_sync import GmailSync, SyncError, gmail_reader
from emailctx.store.models import Message, SyncState

# --- Helpers ----------------------------------------------------------------------------------

REGISTRY = {
    "own_domains": ["ourstudio.io"],
    "companies": [
        {"id": "globex", "domains": ["globex.com"],
         "projects": [{"id": "gx-portal", "label": "Clients/Globex"}]},
    ],
}


def mail(n: int, frm: str = "ops@globex.com", body: str = "Status update.", subject: str | None = None) -> bytes:
    msg = EmailMessage()
    msg["From"] = frm
    msg["To"] = "sam@ourstudio.io"
    msg["Subject"] = subject or f"Update {n}"
    msg["Date"] = f"Sun, {n:02d} Nov 2026 09:00:00 +0000"
    msg["Message-ID"] = f"<g{n}@globex.com>"
    msg.set_content(body)
    return msg.as_bytes()


def quiet() -> RecordedLLM:
    return RecordedLLM({"gate": [{"has_change": False, "reason": "test"}] * 50})


@pytest.fixture
def genv(env):
    with env.sessions() as s:
        load_registry(s, RegistrySpec.model_validate(REGISTRY))
        s.commit()
    env.settings = env.settings.model_copy(
        update={"gmail": env.settings.gmail.model_copy(update={"query": "label:clients"})}
    )
    return env


def sync(env, gmail: FakeGmail, llm=None, **kw):
    rt = Runtime(env.settings, env.sessions, llm or quiet())
    return GmailSync(gmail, rt, **kw).run()


def cursor(env, account="sam@ourstudio.io") -> str | None:
    with env.sessions() as s:
        state = s.get(SyncState, account)
        return state.history_id if state else None


# --- Sync ---------------------------------------------------------------------------------------


def test_backfill_then_incremental(genv):
    g = FakeGmail()
    g.add("a1", mail(1))
    g.add("a2", mail(2))
    g.add("x1", mail(3, frm="news@shop.example"), matches=False)

    first = sync(genv, g)
    assert first.mode == "backfill"
    assert [gid for gid, _ in first.results] == ["a1", "a2"]  # oldest first, query applied
    assert all(r.project_id == "gx-portal" for _, r in first.results)
    assert cursor(genv) == "1003"

    g.add("a3", mail(4))
    g.add("x2", mail(5, frm="news@shop.example"), matches=False)
    g.add("d1", mail(6), labels=("DRAFT",))
    second = sync(genv, g)
    assert second.mode == "incremental"
    assert [gid for gid, _ in second.results] == ["a3"]
    assert (second.not_matching, second.skipped_labels) == (1, 1)
    assert cursor(genv) == "1006"

    g.calls.clear()
    third = sync(genv, g)
    assert (third.mode, third.candidates, third.results) == ("incremental", 0, [])
    assert g.calls["get"] == 0


def test_backfill_cap_takes_most_recent_in_date_order(genv):
    g = FakeGmail()
    for n in range(1, 6):
        g.add(f"m{n}", mail(n))
    report = sync(genv, g, backfill_max=3)
    assert [gid for gid, _ in report.results] == ["m3", "m4", "m5"]


def test_expired_history_falls_back_to_backfill_without_redownloading(genv):
    g = FakeGmail()
    g.add("a1", mail(1))
    g.add("a2", mail(2))
    sync(genv, g)
    g.add("a3", mail(3))
    g.expired_before = 10_000  # Google dropped our historyId
    g.calls.clear()

    report = sync(genv, g)
    assert report.mode == "backfill_after_expired_history"
    assert [gid for gid, _ in report.results] == ["a3"]
    assert report.known == 2
    assert g.calls["get"] == 1  # known messages are not downloaded again
    assert cursor(genv) == str(g.history_id)


def test_failure_keeps_cursor_and_next_run_resumes(genv):
    g = FakeGmail()
    g.add("a1", mail(1))
    g.add("a2", mail(2))
    one_gate = RecordedLLM({"gate": [{"has_change": False}]})  # second email fails
    with pytest.raises(SyncError, match="stopped at Gmail message a2") as err:
        sync(genv, g, llm=one_gate)
    assert [gid for gid, _ in err.value.report.results] == ["a1"]
    assert cursor(genv) is None  # nothing skipped

    report = sync(genv, g)
    assert report.known == 1
    assert [gid for gid, _ in report.results] == ["a2"]
    assert cursor(genv) == "1002"


def test_same_email_from_file_and_gmail_is_processed_once(genv, tmp_path):
    from emailctx.ingest import ingest_file

    path = tmp_path / "g1.eml"
    path.write_bytes(mail(1))
    ingest_file(path, settings=genv.settings, sessions=genv.sessions, llm=quiet())

    g = FakeGmail()
    g.add("a1", mail(1))
    report = sync(genv, g)
    [(_, result)] = report.results
    assert (result.status, result.outcome) == ("skipped", "already_processed")
    with genv.sessions() as s:
        assert s.get(Message, "g1@globex.com").gmail_id == "a1"


def test_gmail_labels_are_explicit_routing_signals(genv):
    g = FakeGmail()
    g.add("a1", mail(1, frm="lee@ourstudio.io"), labels=("INBOX", "Label_7"))  # internal email
    [(_, result)] = sync(genv, g).results
    assert (result.route.step, result.project_id) == ("label", "gx-portal")


def test_review_item_from_gmail_is_read_again_from_gmail(genv):
    g = FakeGmail()
    g.add("u1", mail(1, frm="ceo@newclient.io", body="We need SSO for the portal."))
    [(_, result)] = sync(genv, g).results
    assert result.outcome == "review"
    with genv.sessions() as s:
        [item] = list_open(s)
    assert item.source_ref == "gmail:u1"

    rt = Runtime(genv.settings, genv.sessions, quiet())
    with pytest.raises(ReviewError, match="emailctx auth"):
        assign(item.id, "gx-portal", rt, reader=make_reader())
    done = assign(item.id, "gx-portal", rt, reader=make_reader(gmail=gmail_reader(g)))
    assert (done.route.step, done.project_id) == ("manual", "gx-portal")


def test_gmail_sync_stores_no_email_content(genv):
    g = FakeGmail()
    secret_body = "Confidential: the merger closes on Friday, keep this quiet."
    g.add("a1", mail(1, body=secret_body))
    g.add("u1", mail(2, frm="ceo@newclient.io", body="Our launch budget is 2.4M."))
    sync(genv, g)
    db = genv.settings.db_path.read_bytes()
    for phrase in ("merger closes", "launch budget", "ops@globex.com", "ceo@newclient.io"):
        assert phrase.encode() not in db, phrase


def test_sync_applies_requirement_changes(genv):
    g = FakeGmail()
    g.add("a1", mail(1, body="The portal must support SSO via Okta."))
    llm = RecordedLLM({
        "gate": [{"has_change": True}],
        "extract": [{"ops": [{"op": "add", "text": "The portal must support SSO via Okta.",
                              "category": "functional", "evidence": "must support SSO via Okta"}]}],
    })
    [(_, result)] = sync(genv, g, llm=llm).results
    assert result.outcome == "applied"
    md = genv.md_bytes("globex").decode()
    assert "- **REQ-001** The portal must support SSO via Okta.[^1]" in md
    assert '[^1]: Email "Update 1", 2026-11-01, ID `g1@globex.com`' in md


def test_sync_cli(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    import emailctx.cli
    import emailctx.llm
    from emailctx.cli import app

    config = tmp_path / "config.yaml"
    config.write_text(
        "db_path: state.db\noutput:\n  local_dir: out\ngmail:\n  query: label:clients\n",
        encoding="utf-8",
    )
    registry = tmp_path / "registry.yaml"
    registry.write_text(json.dumps(REGISTRY), encoding="utf-8")  # JSON is valid YAML
    g = FakeGmail()
    g.add("a1", mail(1))
    monkeypatch.setattr(emailctx.cli, "_gmail_service", lambda settings: g)
    monkeypatch.setattr(emailctx.llm, "AnthropicLLM", lambda settings: quiet())
    runner = CliRunner()
    cfg = ["--config", str(config)]

    assert runner.invoke(app, ["registry", "load", str(registry), *cfg]).exit_code == 0
    first = runner.invoke(app, ["sync", *cfg])
    assert first.exit_code == 0, first.output
    assert "a1  gated_out" in first.output and "gx-portal" in first.output
    assert "sam@ourstudio.io: backfill, 1 candidates, 1 processed" in first.output
    second = runner.invoke(app, ["sync", *cfg])
    assert "incremental, 0 candidates, 0 processed" in second.output


def test_auth_cli_logout(monkeypatch):
    from typer.testing import CliRunner

    import emailctx.auth
    from emailctx.cli import app

    store = MemoryTokenStore("{}")
    monkeypatch.setattr(emailctx.auth, "KeyringTokenStore", lambda: store)
    result = CliRunner().invoke(app, ["auth", "--logout"])
    assert result.exit_code == 0 and "signed out" in result.output
    assert store.token_json is None


# --- OAuth ---------------------------------------------------------------------------------------


def _token(expiry: datetime, refresh: str | None = "r-token") -> str:
    data = {"token": "a-token", "client_id": "cid", "client_secret": "csecret",
            "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
            "expiry": expiry.replace(tzinfo=None).isoformat() + "Z"}
    if refresh:
        data["refresh_token"] = refresh
    return json.dumps(data)


def test_only_read_only_scopes_are_accepted():
    with pytest.raises(ValidationError, match="read-only"):
        GoogleSettings(scopes=["https://www.googleapis.com/auth/gmail.modify"])
    with pytest.raises(ValidationError, match="read-only"):
        Settings.model_validate({"google": {"scopes": ["https://mail.google.com/"]}})


def test_not_signed_in():
    with pytest.raises(AuthError, match="run `emailctx auth`"):
        load_credentials(Settings(), MemoryTokenStore())


def test_missing_client_file_explains_setup(tmp_path):
    settings = Settings(google=GoogleSettings(client_secrets=tmp_path / "nope.json"))
    with pytest.raises(AuthError, match="Desktop app"):
        run_auth_flow(settings, MemoryTokenStore())


def test_auth_flow_stores_token_in_store(tmp_path):
    secrets = tmp_path / "client_secret.json"
    secrets.write_text("{}", encoding="utf-8")
    seen = {}

    class Creds:
        scopes = ["https://www.googleapis.com/auth/gmail.readonly"]

        def to_json(self):
            return '{"token": "t"}'

    class Flow:
        def run_local_server(self, port, open_browser):
            return Creds()

    def factory(path, scopes):
        seen["args"] = (path, scopes)
        return Flow()

    store = MemoryTokenStore()
    run_auth_flow(Settings(google=GoogleSettings(client_secrets=secrets)), store, flow_factory=factory)
    assert store.token_json == '{"token": "t"}'
    assert seen["args"] == (str(secrets), ["https://www.googleapis.com/auth/gmail.readonly"])


def test_valid_token_loads_without_refresh():
    store = MemoryTokenStore(_token(datetime.now(UTC) + timedelta(hours=1)))
    creds = load_credentials(Settings(), store)
    assert creds.valid and creds.token == "a-token"


def test_expired_token_refreshes_and_is_stored_again(monkeypatch):
    from google.oauth2.credentials import Credentials

    def fake_refresh(self, request):
        self.token = "fresh-token"
        self.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)
    store = MemoryTokenStore(_token(datetime.now(UTC) - timedelta(hours=1)))
    creds = load_credentials(Settings(), store)
    assert creds.token == "fresh-token"
    assert json.loads(store.token_json)["token"] == "fresh-token"


def test_expired_token_without_refresh_token_asks_to_sign_in_again():
    store = MemoryTokenStore(_token(datetime.now(UTC) - timedelta(hours=1), refresh=None))
    with pytest.raises(AuthError, match="run `emailctx auth` again") as err:
        load_credentials(Settings(), store)
    assert "a-token" not in str(err.value) and "csecret" not in str(err.value)
