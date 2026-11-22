"""MCP server login with Google (emailctx serve --auth google), end to end over HTTP.

Google is faked: the token exchange returns whatever claims the test sets. Everything
else (registration, /authorize, the callback, /token, Bearer checks on /mcp) is the
real MCP SDK plus emailctx.oauth.
"""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from emailctx.mcp_server import build_server, current_user_default
from emailctx.oauth import GoogleOAuthConfig, install_callback_route, setup_google_auth
from emailctx.store.db import make_engine, make_sessionmaker
from emailctx.store.models import OAuthToken

PUBLIC = "http://localhost:8000"
CLAUDE_CALLBACK = "http://localhost:33418/callback"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


class FakeGoogle:
    def __init__(self) -> None:
        self.claims = {"email": "dev@ourstudio.io", "email_verified": True}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, cfg, code, verifier):
        self.calls.append((code, verifier))
        return dict(self.claims)


def make_app(db_path, google: FakeGoogle):
    cfg = GoogleOAuthConfig(
        client_id="google-client",
        client_secret="google-secret",
        public_url=PUBLIC,
        allowed_emails=["bilalnc8@gmail.com"],
        allowed_domains=["ourstudio.io"],
    )
    sessions = make_sessionmaker(make_engine(db_path))
    provider, kwargs = setup_google_auth(cfg, sessions, google)

    def runtime():
        raise AssertionError("no tool runs in these tests")

    server = build_server(runtime, **kwargs)
    install_callback_route(server, provider)
    return server.streamable_http_app(), sessions


@pytest.fixture
def google():
    return FakeGoogle()


@pytest.fixture
def app(tmp_path, google):
    app, sessions = make_app(tmp_path / "auth.db", google)
    app.state_sessions = sessions
    return app


def client(app) -> TestClient:
    return TestClient(app, base_url=PUBLIC, follow_redirects=False)


def register(c: TestClient) -> str:
    r = c.post(
        "/register",
        json={
            "redirect_uris": [CLAUDE_CALLBACK],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_name": "Claude Code",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def start_login(c: TestClient, client_id: str) -> str:
    """/authorize -> Google. Returns the state emailctx gave Google."""
    r = c.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CLAUDE_CALLBACK,
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "state": "claude-state",
            "resource": f"{PUBLIC}/mcp",
        },
    )
    assert r.status_code == 302, r.text
    google = urlparse(r.headers["location"])
    assert google.netloc == "accounts.google.com"
    q = parse_qs(google.query)
    assert q["redirect_uri"] == [f"{PUBLIC}/oauth/google/callback"]
    assert q["scope"] == ["openid email"]
    assert q["code_challenge_method"] == ["S256"]
    return q["state"][0]


def finish_login(c: TestClient, state: str) -> dict[str, list[str]]:
    """Google -> /oauth/google/callback -> Claude's callback. Returns its query."""
    r = c.get("/oauth/google/callback", params={"state": state, "code": "google-code"})
    assert r.status_code == 302, r.text
    back = urlparse(r.headers["location"])
    assert f"{back.scheme}://{back.netloc}{back.path}" == CLAUDE_CALLBACK
    return parse_qs(back.query)


def get_tokens(c: TestClient, client_id: str, code: str) -> dict:
    r = c.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CLAUDE_CALLBACK,
            "client_id": client_id,
            "code_verifier": VERIFIER,
            "resource": f"{PUBLIC}/mcp",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def sign_in(c: TestClient) -> tuple[str, dict]:
    client_id = register(c)
    q = finish_login(c, start_login(c, client_id))
    assert q["state"] == ["claude-state"]
    return client_id, get_tokens(c, client_id, q["code"][0])


def test_mcp_requires_login_and_points_to_the_auth_server(app):
    with client(app) as c:
        r = c.post("/mcp", json=INIT, headers=MCP_HEADERS)
        assert r.status_code == 401
        assert "resource_metadata" in r.headers["www-authenticate"]
        meta = c.get("/.well-known/oauth-authorization-server").json()
        assert meta["issuer"].rstrip("/") == PUBLIC
        assert meta["registration_endpoint"] == f"{PUBLIC}/register"


def test_full_login_gives_a_token_that_opens_mcp_as_the_user(app, google):
    with client(app) as c:
        _, tokens = sign_in(c)
        assert google.calls and google.calls[0][0] == "google-code"
        r = c.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {tokens['access_token']}"})
        assert r.status_code == 200, r.text


