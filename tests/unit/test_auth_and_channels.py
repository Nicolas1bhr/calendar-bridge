"""OAuth flow pieces, credential storage, alert delivery channels."""

from __future__ import annotations

import json
import socket
import threading
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from calendar_bridge.clock import SystemClock
from calendar_bridge.config import Settings
from calendar_bridge.credentials import CredentialStore, StoredCredentialProviders
from calendar_bridge.crypto import SecretBox, generate_key
from calendar_bridge.db.database import Database
from calendar_bridge.db.repo import Repo
from calendar_bridge.errors import AuthError, ConfigError, CredentialsMissing
from calendar_bridge.providers.google import auth as gauth
from calendar_bridge.status import alerts


def test_client_secrets_must_be_desktop_type(tmp_path: Path) -> None:
    desktop = tmp_path / "desktop.json"
    desktop.write_text(json.dumps({"installed": {"client_id": "id", "client_secret": "secret"}}))
    assert gauth.load_client_secrets(desktop).client_id == "id"
    web = tmp_path / "web.json"
    web.write_text(json.dumps({"web": {"client_id": "id", "client_secret": "secret"}}))
    with pytest.raises(ConfigError, match="Desktop app"):
        gauth.load_client_secrets(web)
    with pytest.raises(ConfigError):
        gauth.scopes_for("everything")


def test_client_secrets_from_a_path_or_pasted_json(tmp_path: Path) -> None:
    content = json.dumps({"installed": {"client_id": "id", "client_secret": "secret"}})
    downloaded = tmp_path / "client secret.json"
    downloaded.write_text(content, encoding="utf-8-sig")  # a byte-order mark must not matter
    assert gauth.client_secrets_from_input(f'"{downloaded}"').client_id == "id"
    assert gauth.client_secrets_from_input(f"  {content}  ").client_secret == "secret"
    with pytest.raises(ConfigError, match="complete JSON"):
        gauth.client_secrets_from_input(content[:-3])
    with pytest.raises(ConfigError, match="client_id"):
        gauth.client_secrets_from_input(json.dumps({"installed": {"client_id": "id"}}))
    with pytest.raises(ConfigError, match="cannot read"):
        gauth.client_secrets_from_input(str(tmp_path / "missing.json"))


def test_consent_url_requests_offline_minimal_scopes_with_pkce() -> None:
    flow = gauth.ConsentFlow(gauth.ClientSecrets("cid", "csecret"), gauth.SCOPES_MINIMAL, 8765)
    query = parse_qs(urlsplit(flow.authorization_url()).query)
    assert query["access_type"] == ["offline"] and query["prompt"] == ["consent select_account"]
    assert query["redirect_uri"] == ["http://127.0.0.1:8765/"]
    assert query["code_challenge_method"] == ["S256"] and query["code_challenge"]
    assert set(query["scope"][0].split()) == set(gauth.SCOPES_MINIMAL)
    assert "https://www.googleapis.com/auth/calendar" not in query["scope"][0].split()
    with pytest.raises(AuthError, match=r"does not start with http://127.0.0.1:8765/"):
        flow.finish("https://evil.example/?code=x")
    with pytest.raises(AuthError, match="not allowed on Google's consent page"):
        flow.finish("http://127.0.0.1:8765/?error=access_denied&state=s")


def test_local_server_opens_the_address_already_shown_and_logs_no_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    flow = gauth.ConsentFlow(gauth.ClientSecrets("cid", "csecret"), gauth.SCOPES_MINIMAL, port)
    shown = flow.authorization_url()
    opened: list[str] = []
    browsers: list[threading.Thread] = []

    def fake_browser(url: str) -> bool:
        opened.append(url)
        state = parse_qs(urlsplit(url).query)["state"][0]
        redirect = f"http://127.0.0.1:{port}/?state={state}&code=secret-code"
        browsers.append(threading.Thread(target=lambda: urllib.request.urlopen(redirect, timeout=10).read()))
        browsers[-1].start()
        return True

    monkeypatch.setattr(webbrowser, "open", fake_browser)
    monkeypatch.setattr(flow, "finish", lambda url: {"redirect": url})
    result = flow.run_local_server(open_browser=True)
    browsers[0].join(timeout=10)
    assert opened == [shown]
    assert parse_qs(urlsplit(result["redirect"]).query)["state"] == parse_qs(urlsplit(shown).query)["state"]
    assert "secret-code" not in capsys.readouterr().err


