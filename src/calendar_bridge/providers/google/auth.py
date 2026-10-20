"""Google OAuth 2.0 for an installed ("Desktop app") client with offline access.

The bridge authenticates as its *own* OAuth client, separately from ChatGPT's connector. The
consent step runs once through ``calendar-bridge auth google``: the user opens the printed URL in
any browser, approves, and the redirect (to a loopback address) carries the authorization code.
On a headless server the user pastes that redirect URL back into the terminal; PKCE and the
``state`` parameter protect the exchange either way.
"""

from __future__ import annotations

import dataclasses
import json
import os
import socket
import time
import wsgiref.simple_server
import wsgiref.util
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import requests

from calendar_bridge.errors import AuthError, ConfigError, TransientError

# Least privilege: read the calendar list, manage events only on calendars the user owns, and
# create/manage the calendars the bridge itself creates (status calendar, extra mappings).
SCOPES_MINIMAL = (
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events.owned",
    "https://www.googleapis.com/auth/calendar.app.created",
)
# Needed only to sync a Google calendar that someone else owns and shared with write access.
SCOPES_SHARED = (
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.app.created",
)
TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"


@dataclasses.dataclass(frozen=True)
class ClientSecrets:
    client_id: str
    client_secret: str
    token_uri: str = TOKEN_URI
    auth_uri: str = "https://accounts.google.com/o/oauth2/auth"

    def to_json(self) -> dict[str, str]:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ClientSecrets:
        return cls(**{k: data[k] for k in ("client_id", "client_secret", "token_uri", "auth_uri") if k in data})

    def oauthlib_config(self, redirect_uri: str) -> dict[str, Any]:
        return {
            "installed": {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "auth_uri": self.auth_uri,
                "token_uri": self.token_uri,
                "redirect_uris": [redirect_uri],
            }
        }


def parse_client_secrets(data: Any) -> ClientSecrets:
    """The JSON Google Cloud Console offers for download when it creates an OAuth client."""
    if isinstance(data, dict) and "web" in data:
        raise ConfigError("this is a 'Web application' OAuth client; create one of type 'Desktop app' instead")
    section = data.get("installed") if isinstance(data, dict) else None
    if not isinstance(section, dict):
        raise ConfigError("not an OAuth client file: expected the JSON of a 'Desktop app' client from Google Cloud")
    if not section.get("client_id") or not section.get("client_secret"):
        raise ConfigError("the OAuth client JSON has no client_id/client_secret; download it again from Google Cloud")
    return ClientSecrets(
        client_id=str(section["client_id"]),
        client_secret=str(section["client_secret"]),
        token_uri=str(section.get("token_uri") or TOKEN_URI),
        auth_uri=str(section.get("auth_uri") or "https://accounts.google.com/o/oauth2/auth"),
    )


def load_client_secrets(path: Path) -> ClientSecrets:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read OAuth client file {path}: {exc}") from exc
    return parse_client_secrets(data)


def client_secrets_from_input(text: str) -> ClientSecrets:
    """A path to the downloaded client JSON, or its contents pasted as they are."""
    text = text.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ConfigError("that is not complete JSON; paste the whole content of the file") from exc
        return parse_client_secrets(data)
    return load_client_secrets(Path(text.strip("'\"")))


def scopes_for(profile: str) -> tuple[str, ...]:
    if profile == "shared":
        return SCOPES_SHARED
    if profile == "minimal":
        return SCOPES_MINIMAL
    raise ConfigError("scope profile must be 'minimal' or 'shared'")


class _LoopbackServer(wsgiref.simple_server.WSGIServer):
    """Owns its port. On Windows, SO_REUSEADDR lets a second program listen on a port that is
    already taken, and the redirect (authorization code included) then reaches the first one."""

    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()


class _QuietHandler(wsgiref.simple_server.WSGIRequestHandler):
    timeout = 10  # a connection that sends nothing must not block the wait for Google's answer

    def log_message(self, *args: Any, **kwargs: Any) -> None:
        """The request line carries the authorization code: keep it out of the terminal."""


