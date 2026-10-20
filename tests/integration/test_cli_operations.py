"""Operational CLI commands: authorization, revocation, retries, holds, tombstones, restore."""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from calendar_bridge import cli as cli_module
from calendar_bridge.app import open_app
from calendar_bridge.errors import AuthError
from calendar_bridge.providers.google import auth as gauth
from calendar_bridge.providers.icloud.client import CalDAVClient
from tests.conftest import Bridge, TestProviders


@pytest.fixture
def run(bridge: Bridge, monkeypatch: pytest.MonkeyPatch) -> Any:
    providers = TestProviders(bridge.google_client, bridge.icloud_client)
    monkeypatch.setattr(cli_module, "load_settings", lambda config=None: bridge.app.settings)
    monkeypatch.setattr(cli_module, "open_app", lambda settings, **kw: open_app(settings, providers=providers))
    runner = CliRunner()

    def invoke(*args: str, input: str | None = None, code: int | None = 0) -> str:
        result = runner.invoke(cli_module.cli, list(args), input=input, catch_exceptions=False)
        if code is not None:
            assert result.exit_code == code, result.output
        return result.output

    return invoke


def test_auth_icloud_discovers_principal_and_refuses_account_switch(run: Any, bridge: Bridge) -> None:
    password = bridge.app.settings.data_dir / "pw"
    password.parent.mkdir(parents=True, exist_ok=True)
    password.write_text(bridge.server.password)
    out = run(
        "auth",
        "icloud",
        "--apple-id",
        bridge.server.user,
        "--password-file",
        str(password),
        "--url",
        bridge.server.base_url,
    )
    assert "iCloud authorized" in out
    assert bridge.server.password not in out
    assert bridge.app.repo.get_account("icloud")["identity"].endswith("/")
    with bridge.app.db.tx():
        bridge.app.repo.set_account("icloud", "https://other/principal/", "other")
    out = run(
        "auth",
        "icloud",
        "--apple-id",
        bridge.server.user,
        "--password-file",
        str(password),
        "--url",
        bridge.server.base_url,
        code=1,
    )
    assert "different CalDAV principal" in out


def test_auth_icloud_refuses_a_non_app_password_before_contacting_apple(
    run: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []

    def discover(self: CalDAVClient) -> Any:
        sent.append(self.session.auth.password)
        raise AuthError("stopped before reaching Apple", provider="icloud")

    monkeypatch.setattr(CalDAVClient, "discover", discover)
    apple = ("auth", "icloud", "--apple-id", "me@icloud.com", "--url", "https://caldav.icloud.com/")
    out = run(*apple, input="MyMainPassword1!\n", code=1)
    assert "not an app-specific password, so nothing was sent" in out and not sent
    assert "stopped before reaching Apple" in run(*apple, input="ABCD efgh ijklmnop\n", code=1)
    assert sent == ["abcd-efgh-ijkl-mnop"]


def test_auth_google_paste_flow(run: Any, bridge: Bridge, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    secrets = tmp_path / "client_secret.json"
    secrets.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "csecret"}}))
    token = {"refresh_token": "refresh-xyz", "token": "access", "expiry": None, "scopes": list(gauth.SCOPES_MINIMAL)}
    monkeypatch.setattr(gauth.ConsentFlow, "finish", lambda self, url: token)
    monkeypatch.setattr(gauth, "authorized_session", lambda creds: bridge.google.session())
    out = run("auth", "google", "--client-secrets", str(secrets), input="http://127.0.0.1:8765/?code=abc&state=s\n")
    assert "accounts.google.com" in out and "Google authorized for me@gmail.com" in out
    assert "refresh-xyz" not in out
    assert bridge.app.repo.get_account("google")["identity"] == "me@gmail.com"
    assert bridge.app.repo.get_meta("credentials_changed_at")


def test_revoke_icloud_keeps_sync_state(run: Any, bridge: Bridge) -> None:
    bridge.map()
    bridge.sync_ok()
    run("auth", "revoke", "icloud", "--yes")
    assert "icloud" not in bridge.app.repo.credential_names()
    assert bridge.app.repo.get_account("icloud")["auth_state"] == "revoked"
    assert bridge.app.repo.mappings(("active",)), "mappings and items are preserved"