def test_listener_never_shares_a_taken_port() -> None:
    with socket.socket() as other:
        # Like most HTTP servers, Python's included: on Windows this used to let both listen.
        other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        other.bind(("127.0.0.1", 0))
        other.listen()
        taken = other.getsockname()[1]
        flow = gauth.ConsentFlow(gauth.ClientSecrets("cid", "csecret"), gauth.SCOPES_MINIMAL, taken)
        flow.listen()
        try:
            redirect = parse_qs(urlsplit(flow.authorization_url()).query)["redirect_uri"][0]
            assert redirect == flow.redirect_uri and redirect != f"http://127.0.0.1:{taken}/"
        finally:
            flow.close()
        shown = gauth.ConsentFlow(gauth.ClientSecrets("cid", "csecret"), gauth.SCOPES_MINIMAL, taken)
        shown.authorization_url()
        with pytest.raises(AuthError, match="in use by another program"):
            shown.listen()


def test_finish_rejects_missing_scopes_and_missing_refresh_token(monkeypatch: pytest.MonkeyPatch) -> None:
    flow = gauth.ConsentFlow(gauth.ClientSecrets("cid", "csecret"), gauth.SCOPES_MINIMAL, 8765)

    class Creds:
        refresh_token = None
        granted_scopes = list(gauth.SCOPES_MINIMAL)
        scopes = list(gauth.SCOPES_MINIMAL)
        token = "t"
        expiry = None

    monkeypatch.setattr(flow._flow, "fetch_token", lambda **kw: None)
    monkeypatch.setattr(type(flow._flow), "credentials", property(lambda self: Creds))
    with pytest.raises(AuthError, match="refresh token"):
        flow.finish("http://127.0.0.1:8765/?code=abc&state=s")
    Creds.refresh_token = "r"
    Creds.granted_scopes = list(gauth.SCOPES_MINIMAL[:1])
    with pytest.raises(AuthError, match="missing: create its own calendars, manage events on your calendars"):
        flow.finish("http://127.0.0.1:8765/?code=abc&state=s")
    Creds.granted_scopes = list(gauth.SCOPES_MINIMAL)
    assert flow.finish("http://127.0.0.1:8765/?code=abc&state=s")["refresh_token"] == "r"

    def unticked(**kw: Any) -> None:  # what oauthlib does when the user unticks a permission
        warning = Warning("Scope has changed")
        warning.new_scope = list(gauth.SCOPES_MINIMAL[:2])  # type: ignore[attr-defined]
        raise warning

    monkeypatch.setattr(flow._flow, "fetch_token", unticked)
    with pytest.raises(AuthError, match=r"missing: create its own calendars\)"):
        flow.finish("http://127.0.0.1:8765/?code=abc&state=s")


