"""MCP server login with Google (`emailctx serve --auth google`).

MCP clients (Claude Code, Claude Desktop, claude.ai) register themselves with the
server they connect to (dynamic client registration). Google doesn't support that, so
emailctx is the OAuth authorization server for Claude and hands only the sign-in step
to Google:

    Claude -> /mcp: 401, plus where to log in (served by the MCP SDK)
    Claude -> /register, /authorize (SDK) -> GoogleProxyProvider.authorize
           -> redirect to Google sign-in (openid email, PKCE, state = pending key)
    Google -> /oauth/google/callback -> handle_google_callback: exchange Google's code,
           verify the ID token, require a verified, allowed email, then redirect to
           Claude's callback with an emailctx authorization code
    Claude -> /token (SDK): emailctx access + refresh tokens
    Claude -> /mcp with the Bearer token; the user's email is the token's subject,
           so every write records who made it.

Everything is stored in SQLite (hashed), so restarting the server signs nobody out.

Configuration comes from environment variables, or a `.env` file next to config.yaml:
    EMAILCTX_GOOGLE_CLIENT_ID, EMAILCTX_GOOGLE_CLIENT_SECRET   (OAuth "Web application")
    EMAILCTX_ALLOWED_EMAILS    comma-separated exact addresses, and/or
    EMAILCTX_ALLOWED_DOMAINS   comma-separated domains, e.g. yourcompany.com
    EMAILCTX_PUBLIC_URL        the server's URL as Claude sees it (default http://localhost:8000)
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from emailctx.store.models import OAuthClient, OAuthCode, OAuthPending, OAuthToken

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
CALLBACK_PATH = "/oauth/google/callback"
SCOPE = "emailctx"  # the one scope emailctx tokens carry

PENDING_TTL = 10 * 60  # seconds to finish Google sign-in
CODE_TTL = 5 * 60
ACCESS_TTL = 60 * 60
REFRESH_TTL = 30 * 24 * 60 * 60


class OAuthConfigError(RuntimeError):
    pass


class GoogleLoginError(RuntimeError):
    """Google sign-in failed or the account is not allowed. The message is shown to the user."""


@dataclass
class GoogleOAuthConfig:
    client_id: str
    client_secret: str
    public_url: str = "http://localhost:8000"
    allowed_emails: list[str] = field(default_factory=list)
    allowed_domains: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.public_url = self.public_url.rstrip("/")
        self.allowed_emails = [e.strip().lower() for e in self.allowed_emails if e.strip()]
        self.allowed_domains = [d.strip().lower().lstrip("@") for d in self.allowed_domains if d.strip()]

    @property
    def redirect_uri(self) -> str:
        return self.public_url + CALLBACK_PATH

    @property
    def resource_url(self) -> str:
        return self.public_url + "/mcp"

    def allows(self, email: str) -> bool:
        email = email.lower()
        return email in self.allowed_emails or email.rpartition("@")[2] in self.allowed_domains

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> GoogleOAuthConfig:
        if env_file is not None:
            load_env_file(env_file)

        def get(name: str) -> str:
            return os.environ.get(name, "").strip()

        def split(name: str) -> list[str]:
            return [p for p in get(name).split(",") if p.strip()]

        missing = [n for n in ("EMAILCTX_GOOGLE_CLIENT_ID", "EMAILCTX_GOOGLE_CLIENT_SECRET") if not get(n)]
        if missing:
            raise OAuthConfigError(
                f"--auth google needs {', '.join(missing)} (from the Google Cloud 'Web application' "
                "OAuth client), in the environment or in .env next to config.yaml"
            )
        cfg = cls(
            client_id=get("EMAILCTX_GOOGLE_CLIENT_ID"),
            client_secret=get("EMAILCTX_GOOGLE_CLIENT_SECRET"),
            public_url=get("EMAILCTX_PUBLIC_URL") or "http://localhost:8000",
            allowed_emails=split("EMAILCTX_ALLOWED_EMAILS"),
            allowed_domains=split("EMAILCTX_ALLOWED_DOMAINS"),
        )
        if not cfg.allowed_emails and not cfg.allowed_domains:
            raise OAuthConfigError(
                "--auth google needs EMAILCTX_ALLOWED_EMAILS and/or EMAILCTX_ALLOWED_DOMAINS, "
                "or nobody could sign in"
            )
        return cfg


def load_env_file(path: Path) -> None:
    """Minimal .env reader: KEY=VALUE lines, # comments, optional quotes. Variables
    already set in the environment win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


GoogleExchange = Callable[[GoogleOAuthConfig, str, str], dict[str, Any]]


def google_exchange(cfg: GoogleOAuthConfig, code: str, verifier: str) -> dict[str, Any]:
    """Trade Google's code for an ID token and return its verified claims (blocking)."""
    import httpx
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token

    resp = httpx.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "redirect_uri": cfg.redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise GoogleLoginError("Google did not accept the sign-in. Try connecting again.")
    raw = resp.json().get("id_token")
    if not raw:
        raise GoogleLoginError("Google returned no identity. Try connecting again.")
    try:
        # Checks the signature, expiry, issuer (accounts.google.com) and audience.
        return id_token.verify_oauth2_token(raw, Request(), audience=cfg.client_id)
    except ValueError:
        raise GoogleLoginError("Google's identity token could not be verified.") from None


