"""Regression tests for the findings of the independent data-safety review."""

from __future__ import annotations

import datetime as dt

import pytest

from calendar_bridge.model.canonical import CanonicalEvent, EventData, EventTime, Recurrence
from calendar_bridge.model.text import no_footer
from calendar_bridge.providers.icloud import convert as ic
from calendar_bridge.sync.context import MappingContext
from calendar_bridge.sync.initial import apply_plan, build_plan
from tests.conftest import TZ, Bridge, reopen
from tests.fakes.google_api import SimulatedCrash

ME_ICLOUD = "me@icloud.com"


def resolve(b: Bridge, conflict_id: int, resolution: str, data: dict | None = None) -> None:
    with b.app.db.tx():
        b.app.repo.update_conflict(conflict_id, state="resolving", resolution=resolution, resolution_data=data or {})


def map_with_my_addresses(b: Bridge) -> None:
    """Map, then apply the initial plan knowing the user's iCloud addresses."""
    b.icloud_url = b.server.calendar_url("home")
    b.map(apply=False)
    with b.app.db.tx():
        b.app.repo.set_meta_json("icloud_addresses", [ME_ICLOUD])
    ctx = b.app.ctx
    mc = MappingContext.build(ctx, b.app.repo.mapping(b.mapping_id))
    apply_plan(ctx, mc, build_plan(ctx, mc, b.google_client, b.icloud_client), accept_ambiguous=False)


MEETING_PEOPLE = (
    f"ORGANIZER;CN=Me:mailto:{ME_ICLOUD}\r\n"
    f"ATTENDEE;CN=Me;PARTSTAT=ACCEPTED:mailto:{ME_ICLOUD}\r\n"
    "ATTENDEE;CN=Guest;PARTSTAT=NEEDS-ACTION:mailto:guest@example.com\r\n"
    "ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:me@gmail.com\r\n"
)


# ---------------------------------------------------------------- finding 1


def test_meeting_organized_in_icloud_and_delivered_to_gmail_is_never_cancelled(bridge: Bridge) -> None:
    bridge.icloud_url = bridge.server.calendar_url("home")
    bridge.i_create("Board meeting", "20301001T090000", "20301001T100000", uid="meet-1@icloud", extra=MEETING_PEOPLE)
    invitation = bridge.google.user_insert(
        bridge.google_cal,
        {
            "id": "inv00001",
            "iCalUID": "meet-1@icloud",
            "summary": "Board meeting",
            "start": {"dateTime": "2030-10-01T09:00:00", "timeZone": TZ},
            "end": {"dateTime": "2030-10-01T10:00:00", "timeZone": TZ},
            "attendees": [
                {"email": "me@gmail.com", "self": True, "responseStatus": "needsAction"},
                {"email": "guest@example.com"},
                {"email": ME_ICLOUD, "organizer": True},
            ],
        },
    )
    # The fake makes the calendar owner the organizer; a real invitation has the iCloud organizer.
    bridge.google.calendars[bridge.google_cal].events[invitation["id"]]["organizer"] = {"email": ME_ICLOUD}
    map_with_my_addresses(bridge)
    bridge.sync_ok()
    (item,) = bridge.items()
    assert item.authority == "icloud", "the side where the user organizes the meeting owns it"

    bridge.g_delete(invitation["id"])  # e.g. the Gmail address was removed from the guest list
    bridge.sync_ok()
    _url, ics = bridge.i_by_summary("Board meeting")
    assert "guest@example.com" in ics, "the organizer's meeting is untouched"
    (conflict,) = bridge.conflicts()
    assert conflict.kind == "guest_managed"


def test_guard_refuses_automatic_writes_to_an_organized_icloud_meeting(bridge: Bridge) -> None:
    bridge.icloud_url = bridge.server.calendar_url("home")
    bridge.i_create("Planning", "20301002T090000", "20301002T100000", uid="meet-2@icloud", extra=MEETING_PEOPLE)
    map_with_my_addresses(bridge)
    bridge.sync_ok()
    (item,) = bridge.items()
    # Simulate a wrong owner classification: the guard must still hold.
    with bridge.app.db.tx():
        bridge.app.repo.update_item(item.id, authority="google")
    google_copy = bridge.g_by_summary("Planning")
    bridge.g_patch(google_copy["id"], {"summary": "Planning (moved room)"})
    puts_before = len([r for r in bridge.server.middleware.requests if r[0] == "PUT"])
    bridge.sync_ok()
    assert len([r for r in bridge.server.middleware.requests if r[0] == "PUT"]) == puts_before
    (conflict,) = bridge.conflicts()
    assert conflict.kind == "guest_managed" and "summary" in conflict.groups
    bridge.i_by_summary("Planning")


