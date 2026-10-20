"""Stateful in-memory fake of the Google Calendar API v3 subset the bridge uses.

Behaviours modelled on the real service: client-chosen ids (409 on reuse), ETags with
If-Match (412), PATCH merge semantics with null clearing, incremental sync tokens (410 once
invalidated), pagination, ``showDeleted`` cancelled records, recurring masters with exception
instances addressed as ``<masterId>_<originalStart>``, private extended property and iCalUID
lookups. Fault injection lets tests fail requests or "crash" right after a write is applied.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import itertools
import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import unquote, urlsplit

from dateutil import rrule as du_rrule

from calendar_bridge.model import timeutil

ID_RE = re.compile(r"^[a-v0-9]{5,1024}$")
INSTANCE_RE = re.compile(r"^(?P<master>.+)_(?P<key>\d{8}(T\d{6}Z)?)$")


class SimulatedCrash(BaseException):
    """Raised after a write was applied, to simulate the process dying before it hears back."""


@dataclasses.dataclass
class FakeResponse:
    status_code: int
    data: Any = None
    headers: dict[str, str] = dataclasses.field(default_factory=dict)
    reason: str = ""

    def json(self) -> Any:
        if self.data is None:
            raise ValueError("no body")
        return self.data


@dataclasses.dataclass
class Fault:
    predicate: Callable[[str, str], bool]
    status: int | None = 500
    reason: str = "backendError"
    crash_after_apply: bool = False
    crash_before_apply: bool = False
    network_error: bool = False
    remaining: int = 1


def _error(status: int, reason: str, message: str = "") -> FakeResponse:
    body = {"error": {"code": status, "message": message or reason, "errors": [{"reason": reason}]}}
    return FakeResponse(status, body, reason=reason)


def _merge(target: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif (
            isinstance(value, dict)
            and isinstance(target.get(key), dict)
            and key not in ("start", "end", "originalStartTime")
        ):
            _merge(target[key], value)
        elif isinstance(value, dict):
            target[key] = {k: v for k, v in value.items() if v is not None}
        else:
            target[key] = copy.deepcopy(value)


def _instant(obj: dict[str, Any]) -> dt.datetime | dt.date:
    if obj.get("date"):
        return dt.date.fromisoformat(obj["date"])
    value = dt.datetime.fromisoformat(obj["dateTime"].replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timeutil.zone(timeutil.canonical_tzid(obj.get("timeZone")) or "UTC"))
    return value


def key_of(obj: dict[str, Any]) -> str:
    value = _instant(obj)
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return value.strftime("%Y%m%d")


class FakeCalendar:
    def __init__(
        self,
        cal_id: str,
        summary: str,
        *,
        primary: bool = False,
        access: str = "owner",
        timezone: str = "Europe/Brussels",
        reminders: list[dict[str, Any]] | None = None,
        description: str = "",
    ) -> None:
        self.id = cal_id
        self.summary = summary
        self.description = description
        self.primary = primary
        self.access = access
        self.timezone = timezone
        self.default_reminders = reminders if reminders is not None else [{"method": "popup", "minutes": 10}]
        self.events: dict[str, dict[str, Any]] = {}
        self.changes: dict[str, int] = {}
        self.token_floor = 0

    def entry(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "summary": self.summary,
            "description": self.description,
            "accessRole": self.access,
            "primary": self.primary,
            "timeZone": self.timezone,
            "defaultReminders": copy.deepcopy(self.default_reminders),
        }


class FakeGoogle:
    def __init__(self) -> None:
        self.calendars: dict[str, FakeCalendar] = {}
        self.seq = itertools.count(1)
        self.etags = itertools.count(1)
        self.faults: list[Fault] = []
        self.requests: list[tuple[str, str]] = []
        self.clock = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)
        self.ids = itertools.count(1)
        # (predicate(method, path), action) run once just before a matching request is handled,
        # e.g. to simulate a user editing the calendar while the bridge is writing.
        self.before_hooks: list[tuple[Callable[[str, str], bool], Callable[[], None]]] = []
        # The API only guarantees id and status on deleted events; emulate the minimal form.
        self.bare_cancellations = False

    # ----------------------------------------------------------------- setup helpers

    def add_calendar(self, cal_id: str, summary: str, **kw: Any) -> FakeCalendar:
        cal = FakeCalendar(cal_id, summary, **kw)
        self.calendars[cal_id] = cal
        return cal

    def session(self) -> FakeSession:
        return FakeSession(self)

    def writes(self) -> list[tuple[str, str]]:
        return [r for r in self.requests if r[0] in ("POST", "PATCH", "PUT", "DELETE")]

    def fail(self, method: str | None = None, path_contains: str = "", **kw: Any) -> Fault:
        fault = Fault(lambda m, p: (method is None or m == method) and path_contains in p, **kw)
        self.faults.append(fault)
        return fault

    def expire_tokens(self, cal_id: str) -> None:
        cal = self.calendars[cal_id]
        cal.token_floor = max(cal.changes.values(), default=0) + 1

    # ----------------------------------------------------------------- "user" operations (not via the bridge)

    def user_insert(self, cal_id: str, body: dict[str, Any]) -> dict[str, Any]:
        body = copy.deepcopy(body)
        body.setdefault("id", f"user{next(self.ids):06d}")
        response = self._insert(self.calendars[cal_id], body)
        assert response.status_code == 200, response.data
        return response.data

    def user_patch(self, cal_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self._patch(self.calendars[cal_id], event_id, body, None)
        assert response.status_code == 200, response.data
        return response.data

    def user_delete(self, cal_id: str, event_id: str) -> None:
        response = self._delete(self.calendars[cal_id], event_id, None)
        assert response.status_code in (204, 410), response.data

    def event(self, cal_id: str, event_id: str) -> dict[str, Any] | None:
        return copy.deepcopy(self.calendars[cal_id].events.get(event_id))

    def live_events(self, cal_id: str) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(e)
            for e in self.calendars[cal_id].events.values()
            if e.get("status") != "cancelled" and not e.get("recurringEventId")
        ]

    def instances_of(self, cal_id: str, master_id: str) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(e) for e in self.calendars[cal_id].events.values() if e.get("recurringEventId") == master_id
        ]

    # ----------------------------------------------------------------- request handling

    def handle(self, method: str, url: str, params: dict[str, Any], body: Any, headers: dict[str, str]) -> FakeResponse:
        path = unquote(urlsplit(url).path).replace("/calendar/v3", "", 1)
        self.requests.append((method, path))
        for hook in list(self.before_hooks):
            if hook[0](method, path):
                self.before_hooks.remove(hook)
                hook[1]()
        for fault in list(self.faults):
            if fault.remaining > 0 and fault.predicate(method, path):
                fault.remaining -= 1
                if fault.remaining == 0:
                    self.faults.remove(fault)
                if fault.network_error:
                    import requests

                    raise requests.exceptions.ConnectionError("simulated network failure")
                if fault.crash_after_apply:
                    self._route(method, path, params, body, headers)
                    raise SimulatedCrash(f"{method} {path}")
                if fault.crash_before_apply:
                    raise SimulatedCrash(f"before {method} {path}")
                return _error(fault.status or 500, fault.reason)
        return self._route(method, path, params, body, headers)

    def _route(
        self, method: str, path: str, params: dict[str, Any], body: Any, headers: dict[str, str]
    ) -> FakeResponse:
        if path == "/users/me/calendarList" and method == "GET":
            return FakeResponse(200, {"items": [c.entry() for c in self.calendars.values()]})
        if path == "/calendars" and method == "POST":
            cal_id = f"cb{next(self.ids):04d}@group.calendar.google.com"
            cal = self.add_calendar(
                cal_id,
                body["summary"],
                timezone=body.get("timeZone", "UTC"),
                reminders=[],
                description=body.get("description", ""),
            )
            return FakeResponse(200, {"id": cal.id, "summary": cal.summary, "timeZone": cal.timezone})
        deleted = re.match(r"^/calendars/([^/]+)$", path)
        if deleted and method == "DELETE":
            if self.calendars.pop(deleted.group(1), None) is None:
                return _error(404, "notFound", "calendar")
            return FakeResponse(204, None)
        match = re.match(r"^/calendars/([^/]+)/events(?:/([^/]+))?(/instances)?$", path)
        if not match:
            return _error(404, "notFound", path)
        cal = self.calendars.get(match.group(1))
        if cal is None:
            return _error(404, "notFound", "calendar")
        event_id = match.group(2)
        if event_id is None:
            if method == "GET":
                return self._list(cal, params)
            if method == "POST":
                return self._insert(cal, copy.deepcopy(body))
        elif method == "GET":
            return self._get(cal, event_id)
        elif method == "PATCH":
            return self._patch(cal, event_id, body, headers.get("If-Match"))
        elif method == "DELETE":
            return self._delete(cal, event_id, headers.get("If-Match"))
        return _error(400, "badRequest", f"{method} {path}")

    # ----------------------------------------------------------------- storage primitives

    def _touch(self, cal: FakeCalendar, event: dict[str, Any]) -> None:
        self.clock += dt.timedelta(seconds=1)
        event["etag"] = f'"{next(self.etags)}"'
        event["updated"] = self.clock.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        cal.events[event["id"]] = event
        cal.changes[event["id"]] = next(self.seq)

    def _owner(self, cal: FakeCalendar) -> dict[str, Any]:
        return {"email": cal.id, "self": True}

    def _insert(self, cal: FakeCalendar, body: dict[str, Any]) -> FakeResponse:
        event_id = body.get("id") or f"gen{next(self.ids):08d}"
        if not ID_RE.match(event_id):
            return _error(400, "invalid", "invalid id")
        if event_id in cal.events:
            return _error(409, "duplicate", "The requested identifier already exists.")
        if "start" not in body or "end" not in body:
            return _error(400, "required", "start/end")
        if body.get("recurrence") and body["start"].get("dateTime") and not body["start"].get("timeZone"):
            return _error(400, "invalid", "timeZone required for recurring events")
        event = {k: v for k, v in body.items() if v is not None}
        event["id"] = event_id
        event["kind"] = "calendar#event"
        event.setdefault("status", "confirmed")
        event.setdefault("iCalUID", f"{event_id}@google.com")
        event.setdefault("reminders", {"useDefault": True})
        event.setdefault("eventType", "default")
        event["organizer"] = self._owner(cal)
        event["creator"] = self._owner(cal)
        event["created"] = self.clock.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        event["sequence"] = 0
        self._normalize(event)
        self._touch(cal, event)
        return FakeResponse(200, copy.deepcopy(event))

    def _normalize(self, event: dict[str, Any]) -> None:
        for field in ("start", "end"):
            obj = event.get(field) or {}
            if obj.get("dateTime"):
                value = dt.datetime.fromisoformat(obj["dateTime"].replace("Z", "+00:00"))
                tz = obj.get("timeZone")
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timeutil.zone(timeutil.canonical_tzid(tz) or "UTC"))
                zone = timeutil.zone(timeutil.canonical_tzid(tz) or "UTC") if tz else value.tzinfo
                event[field] = {"dateTime": value.astimezone(zone).isoformat()}
                if tz:
                    event[field]["timeZone"] = tz
        for empty in ("description", "location", "summary"):
            if event.get(empty) == "":
                event.pop(empty)
        reminders = event.get("reminders")
        if reminders and reminders.get("useDefault"):
            event["reminders"] = {"useDefault": True}
        elif reminders is not None and not reminders.get("overrides"):
            event["reminders"] = {"useDefault": False}
        if event.get("visibility") == "default":
            event.pop("visibility")
        if event.get("transparency") == "opaque":
            event.pop("transparency")

    def _occurrence(self, cal: FakeCalendar, master: dict[str, Any], key: str) -> dict[str, Any] | None:
        """Unmodified occurrence of ``master`` with recurrence key ``key``, or None."""
        start = _instant(master["start"])
        end = _instant(master["end"])
        rules = [line[6:] for line in master.get("recurrence", []) if line.startswith("RRULE:")]
        if not rules:
            return None
        if isinstance(start, dt.datetime):
            tz = master["start"].get("timeZone") or "UTC"
            zone = timeutil.zone(timeutil.canonical_tzid(tz) or "UTC")
            local_start = start.astimezone(zone)
            for rule in rules:
                parsed = du_rrule.rrulestr(rule, dtstart=local_start)
                target = dt.datetime.strptime(key, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC)
                for occ in itertools.islice(parsed, 3000):
                    if occ.astimezone(dt.UTC) == target:
                        duration = end - start  # type: ignore[operator]
                        return {
                            "start": {"dateTime": occ.isoformat(), "timeZone": tz},
                            "end": {"dateTime": (occ + duration).astimezone(zone).isoformat(), "timeZone": tz},
                            "originalStartTime": {"dateTime": occ.isoformat(), "timeZone": tz},
                        }
                    if occ.astimezone(dt.UTC) > target:
                        break
            return None
        for rule in rules:
            parsed = du_rrule.rrulestr(rule.replace("Z", ""), dtstart=dt.datetime.combine(start, dt.time()))
            target_day = dt.datetime.strptime(key[:8], "%Y%m%d")
            for occ in itertools.islice(parsed, 3000):
                if occ == target_day:
                    days = end - start  # type: ignore[operator]
                    return {
                        "start": {"date": occ.date().isoformat()},
                        "end": {"date": (occ.date() + days).isoformat()},
                        "originalStartTime": {"date": occ.date().isoformat()},
                    }
                if occ > target_day:
                    break
        return None

    def _materialize(self, cal: FakeCalendar, event_id: str) -> dict[str, Any] | None:
        if event_id in cal.events:
            return cal.events[event_id]
        match = INSTANCE_RE.match(event_id)
        if not match or match.group("master") not in cal.events:
            return None
        master = cal.events[match.group("master")]
        if master.get("status") == "cancelled":
            return None
        occurrence = self._occurrence(cal, master, match.group("key"))
        if occurrence is None:
            return None
        instance = {
            k: copy.deepcopy(v)
            for k, v in master.items()
            if k not in ("id", "recurrence", "etag", "updated", "start", "end")
        }
        instance.update(occurrence)
        instance["id"] = event_id
        instance["recurringEventId"] = master["id"]
        return instance

    def _get(self, cal: FakeCalendar, event_id: str) -> FakeResponse:
        event = cal.events.get(event_id)
        if event is None:
            materialized = self._materialize(cal, event_id)
            if materialized is None:
                return _error(404, "notFound")
            return FakeResponse(200, copy.deepcopy(materialized))
        return FakeResponse(200, copy.deepcopy(event))

    def _patch(self, cal: FakeCalendar, event_id: str, body: dict[str, Any], if_match: str | None) -> FakeResponse:
        existing = cal.events.get(event_id)
        if existing is None:
            existing = self._materialize(cal, event_id)
            if existing is None:
                return _error(404, "notFound")
        elif if_match and if_match != existing.get("etag"):
            return _error(412, "conditionNotMet", "Precondition Failed")
        event = copy.deepcopy(existing)
        if (
            event.get("status") == "cancelled"
            and body.get("status") not in ("confirmed", "tentative")
            and not event.get("recurringEventId")
        ):
            return _error(410, "deleted", "Resource has been deleted")
        if event.get("status") == "cancelled" and event.get("recurringEventId") and "start" not in event:
            cal.events.pop(event_id)
            restored = self._materialize(cal, event_id)
            cal.events[event_id] = existing
            if restored is None:
                return _error(404, "notFound")
            event = restored
        _merge(event, body)
        if event.get("recurringEventId") and "recurrence" in event:
            event.pop("recurrence")
        self._normalize(event)
        event["sequence"] = event.get("sequence", 0) + 1
        self._touch(cal, event)
        return FakeResponse(200, copy.deepcopy(event))

    def _delete(self, cal: FakeCalendar, event_id: str, if_match: str | None) -> FakeResponse:
        existing = cal.events.get(event_id) or self._materialize(cal, event_id)
        if existing is None:
            return _error(404, "notFound")
        if existing.get("status") == "cancelled":
            return _error(410, "deleted", "Resource has been deleted")
        if if_match and event_id in cal.events and if_match != existing.get("etag"):
            return _error(412, "conditionNotMet", "Precondition Failed")
        if existing.get("recurringEventId"):
            cancelled = {
                "id": event_id,
                "status": "cancelled",
                "recurringEventId": existing["recurringEventId"],
                "originalStartTime": existing["originalStartTime"],
                "iCalUID": existing.get("iCalUID"),
            }
        else:
            cancelled = {"id": event_id, "status": "cancelled", "iCalUID": existing.get("iCalUID")}
            for other in list(cal.events.values()):
                if other.get("recurringEventId") == event_id and other.get("status") != "cancelled":
                    self._touch(
                        cal,
                        {
                            **{k: other[k] for k in ("id", "recurringEventId", "originalStartTime")},
                            "status": "cancelled",
                            "iCalUID": other.get("iCalUID"),
                        },
                    )
        cancelled["_deleted_snapshot"] = json.dumps(existing)
        self._touch(cal, cancelled)
        return FakeResponse(204)

    def _visible(self, event: dict[str, Any]) -> dict[str, Any]:
        out = copy.deepcopy(event)
        out.pop("_deleted_snapshot", None)
        if self.bare_cancellations and out.get("status") == "cancelled":
            return {"id": out["id"], "status": "cancelled", "etag": out.get("etag")}
        return out

    def _list(self, cal: FakeCalendar, params: dict[str, Any]) -> FakeResponse:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        show_deleted = str(params.get("showDeleted", "false")).lower() == "true"
        sync_token = params.get("syncToken")
        if sync_token:
            if not sync_token.startswith(f"tok:{cal.id}:"):
                return _error(410, "fullSyncRequired")
            since = int(sync_token.rsplit(":", 1)[1])
            if since < cal.token_floor:
                return _error(410, "fullSyncRequired", "Sync token is no longer valid, a full sync is required.")
            ids = [i for i, seq in cal.changes.items() if seq > since]
            show_deleted = True
        else:
            ids = list(cal.events)
        events = [cal.events[i] for i in sorted(ids, key=lambda i: cal.changes[i])]
        if not show_deleted:
            events = [e for e in events if e.get("status") != "cancelled"]
        if params.get("iCalUID"):
            events = [e for e in events if e.get("iCalUID") == params["iCalUID"]]
        if params.get("privateExtendedProperty"):
            key, value = params["privateExtendedProperty"].split("=", 1)
            events = [e for e in events if ((e.get("extendedProperties") or {}).get("private") or {}).get(key) == value]
        page_size = int(params.get("maxResults", 250))
        offset = int(params.get("pageToken", 0) or 0)
        page = events[offset : offset + page_size]
        body: dict[str, Any] = {"items": [self._visible(e) for e in page]}
        if offset + page_size < len(events):
            body["nextPageToken"] = str(offset + page_size)
        else:
            body["nextSyncToken"] = f"tok:{cal.id}:{max(cal.changes.values(), default=0)}"
        return FakeResponse(200, body)


class FakeSession:
    def __init__(self, google: FakeGoogle) -> None:
        self.google = google

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        return self.google.handle(
            method,
            url,
            kwargs.get("params") or {},
            copy.deepcopy(kwargs.get("json")),
            dict(kwargs.get("headers") or {}),
        )