class ConsentFlow:
    """Wraps google-auth-oauthlib's Flow with PKCE for the loopback redirect."""

    def __init__(self, secrets: ClientSecrets, scopes: tuple[str, ...], port: int) -> None:
        self.secrets = secrets
        self.scopes = scopes
        self._url: str | None = None
        self._server: _LoopbackServer | None = None
        self._captured: dict[str, str] = {}
        self._use_port(port)

    def _use_port(self, port: int) -> None:
        from google_auth_oauthlib.flow import Flow

        self.redirect_uri = f"http://127.0.0.1:{port}/"
        self._flow = Flow.from_client_config(
            self.secrets.oauthlib_config(self.redirect_uri),
            scopes=list(self.scopes),
            redirect_uri=self.redirect_uri,
            autogenerate_code_verifier=True,
        )
        self._url = None

    def listen(self) -> None:
        """Bind the redirect listener before the consent address is built: on the configured port
        when it is free, otherwise on any free one (Google accepts any loopback port for Desktop
        clients), so that another program's listener never receives the redirect."""
        if self._server is not None:
            return
        port = int(self.redirect_uri.rsplit(":", 1)[1].strip("/"))
        try:
            self._server = self._bind(port)
        except OSError:
            if self._url is not None:
                raise AuthError(f"port {port} on 127.0.0.1 is in use by another program", provider="google") from None
            self._server = self._bind(0)
            self._use_port(self._server.server_port)

    def _bind(self, port: int) -> _LoopbackServer:
        return wsgiref.simple_server.make_server(
            "127.0.0.1", port, self._capture, server_class=_LoopbackServer, handler_class=_QuietHandler
        )

    def _capture(self, environ: dict[str, Any], start_response: Any) -> list[bytes]:
        query = parse_qs(environ.get("QUERY_STRING", ""))
        start_response("200 OK", [("Content-Type", "text/plain; charset=utf-8")])
        if "code" not in query and "error" not in query:  # e.g. the browser asking for a favicon
            return [b"Calendar Bridge is waiting for Google's answer."]
        self._captured["url"] = wsgiref.util.request_uri(environ)
        return [b"Calendar Bridge: answer received. You can close this tab."]

    def close(self) -> None:
        if self._server is not None:
            self._server.server_close()
            self._server = None

    def authorization_url(self) -> str:
        # Built once: every call to the underlying flow mints a new ``state``, which would make the
        # address already shown to the user fail with a state mismatch. ``select_account`` makes the
        # user pick the account explicitly instead of silently using whichever one is signed in.
        if self._url is None:
            url, _state = self._flow.authorization_url(
                access_type="offline", prompt="consent select_account", include_granted_scopes="false"
            )
            self._url = str(url)
        return self._url

    def finish(self, redirect_url: str, *, timeout: float = 30.0) -> dict[str, Any]:
        from oauthlib.oauth2.rfc6749 import errors as oauth

        redirect_url = redirect_url.strip()
        if not redirect_url.startswith(self.redirect_uri.rstrip("/")):
            raise AuthError(
                f"the pasted address does not start with {self.redirect_uri}; copy the whole address the "
                "browser was sent to after you allowed access",
                provider="google",
            )
        refused = parse_qs(urlsplit(redirect_url).query).get("error")
        if refused:
            raise AuthError(
                "access was not allowed on Google's consent page"
                + ("" if refused[0] == "access_denied" else f" ({refused[0]})")
                + "; start again and choose Continue/Allow",
                provider="google",
            )
        # oauthlib insists on https; Google explicitly allows http for loopback redirects.
        secure = "https://" + redirect_url[len("http://") :]
        try:
            self._flow.fetch_token(authorization_response=secure, timeout=timeout)
        except Warning as exc:  # oauthlib raises this when fewer permissions were granted
            raise _missing_permissions(set(self.scopes) - set(getattr(exc, "new_scope", None) or [])) from exc
        except oauth.MismatchingStateError as exc:
            raise AuthError(
                "that address belongs to an earlier attempt; use the address from this attempt", provider="google"
            ) from exc
        except oauth.InvalidGrantError as exc:
            raise AuthError(
                "Google refused the code: it expired or was already used. Start again", provider="google"
            ) from exc
        except oauth.OAuth2Error as exc:
            raise AuthError(f"Google refused the authorization ({exc.error})", provider="google") from exc
        except requests.RequestException as exc:
            name = type(exc).__name__
            raise TransientError(f"could not reach Google ({name})", provider="google", reason=name) from exc
        creds = self._flow.credentials
        missing = set(self.scopes) - set(creds.granted_scopes or creds.scopes or [])
        if missing:
            raise _missing_permissions(missing)
        if not creds.refresh_token:
            raise AuthError(
                "Google did not return a refresh token; remove the app's access at "
                "https://myaccount.google.com/permissions and run the authorization again",
                provider="google",
            )
        return token_record(creds)

    def run_local_server(self, open_browser: bool) -> dict[str, Any]:
        """Capture the redirect on 127.0.0.1 (desktop use or SSH port forwarding)."""
        self.listen()
        assert self._server is not None
        deadline = time.monotonic() + 300
        try:
            if open_browser:
                import webbrowser

                webbrowser.open(self.authorization_url())
            while "url" not in self._captured and (remaining := deadline - time.monotonic()) > 0:
                self._server.timeout = remaining
                self._server.handle_request()
        finally:
            self.close()
        if "url" not in self._captured:
            raise AuthError("no answer from Google within 5 minutes; start again", provider="google")
        return self.finish(self._captured["url"])


SCOPE_PURPOSE = {
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly": "see your list of calendars",
    "https://www.googleapis.com/auth/calendar.events.owned": "manage events on your calendars",
    "https://www.googleapis.com/auth/calendar.events": "manage events on calendars shared with you",
    "https://www.googleapis.com/auth/calendar.app.created": "create its own calendars",
}


def _missing_permissions(missing: set[str]) -> AuthError:
    names = ", ".join(SCOPE_PURPOSE.get(scope, scope) for scope in sorted(missing))
    return AuthError(
        f"Google did not grant every permission (missing: {names}). Start again and leave every box ticked "
        "on the consent page",
        provider="google",
    )


def token_record(creds: Any) -> dict[str, Any]:
    return {
        "refresh_token": creds.refresh_token,
        "token": creds.token,
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
        "scopes": sorted(creds.granted_scopes or creds.scopes or []),
    }


def build_credentials(secrets: ClientSecrets, token: dict[str, Any]) -> Any:
    from google.oauth2.credentials import Credentials

    return Credentials(
        token=token.get("token"),
        refresh_token=token["refresh_token"],
        token_uri=secrets.token_uri,
        client_id=secrets.client_id,
        client_secret=secrets.client_secret,
        scopes=token.get("scopes") or list(SCOPES_MINIMAL),
    )


def authorized_session(credentials: Any) -> Any:
    from google.auth.transport.requests import AuthorizedSession

    return AuthorizedSession(credentials)


def revoke(token: str, timeout: float = 30.0) -> bool:
    """Revoke a refresh (or access) token at Google. Returns True if Google confirmed; never
    raises, so the stored token is deleted even when Google cannot be reached."""
    try:
        response = requests.post(
            REVOKE_URI,
            data={"token": token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=timeout,
        )
    except requests.RequestException:
        return False
    return response.status_code == 200