class GoogleProxyProvider:
    """OAuthAuthorizationServerProvider for the MCP SDK, with Google as the login step."""

    def __init__(
        self,
        cfg: GoogleOAuthConfig,
        sessions: sessionmaker[Session],
        exchange: GoogleExchange = google_exchange,
    ) -> None:
        self.cfg = cfg
        self.sessions = sessions
        self.exchange = exchange

    # --- client registration -----------------------------------------------------

    async def get_client(self, client_id: str):
        from mcp.shared.auth import OAuthClientInformationFull

        with self.sessions() as s:
            row = s.get(OAuthClient, client_id)
            return OAuthClientInformationFull.model_validate_json(row.info_json) if row else None

    async def register_client(self, client_info) -> None:
        with self.sessions.begin() as s:
            s.merge(OAuthClient(client_id=client_info.client_id, info_json=client_info.model_dump_json(), created_at=time.time()))

    # --- authorization -------------------------------------------------------------

    async def authorize(self, client, params) -> str:
        key = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        now = time.time()
        with self.sessions.begin() as s:
            s.execute(delete(OAuthPending).where(OAuthPending.expires_at < now))
            s.add(
                OAuthPending(
                    key_hash=_hash(key),
                    client_id=client.client_id,
                    params_json=params.model_dump_json(),
                    google_verifier=verifier,
                    expires_at=now + PENDING_TTL,
                )
            )
        query = {
            "client_id": self.cfg.client_id,
            "redirect_uri": self.cfg.redirect_uri,
            "response_type": "code",
            "scope": "openid email",
            "state": key,
            "code_challenge": _pkce_challenge(verifier),
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
        if len(self.cfg.allowed_domains) == 1 and not self.cfg.allowed_emails:
            query["hd"] = self.cfg.allowed_domains[0]  # a hint only; the callback enforces it
        return f"{GOOGLE_AUTH_URL}?{urlencode(query)}"

    async def handle_google_callback(self, state: str | None, code: str | None, error: str | None) -> str:
        """Finish sign-in and return the URL to send the browser to (Claude's callback).

        Raises GoogleLoginError when the request can't be traced back to Claude, so
        there is nowhere to redirect; the route shows the message instead.
        """
        import anyio
        from mcp.server.auth.provider import AuthorizationCode, AuthorizationParams, construct_redirect_uri

        if not state:
            raise GoogleLoginError("This sign-in link is incomplete. Start again from Claude.")
        with self.sessions.begin() as s:
            pending = s.get(OAuthPending, _hash(state))
            if pending is not None:
                s.expunge(pending)
                # One use only. A DELETE statement, not s.delete(): expunging cancels that.
                s.execute(delete(OAuthPending).where(OAuthPending.key_hash == pending.key_hash))
        if pending is None or pending.expires_at < time.time():
            raise GoogleLoginError("This sign-in has expired or was already used. Start again from Claude.")
        params = AuthorizationParams.model_validate_json(pending.params_json)

        def back_to_claude(**extra: str | None) -> str:
            return construct_redirect_uri(str(params.redirect_uri), state=params.state, **extra)

        if error or not code:
            return back_to_claude(error="access_denied", error_description="Google sign-in was cancelled")
        claims = await anyio.to_thread.run_sync(self.exchange, self.cfg, code, pending.google_verifier)
        email = str(claims.get("email") or "").lower()
        if not email or claims.get("email_verified") is not True:
            return back_to_claude(error="access_denied", error_description="Google account has no verified email")
        if not self.cfg.allows(email):
            return back_to_claude(error="access_denied", error_description=f"{email} is not allowed on this server")

        our_code = secrets.token_urlsafe(32)
        auth_code = AuthorizationCode(
            code=our_code,
            scopes=params.scopes or [SCOPE],
            expires_at=time.time() + CODE_TTL,
            client_id=pending.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=email,
        )
        with self.sessions.begin() as s:
            s.execute(delete(OAuthCode).where(OAuthCode.expires_at < time.time()))
            s.add(
                OAuthCode(
                    code_hash=_hash(our_code),
                    client_id=pending.client_id,
                    data_json=auth_code.model_dump_json(exclude={"code"}),
                    expires_at=auth_code.expires_at,
                )
            )
        return back_to_claude(code=our_code)

    async def load_authorization_code(self, client, authorization_code: str):
        from mcp.server.auth.provider import AuthorizationCode

        with self.sessions() as s:
            row = s.get(OAuthCode, _hash(authorization_code))
            if row is None or row.client_id != client.client_id:
                return None
            data = json.loads(row.data_json)
        return AuthorizationCode(code=authorization_code, **data)

    async def exchange_authorization_code(self, client, authorization_code):
        from mcp.server.auth.provider import TokenError

        with self.sessions.begin() as s:
            row = s.get(OAuthCode, _hash(authorization_code.code))
            if row is None:  # already used
                raise TokenError("invalid_grant", "authorization code was already used")
            s.delete(row)
            return self._issue(
                s, client.client_id, authorization_code.scopes, authorization_code.subject or "", authorization_code.resource
            )

    # --- tokens --------------------------------------------------------------------

    def _issue(self, s: Session, client_id: str, scopes: list[str], subject: str, resource: str | None):
        from mcp.shared.auth import OAuthToken as TokenResponse

        now = int(time.time())
        family = secrets.token_urlsafe(16)
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        common = dict(family=family, client_id=client_id, subject=subject, scopes=" ".join(scopes), resource=resource)
        s.add(OAuthToken(token_hash=_hash(access), kind="access", expires_at=now + ACCESS_TTL, **common))
        s.add(OAuthToken(token_hash=_hash(refresh), kind="refresh", expires_at=now + REFRESH_TTL, **common))
        return TokenResponse(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            refresh_token=refresh,
            scope=" ".join(scopes) or None,
        )

    def _load(self, token: str, kind: str) -> OAuthToken | None:
        with self.sessions() as s:
            row = s.get(OAuthToken, _hash(token))
            if row is None or row.kind != kind:
                return None
            if row.expires_at is not None and row.expires_at < time.time():
                return None
            s.expunge(row)
            return row

    async def load_refresh_token(self, client, refresh_token: str):
        from mcp.server.auth.provider import RefreshToken

        row = self._load(refresh_token, "refresh")
        if row is None or row.client_id != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row.client_id,
            scopes=row.scopes.split(),
            expires_at=row.expires_at,
            resource=row.resource,
            subject=row.subject,
        )

    async def exchange_refresh_token(self, client, refresh_token, scopes: list[str]):
        from mcp.server.auth.provider import TokenError

        with self.sessions.begin() as s:
            row = s.get(OAuthToken, _hash(refresh_token.token))
            if row is None:
                raise TokenError("invalid_grant", "refresh token was revoked")
            # Rotate: the old access and refresh tokens stop working.
            s.execute(delete(OAuthToken).where(OAuthToken.family == row.family))
            return self._issue(s, client.client_id, scopes, row.subject, row.resource)

    async def load_access_token(self, token: str):
        from mcp.server.auth.provider import AccessToken

        row = self._load(token, "access")
        if row is None:
            return None
        if row.resource and row.resource.rstrip("/") != self.cfg.resource_url:
            return None  # issued for a different server
        return AccessToken(
            token=token,
            client_id=row.client_id,
            scopes=row.scopes.split(),
            expires_at=row.expires_at,
            resource=row.resource,
            subject=row.subject,
        )

    async def revoke_token(self, token) -> None:
        with self.sessions.begin() as s:
            row = s.get(OAuthToken, _hash(token.token))
            if row is not None:
                s.execute(delete(OAuthToken).where(OAuthToken.family == row.family))

    async def exchange_identity_assertion(self, client, params):  # pragma: no cover - not offered
        from mcp.server.auth.provider import TokenError

        raise TokenError("unsupported_grant_type", "identity assertions are not supported")


