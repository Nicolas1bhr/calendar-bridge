"""Recurrence, reminders, time zones, all-day events, meetings and data preservation."""

from __future__ import annotations

from calendar_bridge.model.text import FOOTER_MARKER
from tests.conftest import TZ, Bridge


def unfold(ics: str) -> str:
    return ics.replace("\r\n ", "").replace("\r\n", "\n")


# ------------------------------------------------------------------ recurrence


def test_google_series_with_moved_and_cancelled_occurrences(bridge: Bridge) -> None:
    bridge.map()
    master = bridge.g_create(
        "Team sync", "2030-03-04T10:00:00", "2030-03-04T11:00:00", recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=8"]
    )
    bridge.sync_ok()
    # Move the 2nd occurrence (crosses no DST) and cancel the 4th (after DST starts on 31 March).
    bridge.g_patch(
        f"{master}_20300311T090000Z",
        {
            "summary": "Team sync (moved)",
            "start": {"dateTime": "2030-03-12T14:00:00", "timeZone": TZ},
            "end": {"dateTime": "2030-03-12T15:00:00", "timeZone": TZ},
        },
    )
    bridge.g_delete(f"{master}_20300401T080000Z")
    bridge.sync_ok()
    _, ics = bridge.i_by_summary("Team sync")
    text = unfold(ics)
    assert "RRULE:FREQ=WEEKLY;COUNT=8;BYDAY=MO" in text or "RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=8" in text
    assert "RECURRENCE-ID;TZID=Europe/Brussels:20300311T100000" in text
    assert "DTSTART;TZID=Europe/Brussels:20300312T140000" in text
    assert "EXDATE;TZID=Europe/Brussels:20300401T100000" in text
    assert {i.state for i in bridge.items()} == {"synced"}


def test_icloud_series_exceptions_reach_google(bridge: Bridge) -> None:
    bridge.map()
    uid = "series-1@example.com"
    override = (
        f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:20300101T000000Z\r\n"
        f"RECURRENCE-ID;TZID={TZ}:20300306T080000\r\nDTSTART;TZID={TZ}:20300306T093000\r\n"
        f"DTEND;TZID={TZ}:20300306T103000\r\nSUMMARY:Run (late)\r\nEND:VEVENT\r\n"
    )
    bridge.i_create(
        "Run",
        "20300304T080000",
        "20300304T090000",
        uid=uid,
        extra=f"RRULE:FREQ=DAILY;COUNT=6\r\nEXDATE;TZID={TZ}:20300305T080000\r\n",
        components=override,
    )
    bridge.sync_ok()
    master = bridge.g_by_summary("Run")
    assert any(line.startswith("EXDATE") for line in master.get("recurrence", [])) or any(
        e["status"] == "cancelled" for e in bridge.google.instances_of(bridge.google_cal, master["id"])
    )
    moved = [e for e in bridge.google.instances_of(bridge.google_cal, master["id"]) if e.get("status") != "cancelled"]
    assert len(moved) == 1 and moved[0]["summary"] == "Run (late)"
    assert moved[0]["start"]["dateTime"].startswith("2030-03-06T09:30:00")

    # Cancel another occurrence on the iPhone (adds an EXDATE) -> Google instance cancelled.
    bridge.i_edit(
        "Run",
        lambda ics: ics.replace(
            f"EXDATE;TZID={TZ}:20300305T080000", f"EXDATE;TZID={TZ}:20300305T080000,20300307T080000"
        ),
    )
    bridge.sync_ok()
    cancelled = [
        e["id"] for e in bridge.google.instances_of(bridge.google_cal, master["id"]) if e["status"] == "cancelled"
    ]
    assert f"{master['id']}_20300307T070000Z" in cancelled
    assert {i.state for i in bridge.items()} == {"synced"}


def test_dst_spanning_series_keeps_wall_clock_time(bridge: Bridge) -> None:
    bridge.map()
    bridge.i_create("Evening class", "20300320T190000", "20300320T200000", extra="RRULE:FREQ=WEEKLY;COUNT=4\r\n")
    bridge.sync_ok()
    event = bridge.g_by_summary("Evening class")
    assert event["start"]["timeZone"] == TZ
    assert event["start"]["dateTime"].startswith("2030-03-20T19:00:00+01:00")
    assert event["recurrence"] == ["RRULE:FREQ=WEEKLY;COUNT=4"]


# ------------------------------------------------------------------ reminders


