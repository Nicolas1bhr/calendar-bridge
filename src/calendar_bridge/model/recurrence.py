"""Recurrence identity and RRULE normalization.

An occurrence is identified by its *original* start ("recurrence key"), never by its current
start: moving an occurrence does not change which occurrence it is. Keys are ``YYYYMMDD`` for
all-day series and the UTC instant ``YYYYMMDDTHHMMSSZ`` for timed series, which is also the
suffix Google uses for instance ids (``<masterId>_<key>``).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any

from dateutil import rrule as du_rrule

from calendar_bridge.model import timeutil
from calendar_bridge.model.canonical import CanonicalEvent, EventData, EventTime

RRULE_ORDER = (
    "FREQ",
    "INTERVAL",
    "COUNT",
    "UNTIL",
    "BYSECOND",
    "BYMINUTE",
    "BYHOUR",
    "BYDAY",
    "BYMONTHDAY",
    "BYYEARDAY",
    "BYWEEKNO",
    "BYMONTH",
    "BYSETPOS",
    "WKST",
)
WEEKDAY_ORDER = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
MAX_EXPANSION = 10_000


class RecurrenceError(ValueError):
    pass


def parse_rrule(text: str) -> dict[str, str]:
    body = text.strip()
    if body.upper().startswith("RRULE:"):
        body = body[6:]
    parts: dict[str, str] = {}
    for raw in body.split(";"):
        chunk = raw.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise RecurrenceError(f"malformed RRULE part {chunk!r}")
        name, value = chunk.split("=", 1)
        parts[name.strip().upper()] = value.strip()
    if "FREQ" not in parts:
        raise RecurrenceError("RRULE without FREQ")
    return parts


def _byday_sort_key(token: str) -> tuple[int, int]:
    day = token[-2:]
    ordinal = token[:-2]
    return (int(ordinal) if ordinal not in ("", "+") else 0, WEEKDAY_ORDER.get(day, 9))


def _normalize_list(name: str, value: str) -> str:
    tokens = [t.strip().upper().lstrip("+") for t in value.split(",") if t.strip()]
    if name == "BYDAY":
        tokens.sort(key=_byday_sort_key)
    else:
        try:
            tokens.sort(key=int)
        except ValueError:
            tokens.sort()
    return ",".join(dict.fromkeys(tokens))


def _normalize_until(value: str, start: EventTime, default_tz: str) -> str:
    value = value.strip().upper()
    if len(value) == 8 and value.isdigit():
        return value
    if value.endswith("Z"):
        return value
    try:
        local = dt.datetime.strptime(value, "%Y%m%dT%H%M%S")
    except ValueError as exc:
        raise RecurrenceError(f"malformed UNTIL {value!r}") from exc
    if start.is_all_day or start.is_floating:
        return value
    # RFC 5545: UNTIL must be UTC when DTSTART has a zone; convert a local UNTIL accordingly.
    return timeutil.local_to_utc(local, start.tzid or default_tz).strftime("%Y%m%dT%H%M%SZ")


def normalize_rrule(text: str, start: EventTime, default_tz: str) -> str:
    parts = parse_rrule(text)
    out: dict[str, str] = {}
    for name, value in parts.items():
        upper = value.upper()
        if name == "INTERVAL" and upper == "1":
            continue
        if name == "WKST" and upper == "MO":
            continue
        if name == "UNTIL":
            out[name] = _normalize_until(value, start, default_tz)
        elif name.startswith("BY"):
            out[name] = _normalize_list(name, upper)
        else:
            out[name] = upper
    ordered = [n for n in RRULE_ORDER if n in out]
    ordered += sorted(n for n in out if n not in RRULE_ORDER)
    return ";".join(f"{n}={out[n]}" for n in ordered)


def key_for_value(value: dt.date | dt.datetime, default_tz: str) -> str:
    """Recurrence key for a parsed iCalendar date/datetime value."""
    if not isinstance(value, dt.datetime):
        return value.strftime("%Y%m%d")
    if value.tzinfo is None:
        value = timeutil.local_to_utc(value, default_tz)
    return value.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def key_for_google_time(obj: dict[str, Any]) -> str:
    """Recurrence key for a Google ``originalStartTime``/``start`` object."""
    if obj.get("date"):
        return dt.date.fromisoformat(obj["date"]).strftime("%Y%m%d")
    instant = dt.datetime.fromisoformat(obj["dateTime"].replace("Z", "+00:00"))
    if instant.tzinfo is None:
        tz = timeutil.canonical_tzid(obj.get("timeZone")) or "UTC"
        instant = timeutil.local_to_utc(instant, tz)
    return instant.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def is_date_key(key: str) -> bool:
    return len(key) == 8


def key_instant(key: str, default_tz: str) -> dt.datetime:
    if is_date_key(key):
        day = dt.datetime.strptime(key, "%Y%m%d")
        return timeutil.local_to_utc(day, default_tz)
    return dt.datetime.strptime(key, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC)


def key_to_eventtime(key: str, like: EventTime, default_tz: str) -> EventTime:
    """Express a recurrence key in the same form as the series' DTSTART (as RFC 5545 requires
    for RECURRENCE-ID and EXDATE values)."""
    if is_date_key(key):
        return EventTime.all_day(dt.datetime.strptime(key, "%Y%m%d").date())
    instant = key_instant(key, default_tz)
    if like.is_all_day:
        return EventTime.all_day(timeutil.utc_to_local(instant, default_tz).date())
    if like.tzid:
        return EventTime.zoned(timeutil.utc_to_local(instant, like.tzid), like.tzid)
    return EventTime.floating(timeutil.utc_to_local(instant, default_tz))


def google_instance_id(master_id: str, key: str) -> str:
    return f"{master_id}_{key}"


def time_like(instant: dt.datetime, like: EventTime, default_tz: str) -> EventTime:
    """Express an instant in the same zone form as ``like`` (zoned, UTC or floating)."""
    if like.tzid:
        return EventTime.zoned(timeutil.utc_to_local(instant, like.tzid), like.tzid)
    return EventTime.floating(timeutil.utc_to_local(instant, default_tz))


def expected_occurrence(master: EventData, key: str, default_tz: str) -> tuple[EventTime, EventTime]:
    """Start and end an unmodified occurrence with recurrence key ``key`` would have."""
    start = key_to_eventtime(key, master.start, default_tz)
    if master.start.is_all_day and master.end.is_all_day:
        assert start.date is not None and master.start.date and master.end.date
        return start, EventTime.all_day(start.date + (master.end.date - master.start.date))
    end_instant = start.instant(default_tz) + master.duration(default_tz)
    return start, time_like(end_instant, master.end, default_tz)


def is_trivial_override(master: EventData, data: EventData, key: str, default_tz: str) -> bool:
    """True when an override is indistinguishable from the occurrence the rule generates.

    Providers create such records as a side effect (Google for per-instance RSVPs, some clients
    when an edit is undone); treating them as "no override" avoids pointless churn.
    """
    try:
        start, end = expected_occurrence(master, key, default_tz)
    except (ValueError, AssertionError):
        return False
    # Same comparison as occurrence groups (guest lists and call links are informational).
    return dataclasses.replace(master, start=start, end=end).content_json() == data.content_json()


def _dtstart_for_expansion(start: EventTime, default_tz: str) -> dt.datetime:
    if start.is_all_day:
        assert start.date is not None
        return dt.datetime.combine(start.date, dt.time())
    if start.is_floating:
        assert start.local is not None
        return start.local
    assert start.local is not None and start.tzid is not None
    return start.local.replace(tzinfo=timeutil.zone(start.tzid))


def _rule_for_expansion(rule: str, naive: bool) -> str:
    parts = parse_rrule(rule)
    until = parts.get("UNTIL")
    if until and naive and until.endswith("Z"):
        parts["UNTIL"] = until[:-1]
    if until and not naive and len(until) == 8:
        parts["UNTIL"] = until + "T235959Z"
    return ";".join(f"{k}={v}" for k, v in parts.items())


def last_occurrence_end(event: CanonicalEvent, default_tz: str) -> dt.datetime | None:
    """UTC end of the last occurrence, or ``None`` if the series is unbounded or unknowable.

    Used only to decide whether old events fall outside a mapping's history window; any doubt
    resolves to "unbounded" so that the event is kept in scope.
    """
    master = event.master
    duration = master.duration(default_tz)
    starts: list[dt.datetime] = [master.start.instant(default_tz)]
    for rule in event.recurrence.rrules:
        parts = parse_rrule(rule)
        if "COUNT" not in parts and "UNTIL" not in parts:
            return None
        dtstart = _dtstart_for_expansion(master.start, default_tz)
        naive = dtstart.tzinfo is None
        try:
            expanded = du_rrule.rrulestr(_rule_for_expansion(rule, naive), dtstart=dtstart)
            last: dt.datetime | None = None
            for count, occurrence in enumerate(expanded):
                last = occurrence
                if count >= MAX_EXPANSION:
                    return None
        except (ValueError, TypeError):
            return None
        if last is not None:
            if last.tzinfo is None:
                last = timeutil.local_to_utc(last, default_tz)
            starts.append(last.astimezone(dt.UTC))
    for key in event.recurrence.rdates:
        starts.append(key_instant(key, default_tz))
    ends = [s + duration for s in starts]
    for _, override in event.overrides:
        ends.append(override.end.instant(default_tz))
    return max(ends)


def in_window(event: CanonicalEvent, window_start: dt.datetime | None, default_tz: str) -> bool:
    if window_start is None:
        return True
    last_end = last_occurrence_end(event, default_tz)
    return last_end is None or last_end >= window_start
