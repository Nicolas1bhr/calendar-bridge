"""End-to-end synchronization against the fake Google API and a real CalDAV server."""

from __future__ import annotations

from tests.conftest import Bridge, reopen


def caldav_writes(b: Bridge) -> list[tuple[str, str]]:
    return [r for r in b.server.middleware.requests if r[0] in ("PUT", "DELETE")]


def test_google_event_is_created_in_icloud(bridge: Bridge) -> None:
    bridge.map()
    event_id = bridge.g_create("Dentist", "2030-03-05T10:00:00", "2030-03-05T11:00:00", description="Bring card")
    bridge.sync_ok()
    _url, ics = bridge.i_by_summary("Dentist")
    assert "DTSTART;TZID=Europe/Brussels:20300305T100000" in ics
    assert "Bring card" in ics
    assert "X-CALENDAR-BRIDGE-GOOGLE-ID:" + event_id in ics.replace("\r\n ", "")
    (item,) = bridge.items()
    assert item.state == "synced"
    assert item.last_icloud_verified_at is not None


def test_icloud_event_is_created_in_google(bridge: Bridge) -> None:
    bridge.map()
    bridge.i_create("Haircut", "20300306T150000", "20300306T160000", extra="LOCATION:Main street 1\r\n")
    bridge.sync_ok()
    event = bridge.g_by_summary("Haircut")
    assert event["location"] == "Main street 1"
    assert event["start"]["timeZone"] == "Europe/Brussels"
    assert event["start"]["dateTime"].startswith("2030-03-06T15:00:00")
    private = event["extendedProperties"]["private"]
    assert private["cbOrigin"] == "icloud" and private["calendarBridge"] == "1"
    assert "attendees" not in event


def test_updates_propagate_both_ways(bridge: Bridge) -> None:
    bridge.map()
    event_id = bridge.g_create("Lunch", "2030-03-07T12:00:00", "2030-03-07T13:00:00")
    bridge.sync_ok()
    bridge.g_patch(
        event_id,
        {
            "summary": "Lunch with Ana",
            "start": {"dateTime": "2030-03-07T12:30:00", "timeZone": "Europe/Brussels"},
            "end": {"dateTime": "2030-03-07T13:30:00", "timeZone": "Europe/Brussels"},
        },
    )
    bridge.sync_ok()
    _, ics = bridge.i_by_summary("Lunch with Ana")
    assert "DTSTART;TZID=Europe/Brussels:20300307T123000" in ics

    bridge.i_edit("Lunch with Ana", lambda ics: ics.replace("SUMMARY:Lunch with Ana", "SUMMARY:Lunch with Ana and Bo"))
    bridge.sync_ok()
    assert bridge.google.event(bridge.google_cal, event_id)["summary"] == "Lunch with Ana and Bo"
    (item,) = bridge.items()
    assert item.state == "synced"


def test_deletions_propagate_both_ways(bridge: Bridge) -> None:
    bridge.map()
    a = bridge.g_create("Delete me in Google", "2030-03-08T09:00:00", "2030-03-08T10:00:00")
    bridge.i_create("Delete me in iCloud", "20300308T110000", "20300308T120000")
    bridge.sync_ok()
    assert len(bridge.i_resources()) == 2 and len(bridge.g_live()) == 2

    bridge.g_delete(a)
    url, _ = bridge.i_by_summary("Delete me in iCloud")
    bridge.server.delete(url)
    bridge.sync_ok()
    assert bridge.i_resources() == {}
    assert bridge.g_live() == []
    assert {i.state for i in bridge.items()} == {"deleted"}


def test_steady_state_makes_no_writes(bridge: Bridge) -> None:
    bridge.map()
    bridge.g_create(
        "Standup", "2030-03-09T09:00:00", "2030-03-09T09:15:00", recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO,WE;COUNT=10"]
    )
    bridge.i_create("Gym", "20300309T180000", "20300309T190000", extra="RRULE:FREQ=DAILY;COUNT=5\r\n")
    bridge.g_create("Holiday", "2030-04-01", "2030-04-03")
    bridge.sync_ok()
    google_writes = len(bridge.google.writes())
    dav_writes = len(caldav_writes(bridge))
    for _ in range(3):
        bridge.sync_ok()
    bridge.sync_ok(force_full=True)
    assert len(bridge.google.writes()) == google_writes
    assert len(caldav_writes(bridge)) == dav_writes
    assert {i.state for i in bridge.items()} == {"synced"}


def test_the_zone_for_floating_times_never_drifts_once_syncing(bridge: Bridge) -> None:
    from calendar_bridge.sync.discovery import refresh_google_calendars

    bridge.map()
    assert bridge.app.repo.get_meta("home_timezone") == "Europe/Brussels", "recorded when the pair started"
    bridge.google.calendars[bridge.google_cal].timezone = "America/New_York"  # e.g. the user travels
    refresh_google_calendars(bridge.app.ctx, bridge.google_client)
    assert bridge.app.ctx.default_tz == "Europe/Brussels"


def test_pairs_started_by_0_1_0_keep_its_default_zone(bridge: Bridge) -> None:
    bridge.map()
    with bridge.app.db.tx():
        bridge.app.repo.set_meta("home_timezone", None)  # a database written by 0.1.0
    bridge.google.calendars[bridge.google_cal].timezone = "Asia/Tokyo"
    reopened = reopen(bridge)
    assert reopened.app.repo.get_meta("home_timezone") == "Europe/Brussels"
