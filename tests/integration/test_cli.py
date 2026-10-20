"""The command-line interface, driven end to end with the test providers."""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner
from cryptography.fernet import Fernet

from calendar_bridge import cli as cli_module
from calendar_bridge.app import open_app
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


def test_help_and_keys(run: Any) -> None:
    out = run("--help")
    for command in (
        "auth",
        "calendars",
        "mappings",
        "reconcile",
        "sync",
        "daemon",
        "status",
        "health",
        "doctor",
        "conflicts",
        "items",
        "safety",
        "tombstones",
        "db",
    ):
        assert command in out
    key = run("keys", "generate").strip()
    Fernet(key.encode())


def test_setup_flow_mapping_reconcile_sync_status(run: Any, bridge: Bridge) -> None:
    bridge.icloud_url = bridge.server.calendar_url("home")
    bridge.g_create("From Google", "2030-08-01T09:00:00", "2030-08-01T10:00:00")
    assert "Encryption key : configured" in run("init")
    listing = run("calendars")
    assert "Home" in listing and "me@gmail.com" in listing and "Holidays" in listing
    out = run("mappings", "add", "--icloud", "Home", "--google", "primary", "--history-days", "all")
    assert "pending initial reconciliation" in out
    plan = run("reconcile", "--mapping", "1", "--details")
    assert "Verdict: SAFE" in plan and "Dry run only" in plan and "From Google" in plan
    assert bridge.i_resources() == {}, "a dry run writes nothing"
    assert "Mapping activated" in run("reconcile", "--mapping", "1", "--apply")
    run("sync")
    bridge.i_by_summary("From Google")
    status = json.loads(run("status", "--json"))
    assert status["schema"] == "calendar-bridge.status/v1"
    assert status["mappings"][0]["items"]["synced"] == 1
    assert "Overall:" in run("status")
    assert "Home" in run("mappings", "list")
    assert "From Google" in run("items", "list")
    assert "recent writes" in run("items", "show", "1")


def test_status_says_what_to_do_next(run: Any, bridge: Bridge) -> None:
    assert "Next: set up a calendar pair with `calendar-bridge setup`" in run("status")
    bridge.map()
    bridge.sync_ok()
    with bridge.app.db.tx():
        bridge.app.repo.set_hold(bridge.mapping_id, "test hold")
    assert "Next: review the held deletions" in run("status")


def test_health_reflects_heartbeat(run: Any, bridge: Bridge) -> None:
    assert "UNHEALTHY" in run("health", code=1)
    with bridge.app.db.tx():
        bridge.app.repo.set_meta("heartbeat_at", bridge.app.repo.now())
    assert run("health").startswith("OK")
    assert "DEGRADED" in run("health", "--strict", code=2)


def test_conflict_commands(run: Any, bridge: Bridge) -> None:
    bridge.map()
    event_id = bridge.g_create("Contested", "2030-08-02T09:00:00", "2030-08-02T10:00:00")
    bridge.sync_ok()
    bridge.g_patch(event_id, {"summary": "Contested (G)"})
    bridge.i_edit("Contested", lambda ics: ics.replace("SUMMARY:Contested", "SUMMARY:Contested (I)"))
    bridge.sync_ok()
    listing = run("conflicts", "list")
    assert "field_conflict" in listing
    detail = run("conflicts", "show", "1")
    assert "Contested (G)" in detail and "Contested (I)" in detail and "keep_google" in detail
    out = run("conflicts", "resolve", "1", "--keep", "google", "--now")
    assert "resolved" in out
    bridge.i_by_summary("Contested (G)")
    assert "No conflicts." in run("conflicts", "list")


def test_safety_db_and_auth_commands(run: Any, bridge: Bridge) -> None:
    bridge.map()
    bridge.sync_ok()
    assert "deletions allowed" in run("safety", "status")
    assert "integrity ok" in run("db", "check")
    assert "Backup written" in run("db", "backup")
    assert "calendar-bridge-" in run("db", "backups")
    auth = run("auth", "status")
    assert "google" in auth and "icloud" in auth
    doctor = run("doctor", code=None)
    assert "[PASS] Google access" in doctor and "[PASS] iCloud access" in doctor
    assert "[PASS] database integrity" in doctor


def test_mapping_validation_rejects_read_only_and_duplicates(run: Any, bridge: Bridge) -> None:
    run("calendars")
    out = run("mappings", "add", "--icloud", "Home", "--google", "Holidays", code=1)
    assert "read-only" in out
    assert "not 'abc'" in run(
        "mappings", "add", "--icloud", "Home", "--google", "Work", "--history-days", "abc", code=1
    )
    run("mappings", "add", "--icloud", "Home", "--google", "primary")
    out = run("mappings", "add", "--icloud", "Home", "--google", "Work", code=1)
    assert "already part of mapping" in out
    bridge.server.make_calendar("work", "Work")
    out = run("mappings", "add", "--icloud", "Work", "--google", "Work", "--name", "Home", code=1)
    assert "a mapping named 'Home' already exists" in out


def test_unexpected_errors_are_reported_without_a_traceback(
    run: Any, bridge: Bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("something odd")

    monkeypatch.setattr("calendar_bridge.status.report.build_status", broken)
    out = run("status", code=1)
    assert "unexpected error (RuntimeError: something odd)" in out and "Traceback" not in out
