"""Property-based tests: conversions round-trip, updates converge, merge rules are consistent."""

from __future__ import annotations

import dataclasses
import datetime as dt
import itertools

from dateutil import rrule as du_rrule
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from calendar_bridge.model import timeutil
from calendar_bridge.model.canonical import (
    Alarm,
    CanonicalEvent,
    EventData,
    EventTime,
    Recurrence,
    changed_groups,
    group_values,
    with_groups,
)
from calendar_bridge.model.merge import SideState, decide
from calendar_bridge.model.text import no_footer
from calendar_bridge.providers.google import convert as gc
from calendar_bridge.providers.icloud import convert as ic
from tests.fakes.google_api import FakeGoogle

DEFAULT_TZ = "Europe/Brussels"
ZONES = ["Europe/Brussels", "America/New_York", "Asia/Kolkata", "UTC", "Australia/Sydney"]
GCTX = gc.GoogleContext(default_tz=DEFAULT_TZ, calendar_tz=DEFAULT_TZ, default_reminders=())
ICTX = ic.ICloudContext(default_tz=DEFAULT_TZ)
NOW = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)
INFO = {"people", "conference"}
PROFILE = settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])

text_chars = st.characters(blacklist_categories=("Cs", "Cc", "Zl", "Zp"), blacklist_characters="\\")
line = st.text(text_chars, max_size=40).map(str.strip)
multiline = st.lists(st.text(text_chars, max_size=40), max_size=4).map(lambda ls: "\n".join(ls).rstrip())


@st.composite
def event_times(draw: st.DrawFn) -> tuple[EventTime, EventTime]:
    if draw(st.booleans()):
        day = draw(st.dates(min_value=dt.date(2026, 1, 1), max_value=dt.date(2034, 12, 31)))
        days = draw(st.integers(1, 5))
        return EventTime.all_day(day), EventTime.all_day(day + dt.timedelta(days=days))
    tz = draw(st.sampled_from(ZONES))
    day = draw(st.dates(min_value=dt.date(2026, 1, 1), max_value=dt.date(2034, 12, 31)))
    local = dt.datetime.combine(day, dt.time(draw(st.integers(0, 23)), draw(st.sampled_from([0, 15, 30, 45]))))
    # Wall-clock times inside a DST gap do not exist; providers normalise them, so skip them here.
    assume(timeutil.utc_to_local(timeutil.local_to_utc(local, tz), tz) == local)
    duration = dt.timedelta(minutes=15 * draw(st.integers(0, 96)))
    start = EventTime.zoned(local, tz)
    end = EventTime.from_instant(start.instant(DEFAULT_TZ) + duration, tz)
    return start, end


alarms = st.lists(
    st.builds(Alarm, st.sampled_from(["display", "email"]), st.integers(0, 40320)), max_size=5, unique=True
).map(tuple)


@st.composite
def event_data(draw: st.DrawFn) -> EventData:
    start, end = draw(event_times())
    return EventData(
        start=start,
        end=end,
        summary=draw(line),
        description=draw(multiline),
        location=draw(line),
        status=draw(st.sampled_from(["confirmed", "tentative"])),
        transparency=draw(st.sampled_from(["opaque", "transparent"])),
        visibility=draw(st.sampled_from(["default", "public", "private", "confidential"])),
        alarms=draw(alarms),
        url=draw(st.one_of(st.just(""), st.from_regex(r"https://example\.com/[a-z]{1,10}", fullmatch=True))),
    )


RULES = [
    "FREQ=DAILY;COUNT=6",
    "FREQ=WEEKLY;BYDAY=MO,WE;COUNT=8",
    "FREQ=MONTHLY;COUNT=4",
    "FREQ=WEEKLY;INTERVAL=2;COUNT=5",
]


def occurrence_keys(master: EventData, rule: str) -> list[str]:
    if master.start.is_all_day:
        assert master.start.date is not None
        start = dt.datetime.combine(master.start.date, dt.time())
        return [o.strftime("%Y%m%d") for o in du_rrule.rrulestr(rule, dtstart=start)]
    assert master.start.local is not None and master.start.tzid is not None
    start = master.start.local.replace(tzinfo=timeutil.zone(master.start.tzid))
    return [
        o.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        for o in itertools.islice(du_rrule.rrulestr(rule, dtstart=start), 20)
    ]


