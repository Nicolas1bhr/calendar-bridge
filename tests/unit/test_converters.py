from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from calendar_bridge.model.canonical import Alarm, CanonicalEvent, EventData, EventTime, Recurrence, changed_groups
from calendar_bridge.model.text import FOOTER_MARKER, no_footer, render_footer
from calendar_bridge.providers.google import convert as gc
from calendar_bridge.providers.icloud import convert as ic

TZ = "Europe/Brussels"
GCTX = gc.GoogleContext(
    default_tz=TZ, calendar_tz=TZ, default_reminders=(Alarm("display", 10),), self_email="me@gmail.com"
)
ICTX = ic.ICloudContext(default_tz=TZ, self_addresses=frozenset({"me@icloud.com"}))
NOW = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)

APPLE_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Apple Inc.//macOS 15//EN
BEGIN:VTIMEZONE
TZID:Europe/Brussels
BEGIN:DAYLIGHT
TZOFFSETFROM:+0100
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
DTSTART:19810329T020000
TZNAME:CEST
TZOFFSETTO:+0200
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:+0200
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
DTSTART:19961027T030000
TZNAME:CET
TZOFFSETTO:+0100
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:6F1C2E8A-0B3D-4C5E-9F10-ABCDEF012345
DTSTAMP:20300101T000000Z
DTSTART;TZID=Europe/Brussels:20300304T090000
DTEND;TZID=Europe/Brussels:20300304T100000
RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=6
EXDATE;TZID=Europe/Brussels:20300311T090000
SUMMARY:Piano
LOCATION:Music school
DESCRIPTION:Bring sheets
X-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-TITLE=Music school:geo:50.85,4.35
ORGANIZER;CN=Me:mailto:me@icloud.com
ATTENDEE;CN=Teacher;PARTSTAT=ACCEPTED:mailto:teacher@example.com
ATTENDEE;CN=Me;PARTSTAT=ACCEPTED:mailto:me@icloud.com
BEGIN:VALARM
ACTION:NONE
TRIGGER;VALUE=DATE-TIME:19760401T005545Z
END:VALARM
BEGIN:VALARM
ACTION:AUDIO
TRIGGER;RELATED=END:-PT1H15M
END:VALARM
END:VEVENT
BEGIN:VEVENT
UID:6F1C2E8A-0B3D-4C5E-9F10-ABCDEF012345
DTSTAMP:20300101T000000Z
RECURRENCE-ID;TZID=Europe/Brussels:20300318T090000
DTSTART;TZID=Europe/Brussels:20300318T110000
DTEND;TZID=Europe/Brussels:20300318T120000
SUMMARY:Piano (moved)
END:VEVENT
BEGIN:VEVENT
UID:6F1C2E8A-0B3D-4C5E-9F10-ABCDEF012345
DTSTAMP:20300101T000000Z
RECURRENCE-ID;TZID=Europe/Brussels:20300325T090000
DTSTART;TZID=Europe/Brussels:20300325T090000
DTEND;TZID=Europe/Brussels:20300325T100000
STATUS:CANCELLED
SUMMARY:Piano
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n")


def test_apple_resource_to_canonical() -> None:
    conv = ic.icloud_to_canonical(APPLE_ICS, ICTX)
    assert conv.supported, conv.unsupported
    event = conv.event
    assert event is not None
    assert event.master.summary == "Piano" and event.master.location == "Music school"
    assert event.recurrence.rrules == ("FREQ=WEEKLY;COUNT=6;BYDAY=MO",)
    # EXDATE plus the STATUS:CANCELLED override both become exclusions.
    assert event.recurrence.exdates == ("20300311T080000Z", "20300325T080000Z")
    assert [k for k, _ in event.overrides] == ["20300318T080000Z"]
    assert event.master.alarms == (Alarm("display", 15),)  # RELATED=END -1h15 on a 1h event
    assert event.is_meeting and event.master.organizer is not None and event.master.organizer.is_self