def test_new_occurrence_inside_a_meeting_keeps_its_guests() -> None:
    ctx = ic.ICloudContext(default_tz=TZ, self_addresses=frozenset({ME_ICLOUD}))
    source = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:m\r\nDTSTAMP:20300101T000000Z\r\n"
        f"DTSTART;TZID={TZ}:20301005T090000\r\nDTEND;TZID={TZ}:20301005T100000\r\nRRULE:FREQ=DAILY;COUNT=3\r\n"
        f"SUMMARY:Daily\r\n{MEETING_PEOPLE}END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    current = ic.icloud_to_canonical(source, ctx).event
    assert current is not None
    moved = EventData(
        start=EventTime.zoned(dt.datetime(2030, 10, 6, 11), TZ),
        end=EventTime.zoned(dt.datetime(2030, 10, 6, 12), TZ),
        summary="Daily (moved)",
    )
    target = CanonicalEvent(current.master, current.recurrence, (("20301006T070000Z", moved),))
    out = ic.apply_to_ics(
        source,
        target,
        {"override:20301006T070000Z"},
        ctx=ctx,
        footer=no_footer,
        now=dt.datetime(2030, 1, 1, tzinfo=dt.UTC),
        mirror_conference=False,
    )
    override = out.split("BEGIN:VEVENT")[2].replace("\r\n ", "")
    assert "RECURRENCE-ID" in override and "ORGANIZER" in override and "guest@example.com" in override


# ---------------------------------------------------------------- finding 2


def test_ambiguous_hold_survives_edits_and_bad_links_do_not_crash(bridge: Bridge) -> None:
    bridge.icloud_url = bridge.server.calendar_url("home")
    g_ids = [bridge.g_create("Lunch", "2030-10-03T12:00:00", "2030-10-03T13:00:00") for _ in range(2)]
    for _ in range(2):
        bridge.i_create("Lunch", "20301003T120000", "20301003T130000")
    bridge.map(accept_ambiguous=True)
    bridge.sync_ok()
    bridge.g_patch(g_ids[0], {"description": "edited while on hold"})
    bridge.sync_ok()
    assert len(bridge.i_resources()) == 2, "an edit must not release the hold and copy the event"

    (conflict,) = bridge.conflicts()
    icloud_hrefs = [c["id"] for c in conflict.candidates if c["provider"] == "icloud"]
    resolve(bridge, conflict.id, "link", {"google_event_id": g_ids[0], "icloud_href": icloud_hrefs[0]})
    bridge.sync_ok()
    bridge.sync_ok()
    assert len(bridge.g_live()) == 3 and len(bridge.i_resources()) == 3

    # A link naming an event that is already synchronized is refused, without stopping the cycle.
    with bridge.app.db.tx():
        bad = bridge.app.repo.create_conflict(
            mapping_id=bridge.mapping_id,
            item_id=None,
            kind="ambiguous_match",
            groups=[],
            google=None,
            icloud=None,
            google_base=None,
            icloud_base=None,
            reason="test",
            candidates=conflict.candidates,
        )
    resolve(bridge, bad, "link", {"google_event_id": g_ids[0], "icloud_href": icloud_hrefs[1]})
    bridge.sync_ok()
    assert bridge.app.repo.conflict(bad).state == "open"
    assert len(bridge.g_live()) == 3 and len(bridge.i_resources()) == 3


# ---------------------------------------------------------------- finding 3


def test_concurrent_edit_during_a_write_still_propagates(bridge: Bridge) -> None:
    bridge.map()
    master = bridge.g_create(
        "Yoga", "2030-10-07T08:00:00", "2030-10-07T09:00:00", recurrence=["RRULE:FREQ=DAILY;COUNT=4"]
    )
    bridge.sync_ok()
    bridge.i_edit("Yoga", lambda ics: ics.replace("SUMMARY:Yoga", "SUMMARY:Morning yoga"))

    def user_moves_an_occurrence() -> None:
        bridge.google.user_patch(
            bridge.google_cal,
            f"{master}_20301008T060000Z",
            {
                "start": {"dateTime": "2030-10-08T10:00:00", "timeZone": TZ},
                "end": {"dateTime": "2030-10-08T11:00:00", "timeZone": TZ},
            },
        )

    bridge.google.before_hooks.append(
        (lambda m, p: m == "PATCH" and p.endswith(f"/events/{master}"), user_moves_an_occurrence)
    )
    bridge.sync_ok()
    assert bridge.google.event(bridge.google_cal, master)["summary"] == "Morning yoga"
    bridge.sync_ok()
    _, ics = bridge.i_by_summary("Morning yoga")
    assert "DTSTART;TZID=Europe/Brussels:20301008T100000" in ics.replace("\r\n ", ""), (
        "the concurrent move reached iCloud"
    )


# ---------------------------------------------------------------- finding 4