def test_reminders_sync_both_ways_and_unrepresentable_alarms_survive(bridge: Bridge) -> None:
    bridge.map()
    alarms = (
        "BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT30M\r\nDESCRIPTION:x\r\nEND:VALARM\r\n"
        "BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:PT9H\r\nDESCRIPTION:on the day\r\nEND:VALARM\r\n"
        "X-APPLE-TRAVEL-ADVISORY-BEHAVIOR:AUTOMATIC\r\n"
    )
    bridge.i_create("Flight", "20300310T070000", "20300310T090000", extra=alarms)
    bridge.sync_ok()
    event = bridge.g_by_summary("Flight")
    assert event["reminders"] == {"useDefault": False, "overrides": [{"method": "popup", "minutes": 30}]}

    bridge.g_patch(
        event["id"],
        {
            "reminders": {
                "useDefault": False,
                "overrides": [{"method": "popup", "minutes": 30}, {"method": "email", "minutes": 1440}],
            }
        },
    )
    bridge.sync_ok()
    _, ics = bridge.i_by_summary("Flight")
    text = unfold(ics)
    assert "TRIGGER:-PT30M" in text and "TRIGGER:-P1D" in text and "ACTION:EMAIL" in text
    assert "TRIGGER:PT9H" in text, "an alert Google cannot hold must be preserved in iCloud"
    assert "X-APPLE-TRAVEL-ADVISORY-BEHAVIOR:AUTOMATIC" in text, "unknown properties must survive"


def test_google_default_reminders_become_explicit_alarms(bridge: Bridge) -> None:
    bridge.map()
    bridge.g_create("Call", "2030-03-11T16:00:00", "2030-03-11T16:30:00")  # useDefault -> 10 min popup
    bridge.sync_ok()
    _, ics = bridge.i_by_summary("Call")
    assert "TRIGGER:-PT10M" in unfold(ics)


# ------------------------------------------------------------------ all-day, zones


def test_all_day_events_both_ways(bridge: Bridge) -> None:
    bridge.map()
    bridge.g_create("Conference", "2030-05-01", "2030-05-04")
    bridge.i_create("Birthday party", "20300510", "20300511", extra="TRANSP:TRANSPARENT\r\n")
    bridge.sync_ok()
    _, ics = bridge.i_by_summary("Conference")
    assert "DTSTART;VALUE=DATE:20300501" in ics and "DTEND;VALUE=DATE:20300504" in ics
    party = bridge.g_by_summary("Birthday party")
    assert party["start"] == {"date": "2030-05-10"} and party["end"] == {"date": "2030-05-11"}
    assert party["transparency"] == "transparent"


def test_utc_and_floating_times(bridge: Bridge) -> None:
    bridge.map()
    bridge.i_create("UTC event", "20300312T080000Z", "20300312T090000Z", tz=None)
    bridge.i_create("Floating", "20300312T120000", "20300312T130000", tz=None)
    bridge.sync_ok()
    utc = bridge.g_by_summary("UTC event")
    assert utc["start"]["dateTime"] in ("2030-03-12T08:00:00+00:00", "2030-03-12T08:00:00Z")
    floating = bridge.g_by_summary("Floating")
    assert floating["start"]["timeZone"] == TZ  # floating times are pinned to the default zone
    assert floating["start"]["dateTime"].startswith("2030-03-12T12:00:00")
    (item,) = [i for i in bridge.items() if "floating" in (i.notes_json or "")] or [None]
    assert item is not None


# ------------------------------------------------------------------ conflicts


def test_independent_edits_merge(bridge: Bridge) -> None:
    bridge.map()
    event_id = bridge.g_create("Plan", "2030-03-13T10:00:00", "2030-03-13T11:00:00")
    bridge.sync_ok()
    bridge.g_patch(event_id, {"location": "Room 4"})
    bridge.i_edit("Plan", lambda ics: ics.replace("SUMMARY:Plan", "SUMMARY:Plan review"))
    bridge.sync_ok()
    event = bridge.google.event(bridge.google_cal, event_id)
    assert event["summary"] == "Plan review" and event["location"] == "Room 4"
    _, ics = bridge.i_by_summary("Plan review")
    assert "LOCATION:Room 4" in ics
    assert bridge.conflicts() == []


def test_same_field_edited_on_both_sides_is_a_conflict(bridge: Bridge) -> None:
    bridge.map()
    event_id = bridge.g_create("Review", "2030-03-14T10:00:00", "2030-03-14T11:00:00")
    bridge.sync_ok()
    bridge.g_patch(event_id, {"summary": "Review (Google)"})
    bridge.i_edit("Review", lambda ics: ics.replace("SUMMARY:Review", "SUMMARY:Review (iPhone)"))
    google_writes = len(bridge.google.writes())
    bridge.sync_ok()
    (conflict,) = bridge.conflicts()
    assert conflict.kind == "field_conflict" and "summary" in conflict.groups
    # Both versions are preserved, nothing was overwritten.
    assert bridge.google.event(bridge.google_cal, event_id)["summary"] == "Review (Google)"
    bridge.i_by_summary("Review (iPhone)")
    assert len(bridge.google.writes()) == google_writes
    (item,) = bridge.items()
    assert item.state == "conflicted"

    # Resolve: keep the iPhone version.
    with bridge.app.db.tx():
        bridge.app.repo.update_conflict(conflict.id, state="resolving", resolution="keep_icloud")
    bridge.sync_ok()
    assert bridge.google.event(bridge.google_cal, event_id)["summary"] == "Review (iPhone)"
    assert bridge.app.repo.conflict(conflict.id).state == "resolved"
    assert bridge.items()[0].state == "synced"