def test_apply_to_ics_touches_only_changed_groups() -> None:
    current = ic.icloud_to_canonical(APPLE_ICS, ICTX).event
    assert current is not None
    target = dataclasses.replace(current, master=dataclasses.replace(current.master, summary="Piano lesson"))
    out = ic.apply_to_ics(APPLE_ICS, target, {"summary"}, ctx=ICTX, footer=no_footer, now=NOW, mirror_conference=False)
    unfolded = out.replace("\r\n ", "")
    assert "SUMMARY:Piano lesson" in unfolded
    for kept in (
        "X-APPLE-STRUCTURED-LOCATION",
        "ACTION:NONE",
        "ATTENDEE;CN=Teacher",
        "ORGANIZER;CN=Me",
        "RELATED=END",
        "DESCRIPTION:Bring sheets",
        "SUMMARY:Piano (moved)",
    ):
        assert kept in unfolded, kept
    again = ic.icloud_to_canonical(out, ICTX).event
    assert again is not None and changed_groups(target, again) == set()


def test_apply_to_ics_exdates_and_overrides() -> None:
    current = ic.icloud_to_canonical(APPLE_ICS, ICTX).event
    assert current is not None
    exdates = (*current.recurrence.exdates, "20300318T080000Z")
    target = dataclasses.replace(current, recurrence=dataclasses.replace(current.recurrence, exdates=exdates))
    out = ic.apply_to_ics(APPLE_ICS, target, {"exdates"}, ctx=ICTX, footer=no_footer, now=NOW, mirror_conference=False)
    parsed = ic.icloud_to_canonical(out, ICTX).event
    assert parsed is not None
    assert "20300318T080000Z" in parsed.recurrence.exdates
    assert parsed.overrides == (), "an exdated occurrence must not keep its override"


def test_built_mirror_contains_no_scheduling_properties() -> None:
    event = ic.icloud_to_canonical(APPLE_ICS, ICTX).event
    assert event is not None
    out = ic.build_ics(
        event,
        uid="x@google.com",
        ctx=ICTX,
        footer=lambda d: render_footer(d, "Google Calendar"),
        markers={"ORIGIN": "google"},
        now=NOW,
    )
    assert "ATTENDEE:" not in out and "ORGANIZER" not in out
    assert FOOTER_MARKER in out.replace("\r\n ", "")
    assert ic.icloud_to_canonical(out, ICTX).event.master.description == "Bring sheets"


@pytest.mark.parametrize(
    ("ics_times", "unsupported", "note"),
    [
        ("DTSTART;TZID=Unknown/Zone:20300101T090000\r\nDTEND;TZID=Unknown/Zone:20300101T100000\r\n", True, None),
        ("DTSTART:20300101T090000\r\nDURATION:P1DT2H\r\n", False, None),
    ],
)
def test_time_edge_cases(ics_times: str, unsupported: bool, note: str | None) -> None:
    ics = f"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:e\r\n{ics_times}SUMMARY:x\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    conv = ic.icloud_to_canonical(ics, ICTX)
    assert bool(conv.unsupported) == unsupported
    if not unsupported:
        assert conv.event.master.end == EventTime.floating(dt.datetime(2030, 1, 2, 11))