def test_interrupted_create_of_a_series_is_completed_after_restart(bridge: Bridge) -> None:
    bridge.map()
    override = (
        "BEGIN:VEVENT\r\nUID:series-crash\r\nDTSTAMP:20300101T000000Z\r\n"
        f"RECURRENCE-ID;TZID={TZ}:20301011T090000\r\nDTSTART;TZID={TZ}:20301011T150000\r\n"
        f"DTEND;TZID={TZ}:20301011T160000\r\nSUMMARY:Class (afternoon)\r\nEND:VEVENT\r\n"
    )
    bridge.i_create(
        "Class",
        "20301010T090000",
        "20301010T100000",
        uid="series-crash",
        extra="RRULE:FREQ=DAILY;COUNT=3\r\n",
        components=override,
    )
    bridge.google.fail("PATCH", "_20301011T070000Z", crash_before_apply=True)
    with pytest.raises(SimulatedCrash):
        bridge.sync()
    restarted = reopen(bridge)
    restarted.sync_ok()
    restarted.sync_ok()
    master = restarted.g_by_summary("Class")
    moved = restarted.google.event(restarted.google_cal, f"{master['id']}_20301011T070000Z")
    assert moved is not None and moved["summary"] == "Class (afternoon)"


# ---------------------------------------------------------------- finding 5


def test_merge_keeps_cancelled_occurrences_from_both_sides(bridge: Bridge) -> None:
    bridge.map()
    master = bridge.g_create(
        "Run", "2030-10-14T07:00:00", "2030-10-14T08:00:00", recurrence=["RRULE:FREQ=DAILY;COUNT=5"]
    )
    bridge.sync_ok()
    bridge.g_delete(f"{master}_20301015T050000Z")
    bridge.g_patch(master, {"summary": "Run (Google)"})
    bridge.i_edit(
        "Run",
        lambda ics: ics.replace("SUMMARY:Run", "SUMMARY:Run (iPhone)").replace(
            "RRULE:FREQ=DAILY;COUNT=5", f"RRULE:FREQ=DAILY;COUNT=5\r\nEXDATE;TZID={TZ}:20301016T070000", 1
        ),
    )
    bridge.sync_ok()
    (conflict,) = bridge.conflicts()
    assert set(conflict.groups) == {"summary"}
    resolve(bridge, conflict.id, "merge", {"groups": {"summary": "google"}})
    bridge.sync_ok()
    item = bridge.items()[0]
    assert item.google_base.recurrence.exdates == item.icloud_base.recurrence.exdates
    assert set(item.icloud_base.recurrence.exdates) == {"20301015T050000Z", "20301016T050000Z"}
    _, ics = bridge.i_by_summary("Run (Google)")
    assert "20301015T070000" in ics and "20301016T070000" in ics


# ---------------------------------------------------------------- finding 6


def test_retry_backoff_is_respected(bridge: Bridge) -> None:
    bridge.app.settings.item_retry_base = 3600
    bridge.app.settings.item_retry_max = 3600
    bridge.map()
    bridge.i_create("Backoff", "20301017T090000", "20301017T100000")
    bridge.google.fail("POST", "/events", status=503, reason="backendError")
    bridge.sync()
    (item,) = bridge.items()
    assert item.state == "pending" and not item.dirty and item.next_attempt_at
    posts = len([r for r in bridge.google.requests if r[0] == "POST"])
    bridge.sync_ok()
    assert len([r for r in bridge.google.requests if r[0] == "POST"]) == posts, "no retry before the backoff expires"


# ---------------------------------------------------------------- finding 7


def test_rsvp_only_occurrence_is_ignored_consistently(bridge: Bridge) -> None:
    bridge.map()
    attendees = [
        {"email": "me@gmail.com", "self": True, "responseStatus": "accepted"},
        {"email": "colleague@example.com", "responseStatus": "accepted"},
    ]
    master = bridge.g_create(
        "Standup",
        "2030-10-21T09:00:00",
        "2030-10-21T09:15:00",
        recurrence=["RRULE:FREQ=DAILY;COUNT=3"],
        attendees=attendees,
    )
    bridge.sync_ok()
    bridge.g_patch(
        f"{master}_20301022T070000Z",
        {"attendees": [attendees[0], {"email": "colleague@example.com", "responseStatus": "declined"}]},
    )
    bridge.sync_ok()
    assert {i.state for i in bridge.items()} == {"synced"}
    _, ics = bridge.i_by_summary("Standup")
    assert "RECURRENCE-ID" not in ics


# ---------------------------------------------------------------- finding 8


def test_bare_cancellation_record_still_cancels_the_right_occurrence(bridge: Bridge) -> None:
    bridge.map()
    master = bridge.g_create(
        "Swim", "2030-10-24T07:00:00", "2030-10-24T08:00:00", recurrence=["RRULE:FREQ=DAILY;COUNT=4"]
    )
    bridge.g_patch(f"{master}_20301025T050000Z", {"summary": "Swim (pool B)"})
    bridge.sync_ok()
    bridge.google.bare_cancellations = True
    bridge.g_delete(f"{master}_20301025T050000Z")
    bridge.sync_ok()
    _, ics = bridge.i_by_summary("Swim")
    text = ics.replace("\r\n ", "")
    assert "EXDATE;TZID=Europe/Brussels:20301025T070000" in text and "Swim (pool B)" not in text


def test_full_suite_helpers_still_build() -> None:
    assert Recurrence().is_recurring is False
