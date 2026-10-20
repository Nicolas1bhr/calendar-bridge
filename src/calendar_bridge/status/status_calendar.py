"""Bridge status published as an all-day event in a dedicated Google calendar.

This lets ChatGPT's existing Google Calendar connector answer "is my calendar bridge working?"
and, crucially, tell a Google-only event apart from one that has been *verified* in iCloud:
the event description lists what was confirmed in iCloud and what is still waiting.

The status events never block availability (transparent), have no reminders, are private, and
live in a calendar the bridge created itself, which is never part of any mapping.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

from calendar_bridge.clock import from_iso, is_due
from calendar_bridge.errors import AlreadyExists, NotFound
from calendar_bridge.logs import get_logger
from calendar_bridge.model import timeutil
from calendar_bridge.providers.google.client import GoogleCalendarClient
from calendar_bridge.sync.context import EngineContext

log = get_logger("status_calendar")
KEEP_DAYS = 7
STATUS_DESCRIPTION = "Health of the Calendar Bridge (iCloud <-> Google sync). Managed automatically."
HEADLINE = {
    "ok": "OK",
    "degraded": "ATTENTION",
    "error": "PROBLEM",
    "not_configured": "NOT CONFIGURED",
}


def _local(ts: str | None, tz: str) -> str:
    if not ts:
        return "never"
    return timeutil.utc_to_local(from_iso(ts), tz).strftime("%a %d %b %H:%M")


def render_description(status: dict[str, Any], tz: str) -> str:
    lines = [
        f"Overall: {HEADLINE.get(status['overall'], status['overall'])}",
        f"Status generated: {_local(status['generated_at'], tz)} ({tz})",
        f"Last fully successful sync: {_local(status['daemon']['last_ok_cycle_at'], tz)}",
        "",
        "HOW TO READ THIS: an event created or changed in Google is copied to iCloud within about "
        f"{max(1, status['daemon']['sync_interval_seconds'] // 60)} minute(s). It is only confirmed "
        "in iCloud once it is listed under 'Confirmed in iCloud'. Anything under 'Waiting' or not "
        "listed yet is NOT yet in iCloud.",
        "",
        f"Google authorization: {status['google']['state']}",
        f"iCloud authorization: {status['icloud']['state']}",
    ]
    if status.get("global_hold"):
        lines.append(f"DESTRUCTIVE WRITES SUSPENDED: {status['global_hold']}")
    for mapping in status["mappings"]:
        items = mapping["items"]
        lines += [
            "",
            f"Calendar pair '{mapping['name']}' ({mapping['state']}): iCloud '{mapping['icloud_calendar']['name']}' "
            f"<-> Google '{mapping['google_calendar']['name']}'",
            f"  Last read: Google {_local(mapping['last_google_read_at'], tz)}, "
            f"iCloud {_local(mapping['last_icloud_read_at'], tz)}",
            f"  Last verified write: to iCloud {_local(mapping['last_icloud_write_verified_at'], tz)}, "
            f"to Google {_local(mapping['last_google_write_verified_at'], tz)}",
            f"  Events: {items.get('synced', 0)} in sync, {items.get('pending', 0)} waiting, "
            f"{items.get('conflicted', 0)} in conflict, {items.get('failed', 0) + items.get('rejected', 0)} failed, "
            f"{items.get('unsupported', 0)} not syncable",
        ]
        if mapping["destructive_writes"] == "suspended":
            lines.append(f"  DELETIONS SUSPENDED: {mapping['destructive_hold_reason']}")
        for provider, error in mapping["read_errors"].items():
            if error:
                lines.append(f"  {provider} currently unreachable: {error}")
        if mapping["recently_verified_in_icloud"]:
            lines.append("  Confirmed in iCloud (last 24 h):")
            for entry in mapping["recently_verified_in_icloud"][:15]:
                lines.append(
                    f"    - {_local(entry['verified_in_icloud_at'], tz)}: {entry.get('title', 'event')} "
                    f"[{entry.get('start', '?')}]"
                )
        if mapping["pending"]:
            lines.append("  Waiting (not yet confirmed):")
            for entry in mapping["pending"][:15]:
                lines.append(
                    f"    - {entry.get('title', 'event')} [{entry.get('start', '?')}] -> {entry['waiting_for']}"
                )
    conflicts = status["conflicts"]
    lines += ["", f"Unresolved conflicts: {conflicts['open']}"]
    for conflict in conflicts["items"][:10]:
        lines.append(f"  - #{conflict['id']} {conflict['kind']}: {conflict['reason']}")
    if status["alerts"]:
        lines += ["", "Active alerts:"] + [f"  - {a['message']}" for a in status["alerts"]]
    lines += ["", "Managed automatically by Calendar Bridge. Do not edit this event."]
    return "\n".join(lines)


def material_fingerprint(status: dict[str, Any]) -> str:
    """Changes only when something a reader cares about changes (not on every timestamp tick)."""
    material = {
        "overall": status["overall"],
        "auth": [status["google"]["state"], status["icloud"]["state"]],
        "hold": status.get("global_hold"),
        "conflicts": status["conflicts"]["open"],
        "alerts": sorted(a["key"] for a in status["alerts"]),
        "mappings": [
            [
                m["id"],
                m["state"],
                m["items"],
                m["destructive_writes"],
                sorted(filter(None, m["read_errors"].values())),
                [p["item_id"] for p in m["pending"]],
                [v["item_id"] for v in m["recently_verified_in_icloud"]],
            ]
            for m in status["mappings"]
        ],
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()


def status_event_id(day: dt.date) -> str:
    # base32hex alphabet (0-9, a-v) as required for client-chosen event ids
    return f"cbstatus{day:%Y%m%d}"


class StatusCalendar:
    def __init__(self, ctx: EngineContext, client: GoogleCalendarClient) -> None:
        self.ctx = ctx
        self.client = client
        self.repo = ctx.repo
        self.settings = ctx.settings

    def ensure(self) -> str:
        existing = self.repo.status_calendar()
        if existing is not None and not existing.missing_since:
            return existing.remote_id
        if existing is not None:  # deleted in Google: forget it, so it is not looked up again
            with self.ctx.db.tx():
                self.repo.set_calendar_role(existing.id, None)
        tz = self.ctx.default_tz
        # Adopt the status calendar made earlier (an answer lost after Google created it, or a
        # restored database) rather than creating a second one.
        created = next(
            (
                entry
                for entry in self.client.calendar_list()
                if entry.get("accessRole") == "owner"
                and entry.get("summary") == self.settings.status_calendar_name
                and entry.get("description") == STATUS_DESCRIPTION
            ),
            None,
        ) or self.client.insert_calendar(self.settings.status_calendar_name, tz, STATUS_DESCRIPTION)
        with self.ctx.db.tx():
            calendar_id = self.repo.upsert_calendar(
                provider="google",
                remote_id=created["id"],
                display_name=created.get("summary"),
                access="owner",
                writable=True,
                supports_events=True,
                is_primary=False,
                timezone=tz,
            )
            self.repo.set_calendar_role(calendar_id, "status")
            self.repo.audit("status_calendar_created", provider="google")
        return str(created["id"])

    def publish(self, status: dict[str, Any], *, force: bool = False) -> bool:
        tz = self.ctx.default_tz
        description = render_description(status, tz)
        title = f"Calendar Bridge: {HEADLINE.get(status['overall'], status['overall'])}"
        fingerprint = material_fingerprint(status)
        due = is_due(
            self.repo.get_meta("status_published_at"),
            self.ctx.now(),
            dt.timedelta(seconds=self.settings.status_update_interval),
        )
        if not force and fingerprint == self.repo.get_meta("status_fingerprint") and not due:
            return False
        calendar_id = self.ensure()
        today = timeutil.utc_to_local(self.ctx.now(), tz).date()
        event_id = status_event_id(today)
        body = {
            "summary": title,
            "description": description,
            "start": {"date": today.isoformat()},
            "end": {"date": (today + dt.timedelta(days=1)).isoformat()},
            "transparency": "transparent",
            "visibility": "private",
            "reminders": {"useDefault": False, "overrides": []},
            "status": "confirmed",
        }
        try:
            self.client.patch_event(calendar_id, event_id, body)
        except NotFound:
            try:
                self.client.insert_event(calendar_id, {"id": event_id, **body})
            except AlreadyExists:
                # Deleted earlier the same day: restore it in place.
                self.client.patch_event(calendar_id, event_id, body)
        self._prune(calendar_id, today)
        with self.ctx.db.tx():
            self.repo.set_meta("status_fingerprint", fingerprint)
            self.repo.set_meta("status_published_at", self.repo.now())
        return True

    def _prune(self, calendar_id: str, today: dt.date) -> None:
        last = self.repo.get_meta("status_pruned_for")
        if last == today.isoformat():
            return
        for offset in range(KEEP_DAYS, KEEP_DAYS + 30):
            try:
                self.client.delete_event(calendar_id, status_event_id(today - dt.timedelta(days=offset)))
            except NotFound:
                continue
        with self.ctx.db.tx():
            self.repo.set_meta("status_pruned_for", today.isoformat())