def auth_server_kwargs(provider: GoogleProxyProvider) -> dict[str, Any]:
    """Keyword arguments for MCPServer(...) that turn on login."""
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions

    cfg = provider.cfg
    return {
        "auth_server_provider": provider,
        "auth": AuthSettings(
            issuer_url=cfg.public_url,
            resource_server_url=cfg.resource_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=[SCOPE],
            validate_token_resource=False,  # load_access_token checks the resource itself
        ),
    }


def _page(title: str, message: str, status: int):
    from starlette.responses import HTMLResponse

    body = (
        f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title>"
        f"<body style='font-family:system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem'>"
        f"<h1 style='font-size:1.3rem'>{html.escape(title)}</h1><p>{html.escape(message)}</p>"
    )
    return HTMLResponse(body, status_code=status)


def install_callback_route(server: Any, provider: GoogleProxyProvider) -> None:
    """Add GET /oauth/google/callback, where Google sends the browser after sign-in."""
    from starlette.requests import Request
    from starlette.responses import RedirectResponse

    @server.custom_route(CALLBACK_PATH, methods=["GET"], include_in_schema=False)
    async def google_callback(request: Request):
        q = request.query_params
        try:
            url = await provider.handle_google_callback(q.get("state"), q.get("code"), q.get("error"))
        except GoogleLoginError as e:
            return _page("emailctx sign-in failed", str(e), 400)
        return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})


def setup_google_auth(
    cfg: GoogleOAuthConfig,
    sessions: sessionmaker[Session],
    exchange: GoogleExchange = google_exchange,
) -> tuple[GoogleProxyProvider, dict[str, Any]]:
    provider = GoogleProxyProvider(cfg, sessions, exchange)
    return provider, auth_server_kwargs(provider)


__all__ = [
    "GoogleLoginError",
    "GoogleOAuthConfig",
    "GoogleProxyProvider",
    "OAuthConfigError",
    "auth_server_kwargs",
    "install_callback_route",
    "load_env_file",
    "setup_google_auth",
]