def test_credential_store_and_provider_construction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = Database(tmp_path / "x.db", SystemClock())
    db.migrate()
    repo = Repo(db)
    store = CredentialStore(repo, SecretBox([generate_key()]))
    settings = Settings(data_dir=tmp_path, key_file=None)
    providers = StoredCredentialProviders(settings, store)
    with pytest.raises(CredentialsMissing):
        providers.google()
    with pytest.raises(CredentialsMissing):
        providers.icloud()
    with db.tx():
        store.save_google_client_secrets(gauth.ClientSecrets("cid", "csecret"))
        store.save_google_token(
            {"refresh_token": "refresh-token-one", "token": None, "scopes": list(gauth.SCOPES_MINIMAL)}
        )
        store.save_icloud(username="me@icloud.com", password="abcd-efgh-ijkl-mnop", url="https://caldav.icloud.com/")
    raw = db.query("SELECT ciphertext FROM credentials")
    assert all("abcd-efgh" not in r[0] and "refresh-token-one" not in r[0] for r in raw), "secrets are stored encrypted"
    assert providers.google() is providers.google()
    assert providers.icloud().username == "me@icloud.com"
    providers._google_creds._refresh_token = "refresh-token-two"  # Google rotated the refresh token
    providers.after_cycle()
    assert store.google_token()["refresh_token"] == "refresh-token-two"
    with db.tx():
        assert store.rotate() == 3


def test_revoke_posts_token(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class Response:
        status_code = 200

    def fake_post(url: str, data: dict[str, str], **kw: Any) -> Response:
        seen.update(url=url, data=data)
        return Response()

    monkeypatch.setattr(gauth.requests, "post", fake_post)
    assert gauth.revoke("refresh-token")
    assert seen == {"url": gauth.REVOKE_URI, "data": {"token": "refresh-token"}}

    def offline(*args: Any, **kw: Any) -> Response:
        raise gauth.requests.exceptions.ConnectionError("no network")

    monkeypatch.setattr(gauth.requests, "post", offline)
    assert gauth.revoke("refresh-token") is False, "unconfirmed, so the caller still deletes the token"


def test_alert_channels(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    posts: list[tuple[str, Any, dict[str, str]]] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

    def fake_post(url: str, data: Any = None, json: Any = None, headers: Any = None, timeout: Any = None) -> Response:
        posts.append((url, data if data is not None else json, headers or {}))
        return Response()

    monkeypatch.setattr(alerts.requests, "post", fake_post)
    token_file = tmp_path / "ntfy"
    token_file.write_text("tk")
    settings = Settings(
        data_dir=tmp_path,
        alert_ntfy_url="https://ntfy.sh/private-topic",
        alert_ntfy_token_file=token_file,
        alert_webhook_url="https://hooks.example/abc",
    )
    channels = alerts.channels_from_settings(settings)
    assert [c.name for c in channels] == ["ntfy", "webhook"]
    for channel in channels:
        channel.send("Problem: x", "message", "critical")
    assert posts[0][2]["Authorization"] == "Bearer tk" and posts[0][2]["Priority"] == "high"
    assert posts[1][1]["text"].startswith("Problem: x")

    sent: list[Any] = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            sent.append(("connect", host, port))

        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def starttls(self, context: Any) -> None:
            sent.append("starttls")

        def login(self, user: str, password: str) -> None:
            sent.append(("login", user))

        def send_message(self, msg: Any) -> None:
            sent.append(("send", msg["To"], msg["Subject"]))

    password = tmp_path / "smtp"
    password.write_text("pw")
    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    email = alerts.EmailChannel(
        Settings(
            data_dir=tmp_path,
            alert_email_to="me@example.com",
            alert_smtp_host="smtp.example",
            alert_smtp_user="bot",
            alert_smtp_password_file=password,
        )
    )
    email.send("Problem: y", "body", "warning")
    assert "starttls" in sent and ("login", "bot") in sent and sent[-1][1] == "me@example.com"


def test_heartbeat_ping(monkeypatch: pytest.MonkeyPatch) -> None:
    urls: list[str] = []
    monkeypatch.setattr(alerts.requests, "get", lambda url, timeout: urls.append(url))
    alerts.ping_heartbeat("https://hc-ping.com/uuid", True, 5)
    alerts.ping_heartbeat("https://hc-ping.com/uuid", False, 5)
    alerts.ping_heartbeat(None, True, 5)
    assert urls == ["https://hc-ping.com/uuid", "https://hc-ping.com/uuid/fail"]
