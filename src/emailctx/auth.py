"""Google OAuth: Gmail read-only, plus drive.file (only files the app itself creates).

`emailctx auth` runs the installed-app flow in the browser once. The resulting token
(including the refresh token) is stored in the OS keyring: Windows Credential Manager,
macOS Keychain or the Secret Service on Linux, all encrypted at rest by the OS. It is
never written to a project file and never printed or logged; errors mention only what
to do next.

Scopes come from config and are validated there: only gmail.readonly, drive.readonly
and drive.file are accepted.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from emailctx.config import ALLOWED_SCOPES, Settings


class AuthError(RuntimeError):
    pass


class TokenStore(Protocol):
    def load(self) -> str | None: ...
    def save(self, token_json: str) -> None: ...
    def clear(self) -> None: ...


class KeyringTokenStore:
    SERVICE = "emailctx"
    USERNAME = "google-oauth-token"

    def load(self) -> str | None:
        import keyring

        return keyring.get_password(self.SERVICE, self.USERNAME)

    def save(self, token_json: str) -> None:
        import keyring

        keyring.set_password(self.SERVICE, self.USERNAME, token_json)

    def clear(self) -> None:
        import keyring
        from keyring.errors import PasswordDeleteError

        try:
            keyring.delete_password(self.SERVICE, self.USERNAME)
        except PasswordDeleteError:
            pass


class MemoryTokenStore:
    """For tests."""

    def __init__(self, token_json: str | None = None) -> None:
        self.token_json = token_json

    def load(self) -> str | None:
        return self.token_json

    def save(self, token_json: str) -> None:
        self.token_json = token_json

    def clear(self) -> None:
        self.token_json = None


def _check_scopes(scopes: list[str]) -> None:
    if not scopes or set(scopes) - ALLOWED_SCOPES:
        raise AuthError("only read-only Google scopes (plus drive.file) are allowed")


def run_auth_flow(settings: Settings, store: TokenStore, flow_factory: Any | None = None) -> None:
    """Open the browser consent screen and store the resulting token."""
    scopes = settings.google.scopes
    _check_scopes(scopes)
    secrets = settings.google.client_secrets
    if not secrets.exists():
        raise AuthError(
            f"OAuth client file not found: {secrets}. In Google Cloud Console create an "
            "OAuth client of type 'Desktop app', download its JSON there, or set "
            "google.client_secrets in config."
        )
    if flow_factory is None:
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow_factory = InstalledAppFlow.from_client_secrets_file
    flow = flow_factory(str(secrets), scopes)
    creds = flow.run_local_server(port=0, open_browser=True)
    granted = set(creds.scopes or scopes)
    if not set(scopes) <= granted:
        raise AuthError("the consent screen did not grant all requested scopes")
    store.save(creds.to_json())


def load_credentials(settings: Settings, store: TokenStore) -> Any:
    """Return valid credentials, refreshing (and re-storing) them when expired."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    scopes = settings.google.scopes
    _check_scopes(scopes)
    raw = store.load()
    if not raw:
        raise AuthError("not signed in to Google: run `emailctx auth`")
    try:
        creds = Credentials.from_authorized_user_info(json.loads(raw), scopes)
    except (ValueError, KeyError):
        raise AuthError("stored Google token is unreadable: run `emailctx auth` again") from None
    if creds.scopes and not set(scopes) <= set(creds.scopes):
        raise AuthError("stored Google token lacks the configured scopes: run `emailctx auth` again")
    if not creds.valid:
        if not creds.refresh_token:
            raise AuthError("Google sign-in expired: run `emailctx auth` again")
        try:
            creds.refresh(Request())
        except RefreshError:
            raise AuthError("Google sign-in was revoked or expired: run `emailctx auth` again") from None
        store.save(creds.to_json())
    return creds


def gmail_service(creds: Any) -> Any:
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def drive_service(creds: Any) -> Any:
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=creds, cache_discovery=False)