@st.composite
def events(draw: st.DrawFn) -> CanonicalEvent:
    master = draw(event_data())
    if not draw(st.booleans()):
        return CanonicalEvent(master)
    rule = draw(st.sampled_from(RULES))
    keys = occurrence_keys(master, rule)
    # DST changes shift the UTC key of later occurrences; the key list above already accounts for it.
    exdates = draw(st.lists(st.sampled_from(keys[1:]), max_size=2, unique=True))
    overrides = {}
    for key in draw(
        st.lists(st.sampled_from([k for k in keys[1:] if k not in exdates] or [keys[0]]), max_size=2, unique=True)
    ):
        if key in exdates:
            continue
        start = EventTime.all_day(dt.datetime.strptime(key, "%Y%m%d").date()) if len(key) == 8 else None
        if start is None:
            instant = dt.datetime.strptime(key, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC) + dt.timedelta(hours=2)
            start = EventTime.from_instant(instant, master.start.tzid or "UTC")
            end = EventTime.from_instant(instant + master.duration(DEFAULT_TZ), master.start.tzid or "UTC")
        else:
            assert start.date is not None
            end = EventTime.all_day(start.date + dt.timedelta(days=1))
        overrides[key] = dataclasses.replace(
            master, start=start, end=end, summary=(master.summary + " (moved)").strip()
        )
    normalized = CanonicalEvent(master, Recurrence((rule,), (), tuple(exdates)), tuple(overrides.items()))
    # Normalize the RRULE the way both readers do.
    return dataclasses.replace(
        normalized,
        recurrence=dataclasses.replace(
            normalized.recurrence,
            rrules=tuple(
                __import__("calendar_bridge.model.recurrence", fromlist=["x"]).normalize_rrule(
                    r, master.start, DEFAULT_TZ
                )
                for r in normalized.recurrence.rrules
            ),
        ),
    )


def comparable(event: CanonicalEvent) -> dict[str, object]:
    return {k: v for k, v in group_values(event).items() if k not in INFO}


@PROFILE
@given(events())
def test_icalendar_round_trip(event: CanonicalEvent) -> None:
    expected = ic.project_for_icloud(event, ICTX)
    text = ic.build_ics(expected, uid="prop@test", ctx=ICTX, footer=no_footer, markers={}, now=NOW)
    parsed = ic.icloud_to_canonical(text, ICTX)
    assert parsed.supported, parsed.unsupported
    assert comparable(parsed.event) == comparable(expected)


@PROFILE
@given(events())
def test_google_round_trip(event: CanonicalEvent) -> None:
    google = FakeGoogle()
    google.add_calendar("c", "c", reminders=[])
    expected = gc.project_for_google(event, GCTX)
    body = gc.insert_body(expected, GCTX, event_id="cbprop0001", footer=no_footer, markers={})
    stored = google.user_insert("c", body)
    instances = []
    for key, data in expected.overrides:
        instances.append(
            google.user_patch("c", f"cbprop0001_{key}", gc.instance_patch_body(data, None, GCTX, no_footer))
        )
    back = gc.google_to_canonical(stored, instances, GCTX)
    assert back.supported, back.unsupported
    assert comparable(back.event) == comparable(expected)


@PROFILE
@given(events(), events())
def test_in_place_update_converges(before: CanonicalEvent, after: CanonicalEvent) -> None:
    assume(before.is_recurring == after.is_recurring)
    base = ic.project_for_icloud(before, ICTX)
    target = ic.project_for_icloud(after, ICTX)
    original = ic.build_ics(base, uid="u@test", ctx=ICTX, footer=no_footer, markers={}, now=NOW)
    groups = changed_groups(base, target) - INFO
    updated = ic.apply_to_ics(original, target, groups, ctx=ICTX, footer=no_footer, now=NOW, mirror_conference=False)
    parsed = ic.icloud_to_canonical(updated, ICTX).event
    assert parsed is not None
    expected = with_groups(base, target, groups)
    assert comparable(parsed) == comparable(expected)


def identity(event: CanonicalEvent) -> CanonicalEvent:
    return event


@PROFILE
@given(events(), events(), events())
def test_merge_is_symmetric_and_converges(base: CanonicalEvent, g_now: CanonicalEvent, i_now: CanonicalEvent) -> None:
    forward = decide(
        SideState(base, g_now),
        SideState(base, i_now),
        authority="google",
        meeting_policy="authoritative",
        project_google=identity,
        project_icloud=identity,
    )
    backward = decide(
        SideState(base, i_now),
        SideState(base, g_now),
        authority="google",
        meeting_policy="authoritative",
        project_google=identity,
        project_icloud=identity,
    )
    assert forward.action == backward.action
    if forward.action == "write":
        assert forward.google_groups == backward.icloud_groups
        assert forward.icloud_groups == backward.google_groups
        g_after = forward.google_target or g_now
        i_after = forward.icloud_target or i_now
        assert comparable(g_after) == comparable(i_after), "both sides agree after a merge"
        again = decide(
            SideState(g_after, g_after),
            SideState(i_after, i_after),
            authority="google",
            meeting_policy="authoritative",
            project_google=identity,
            project_icloud=identity,
        )
        assert again.action == "noop"
    if forward.action == "conflict":
        assert not forward.writes


@PROFILE
@given(events())
def test_unchanged_sides_never_write(event: CanonicalEvent) -> None:
    decision = decide(
        SideState(event, event),
        SideState(event, event),
        authority="icloud",
        meeting_policy="authoritative",
        project_google=identity,
        project_icloud=identity,
    )
    assert decision.action == "noop"