def test_retry_hold_release_tombstone_and_pause(run: Any, bridge: Bridge) -> None:
    bridge.map()
    event_id = bridge.g_create("Tombstoned", "2030-10-01T09:00:00", "2030-10-01T10:00:00")
    bridge.sync_ok()
    bridge.g_delete(event_id)
    bridge.sync_ok()
    assert bridge.i_resources() == {}
    assert "Tombstoned" in run("tombstones", "list")
    out = run("tombstones", "restore", "1")
    assert "Restored as item" in out
    bridge.sync_ok()
    assert [e["summary"] for e in bridge.g_live()] == ["Tombstoned"]
    assert len(bridge.i_resources()) == 1

    with bridge.app.db.tx():
        bridge.app.repo.set_hold(bridge.mapping_id, "test hold")
    assert "SUSPENDED" in run("safety", "status")
    run("safety", "release", "--mapping", str(bridge.mapping_id), "--yes")
    assert not bridge.app.repo.mapping(bridge.mapping_id).destructive_hold

    with bridge.app.db.tx():
        bridge.app.repo.update_item(2, state="failed", state_reason="boom")
    assert "queued for retry" in run("items", "retry", "--all-failed")
    assert bridge.app.repo.item(2).state == "pending"

    run("mappings", "pause", str(bridge.mapping_id))
    assert bridge.app.repo.mapping(bridge.mapping_id).state == "paused"
    run("mappings", "resume", str(bridge.mapping_id))
    assert bridge.app.repo.mapping(bridge.mapping_id).state == "active"
    run("mappings", "remove", str(bridge.mapping_id), "--yes")
    assert bridge.app.repo.mapping(bridge.mapping_id).state == "removed"
    assert len(bridge.i_resources()) == 1 and len(bridge.g_live()) == 1, "removing a mapping deletes nothing"


def test_db_restore_and_key_rotation(run: Any, bridge: Bridge) -> None:
    bridge.map()
    bridge.sync_ok()
    run("db", "backup", "--label", "manual")
    backup = next(bridge.app.settings.backup_dir.iterdir())
    bridge.app.close()
    out = run("db", "restore", str(backup), "--yes")
    assert "Restored from" in out
    assert "Re-encrypted" in run("keys", "rotate")


def test_publish_status_and_alert_test(run: Any, bridge: Bridge) -> None:
    bridge.map()
    bridge.sync_ok()
    assert "turned off" in run("publish-status", code=1)
    bridge.app.settings.status_calendar = True
    assert "Status published" in run("publish-status", "--force")
    assert "no alert channel configured" in run("test-alert", code=1)


def test_guards_against_risky_or_mistyped_commands(run: Any, bridge: Bridge, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge.map()
    event_id = bridge.g_create("Physio", "2030-10-02T09:00:00", "2030-10-02T10:00:00")
    bridge.sync_ok()
    assert "no deletions are held" in run("safety", "release", "--mapping", str(bridge.mapping_id), "--yes", code=1)
    assert not bridge.app.repo.get_meta(f"release_once:{bridge.mapping_id}"), "nothing was armed"

    bridge.g_delete(event_id)
    bridge.sync_ok()
    (tombstone,) = bridge.items("deleted")
    run("tombstones", "restore", str(tombstone.id))
    assert "already restored" in run("tombstones", "restore", str(tombstone.id), code=1)
    assert "as item" in run("tombstones", "list")
    bridge.sync_ok()
    assert [e["summary"] for e in bridge.g_live()] == ["Physio"]

    bridge.g_patch(bridge.g_live()[0]["id"], {"summary": "Physio (G)", "location": "Room 1"})
    bridge.i_edit("Physio", lambda ics: ics.replace("SUMMARY:Physio", "SUMMARY:Physio (I)"))
    bridge.sync_ok()
    (conflict,) = bridge.conflicts()
    resolve = ("conflicts", "resolve", str(conflict.id), "--merge")
    assert "NAME=google" in run(*resolve, "--field", "summary", code=1)
    assert "not one of this conflict's fields" in run(*resolve, "--field", "summery=google", code=1)
    assert "goes with --merge" in run(
        "conflicts", "resolve", str(conflict.id), "--keep", "google", "--field", "summary=google", code=1
    )
    assert "Resolution recorded" in run(*resolve, "--field", "summary=icloud")

    run("mappings", "remove", str(bridge.mapping_id), "--yes")
    assert "was removed" in run("mappings", "pause", str(bridge.mapping_id), code=1)
    assert "is not one of" in run("items", "list", "--state", "faild", code=2)


def test_test_alert_fails_when_a_channel_fails(run: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import requests

    class Broken:
        name = "webhook"

        def send(self, title: str, message: str, severity: str) -> None:
            response = requests.Response()
            response.status_code = 404
            raise requests.HTTPError("not found", response=response)

    monkeypatch.setattr("calendar_bridge.status.alerts.channels_from_settings", lambda settings: [Broken()])
    assert "webhook: FAILED (HTTPError HTTP 404)" in run("test-alert", code=1)


def test_key_problems_are_reported_by_init_and_doctor(
    run: Any, bridge: Bridge, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    key_file = tmp_path / "calendar_bridge_key"
    key_file.write_text("not-a-key\n")
    monkeypatch.delenv("CB_ENCRYPTION_KEY")
    bridge.app.settings.key_file = key_file
    assert "Encryption key : PROBLEM - the encryption key is not valid" in run("init")
    assert "[FAIL] encryption key" in run("doctor", code=1)
    key_file.write_text("")
    assert "Replaced the empty key file" in run("keys", "init", str(key_file))
    assert "Encryption key : configured" in run("init")
