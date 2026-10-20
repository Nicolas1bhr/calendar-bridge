"""Google Calendar event resources <-> canonical model.

Writes are built as *partial* bodies (PATCH) containing only the field groups being changed, so
everything Google stores that the canonical model does not cover (colors, attachments,
conference data, guest permissions, extended properties of other apps...) is preserved.

Invitation safety: bodies never contain ``attendees``, ``organizer`` or ``conferenceData``.
Mirrors carry guest lists and call links only as text in the description block.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any

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
)
from calendar_bridge.model.text import (
    FooterFn,
    compose_description,
    norm_line,
    norm_text,
    strip_footer,
)

MAX_REMINDER_OVERRIDES = 5
MAX_REMINDER_MINUTES = 40320
MARKER_KEYS = ("calendarBridge", "cbOrigin", "cbUid", "cbMapping")
SUPPORTED_EVENT_TYPES = frozenset({"default"})
RESPONSE_STATUS = {
    "needsAction": "needs-action",
    "declined": "declined",
    "tentative": "tentative",
    "accepted": "accepted",
}


@dataclasses.dataclass(frozen=True)
class GoogleContext:
    default_tz: str
    calendar_tz: str | None = None
    default_reminders: tuple[Alarm, ...] = ()
    self_email: str | None = None
    mirror_event_types: frozenset[str] = frozenset({"fromGmail"})


# ---------------------------------------------------------------- reading


def google_time(obj: dict[str, Any], ctx: GoogleContext) -> EventTime:
    if obj.get("date"):
        return EventTime.all_day(dt.date.fromisoformat(obj["date"]))
    value = dt.datetime.fromisoformat(obj["dateTime"].replace("Z", "+00:00"))
    tzid = timeutil.canonical_tzid(obj.get("timeZone"))
    if value.tzinfo is None:
        zone = tzid or ctx.calendar_tz or ctx.default_tz
        return EventTime.zoned(value, zone)
    if tzid:
        return EventTime.zoned(timeutil.utc_to_local(value, tzid), tzid)
    return EventTime.from_instant(value, "UTC")


def _default_end(start: EventTime) -> EventTime:
    if start.date is not None:
        return EventTime.all_day(start.date + dt.timedelta(days=1))
    return start


def _alarms(raw: dict[str, Any], start: EventTime, ctx: GoogleContext) -> tuple[Alarm, ...] | None:
    reminders = raw.get("reminders")
    if reminders is None or reminders.get("useDefault"):
        # The API exposes the calendar's default reminders for timed events only.
        return None if start.is_all_day else ctx.default_reminders
    alarms = []
    for override in reminders.get("overrides") or []:
        method = "email" if override.get("method") == "email" else "display"
        alarms.append(Alarm(method, int(override.get("minutes", 0))))
    return tuple(alarms)


def _people(raw: dict[str, Any], ctx: GoogleContext) -> tuple[Person | None, tuple[Person, ...]]:
    self_email = (ctx.self_email or "").lower()
    organizer = None
    org = raw.get("organizer") or {}
    if org.get("email"):
        email = org["email"].lower()
        organizer = Person(
            email=email,
            name=org.get("displayName", ""),
            is_self=bool(org.get("self")) or email == self_email,
        )
    attendees = []
    for att in raw.get("attendees") or []:
        email = (att.get("email") or "").lower()
        if not email or att.get("resource"):
            continue
        attendees.append(
            Person(
                email=email,
                name=att.get("displayName", ""),
                status=RESPONSE_STATUS.get(att.get("responseStatus", ""), ""),
                role="optional" if att.get("optional") else "required",
                is_self=bool(att.get("self")) or email == self_email,
            )
        )
    return organizer, tuple(attendees)


def _conference(raw: dict[str, Any]) -> tuple[Conference, ...]:
    data = raw.get("conferenceData") or {}
    name = (data.get("conferenceSolution") or {}).get("name") or ""
    links = []
    for entry in data.get("entryPoints") or []:
        if entry.get("entryPointType") == "video" and entry.get("uri"):
            links.append(Conference(entry["uri"], name or entry.get("label") or "Video call"))
    if not links and raw.get("hangoutLink"):
        links.append(Conference(raw["hangoutLink"], "Google Meet"))
    return tuple(links)


def eventdata_from_google(raw: dict[str, Any], ctx: GoogleContext) -> EventData:
    start = google_time(raw["start"], ctx)
    end = google_time(raw["end"], ctx) if raw.get("end") else _default_end(start)
    organizer, attendees = _people(raw, ctx)
    status = raw.get("status") or "confirmed"
    return EventData(
        start=start,
        end=end,
        summary=norm_line(raw.get("summary")),
        description=strip_footer(norm_text(raw.get("description"))),
        location=norm_line(raw.get("location")),
        status=status if status in ("confirmed", "tentative", "cancelled") else "confirmed",
        transparency="transparent" if raw.get("transparency") == "transparent" else "opaque",
        visibility=raw.get("visibility") or "default",
        alarms=_alarms(raw, start, ctx),
        url=((raw.get("source") or {}).get("url") or "").strip(),
        conference=_conference(raw),
        organizer=organizer,
        attendees=attendees,
    )


def _split_line(line: str) -> tuple[str, dict[str, str], str]:
    head, _, value = line.partition(":")
    name, *params = head.split(";")
    parsed = {}
    for param in params:
        if "=" in param:
            k, v = param.split("=", 1)
            parsed[k.strip().upper()] = v.strip()
    return name.strip().upper(), parsed, value.strip()


def _key_from_text(value: str, params: dict[str, str], start: EventTime, default_tz: str) -> str:
    if len(value) == 8 and value.isdigit():
        return value
    if value.endswith("Z"):
        return dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").strftime("%Y%m%dT%H%M%SZ")
    local = dt.datetime.strptime(value, "%Y%m%dT%H%M%S")
    tzid = timeutil.canonical_tzid(params.get("TZID")) or start.tzid or default_tz
    return timeutil.local_to_utc(local, tzid).strftime("%Y%m%dT%H%M%SZ")


@dataclasses.dataclass
class ParsedRecurrence:
    rrules: list[str]
    rdates: set[str]
    exdates: set[str]


def parse_recurrence(lines: list[str], start: EventTime, default_tz: str, unsupported: list[str]) -> ParsedRecurrence:
    parsed = ParsedRecurrence([], set(), set())
    for line in lines:
        name, params, value = _split_line(line)
        try:
            if name == "RRULE":
                parsed.rrules.append(rec.normalize_rrule(value, start, default_tz))
            elif name in ("EXDATE", "RDATE"):
                if params.get("VALUE", "").upper() == "PERIOD":
                    unsupported.append(f"{name} with PERIOD values")
                    continue
                target = parsed.exdates if name == "EXDATE" else parsed.rdates
                for token in value.split(","):
                    if token.strip():
                        target.add(_key_from_text(token.strip(), params, start, default_tz))
            else:
                unsupported.append(f"recurrence property {name}")
        except (ValueError, rec.RecurrenceError) as exc:
            unsupported.append(f"unparseable recurrence line ({exc})")
    return parsed


def exdate_line_keys(master_raw: dict[str, Any], ctx: GoogleContext) -> set[str]:
    """Keys excluded through EXDATE lines on the master (as opposed to cancelled instances)."""
    start = google_time(master_raw["start"], ctx)
    return parse_recurrence(master_raw.get("recurrence") or [], start, ctx.default_tz, []).exdates


def google_to_canonical(
    master_raw: dict[str, Any], instances: list[dict[str, Any]], ctx: GoogleContext
) -> ConversionResult:
    """Convert a Google master/single event plus its exception instances."""
    result = ConversionResult(event=None, uid=master_raw.get("iCalUID"))
    private = (master_raw.get("extendedProperties") or {}).get("private") or {}
    result.markers = {k: str(v) for k, v in private.items() if k in MARKER_KEYS}
    event_type = master_raw.get("eventType") or "default"
    if event_type not in SUPPORTED_EVENT_TYPES and event_type not in ctx.mirror_event_types:
        result.unsupported.append(f"Google '{event_type}' event type")
    if "start" not in master_raw:
        result.unsupported.append("event without a start time")
        return result

    master = eventdata_from_google(master_raw, ctx)
    parsed = parse_recurrence(master_raw.get("recurrence") or [], master.start, ctx.default_tz, result.unsupported)
    exdates = set(parsed.exdates)
    overrides: dict[str, EventData] = {}
    for instance in instances:
        original = instance.get("originalStartTime")
        if not original:
            continue
        key = rec.key_for_google_time(original)
        if instance.get("status") == "cancelled":
            exdates.add(key)
            continue
        if "start" not in instance:
            result.notes.append(f"incomplete occurrence record {key} ignored")
            continue
        data = eventdata_from_google(instance, ctx)
        if not rec.is_trivial_override(master, data, key, ctx.default_tz):
            overrides[key] = data
    for key in exdates:
        overrides.pop(key, None)
    if master.alarms is None:
        result.notes.append("uses Google default reminders for all-day events (not exported)")
    result.event = CanonicalEvent(
        master=master,
        recurrence=Recurrence(tuple(parsed.rrules), tuple(parsed.rdates), tuple(exdates)),
        overrides=tuple(overrides.items()),
    )
    return result


# ---------------------------------------------------------------- projection


def _is_http(url: str) -> bool:
    return url.lower().startswith(("http://", "https://"))


def google_alarm_subset(alarms: tuple[Alarm, ...]) -> tuple[Alarm, ...]:
    valid = [a for a in alarms if 0 <= a.minutes_before <= MAX_REMINDER_MINUTES]
    return tuple(valid[:MAX_REMINDER_OVERRIDES])


def _pin(t: EventTime, default_tz: str) -> EventTime:
    if t.is_floating:
        assert t.local is not None
        return EventTime.zoned(t.local, default_tz)
    return t


def _project_data(data: EventData, ctx: GoogleContext) -> EventData:
    return dataclasses.replace(
        data,
        start=_pin(data.start, ctx.default_tz),
        end=_pin(data.end, ctx.default_tz),
        alarms=None if data.alarms is None else google_alarm_subset(data.alarms),
        url=data.url if _is_http(data.url) else "",
    )


def project_for_google(event: CanonicalEvent, ctx: GoogleContext) -> CanonicalEvent:
    """What ``event`` looks like after being written to Google and read back."""
    return CanonicalEvent(
        master=_project_data(event.master, ctx),
        recurrence=event.recurrence,
        overrides=tuple((k, _project_data(v, ctx)) for k, v in event.overrides),
    )


def write_notes(event: CanonicalEvent, ctx: GoogleContext) -> list[str]:
    """Human-readable list of what writing ``event`` to Google cannot carry over."""
    notes: list[str] = []
    for label, data in [("event", event.master), *[(f"occurrence {k}", v) for k, v in event.overrides]]:
        if data.start.is_floating:
            notes.append(f"{label}: floating time pinned to {ctx.default_tz}")
        if data.alarms is not None and google_alarm_subset(data.alarms) != data.alarms:
            notes.append(f"{label}: alarms beyond Google's limits (5, max 4 weeks) not copied")
        if data.url and not _is_http(data.url):
            notes.append(f"{label}: non-web URL not copied")
    return notes


# ---------------------------------------------------------------- writing


def time_body(t: EventTime, ctx: GoogleContext, *, patch: bool = False) -> dict[str, Any]:
    t = _pin(t, ctx.default_tz)
    if t.date is not None:
        body: dict[str, Any] = {"date": t.date.isoformat()}
        if patch:
            body.update(dateTime=None, timeZone=None)
        return body
    assert t.local is not None
    if t.tzid == "UTC":
        body = {"dateTime": t.local.isoformat(timespec="seconds") + "Z", "timeZone": "UTC"}
    else:
        body = {"dateTime": t.local.isoformat(timespec="seconds"), "timeZone": t.tzid}
    if patch:
        body["date"] = None
    return body


def reminders_body(alarms: tuple[Alarm, ...] | None) -> dict[str, Any]:
    if alarms is None:
        return {"useDefault": True, "overrides": []}
    return {
        "useDefault": False,
        "overrides": [
            {"method": "email" if a.method == "email" else "popup", "minutes": a.minutes_before}
            for a in google_alarm_subset(alarms)
        ],
    }


def _date_list_line(name: str, keys: list[str], like: EventTime, default_tz: str) -> str:
    if like.is_all_day:
        values = [k if rec.is_date_key(k) else k[:8] for k in keys]
        return f"{name};VALUE=DATE:{','.join(values)}"
    if like.tzid == "UTC":
        return f"{name}:{','.join(keys)}"
    tzid = like.tzid or default_tz
    values = [timeutil.utc_to_local(rec.key_instant(k, default_tz), tzid).strftime("%Y%m%dT%H%M%S") for k in keys]
    return f"{name};TZID={tzid}:{','.join(values)}"


def recurrence_lines(event: CanonicalEvent, ctx: GoogleContext, exdate_keys: set[str] | None = None) -> list[str]:
    """RRULE/RDATE/EXDATE lines. ``exdate_keys`` restricts which EXDATEs are written as lines
    (the rest are represented by cancelled instances); default is all of them."""
    lines = [f"RRULE:{r}" for r in event.recurrence.rrules]
    start = event.master.start
    if event.recurrence.rdates:
        lines.append(_date_list_line("RDATE", list(event.recurrence.rdates), start, ctx.default_tz))
    exdates = sorted(k for k in event.recurrence.exdates if exdate_keys is None or k in exdate_keys)
    if exdates:
        lines.append(_date_list_line("EXDATE", exdates, start, ctx.default_tz))
    return lines


def _data_fields(
    data: EventData, fields: set[str], ctx: GoogleContext, footer: FooterFn, *, patch: bool
) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if "summary" in fields:
        body["summary"] = data.summary
    if fields & {"description", "people", "conference"}:
        body["description"] = compose_description(data.description, footer(data))
    if "location" in fields:
        body["location"] = data.location
    if "status" in fields and data.status in ("confirmed", "tentative"):
        body["status"] = data.status
    if "transparency" in fields:
        body["transparency"] = data.transparency
    if "visibility" in fields:
        body["visibility"] = data.visibility
    if "alarms" in fields:
        body["reminders"] = reminders_body(data.alarms)
    if "url" in fields:
        body["source"] = {"title": "Link", "url": data.url} if _is_http(data.url) else None
    if "timing" in fields:
        body["start"] = time_body(data.start, ctx, patch=patch)
        body["end"] = time_body(data.end, ctx, patch=patch)
    return body


ALL_DATA_FIELDS = frozenset(
    {"summary", "description", "location", "status", "transparency", "visibility", "alarms", "url", "timing"}
)


def insert_body(
    event: CanonicalEvent,
    ctx: GoogleContext,
    *,
    event_id: str,
    footer: FooterFn,
    markers: dict[str, str],
) -> dict[str, Any]:
    body = _data_fields(event.master, set(ALL_DATA_FIELDS), ctx, footer, patch=False)
    if body.get("source") is None:
        body.pop("source", None)
    for key in ("summary", "description", "location"):
        if not body.get(key):
            body.pop(key, None)
    if event.master.visibility == "default":
        body.pop("visibility", None)
    body["id"] = event_id
    body["extendedProperties"] = {"private": dict(markers)}
    body["guestsCanInviteOthers"] = False
    if event.is_recurring:
        body["recurrence"] = recurrence_lines(event, ctx)
    return body


def master_patch_body(
    target: CanonicalEvent,
    groups: set[str],
    current_raw: dict[str, Any],
    ctx: GoogleContext,
    footer: FooterFn,
    exdate_lines: set[str] | None = None,
) -> dict[str, Any]:
    """PATCH body for the master event. ``exdate_lines`` are the exclusions to express as EXDATE
    lines; by default the ones Google already stores as lines and the target still wants. New
    exclusions are applied as instance cancellations, which is how Google itself records them."""
    fields = (groups & ALL_DATA_FIELDS) | (groups & {"people", "conference"})
    body = _data_fields(target.master, fields, ctx, footer, patch=True)
    if "description" in body and body["description"] == (current_raw.get("description") or ""):
        del body["description"]
    if groups & {"timing", "exdates"}:
        existing = exdate_line_keys(current_raw, ctx) if current_raw.get("recurrence") else set()
        lines = existing & set(target.recurrence.exdates) if exdate_lines is None else exdate_lines
        if target.is_recurring:
            if "timing" in groups or lines != existing:
                body["recurrence"] = recurrence_lines(target, ctx, lines)
        elif current_raw.get("recurrence"):
            body["recurrence"] = []
    return body


def instance_patch_body(
    data: EventData,
    current: EventData | None,
    ctx: GoogleContext,
    footer: FooterFn,
    *,
    restore: bool = False,
) -> dict[str, Any]:
    if current is None:
        fields = set(ALL_DATA_FIELDS)
    else:
        fields = {f for f in ALL_DATA_FIELDS if _field(data, f) != _field(current, f)}
    body = _data_fields(data, fields, ctx, footer, patch=True)
    if restore:
        body["status"] = data.status if data.status in ("confirmed", "tentative") else "confirmed"
    return body


def _field(data: EventData, field: str) -> Any:
    if field == "timing":
        return (data.start, data.end)
    return getattr(data, field)


def marker_dict(*, origin: str, uid: str, mapping_id: int) -> dict[str, str]:
    return {"calendarBridge": "1", "cbOrigin": origin, "cbUid": uid, "cbMapping": str(mapping_id)}
