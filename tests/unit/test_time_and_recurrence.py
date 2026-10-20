from __future__ import annotations

import datetime as dt

import pytest

from calendar_bridge.model import recurrence as rec
from calendar_bridge.model import timeutil
from calendar_bridge.model.canonical import CanonicalEvent, EventData, EventTime, Recurrence

TZ = "Europe/Brussels"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Europe/Brussels", "Europe/Brussels"),
        ("Romance Standard Time", "Europe/Paris"),
        ("W. Europe Standard Time", "Europe/Berlin"),
        ("/mozilla.org/20050126_1/Europe/Brussels", "Europe/Brussels"),
        ("/citadel.org/20190914_1/America/Argentina/Buenos_Aires", "America/Argentina/Buenos_Aires"),
        ("UTC", "UTC"),
        ("Etc/GMT", "UTC"),
        ('"Europe/Brussels"', "Europe/Brussels"),
        ("Customized Time Zone", None),
        ("", None),
        (None, None),
    ],
)
def test_canonical_tzid(name: str | None, expected: str | None) -> None:
    assert timeutil.canonical_tzid(name) == expected


def test_dst_gap_moves_forward_and_overlap_uses_first() -> None:
    gap = timeutil.local_to_utc(dt.datetime(2030, 3, 31, 2, 30), TZ)
    assert gap == dt.datetime(2030, 3, 31, 1, 30, tzinfo=dt.UTC)
    overlap = timeutil.local_to_utc(dt.datetime(2030, 10, 27, 2, 30), TZ)
    assert overlap == dt.datetime(2030, 10, 27, 0, 30, tzinfo=dt.UTC)


def test_eventtime_validation_and_json() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        EventTime()
    with pytest.raises(ValueError, match="naive"):
        EventTime(local=dt.datetime(2030, 1, 1, tzinfo=dt.UTC))
    for t in (
        EventTime.all_day(dt.date(2030, 1, 1)),
        EventTime.zoned(dt.datetime(2030, 1, 1, 9), TZ),
        EventTime.floating(dt.datetime(2030, 1, 1, 9)),
    ):
        assert EventTime.from_json(t.to_json()) == t
    assert EventTime.zoned(dt.datetime(2030, 7, 1, 9), TZ).key(TZ) == "20300701T070000Z"
    assert EventTime.all_day(dt.date(2030, 7, 1)).key(TZ) == "20300701"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("RRULE:freq=weekly;byday=we,mo;interval=1", "FREQ=WEEKLY;BYDAY=MO,WE"),
        ("FREQ=MONTHLY;BYDAY=-1FR;WKST=MO", "FREQ=MONTHLY;BYDAY=-1FR"),
        ("FREQ=WEEKLY;WKST=SU;BYDAY=MO", "FREQ=WEEKLY;BYDAY=MO;WKST=SU"),
        ("FREQ=DAILY;UNTIL=20300110T235959", "FREQ=DAILY;UNTIL=20300110T225959Z"),
        ("FREQ=DAILY;COUNT=5;INTERVAL=2", "FREQ=DAILY;INTERVAL=2;COUNT=5"),
    ],
)
def test_normalize_rrule(raw: str, expected: str) -> None:
    start = EventTime.zoned(dt.datetime(2030, 1, 1, 9), TZ)
    assert rec.normalize_rrule(raw, start, TZ) == expected


def test_rrule_without_freq_is_rejected() -> None:
    with pytest.raises(rec.RecurrenceError):
        rec.parse_rrule("COUNT=3")


def test_keys_and_back() -> None:
    like = EventTime.zoned(dt.datetime(2030, 3, 25, 10), TZ)
    assert rec.key_to_eventtime("20300401T080000Z", like, TZ) == EventTime.zoned(dt.datetime(2030, 4, 1, 10), TZ)
    assert rec.key_for_google_time({"dateTime": "2030-04-01T10:00:00+02:00", "timeZone": TZ}) == "20300401T080000Z"
    assert rec.key_for_google_time({"date": "2030-04-01"}) == "20300401"
    assert rec.google_instance_id("abc", "20300401T080000Z") == "abc_20300401T080000Z"


def test_expected_occurrence_keeps_wall_clock_across_dst() -> None:
    master = EventData(
        start=EventTime.zoned(dt.datetime(2030, 3, 25, 10), TZ), end=EventTime.zoned(dt.datetime(2030, 3, 25, 11), TZ)
    )
    start, end = rec.expected_occurrence(master, "20300401T080000Z", TZ)
    assert start.local == dt.datetime(2030, 4, 1, 10) and end.local == dt.datetime(2030, 4, 1, 11)
    assert rec.is_trivial_override(master, EventData(start=start, end=end), "20300401T080000Z", TZ)


def _series(rule: str | None, start: EventTime, end: EventTime) -> CanonicalEvent:
    return CanonicalEvent(EventData(start=start, end=end), Recurrence((rule,) if rule else ()))


def test_last_occurrence_and_window() -> None:
    start = EventTime.zoned(dt.datetime(2020, 1, 6, 9), TZ)
    end = EventTime.zoned(dt.datetime(2020, 1, 6, 10), TZ)
    window = dt.datetime(2029, 1, 1, tzinfo=dt.UTC)
    assert rec.last_occurrence_end(_series("FREQ=WEEKLY", start, end), TZ) is None
    assert rec.in_window(_series("FREQ=WEEKLY", start, end), window, TZ)
    counted = _series("FREQ=WEEKLY;COUNT=3", start, end)
    assert rec.last_occurrence_end(counted, TZ) == dt.datetime(2020, 1, 20, 9, tzinfo=dt.UTC)
    assert not rec.in_window(counted, window, TZ)
    until = _series("FREQ=DAILY;UNTIL=20200110T080000Z", start, end)
    assert rec.last_occurrence_end(until, TZ) == dt.datetime(2020, 1, 10, 9, tzinfo=dt.UTC)
    allday = _series(
        "FREQ=YEARLY;UNTIL=20290101", EventTime.all_day(dt.date(2020, 5, 1)), EventTime.all_day(dt.date(2020, 5, 2))
    )
    assert rec.last_occurrence_end(allday, TZ) is not None
    assert rec.in_window(_series(None, start, end), None, TZ)
