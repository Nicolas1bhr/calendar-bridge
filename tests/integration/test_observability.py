"""Status document, Google status calendar, alerts, daemon cycle."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from calendar_bridge.daemon import Daemon
from calendar_bridge.status.alerts import Alerter, conditions
from calendar_bridge.status.report import SCHEMA, build_status
from calendar_bridge.status.status_calendar import StatusCalendar, status_event_id
from tests.conftest import Bridge


class RecordingChannel:
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, title: str, message: str, severity: str) -> None:
        self.sent.append((title, message, severity))


def test_status_document_reports_verified_icloud_writes(bridge: Bridge) -> None:
    bridge.map()
    bridge.g_create("Verified one", "2030-07-01T09:00:00", "2030-07-01T10:00:00")
    bridge.sync_ok()
    with bridge.app.db.tx():
        bridge.app.repo.set_meta("last_ok_cycle_at", bridge.app.repo.now())
    status = build_status(bridge.app.ctx)
    assert status["schema"] == SCHEMA
    (mapping,) = status["mappings"]
    assert mapping["items"]["synced"] == 1
    assert mapping["last_icloud_write_verified_at"] is not None
    verified = mapping["recently_verified_in_icloud"]
    assert verified and verified[0]["title"] == "Verified one"
    no_titles = build_status(bridge.app.ctx, include_titles=False)
    assert "title" not in no_titles["mappings"][0]["recently_verified_in_icloud"][0]


def test_pending_google_event_is_not_reported_as_in_icloud(bridge: Bridge) -> None:
    bridge.map()
    bridge.g_create("Not yet in iCloud", "2030-07-02T09:00:00", "2030-07-02T10:00:00")
    bridge.server.middleware.faults.append(
        __import__("tests.fakes.caldav_server", fromlist=["DavFault"]).DavFault(
            lambda m, p, b: m == "PUT", "503 Service Unavailable", remaining=5
        )
    )
    bridge.sync()
    status = build_status(bridge.app.ctx)
    (mapping,) = status["mappings"]
    assert mapping["recently_verified_in_icloud"] == []
    assert mapping["pending"] and mapping["pending"][0]["title"] == "Not yet in iCloud"
    assert mapping["pending"][0]["waiting_for"] == "icloud"


def test_status_calendar_event_is_transparent_and_silent(bridge: Bridge) -> None:
    bridge.map()
    bridge.sync_ok()
    publisher = StatusCalendar(bridge.app.ctx, bridge.google_client)
    assert publisher.publish(build_status(bridge.app.ctx), force=True)
    status_cal = bridge.app.repo.status_calendar()
    assert status_cal is not None and status_cal.role == "status"
    today = dt.datetime.now(dt.UTC).date()
    events = bridge.google.calendars[status_cal.remote_id].events
    event = events.get(status_event_id(today)) or next(iter(events.values()))
    assert event.get("transparency") == "transparent"
    assert event["reminders"] == {"useDefault": False}
    assert event["visibility"] == "private"
    assert (
        "Confirmed in iCloud" in event["description"]
        or "Waiting" in event["description"]
        or "Events:" in event["description"]
    )
    assert not publisher.publish(build_status(bridge.app.ctx)), "unchanged status is not re-published"
    # The status calendar is never offered for mapping.
    assert status_cal.id not in {m.google_calendar_id for m in bridge.app.repo.mappings()}

    # Its record lost (e.g. a database restored from before it existed): it is adopted, not made twice.
    with bridge.app.db.tx():
        bridge.app.repo.set_calendar_role(status_cal.id, None)
    assert publisher.publish(build_status(bridge.app.ctx), force=True)
    named = [c for c in bridge.google.calendars.values() if c.summary == bridge.app.settings.status_calendar_name]
    assert len(named) == 1 and bridge.app.repo.status_calendar().remote_id == status_cal.remote_id


def test_alerts_raise_repeat_and_clear(bridge: Bridge) -> None:
    bridge.map()
    bridge.sync_ok()
    channel = RecordingChannel()
    alerter = Alerter(bridge.app.ctx, [channel])
    status = build_status(bridge.app.ctx)
    status["google"]["state"] = "invalid"
    alerter.evaluate(status)
    assert [t for t, _, _ in channel.sent] == ["Problem: auth:google"]
    alerter.evaluate(status)
    assert len(channel.sent) == 1, "an active alert is not re-sent before the repeat interval"
    healthy = build_status(bridge.app.ctx)
    alerter.evaluate(healthy)
    assert channel.sent[-1][0] == "Resolved: auth:google"
    for _, message, _ in channel.sent:
        assert "@" not in message, "alerts never contain calendar content"


def test_alert_conditions_cover_required_cases(bridge: Bridge) -> None:
    bridge.map()
    status: dict[str, Any] = build_status(bridge.app.ctx)
    now = dt.datetime.now(dt.UTC)
    status["daemon"]["started_at"] = (now - dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    status["global_hold"] = "integrity"
    status["conflicts"]["items"] = [{"since": (now - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}]
    status["failures"]["permanent"] = 2
    found = conditions(status, bridge.app.settings, now)
    assert {"sync_stopped", "hold:global", "conflicts", "failures"} <= set(found)


def test_daemon_runs_cycles_and_writes_heartbeat_and_status_file(bridge: Bridge) -> None:
    bridge.map()
    bridge.g_create("Daemon test", "2030-07-03T09:00:00", "2030-07-03T10:00:00")
    daemon = Daemon(bridge.app)
    assert daemon.run(max_cycles=1) == 0
    assert bridge.app.repo.get_meta("heartbeat_at") is not None
    assert bridge.app.repo.get_meta("last_ok_cycle_at") is not None
    assert bridge.app.settings.status_file.exists()
    assert bridge.app.repo.get_meta("last_backup_at") is not None
    bridge.i_by_summary("Daemon test")


def test_failed_events_are_retried_by_the_daily_housekeeping(bridge: Bridge) -> None:
    bridge.map()
    bridge.g_create("Flaky", "2030-07-04T09:00:00", "2030-07-04T10:00:00")
    bridge.sync_ok()
    (item,) = bridge.items()
    with bridge.app.db.tx():
        bridge.app.repo.update_item(item.id, state="failed", state_reason="gave up after 12 attempts", retry_count=11)
    Daemon(bridge.app)._housekeeping_if_due()
    retried = bridge.app.repo.item(item.id)
    assert retried.state == "pending" and retried.dirty
    assert retried.retry_count == bridge.app.settings.item_max_attempts - 1, "one more attempt, not a new series"
    bridge.sync_ok()
    assert bridge.app.repo.item(item.id).state == "synced"
    assert any(row["action"] == "failed_retried" for row in bridge.app.repo.audit_entries(10))


def test_a_failing_cycle_is_still_reported(bridge: Bridge, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge.map()
    bridge.sync_ok()
    pings: list[tuple[str | None, bool]] = []
    monkeypatch.setattr("calendar_bridge.daemon.ping_heartbeat", lambda url, ok, timeout: pings.append((url, ok)))
    evaluated: list[str] = []
    monkeypatch.setattr(Alerter, "evaluate", lambda self, status: evaluated.append(status["overall"]) or [])

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("cycle-level failure")

    monkeypatch.setattr("calendar_bridge.sync.engine.Engine.run_cycle", broken)
    daemon = Daemon(bridge.app)
    with bridge.app.db.tx():
        bridge.app.repo.set_meta("last_ok_cycle_at", bridge.app.repo.now())
    daemon.run_once()
    assert evaluated, "alerts are evaluated even when the cycle fails"
    assert bridge.app.settings.status_file.exists()
    assert pings[-1][1] is True, "one failed cycle is not an outage for the dead-man's switch"
    with bridge.app.db.tx():
        old = dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)
        bridge.app.repo.set_meta("last_ok_cycle_at", old.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
    daemon.run_once()
    assert pings[-1][1] is False, "no good cycle for longer than the lag limit is"


def test_health_accepts_a_long_running_cycle(bridge: Bridge) -> None:
    from click.testing import CliRunner

    from calendar_bridge import cli as cli_module

    long_ago = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    recent = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=8)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with bridge.app.db.tx():
        bridge.app.repo.set_meta("heartbeat_at", long_ago)
        bridge.app.repo.set_meta("cycle_started_at", recent)
    state = cli_module.State(bridge.app.settings)
    state._app = bridge.app
    result = CliRunner().invoke(cli_module.health, [], obj=state)
    assert result.exit_code == 0 and "cycle running" in result.output


def test_housekeeping_removes_what_deleted_events_leave_behind(bridge: Bridge) -> None:
    bridge.map()
    gone = bridge.g_create("Gone", "2030-07-05T09:00:00", "2030-07-05T10:00:00")
    series = bridge.g_create(
        "Weekly", "2030-07-01T09:00:00", "2030-07-01T10:00:00", recurrence=["RRULE:FREQ=WEEKLY;COUNT=4"]
    )
    bridge.sync_ok()
    occurrence = f"{series}_20300708T070000Z"
    bridge.g_delete(occurrence)  # a cancelled occurrence of a live series
    bridge.g_delete(gone)
    bridge.sync_ok()
    repo = bridge.app.repo
    later = (dt.datetime.now(dt.UTC) + dt.timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with bridge.app.db.tx():
        repo.purge_tombstones(later)
        removed = repo.prune_history(later, audit_before=later)
    assert removed > 0
    assert repo.google_event(bridge.mapping_id, gone) is None, "the deleted event's observation is gone"
    assert repo.google_event(bridge.mapping_id, occurrence) is not None, "a live series keeps its exceptions"
    assert not bridge.app.db.query("SELECT 1 FROM write_intents WHERE state != 'pending'")
    bridge.sync_ok()
    assert [e["summary"] for e in bridge.g_live()] == ["Weekly"]


def test_a_deleted_status_calendar_is_replaced_once(bridge: Bridge) -> None:
    bridge.map()
    bridge.sync_ok()
    publisher = StatusCalendar(bridge.app.ctx, bridge.google_client)
    publisher.publish(build_status(bridge.app.ctx), force=True)
    first = bridge.app.repo.status_calendar()
    assert first is not None
    del bridge.google.calendars[first.remote_id]  # the user deletes it in Google
    from calendar_bridge.sync.discovery import refresh_google_calendars

    refresh_google_calendars(bridge.app.ctx, bridge.google_client)
    for _ in range(3):
        publisher.publish(build_status(bridge.app.ctx), force=True)
    named = [c for c in bridge.google.calendars.values() if c.summary == bridge.app.settings.status_calendar_name]
    assert len(named) == 1
    assert bridge.app.repo.calendar(first.id).role is None