def test_windows_zone_with_vtimezone_maps_to_iana() -> None:
    ics = (
        "BEGIN:VCALENDAR\r\nBEGIN:VTIMEZONE\r\nTZID:Romance Standard Time\r\nBEGIN:STANDARD\r\nDTSTART:16010101T030000\r\n"
        "TZOFFSETFROM:+0200\r\nTZOFFSETTO:+0100\r\nRRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=10\r\nEND:STANDARD\r\n"
        "BEGIN:DAYLIGHT\r\nDTSTART:16010101T020000\r\nTZOFFSETFROM:+0100\r\nTZOFFSETTO:+0200\r\n"
        "RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=3\r\nEND:DAYLIGHT\r\nEND:VTIMEZONE\r\nBEGIN:VEVENT\r\nUID:w\r\n"
        "DTSTART;TZID=Romance Standard Time:20300701T100000\r\nDTEND;TZID=Romance Standard Time:20300701T110000\r\n"
        "SUMMARY:Outlook\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    event = ic.icloud_to_canonical(ics, ICTX).event
    assert event is not None and event.master.start == EventTime.zoned(dt.datetime(2030, 7, 1, 10), "Europe/Paris")


def test_unsupported_shapes() -> None:
    future = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:f\r\nDTSTART:20300101T090000Z\r\nRRULE:FREQ=DAILY\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:f\r\nRECURRENCE-ID;RANGE=THISANDFUTURE:20300105T090000Z\r\nDTSTART:20300105T100000Z\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    assert "THISANDFUTURE" in " ".join(ic.icloud_to_canonical(future, ICTX).unsupported)
    two = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:a\r\nDTSTART:20300101T090000Z\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:b\r\nDTSTART:20300101T090000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    assert ic.icloud_to_canonical(two, ICTX).unsupported
    todo = "BEGIN:VCALENDAR\r\nBEGIN:VTODO\r\nUID:t\r\nEND:VTODO\r\nEND:VCALENDAR\r\n"
    assert ic.icloud_to_canonical(todo, ICTX).is_event is False
    with pytest.raises(ic.ICSParseError):
        ic.parse_ics("not a calendar")
    broken = ic.icloud_to_canonical("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nthis is not ical", ICTX)
    assert broken.event is None and broken.unsupported and broken.is_event


def test_detached_occurrence_is_a_single_event() -> None:
    ics = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:d\r\nRECURRENCE-ID:20300105T090000Z\r\nDTSTART:20300105T100000Z\r\n"
        "DTEND:20300105T110000Z\r\nSUMMARY:One instance\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    conv = ic.icloud_to_canonical(ics, ICTX)
    assert conv.supported and conv.detached_key == "20300105T090000Z"
    assert not conv.event.is_recurring


def test_resource_names_are_deterministic_and_safe() -> None:
    name = ic.resource_name_for_uid("weird/uid with spaces@google.com")
    assert name == ic.resource_name_for_uid("weird/uid with spaces@google.com")
    assert name.startswith("cb-") and name.endswith(".ics") and "/" not in name and " " not in name


# ---------------------------------------------------------------------- google


GOOGLE_EVENT = {
    "id": "evt1",
    "iCalUID": "evt1@google.com",
    "status": "confirmed",
    "summary": " Board review ",
    "description": "<b>Agenda</b><br>1. Budget",
    "start": {"dateTime": "2030-03-25T10:00:00+01:00", "timeZone": TZ},
    "end": {"dateTime": "2030-03-25T11:00:00+01:00", "timeZone": TZ},
    "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20300429T215959Z", "EXDATE;TZID=Europe/Brussels:20300401T100000"],
    "reminders": {
        "useDefault": False,
        "overrides": [{"method": "popup", "minutes": 15}, {"method": "email", "minutes": 60}],
    },
    "organizer": {"email": "boss@example.com", "displayName": "Boss"},
    "attendees": [
        {"email": "boss@example.com", "organizer": True, "responseStatus": "accepted"},
        {"email": "me@gmail.com", "self": True, "responseStatus": "tentative"},
        {"email": "room@resource.calendar.google.com", "resource": True},
    ],
    "conferenceData": {
        "conferenceSolution": {"name": "Google Meet"},
        "entryPoints": [{"entryPointType": "video", "uri": "https://meet.google.com/aaa-bbbb-ccc"}],
    },
    "extendedProperties": {"private": {"cbUid": "u1", "other": "x"}},
}


def test_google_event_to_canonical() -> None:
    instance_cancel = {
        "id": "evt1_20300408T080000Z",
        "status": "cancelled",
        "recurringEventId": "evt1",
        "originalStartTime": {"dateTime": "2030-04-08T10:00:00+02:00", "timeZone": TZ},
    }
    conv = gc.google_to_canonical(GOOGLE_EVENT, [instance_cancel], GCTX)
    event = conv.event
    assert conv.supported and event is not None
    assert event.master.summary == "Board review"
    assert event.master.alarms == (Alarm("display", 15), Alarm("email", 60))
    assert event.recurrence.exdates == ("20300401T080000Z", "20300408T080000Z")
    assert event.is_meeting and {p.email for p in event.master.attendees} == {"boss@example.com", "me@gmail.com"}
    assert event.master.conference[0].uri == "https://meet.google.com/aaa-bbbb-ccc"
    assert conv.markers == {"cbUid": "u1"}
    projected = ic.project_for_icloud(event, ICTX)
    assert projected.master.description == "Agenda\n1. Budget"


