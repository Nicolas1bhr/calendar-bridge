"""The guided setup, the activity log and uninstall, driven through the CLI."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from calendar_bridge import cli as cli_module
from calendar_bridge.app import open_app
from calendar_bridge.providers.google import auth as gauth
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


@pytest.fixture
def google_consent(bridge: Bridge, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A downloaded client JSON, and Google's consent answered without a browser."""
    secrets = tmp_path / "client_secret.json"
    secrets.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "csecret"}}))
    token = {"refresh_token": "refresh-xyz", "token": "access", "expiry": None, "scopes": list(gauth.SCOPES_MINIMAL)}
    monkeypatch.setattr(gauth.ConsentFlow, "finish", lambda self, url: token)
    monkeypatch.setattr(gauth, "authorized_session", lambda creds: bridge.google.session())
    return secrets


def test_setup_connects_both_accounts_maps_and_starts(run: Any, bridge: Bridge, google_consent: Path) -> None:
    bridge.g_create("From Google", "2030-08-01T09:00:00", "2030-08-01T10:00:00")
    answers = [
        "{not json",  # a broken paste is explained and asked again
        str(google_consent),
        "http://127.0.0.1:8765/?code=abc&state=s",
        bridge.server.user,
        bridge.server.password,
        "1",  # iCloud calendar: Home
        "1",  # Google calendar: the primary one is listed first
        "n",  # do not list the events
        "y",  # start
    ]
    out = run("setup", input="\n".join(answers) + "\n")
    assert "not complete JSON" in out
    assert "Google authorized for me@gmail.com" in out and "iCloud authorized" in out
    assert "Verdict: SAFE" in out and "Started:" in out
    assert "docker compose up -d" in out and "CB_ALERT_NTFY_URL=https://ntfy.sh/calendar-bridge-" in out
    assert "refresh-xyz" not in out
    # The test server's password happens to be the example shown in the prompt; it is never echoed.
    assert out.count(bridge.server.password) == out.count("(abcd-efgh-ijkl-mnop, stays invisible)")
    (mapping,) = bridge.app.repo.mappings(("active",))
    assert mapping.name == "main"
    bridge.icloud_url = bridge.app.repo.calendar(mapping.icloud_calendar_id).remote_id
    assert bridge.i_resources() == {}, "nothing is copied before the service runs"

    run("sync")
    bridge.i_by_summary("From Google")

    again = run("setup", input="n\nn\n")
    assert "Connected as me@gmail.com" in again and "Already set up: 'main' (active)" in again
    assert "Nothing new to review." in again


def test_setup_without_a_key_explains_how_to_create_one(run: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CB_ENCRYPTION_KEY")
    out = run("setup", code=1)
    assert "docker compose run --rm keygen" in out


def test_activity_lists_what_the_bridge_changed(run: Any, bridge: Bridge) -> None:
    assert "Nothing has happened yet." in run("activity")
    bridge.map()
    event_id = bridge.g_create("Dentist", "2030-08-01T09:00:00", "2030-08-01T10:00:00")
    bridge.sync_ok()
    bridge.g_patch(event_id, {"summary": "Dentist (moved)"})
    bridge.sync_ok()
    bridge.g_delete(event_id)
    bridge.sync_ok()
    out = run("activity")
    assert "copied to iCloud" in out and "updated in iCloud (summary)" in out
    assert "deleted in iCloud (deleted in Google first)" in out and "Dentist" in out
    assert "Dentist" not in run("activity", "--no-titles")
    rows = json.loads(run("activity", "--json"))
    assert [r["action"] for r in rows][:3] == ["deleted", "updated", "created"]


def test_uninstall_removes_access_and_the_status_calendar_but_no_event(
    run: Any, bridge: Bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    revoked: list[str] = []
    monkeypatch.setattr(gauth, "revoke", lambda token, timeout=30.0: revoked.append(token) or True)
    bridge.map()
    bridge.g_create("Keep me", "2030-08-01T09:00:00", "2030-08-01T10:00:00")
    bridge.sync_ok()
    bridge.app.settings.status_calendar = True
    run("publish-status", "--force")
    status_id = bridge.app.repo.status_calendar().remote_id
    assert status_id in bridge.google.calendars

    out = run("uninstall", input="y\n")
    assert "Deleted the status calendar." in out and "docker compose down -v" in out
    assert status_id not in bridge.google.calendars
    assert revoked == ["test-refresh"]
    assert bridge.app.repo.credential_names() == []
    assert bridge.app.repo.mapping(bridge.mapping_id).state == "paused"
    assert [e["summary"] for e in bridge.g_live()] == ["Keep me"] and len(bridge.i_resources()) == 1


def test_keys_init_creates_a_key_once_and_replaces_dockers_empty_directory(tmp_path: Path) -> None:
    target = tmp_path / "secrets" / "calendar_bridge_key"
    target.mkdir(parents=True)  # what Docker leaves behind when the file is missing
    runner = CliRunner()
    first = runner.invoke(cli_module.cli, ["keys", "init", str(target)], catch_exceptions=False)
    assert first.exit_code == 0, first.output
    assert "Removed the empty directory" in first.output and target.is_file()
    key = target.read_text()
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o400
    second = runner.invoke(cli_module.cli, ["keys", "init", str(target)], catch_exceptions=False)
    assert second.exit_code == 0 and "left unchanged" in second.output
    assert target.read_text() == key, "an existing key is never replaced"
