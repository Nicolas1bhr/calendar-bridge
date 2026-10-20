"""iCalendar (RFC 5545) resources <-> canonical model.

Updates are applied *in place* to the resource's existing iCalendar text: only the properties
belonging to changed field groups are rewritten. Everything else (X-APPLE-* properties,
structured locations, alarms the canonical model cannot express, ATTENDEE/ORGANIZER of events
owned by iCloud) survives untouched.

Invitation safety: resources created by the bridge never contain ORGANIZER or ATTENDEE
properties, so iCloud's server-side scheduling has nothing to send. Guest lists of Google-owned
events appear only in the description block.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import hashlib
import zoneinfo
from collections import Counter
from typing import Any

from icalendar import Alarm as VAlarm
from icalendar import Calendar, Event, vRecur

from calendar_bridge.model import recurrence as rec
from calendar_bridge.model import timeutil
from calendar_bridge.model.canonical import (
    Alarm,
    CanonicalEvent,
    Conference,
    ConversionResult,
    EventData,
    EventTime,
    Person,
    Recurrence,
    is_override_group,
    override_key,
)
from calendar_bridge.model.text import (
    FooterFn,
    compose_description,
    norm_line,
    norm_text,
    plain_description,
    strip_footer,
)

PRODID = "-//Calendar Bridge//calendar-bridge//EN"
MARKER_PREFIX = "X-CALENDAR-BRIDGE-"
PARTSTAT = {
    "ACCEPTED": "accepted",
    "DECLINED": "declined",
    "TENTATIVE": "tentative",
    "NEEDS-ACTION": "needs-action",
}
ROLE = {
    "REQ-PARTICIPANT": "required",
    "OPT-PARTICIPANT": "optional",
    "CHAIR": "chair",
    "NON-PARTICIPANT": "non-participant",
}
DATA_FIELDS = frozenset(
    {"summary", "description", "location", "status", "transparency", "visibility", "alarms", "url", "timing"}
)
SEQUENCE_FIELDS = frozenset({"timing", "status"})


class ICSParseError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class ICloudContext:
    default_tz: str
    self_addresses: frozenset[str] = frozenset()


@dataclasses.dataclass
class ICloudConversion(ConversionResult):
    is_event: bool = True
    detached_key: str | None = None


@dataclasses.dataclass
class _Issues:
    unsupported: list[str]
    notes: list[str]


def parse_ics(text: str) -> Calendar:
    try:
        return Calendar.from_ical(text)
    except Exception as exc:  # icalendar raises a variety of ValueError subclasses
        raise ICSParseError(f"invalid iCalendar data: {type(exc).__name__}") from exc


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _text(component: Any, name: str) -> str:
    value = component.get(name)
    if value is None:
        return ""
    if isinstance(value, list):
        value = value[0]
    return str(value)


# ---------------------------------------------------------------- reading


def ical_time(prop: Any, issues: _Issues, *, recurring: bool) -> EventTime:
    value = prop.dt
    if not isinstance(value, dt.datetime):
        return EventTime.all_day(value)
    tz_param = prop.params.get("TZID")
    if value.tzinfo is None:
        if tz_param:
            tzid = timeutil.canonical_tzid(str(tz_param))
            if tzid:
                return EventTime.zoned(value, tzid)
            issues.unsupported.append(f"unknown time zone '{tz_param}' without definition")
        return EventTime.floating(value)
    instant = value.astimezone(dt.UTC)
    if tz_param:
        tzid = timeutil.canonical_tzid(str(tz_param))
        if tzid:
            return EventTime.from_instant(instant, tzid)
        if recurring:
            issues.unsupported.append(f"recurring event in custom time zone '{tz_param}'")
        else:
            issues.notes.append(f"custom time zone '{tz_param}' converted to UTC")
        return EventTime.from_instant(instant, "UTC")
    key = getattr(value.tzinfo, "key", None)
    tzid = timeutil.canonical_tzid(key) if key else None
    return EventTime.from_instant(instant, tzid or "UTC")


def add_duration(start: EventTime, delta: dt.timedelta, default_tz: str) -> EventTime:
    """RFC 5545 duration arithmetic: day parts are nominal (wall clock), the rest exact."""
    if start.date is not None:
        return EventTime.all_day(start.date + dt.timedelta(days=delta.days))
    assert start.local is not None
    nominal = EventTime(local=start.local + dt.timedelta(days=delta.days), tzid=start.tzid)
    exact = dt.timedelta(seconds=delta.seconds, microseconds=delta.microseconds)
    if not exact:
        return nominal
    instant = nominal.instant(default_tz) + exact
    return rec.time_like(instant, start, default_tz)


def _end_time(component: Any, start: EventTime, issues: _Issues, ctx: ICloudContext, recurring: bool) -> EventTime:
    if component.get("DTEND") is not None:
        return ical_time(component["DTEND"], issues, recurring=recurring)
    duration = component.get("DURATION")
    if duration is not None and isinstance(duration.dt, dt.timedelta):
        return add_duration(start, duration.dt, ctx.default_tz)
    if start.date is not None:
        return EventTime.all_day(start.date + dt.timedelta(days=1))
    return start


def alarm_canonical(valarm: Any, start: EventTime, end: EventTime, default_tz: str, *, recurring: bool) -> Alarm | None:
    """Canonical form of a VALARM, or None when Google could not hold it (it is then kept
    verbatim in iCloud and never rewritten)."""
    method = {"DISPLAY": "display", "AUDIO": "display", "EMAIL": "email"}.get(_text(valarm, "ACTION").upper())
    trigger = valarm.get("TRIGGER")
    if method is None or trigger is None:
        return None
    value = trigger.dt
    if isinstance(value, dt.timedelta):
        offset = value
        if str(trigger.params.get("RELATED", "START")).upper() == "END":
            offset = value + (end.instant(default_tz) - start.instant(default_tz))
    elif isinstance(value, dt.datetime) and not recurring:
        instant = value if value.tzinfo else timeutil.local_to_utc(value, default_tz)
        offset = instant - start.instant(default_tz)
    else:
        return None
    seconds = -offset.total_seconds()
    if seconds < 0:
        return None
    return Alarm(method, round(seconds / 60))


def _alarms(
    component: Any, start: EventTime, end: EventTime, ctx: ICloudContext, issues: _Issues, recurring: bool
) -> tuple[Alarm, ...]:
    alarms = []
    for sub in component.subcomponents:
        if sub.name != "VALARM":
            continue
        canonical = alarm_canonical(sub, start, end, ctx.default_tz, recurring=recurring)
        if canonical is not None:
            alarms.append(canonical)
        elif _text(sub, "ACTION").upper() not in ("NONE", ""):
            issues.notes.append("an alert type Google cannot represent is kept only in iCloud")
    return tuple(alarms)


def _address(value: Any) -> str:
    text = str(value).strip()
    if text.lower().startswith("mailto:"):
        text = text[7:]
    return text.lower()


def _people(component: Any, ctx: ICloudContext) -> tuple[Person | None, tuple[Person, ...]]:
    organizer = None
    org = component.get("ORGANIZER")
    if org is not None:
        email = _address(org)
        if email:
            organizer = Person(email=email, name=str(org.params.get("CN", "")), is_self=email in ctx.self_addresses)
    attendees = []
    for att in _as_list(component.get("ATTENDEE")):
        email = _address(att)
        if not email or str(att.params.get("CUTYPE", "")).upper() in ("ROOM", "RESOURCE"):
            continue
        attendees.append(
            Person(
                email=email,
                name=str(att.params.get("CN", "")),
                status=PARTSTAT.get(str(att.params.get("PARTSTAT", "NEEDS-ACTION")).upper(), ""),
                role=ROLE.get(str(att.params.get("ROLE", "REQ-PARTICIPANT")).upper(), "required"),
                is_self=email in ctx.self_addresses,
            )
        )
    return organizer, tuple(attendees)


def _conference(component: Any) -> tuple[Conference, ...]:
    links = []
    for prop in _as_list(component.get("CONFERENCE")):
        links.append(Conference(str(prop).strip(), str(prop.params.get("LABEL", "")) or "Video call"))
    google = component.get("X-GOOGLE-CONFERENCE")
    if google is not None and not any(c.uri == str(google).strip() for c in links):
        links.append(Conference(str(google).strip(), "Google Meet"))
    return tuple(links)


def eventdata_from_vevent(component: Any, ctx: ICloudContext, issues: _Issues, *, recurring: bool) -> EventData:
    if component.get("DTSTART") is None:
        raise ICSParseError("VEVENT without DTSTART")
    start = ical_time(component["DTSTART"], issues, recurring=recurring)
    end = _end_time(component, start, issues, ctx, recurring)
    organizer, attendees = _people(component, ctx)
    status = _text(component, "STATUS").lower() or "confirmed"
    klass = _text(component, "CLASS").lower()
    return EventData(
        start=start,
        end=end,
        summary=norm_line(_text(component, "SUMMARY")),
        description=strip_footer(norm_text(_text(component, "DESCRIPTION"))),
        location=norm_line(_text(component, "LOCATION")),
        status=status if status in ("confirmed", "tentative", "cancelled") else "confirmed",
        transparency="transparent" if _text(component, "TRANSP").upper() == "TRANSPARENT" else "opaque",
        visibility=klass if klass in ("public", "private", "confidential") else "default",
        alarms=_alarms(component, start, end, ctx, issues, recurring),
        url=_text(component, "URL").strip(),
        conference=_conference(component),
        organizer=organizer,
        attendees=attendees,
    )


def _value_key(prop_value: Any, params: Any, start: EventTime, default_tz: str) -> str:
    """Recurrence key of one RDATE/EXDATE/RECURRENCE-ID value, honouring a TZID parameter
    that icalendar could not resolve to a tzinfo."""
    if isinstance(prop_value, dt.datetime) and prop_value.tzinfo is None:
        tz_param = params.get("TZID") if params is not None else None
        tzid = timeutil.canonical_tzid(str(tz_param)) if tz_param else None
        if tzid:
            return timeutil.local_to_utc(prop_value, tzid).strftime("%Y%m%dT%H%M%SZ")
    return rec.key_for_value(prop_value, default_tz)


def _recurrence(
    component: Any, start: EventTime, ctx: ICloudContext, issues: _Issues
) -> tuple[list[str], set[str], set[str]]:
    rrules: list[str] = []
    for prop in _as_list(component.get("RRULE")):
        try:
            rrules.append(rec.normalize_rrule(prop.to_ical().decode(), start, ctx.default_tz))
        except rec.RecurrenceError as exc:
            issues.unsupported.append(f"unparseable RRULE ({exc})")
    if component.get("EXRULE") is not None:
        issues.unsupported.append("EXRULE (deprecated by RFC 5545)")
    rdates: set[str] = set()
    exdates: set[str] = set()
    for name, target in (("RDATE", rdates), ("EXDATE", exdates)):
        for prop in _as_list(component.get(name)):
            if str(prop.params.get("VALUE", "")).upper() == "PERIOD":
                issues.unsupported.append(f"{name} with PERIOD values")
                continue
            for value in prop.dts:
                if isinstance(value.dt, tuple):
                    issues.unsupported.append(f"{name} with PERIOD values")
                    continue
                target.add(_value_key(value.dt, prop.params, start, ctx.default_tz))
    return rrules, rdates, exdates


def _markers(component: Any) -> dict[str, str]:
    return {
        name[len(MARKER_PREFIX) :]: str(value[0] if isinstance(value, list) else value)
        for name, value in component.items()
        if name.upper().startswith(MARKER_PREFIX)
    }


def recurrence_id_key(component: Any, start_like: EventTime, default_tz: str) -> str:
    rid = component["RECURRENCE-ID"]
    return _value_key(rid.dt, rid.params, start_like, default_tz)


def icloud_to_canonical(text: str, ctx: ICloudContext) -> ICloudConversion:
    result = ICloudConversion(event=None)
    try:
        calendar = parse_ics(text)
    except ICSParseError as exc:
        result.unsupported.append(str(exc))
        return result
    components = list(calendar.walk("VEVENT"))
    if not components:
        result.is_event = False
        return result
    issues = _Issues(result.unsupported, result.notes)
    uids = {_text(c, "UID") for c in components}
    if len(uids) != 1 or "" in uids:
        result.unsupported.append("resource does not have exactly one UID")
        return result
    result.uid = uids.pop()
    masters = [c for c in components if c.get("RECURRENCE-ID") is None]
    overrides = [c for c in components if c.get("RECURRENCE-ID") is not None]
    result.markers = _markers(masters[0] if masters else components[0])
    if len(masters) > 1:
        result.unsupported.append("several master components share one UID")
        return result
    try:
        if not masters:
            if len(overrides) != 1:
                result.unsupported.append("several detached occurrences without their series")
                return result
            data = eventdata_from_vevent(overrides[0], ctx, issues, recurring=False)
            result.detached_key = recurrence_id_key(overrides[0], data.start, ctx.default_tz)
            result.notes.append("single occurrence of a series that is not in this calendar")
            result.event = CanonicalEvent(master=data)
            return result

        master_component = masters[0]
        recurring = any(master_component.get(n) is not None for n in ("RRULE", "RDATE"))
        master = eventdata_from_vevent(master_component, ctx, issues, recurring=recurring)
        rrules, rdates, exdates = _recurrence(master_component, master.start, ctx, issues)
        override_map: dict[str, EventData] = {}
        for component in overrides:
            rid = component["RECURRENCE-ID"]
            if str(rid.params.get("RANGE", "")).upper() == "THISANDFUTURE":
                result.unsupported.append("RANGE=THISANDFUTURE occurrence override")
                continue
            key = recurrence_id_key(component, master.start, ctx.default_tz)
            data = eventdata_from_vevent(component, ctx, issues, recurring=False)
            if data.status == "cancelled":
                exdates.add(key)
            elif not rec.is_trivial_override(master, data, key, ctx.default_tz):
                override_map[key] = data
    except (ICSParseError, ValueError, KeyError, TypeError) as exc:
        result.unsupported.append(f"unreadable event data ({type(exc).__name__})")
        return result
    for key in exdates:
        override_map.pop(key, None)
    result.event = CanonicalEvent(
        master=master,
        recurrence=Recurrence(tuple(rrules), tuple(rdates), tuple(exdates)),
        overrides=tuple(override_map.items()),
    )
    return result


# ---------------------------------------------------------------- projection


def _project_data(data: EventData) -> EventData:
    return dataclasses.replace(
        data,
        description=plain_description(data.description),
        alarms=() if data.alarms is None else data.alarms,
    )


def project_for_icloud(event: CanonicalEvent, ctx: ICloudContext) -> CanonicalEvent:
    """What ``event`` looks like after being written to iCloud and read back."""
    del ctx
    return CanonicalEvent(
        master=_project_data(event.master),
        recurrence=event.recurrence,
        overrides=tuple((k, _project_data(v)) for k, v in event.overrides),
    )


# ---------------------------------------------------------------- writing


def ical_value(t: EventTime) -> dt.date | dt.datetime:
    if t.date is not None:
        return t.date
    assert t.local is not None
    if t.tzid == "UTC":
        return t.local.replace(tzinfo=dt.UTC)
    if t.tzid:
        return t.local.replace(tzinfo=zoneinfo.ZoneInfo(t.tzid))
    return t.local


def _replace(component: Any, name: str, value: Any = None, **kwargs: Any) -> None:
    component.pop(name, None)
    if value not in (None, "", []):
        component.add(name, value, **kwargs)


def _new_alarm(alarm: Alarm, self_address: str | None) -> VAlarm:
    valarm = VAlarm()
    valarm.add("ACTION", "EMAIL" if alarm.method == "email" else "DISPLAY")
    valarm.add("TRIGGER", dt.timedelta(minutes=-alarm.minutes_before))
    valarm.add("DESCRIPTION", "Reminder")
    if alarm.method == "email":
        valarm.add("SUMMARY", "Reminder")
        if self_address:
            valarm.add("ATTENDEE", f"mailto:{self_address}")
    return valarm


def _sync_alarms(component: Any, target: EventData, ctx: ICloudContext, *, recurring: bool) -> None:
    wanted = Counter(target.alarms or ())
    kept = []
    for sub in component.subcomponents:
        if sub.name != "VALARM":
            kept.append(sub)
            continue
        canonical = alarm_canonical(sub, target.start, target.end, ctx.default_tz, recurring=recurring)
        if canonical is None:
            kept.append(sub)  # not representable: never touched
        elif wanted[canonical] > 0:
            kept.append(sub)
            wanted[canonical] -= 1
    self_address = min(ctx.self_addresses) if ctx.self_addresses else None
    for alarm, count in sorted(wanted.items(), key=lambda kv: (kv[0].minutes_before, kv[0].method)):
        kept.extend(_new_alarm(alarm, self_address) for _ in range(count))
    component.subcomponents = kept


def _set_timing(component: Any, data: EventData, ctx: ICloudContext) -> None:
    _replace(component, "DTSTART", ical_value(data.start))
    if component.get("DURATION") is not None and component.get("DTEND") is None:
        if data.start.is_all_day and data.end.is_all_day:
            assert data.start.date and data.end.date
            duration = dt.timedelta(days=(data.end.date - data.start.date).days)
        else:
            duration = data.end.instant(ctx.default_tz) - data.start.instant(ctx.default_tz)
        _replace(component, "DURATION", duration)
    else:
        _replace(component, "DTEND", ical_value(data.end))


def write_data(
    component: Any,
    target: EventData,
    fields: set[str],
    ctx: ICloudContext,
    footer: FooterFn,
    *,
    recurring: bool,
    mirror_conference: bool,
) -> None:
    if "summary" in fields:
        _replace(component, "SUMMARY", target.summary)
    if fields & {"description", "people", "conference"}:
        _replace(
            component,
            "DESCRIPTION",
            compose_description(plain_description(target.description), footer(target)),
        )
    if "location" in fields:
        _replace(component, "LOCATION", target.location)
    if "status" in fields:
        _replace(component, "STATUS", target.status.upper())
    if "transparency" in fields:
        _replace(component, "TRANSP", target.transparency.upper())
    if "visibility" in fields:
        _replace(component, "CLASS", None if target.visibility == "default" else target.visibility.upper())
    if "timing" in fields:
        _set_timing(component, target, ctx)
    if "alarms" in fields:
        _sync_alarms(component, target, ctx, recurring=recurring)
    if "url" in fields:
        _replace(component, "URL", target.url)
    if "conference" in fields and mirror_conference:
        _replace(component, "X-GOOGLE-CONFERENCE", target.conference[0].uri if target.conference else None)


def _set_recurrence(component: Any, event: CanonicalEvent, ctx: ICloudContext, groups: set[str]) -> None:
    start = event.master.start
    if "timing" in groups:
        component.pop("RRULE", None)
        for rule in event.recurrence.rrules:
            component.add("RRULE", vRecur.from_ical(rule))
        _replace(
            component,
            "RDATE",
            [ical_value(rec.key_to_eventtime(k, start, ctx.default_tz)) for k in event.recurrence.rdates],
        )
    if groups & {"timing", "exdates"}:
        _replace(
            component,
            "EXDATE",
            [ical_value(rec.key_to_eventtime(k, start, ctx.default_tz)) for k in event.recurrence.exdates],
        )


def _stamp(component: Any, now: dt.datetime, *, bump_sequence: bool) -> None:
    _replace(component, "DTSTAMP", now)
    _replace(component, "LAST-MODIFIED", now)
    if bump_sequence:
        current = component.get("SEQUENCE")
        _replace(component, "SEQUENCE", int(current) + 1 if current is not None else 1)


def _new_override(
    uid: str,
    key: str,
    master: EventData,
    data: EventData,
    ctx: ICloudContext,
    footer: FooterFn,
    now: dt.datetime,
    mirror_conference: bool,
    template: Any = None,
) -> Event:
    component = Event()
    component.add("UID", uid)
    component.add("DTSTAMP", now)
    component.add("RECURRENCE-ID", ical_value(rec.key_to_eventtime(key, master.start, ctx.default_tz)))
    component.add("SEQUENCE", 0)
    if template is not None:
        # An occurrence of a meeting keeps the meeting's organizer and guests; without them the
        # server would treat the occurrence as having no guests (i.e. cancelled for everyone).
        for name in ("ORGANIZER", "ATTENDEE"):
            for prop in _as_list(template.get(name)):
                component.add(name, prop)
    write_data(
        component,
        data,
        set(DATA_FIELDS) | {"conference"},
        ctx,
        footer,
        recurring=False,
        mirror_conference=mirror_conference,
    )
    return component


def build_ics(
    event: CanonicalEvent,
    *,
    uid: str,
    ctx: ICloudContext,
    footer: FooterFn,
    markers: dict[str, str],
    now: dt.datetime,
    mirror_conference: bool = True,
) -> str:
    calendar = Calendar()
    calendar.add("PRODID", PRODID)
    calendar.add("VERSION", "2.0")
    calendar.add("CALSCALE", "GREGORIAN")
    master = Event()
    master.add("UID", uid)
    master.add("DTSTAMP", now)
    master.add("CREATED", now)
    master.add("LAST-MODIFIED", now)
    master.add("SEQUENCE", 0)
    write_data(
        master,
        event.master,
        set(DATA_FIELDS) | {"conference"},
        ctx,
        footer,
        recurring=event.is_recurring,
        mirror_conference=mirror_conference,
    )
    if event.is_recurring:
        _set_recurrence(master, event, ctx, {"timing", "exdates"})
    for name, value in markers.items():
        master.add(MARKER_PREFIX + name.upper(), value)
    calendar.add_component(master)
    for key, data in event.overrides:
        calendar.add_component(_new_override(uid, key, event.master, data, ctx, footer, now, mirror_conference))
    calendar.add_missing_timezones()
    return calendar.to_ical().decode("utf-8")


def apply_to_ics(
    text: str,
    target: CanonicalEvent,
    groups: set[str],
    *,
    ctx: ICloudContext,
    footer: FooterFn,
    now: dt.datetime,
    mirror_conference: bool,
    detached_key: str | None = None,
) -> str:
    """Rewrite only the properties of ``groups`` in an existing resource."""
    calendar = parse_ics(text)
    components = list(calendar.walk("VEVENT"))
    if detached_key is not None:
        master_component = components[0]
    else:
        master_component = next(c for c in components if c.get("RECURRENCE-ID") is None)
    uid = _text(master_component, "UID")
    master_groups = {g for g in groups if not is_override_group(g)}
    fields = master_groups & (set(DATA_FIELDS) | {"people", "conference"})
    issues = _Issues([], [])

    if fields or master_groups & {"exdates"}:
        write_data(
            master_component,
            target.master,
            fields,
            ctx,
            footer,
            recurring=target.is_recurring,
            mirror_conference=mirror_conference,
        )
        if detached_key is None:
            _set_recurrence(master_component, target, ctx, master_groups)
        _stamp(master_component, now, bump_sequence=bool(master_groups & (SEQUENCE_FIELDS | {"exdates"})))

    if detached_key is None:
        exdates = set(target.recurrence.exdates)
        overrides = target.override_map()
        existing = {
            recurrence_id_key(c, target.master.start, ctx.default_tz): c
            for c in components
            if c.get("RECURRENCE-ID") is not None
        }
        touched = {override_key(g) for g in groups if is_override_group(g)}
        if "exdates" in groups:
            touched |= exdates & set(existing)
        for key in sorted(touched):
            component = existing.get(key)
            data = None if key in exdates else overrides.get(key)
            if data is None:
                if component is not None:
                    calendar.subcomponents.remove(component)
            elif component is None:
                calendar.add_component(
                    _new_override(
                        uid, key, target.master, data, ctx, footer, now, mirror_conference, template=master_component
                    )
                )
            else:
                current = eventdata_from_vevent(component, ctx, issues, recurring=False)
                changed = {f for f in DATA_FIELDS if _field(current, f) != _field(data, f)}
                if current.people_value() != data.people_value() or current.conference != data.conference:
                    changed |= {"people", "conference"}
                write_data(component, data, changed, ctx, footer, recurring=False, mirror_conference=mirror_conference)
                _stamp(component, now, bump_sequence=bool(changed & SEQUENCE_FIELDS))
    calendar.add_missing_timezones()
    return calendar.to_ical().decode("utf-8")


def _field(data: EventData, field: str) -> Any:
    if field == "timing":
        return (data.start, data.end)
    return getattr(data, field)


def resource_name_for_uid(uid: str) -> str:
    """Deterministic, URL-safe resource name: retries of a create hit the same URL, so a
    conditional PUT (If-None-Match: *) can never produce a second copy."""
    digest = hashlib.sha256(uid.encode("utf-8")).digest()
    return "cb-" + base64.b32encode(digest).decode("ascii").rstrip("=").lower()[:40] + ".ics"