def test_google_utc_without_zone_and_all_day_defaults() -> None:
    raw = {"id": "x", "start": {"dateTime": "2030-01-01T09:00:00Z"}, "end": {"dateTime": "2030-01-01T10:00:00Z"}}
    event = gc.google_to_canonical(raw, [], GCTX).event
    assert event.master.start == EventTime.zoned(dt.datetime(2030, 1, 1, 9), "UTC")
    assert event.master.alarms == (Alarm("display", 10),), "timed events with default reminders use calendar defaults"
    allday = {
        "id": "y",
        "start": {"date": "2030-01-01"},
        "end": {"date": "2030-01-02"},
        "reminders": {"useDefault": True},
    }
    conv = gc.google_to_canonical(allday, [], GCTX)
    assert conv.event.master.alarms is None and conv.notes


def test_google_special_types_unsupported_unless_mirrored() -> None:
    raw = {
        "id": "z",
        "eventType": "focusTime",
        "start": {"dateTime": "2030-01-01T09:00:00Z"},
        "end": {"dateTime": "2030-01-01T10:00:00Z"},
    }
    assert gc.google_to_canonical(raw, [], GCTX).unsupported
    gmail = dict(raw, eventType="fromGmail")
    assert gc.google_to_canonical(gmail, [], GCTX).supported


def test_google_write_bodies() -> None:
    event = CanonicalEvent(
        EventData(
            start=EventTime.floating(dt.datetime(2030, 1, 1, 9)),
            end=EventTime.floating(dt.datetime(2030, 1, 1, 10)),
            summary="S",
            alarms=tuple(Alarm("display", m) for m in (0, 5, 10, 15, 20, 30, 50000)),
            url="ftp://nope",
        ),
        Recurrence(("FREQ=DAILY;COUNT=3",), (), ("20300102T080000Z",)),
    )
    body = gc.insert_body(event, GCTX, event_id="cbabc", footer=no_footer, markers={"calendarBridge": "1"})
    assert body["start"] == {"dateTime": "2030-01-01T09:00:00", "timeZone": TZ}
    assert len(body["reminders"]["overrides"]) == 5 and all(
        o["minutes"] <= 40320 for o in body["reminders"]["overrides"]
    )
    assert "source" not in body and "attendees" not in body and "conferenceData" not in body
    assert body["recurrence"] == ["RRULE:FREQ=DAILY;COUNT=3", "EXDATE;TZID=Europe/Brussels:20300102T090000"]
    notes = gc.write_notes(event, GCTX)
    assert any("floating" in n for n in notes) and any("limits" in n for n in notes) and any("URL" in n for n in notes)

    allday_target = dataclasses.replace(
        event,
        master=dataclasses.replace(
            event.master, start=EventTime.all_day(dt.date(2030, 1, 1)), end=EventTime.all_day(dt.date(2030, 1, 2))
        ),
    )
    current = {
        "recurrence": ["RRULE:FREQ=DAILY;COUNT=3"],
        "start": {"dateTime": "2030-01-01T09:00:00+01:00", "timeZone": TZ},
    }
    patch = gc.master_patch_body(allday_target, {"timing"}, current, GCTX, no_footer, exdate_lines=set())
    assert patch["start"] == {"date": "2030-01-01", "dateTime": None, "timeZone": None}


def test_projection_round_trip_through_icalendar() -> None:
    event = gc.google_to_canonical(GOOGLE_EVENT, [], GCTX).event
    assert event is not None
    ics = ic.build_ics(
        ic.project_for_icloud(event, ICTX), uid="evt1@google.com", ctx=ICTX, footer=no_footer, markers={}, now=NOW
    )
    back = ic.icloud_to_canonical(ics, ICTX).event
    expected = ic.project_for_icloud(event, ICTX)
    assert changed_groups(expected, back) <= {"people", "conference"}