def test_tokens_are_stored_hashed_with_the_users_email(app):
    with client(app) as c:
        _, tokens = sign_in(c)
    with app.state_sessions() as s:
        rows = s.query(OAuthToken).all()
    assert {r.kind for r in rows} == {"access", "refresh"}
    assert all(r.subject == "dev@ourstudio.io" for r in rows)
    assert tokens["access_token"] not in {r.token_hash for r in rows}


def test_exact_allowed_address_on_a_personal_domain_gets_in(app, google):
    google.claims = {"email": "Bilalnc8@gmail.com", "email_verified": True}
    with client(app) as c:
        sign_in(c)


@pytest.mark.parametrize(
    "claims",
    [
        {"email": "someone@gmail.com", "email_verified": True},  # not on the list
        {"email": "dev@evil-ourstudio.io", "email_verified": True},  # look-alike domain
        {"email": "dev@ourstudio.io", "email_verified": False},
    ],
)
def test_other_accounts_are_refused_at_the_callback(app, google, claims):
    google.claims = claims
    with client(app) as c:
        client_id = register(c)
        q = finish_login(c, start_login(c, client_id))
        assert q["error"] == ["access_denied"]
        assert q["state"] == ["claude-state"]
        assert "code" not in q


def test_callback_state_works_once(app):
    with client(app) as c:
        state = start_login(c, register(c))
        finish_login(c, state)
        r = c.get("/oauth/google/callback", params={"state": state, "code": "google-code"})
        assert r.status_code == 400
        assert "expired or was already used" in r.text
        assert c.get("/oauth/google/callback", params={"state": "made-up", "code": "x"}).status_code == 400


def test_cancelled_google_sign_in_returns_an_error_to_claude(app, google):
    with client(app) as c:
        state = start_login(c, register(c))
        r = c.get("/oauth/google/callback", params={"state": state, "error": "access_denied"})
        assert r.status_code == 302
        assert parse_qs(urlparse(r.headers["location"]).query)["error"] == ["access_denied"]
    assert google.calls == []


def test_authorization_code_is_single_use(app):
    with client(app) as c:
        client_id = register(c)
        code = finish_login(c, start_login(c, client_id))["code"][0]
        get_tokens(c, client_id, code)
        r = c.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": CLAUDE_CALLBACK,
                "client_id": client_id,
                "code_verifier": VERIFIER,
            },
        )
        assert r.status_code == 400


def test_refresh_rotates_tokens(app):
    with client(app) as c:
        client_id, tokens = sign_in(c)
        r = c.post(
            "/token",
            data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id},
        )
        assert r.status_code == 200, r.text
        new = r.json()
        old_access = {**MCP_HEADERS, "Authorization": f"Bearer {tokens['access_token']}"}
        assert c.post("/mcp", json=INIT, headers=old_access).status_code == 401
        new_access = {**MCP_HEADERS, "Authorization": f"Bearer {new['access_token']}"}
        assert c.post("/mcp", json=INIT, headers=new_access).status_code == 200
        again = c.post(
            "/token",
            data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id},
        )
        assert again.status_code == 400


def test_restart_keeps_everyone_signed_in(tmp_path, google):
    db = tmp_path / "auth.db"
    app1, _ = make_app(db, google)
    with client(app1) as c:
        _, tokens = sign_in(c)
    app2, _ = make_app(db, google)  # a new server process on the same database
    with client(app2) as c:
        r = c.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {tokens['access_token']}"})
        assert r.status_code == 200


def test_signed_in_user_is_who_writes_are_recorded_as():
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken

    assert current_user_default() == "agent"
    token = AccessToken(token="t", client_id="claude", scopes=["emailctx"], subject="dev@ourstudio.io")
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        assert current_user_default() == "dev@ourstudio.io"
    finally:
        auth_context_var.reset(reset)


def test_config_requires_an_allow_list(monkeypatch, tmp_path):
    from emailctx.oauth import OAuthConfigError

    for name in ("EMAILCTX_ALLOWED_EMAILS", "EMAILCTX_ALLOWED_DOMAINS", "EMAILCTX_PUBLIC_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EMAILCTX_GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("EMAILCTX_GOOGLE_CLIENT_SECRET", "secret")
    with pytest.raises(OAuthConfigError):
        GoogleOAuthConfig.from_env(tmp_path / "missing.env")
    env = tmp_path / ".env"
    env.write_text('# team\nEMAILCTX_ALLOWED_EMAILS="a@gmail.com, B@gmail.com"\n', encoding="utf-8")
    monkeypatch.delenv("EMAILCTX_ALLOWED_EMAILS", raising=False)
    cfg = GoogleOAuthConfig.from_env(env)
    assert cfg.allowed_emails == ["a@gmail.com", "b@gmail.com"]
    assert cfg.public_url == PUBLIC
