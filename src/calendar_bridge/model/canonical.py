"""Provider-neutral event model.

A ``CanonicalEvent`` is one synchronization unit: a single event, or a recurring series made of a
master plus its per-occurrence overrides. Both providers are converted *into* this model to be
compared, and changes are converted *out of* it field group by field group, so provider-specific
data that the model does not cover is never rewritten.

Field groups are the unit of change detection and conflict resolution:

* simple groups map to one master field (``summary``, ``alarms``...);
* ``timing`` is start/end/RRULE/RDATE together, because changing any of them shifts occurrences;
* ``exdates`` holds cancelled occurrences (EXDATE and Google's cancelled instances alike);
* ``override:<key>`` is one modified occurrence, keyed by its *original* start;
* ``people`` and ``conference`` are informational: they only ever flow from the provider that
  owns the event to the mirror, rendered as text, never as real invitations.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from calendar_bridge.model import timeutil

STATUSES = ("confirmed", "tentative", "cancelled")
TRANSPARENCIES = ("opaque", "transparent")
VISIBILITIES = ("default", "public", "private", "confidential")
ALARM_METHODS = ("display", "email")

SIMPLE_GROUPS = (
    "summary",
    "description",
    "location",
    "status",
    "transparency",
    "visibility",
    "alarms",
    "url",
)
INFO_GROUPS = ("people", "conference")
SCHEDULE_GROUPS = ("timing", "exdates")
OVERRIDE_PREFIX = "override:"
# Fields an attendee may change on their own copy without affecting other guests.
PERSONAL_GROUPS = frozenset({"alarms", "transparency", "visibility"})


@dataclasses.dataclass(frozen=True, slots=True)
class EventTime:
    """A calendar time: an all-day date, or a wall-clock time in a zone ("UTC" included),
    or a floating wall-clock time (``tzid is None``)."""

    date: dt.date | None = None
    local: dt.datetime | None = None
    tzid: str | None = None

    def __post_init__(self) -> None:
        if (self.date is None) == (self.local is None):
            raise ValueError("EventTime needs exactly one of date or local")
        if self.date is not None and self.tzid is not None:
            raise ValueError("all-day EventTime cannot carry a time zone")
        if self.local is not None:
            if self.local.tzinfo is not None:
                raise ValueError("EventTime.local must be naive wall-clock time")
            if self.local.microsecond:
                object.__setattr__(self, "local", self.local.replace(microsecond=0))

    @classmethod
    def all_day(cls, value: dt.date) -> EventTime:
        if isinstance(value, dt.datetime):
            value = value.date()
        return cls(date=value)

    @classmethod
    def zoned(cls, local: dt.datetime, tzid: str) -> EventTime:
        return cls(local=local, tzid=tzid)

    @classmethod
    def floating(cls, local: dt.datetime) -> EventTime:
        return cls(local=local)

    @classmethod
    def from_instant(cls, instant: dt.datetime, tzid: str = "UTC") -> EventTime:
        return cls(local=timeutil.utc_to_local(instant, tzid), tzid=tzid)

    @property
    def is_all_day(self) -> bool:
        return self.date is not None

    @property
    def is_floating(self) -> bool:
        return self.local is not None and self.tzid is None

    def instant(self, default_tz: str) -> dt.datetime:
        """The UTC instant; all-day dates and floating times are read in ``default_tz``."""
        if self.date is not None:
            return timeutil.local_to_utc(dt.datetime.combine(self.date, dt.time()), default_tz)
        assert self.local is not None
        return timeutil.local_to_utc(self.local, self.tzid or default_tz)

    def key(self, default_tz: str) -> str:
        """Recurrence key: ``YYYYMMDD`` for dates, the UTC instant ``YYYYMMDDTHHMMSSZ`` otherwise."""
        if self.date is not None:
            return self.date.strftime("%Y%m%d")
        return self.instant(default_tz).strftime("%Y%m%dT%H%M%SZ")

    def to_json(self) -> dict[str, Any]:
        if self.date is not None:
            return {"date": self.date.isoformat()}
        assert self.local is not None
        return {"local": self.local.isoformat(timespec="seconds"), "tz": self.tzid}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> EventTime:
        if "date" in data:
            return cls(date=dt.date.fromisoformat(data["date"]))
        return cls(local=dt.datetime.fromisoformat(data["local"]), tzid=data.get("tz"))

    def describe(self) -> str:
        if self.date is not None:
            return self.date.isoformat()
        assert self.local is not None
        text = self.local.isoformat(sep=" ", timespec="minutes")
        return f"{text} ({self.tzid})" if self.tzid else f"{text} (floating)"


@dataclasses.dataclass(frozen=True, slots=True)
class Alarm:
    method: str  # "display" | "email"
    minutes_before: int

    def to_json(self) -> list[Any]:
        return [self.method, self.minutes_before]


@dataclasses.dataclass(frozen=True, slots=True)
class Person:
    email: str
    name: str = ""
    status: str = ""  # accepted | declined | tentative | needs-action
    role: str = ""  # required | optional | chair | non-participant
    is_self: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "name": self.name,
            "status": self.status,
            "role": self.role,
            "self": self.is_self,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Person:
        return cls(
            email=data["email"],
            name=data.get("name", ""),
            status=data.get("status", ""),
            role=data.get("role", ""),
            is_self=bool(data.get("self", False)),
        )

    def label(self) -> str:
        return f"{self.name} <{self.email}>" if self.name else self.email


@dataclasses.dataclass(frozen=True, slots=True)
class Conference:
    uri: str
    label: str = ""

    def to_json(self) -> list[str]:
        return [self.uri, self.label]


@dataclasses.dataclass(frozen=True, slots=True)
class EventData:
    start: EventTime
    end: EventTime
    summary: str = ""
    description: str = ""
    location: str = ""
    status: str = "confirmed"
    transparency: str = "opaque"
    visibility: str = "default"
    # None means "the provider's default reminders" whose offsets are not knowable
    # (Google all-day events using calendar defaults). An empty tuple means "no alarms".
    alarms: tuple[Alarm, ...] | None = ()
    url: str = ""
    conference: tuple[Conference, ...] = ()
    organizer: Person | None = None
    attendees: tuple[Person, ...] = ()

    def __post_init__(self) -> None:
        if self.alarms is not None:
            ordered = tuple(sorted(set(self.alarms), key=lambda a: (a.minutes_before, a.method)))
            object.__setattr__(self, "alarms", ordered)
        object.__setattr__(self, "attendees", tuple(sorted(set(self.attendees), key=lambda p: p.email)))

    @property
    def is_meeting(self) -> bool:
        return any(not p.is_self for p in self.attendees)

    def duration(self, default_tz: str) -> dt.timedelta:
        return self.end.instant(default_tz) - self.start.instant(default_tz)

    def people_value(self) -> dict[str, Any]:
        if not self.is_meeting:
            return {"organizer": None, "attendees": []}
        return {
            "organizer": self.organizer.to_json() if self.organizer else None,
            "attendees": [p.to_json() for p in self.attendees],
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "start": self.start.to_json(),
            "end": self.end.to_json(),
            "summary": self.summary,
            "description": self.description,
            "location": self.location,
            "status": self.status,
            "transparency": self.transparency,
            "visibility": self.visibility,
            "alarms": alarms_json(self.alarms),
            "url": self.url,
            "conference": [c.to_json() for c in self.conference],
            "organizer": self.organizer.to_json() if self.organizer else None,
            "attendees": [p.to_json() for p in self.attendees],
        }

    def content_json(self) -> dict[str, Any]:
        """Comparable content of an occurrence override: guest lists and call links are
        informational and never stored on mirrors, so they are not part of the comparison."""
        data = self.to_json()
        for key in ("organizer", "attendees", "conference"):
            data.pop(key)
        return data

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> EventData:
        alarms = data.get("alarms")
        return cls(
            start=EventTime.from_json(data["start"]),
            end=EventTime.from_json(data["end"]),
            summary=data.get("summary", ""),
            description=data.get("description", ""),
            location=data.get("location", ""),
            status=data.get("status", "confirmed"),
            transparency=data.get("transparency", "opaque"),
            visibility=data.get("visibility", "default"),
            alarms=None if alarms is None else tuple(Alarm(m, int(n)) for m, n in alarms),
            url=data.get("url", ""),
            conference=tuple(Conference(u, label) for u, label in data.get("conference", [])),
            organizer=Person.from_json(data["organizer"]) if data.get("organizer") else None,
            attendees=tuple(Person.from_json(p) for p in data.get("attendees", [])),
        )


def alarms_json(alarms: tuple[Alarm, ...] | None) -> list[Any] | None:
    if alarms is None:
        return None
    return [a.to_json() for a in alarms]


@dataclasses.dataclass(frozen=True, slots=True)
class Recurrence:
    rrules: tuple[str, ...] = ()
    rdates: tuple[str, ...] = ()
    exdates: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "rdates", tuple(sorted(set(self.rdates))))
        object.__setattr__(self, "exdates", tuple(sorted(set(self.exdates))))

    @property
    def is_recurring(self) -> bool:
        return bool(self.rrules or self.rdates)

    def to_json(self) -> dict[str, Any]:
        return {"rrules": list(self.rrules), "rdates": list(self.rdates), "exdates": list(self.exdates)}

    @classmethod
    def from_json(cls, data: Mapping[str, Any] | None) -> Recurrence:
        if not data:
            return cls()
        return cls(
            rrules=tuple(data.get("rrules", ())),
            rdates=tuple(data.get("rdates", ())),
            exdates=tuple(data.get("exdates", ())),
        )


@dataclasses.dataclass(frozen=True, slots=True)
class CanonicalEvent:
    master: EventData
    recurrence: Recurrence = Recurrence()
    overrides: tuple[tuple[str, EventData], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "overrides", tuple(sorted(dict(self.overrides).items())))

    @property
    def is_recurring(self) -> bool:
        return self.recurrence.is_recurring

    @property
    def is_meeting(self) -> bool:
        return self.master.is_meeting

    def override_map(self) -> dict[str, EventData]:
        return dict(self.overrides)

    def to_json(self) -> dict[str, Any]:
        return {
            "v": 1,
            "master": self.master.to_json(),
            "recurrence": self.recurrence.to_json(),
            "overrides": [[key, data.to_json()] for key, data in self.overrides],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> CanonicalEvent:
        return cls(
            master=EventData.from_json(data["master"]),
            recurrence=Recurrence.from_json(data.get("recurrence")),
            overrides=tuple((k, EventData.from_json(v)) for k, v in data.get("overrides", [])),
        )

    def dumps(self) -> str:
        return json.dumps(self.to_json(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def loads(cls, text: str) -> CanonicalEvent:
        return cls.from_json(json.loads(text))

    def fingerprint(self) -> str:
        return fingerprint_values(group_values(self))

    def describe(self) -> str:
        """Short human description for CLI output (never logged)."""
        recurring = " (recurring)" if self.is_recurring else ""
        return f"{self.master.summary or '(no title)'} @ {self.master.start.describe()}{recurring}"


def fingerprint_values(values: Mapping[str, Any]) -> str:
    payload = json.dumps(values, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def group_values(event: CanonicalEvent) -> dict[str, Any]:
    m = event.master
    values: dict[str, Any] = {
        "summary": m.summary,
        "description": m.description,
        "location": m.location,
        "status": m.status,
        "transparency": m.transparency,
        "visibility": m.visibility,
        "alarms": alarms_json(m.alarms),
        "url": m.url,
        "timing": {
            "start": m.start.to_json(),
            "end": m.end.to_json(),
            "rrules": list(event.recurrence.rrules),
            "rdates": list(event.recurrence.rdates),
        },
        "exdates": list(event.recurrence.exdates),
        "people": m.people_value(),
        "conference": [c.to_json() for c in m.conference],
    }
    for key, data in event.overrides:
        values[OVERRIDE_PREFIX + key] = data.content_json()
    return values


def all_groups(*events: CanonicalEvent | None) -> set[str]:
    groups: set[str] = set(SIMPLE_GROUPS) | set(INFO_GROUPS) | set(SCHEDULE_GROUPS)
    for event in events:
        if event is not None:
            groups.update(OVERRIDE_PREFIX + key for key, _ in event.overrides)
    return groups


def changed_groups(old: CanonicalEvent | None, new: CanonicalEvent) -> set[str]:
    """Groups whose value differs between ``old`` and ``new`` (all groups if ``old`` is None)."""
    new_values = group_values(new)
    if old is None:
        return set(new_values)
    old_values = group_values(old)
    keys = set(old_values) | set(new_values)
    return {k for k in keys if old_values.get(k) != new_values.get(k)}


def is_override_group(group: str) -> bool:
    return group.startswith(OVERRIDE_PREFIX)


def override_key(group: str) -> str:
    return group[len(OVERRIDE_PREFIX) :]


def with_groups(base: CanonicalEvent, source: CanonicalEvent, groups: Iterable[str]) -> CanonicalEvent:
    """Return ``base`` with the given groups' values taken from ``source``."""
    groups = set(groups)
    master = base.master
    simple: dict[str, Any] = {}
    for group in SIMPLE_GROUPS:
        if group in groups:
            simple[group] = getattr(source.master, group)
    if "timing" in groups:
        simple["start"] = source.master.start
        simple["end"] = source.master.end
    if "people" in groups:
        simple["organizer"] = source.master.organizer
        simple["attendees"] = source.master.attendees
    if "conference" in groups:
        simple["conference"] = source.master.conference
    if simple:
        master = dataclasses.replace(master, **simple)

    recurrence = base.recurrence
    if "timing" in groups:
        recurrence = dataclasses.replace(recurrence, rrules=source.recurrence.rrules, rdates=source.recurrence.rdates)
    if "exdates" in groups:
        recurrence = dataclasses.replace(recurrence, exdates=source.recurrence.exdates)

    overrides = base.override_map()
    source_overrides = source.override_map()
    for group in groups:
        if is_override_group(group):
            key = override_key(group)
            if key in source_overrides:
                overrides[key] = source_overrides[key]
            else:
                overrides.pop(key, None)
    return CanonicalEvent(master=master, recurrence=recurrence, overrides=tuple(overrides.items()))


def with_exdates(event: CanonicalEvent, exdates: Iterable[str]) -> CanonicalEvent:
    return dataclasses.replace(event, recurrence=dataclasses.replace(event.recurrence, exdates=tuple(exdates)))


@dataclasses.dataclass
class ConversionResult:
    """Outcome of reading one provider representation into the canonical model.

    ``unsupported`` lists reasons the event cannot be mirrored faithfully at all; ``notes`` lists
    aspects that are represented with a documented, acceptable loss (e.g. an alarm type the
    other provider lacks). Neither participates in equality or fingerprints.
    """

    event: CanonicalEvent | None
    uid: str | None = None
    unsupported: list[str] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)
    markers: dict[str, str] = dataclasses.field(default_factory=dict)

    @property
    def supported(self) -> bool:
        return self.event is not None and not self.unsupported