def test_delete_versus_modify_preserves_the_modified_event(bridge: Bridge) -> None:
    bridge.map()
    event_id = bridge.g_create("Trip", "2030-03-15T10:00:00", "2030-03-15T11:00:00")
    bridge.sync_ok()
    bridge.g_delete(event_id)
    bridge.i_edit("Trip", lambda ics: ics.replace("SUMMARY:Trip", "SUMMARY:Trip to Ghent"))
    bridge.sync_ok()
    (conflict,) = bridge.conflicts()
    assert conflict.kind == "delete_vs_modify"
    bridge.i_by_summary("Trip to Ghent")  # still there
    with bridge.app.db.tx():
        bridge.app.repo.update_conflict(conflict.id, state="resolving", resolution="restore")
    bridge.sync_ok()
    assert bridge.g_by_summary("Trip to Ghent")


# ------------------------------------------------------------------ meetings / invitation safety


def test_google_meeting_mirror_has_no_attendees_and_mirror_edits_need_confirmation(bridge: Bridge) -> None:
    bridge.map()
    event_id = bridge.g_create(
        "Client meeting",
        "2030-03-18T10:00:00",
        "2030-03-18T11:00:00",
        attendees=[
            {"email": "me@gmail.com", "self": True, "responseStatus": "accepted"},
            {"email": "client@example.com", "displayName": "Client", "responseStatus": "needsAction"},
        ],
        hangoutLink="https://meet.google.com/abc-defg-hij",
    )
    bridge.sync_ok()
    _, ics = bridge.i_by_summary("Client meeting")
    text = unfold(ics)
    assert "ATTENDEE" not in text and "ORGANIZER" not in text, "mirror must not carry real invitations"
    assert FOOTER_MARKER in text and "client@example.com" in text and "meet.google.com/abc-defg-hij" in text

    # Moving the meeting on the iPhone must not silently change the guest-managed original.
    bridge.i_edit(
        "Client meeting",
        lambda ics: ics.replace("20300318T100000", "20300318T120000").replace("20300318T110000", "20300318T130000"),
    )
    bridge.sync_ok()
    (conflict,) = bridge.conflicts()
    assert conflict.kind == "guest_managed"
    assert bridge.google.event(bridge.google_cal, event_id)["start"]["dateTime"].startswith("2030-03-18T10:00")

    # Accepting the owner's version restores the mirror; no guest was contacted at any point.
    with bridge.app.db.tx():
        bridge.app.repo.update_conflict(conflict.id, state="resolving", resolution="keep_google")
    bridge.sync_ok()
    _, ics = bridge.i_by_summary("Client meeting")
    assert "20300318T100000" in ics


def test_footer_never_syncs_back(bridge: Bridge) -> None:
    bridge.map()
    event_id = bridge.g_create(
        "Sync talk",
        "2030-03-19T10:00:00",
        "2030-03-19T11:00:00",
        description="Agenda",
        hangoutLink="https://meet.google.com/xyz",
    )
    bridge.sync_ok()
    bridge.i_edit("Sync talk", lambda ics: ics.replace("SUMMARY:Sync talk", "SUMMARY:Sync talk v2"))
    bridge.sync_ok()
    event = bridge.google.event(bridge.google_cal, event_id)
    assert event["summary"] == "Sync talk v2"
    assert event.get("description") == "Agenda"


def test_special_google_event_types_are_reported_unsupported(bridge: Bridge) -> None:
    bridge.map()
    bridge.g_create("Out of office", "2030-03-20T09:00:00", "2030-03-20T17:00:00", eventType="outOfOffice")
    bridge.sync_ok()
    assert bridge.i_resources() == {}
    (item,) = bridge.items()
    assert item.state == "unsupported" and "outOfOffice" in (item.state_reason or "")


def test_cancelled_icloud_event_is_removed_from_google_and_reinstated(bridge: Bridge) -> None:
    bridge.map()
    bridge.i_create("Concert", "20300322T200000", "20300322T230000")
    bridge.sync_ok()
    assert bridge.g_by_summary("Concert")
    bridge.i_edit("Concert", lambda ics: ics.replace("SUMMARY:Concert", "STATUS:CANCELLED\r\nSUMMARY:Concert"))
    bridge.sync_ok()
    assert bridge.g_live() == []
    assert len(bridge.i_resources()) == 1, "the iCloud event itself is never deleted"
    bridge.i_edit("Concert", lambda ics: ics.replace("STATUS:CANCELLED\r\n", ""))
    bridge.sync_ok()
    bridge.sync_ok()
    assert bridge.g_by_summary("Concert")
