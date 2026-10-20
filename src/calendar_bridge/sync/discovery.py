"""Calendar discovery for both providers (read-only)."""

from __future__ import annotations

from typing import Any

from calendar_bridge.db.repo import CalendarRow
from calendar_bridge.providers.google.client import GoogleCalendarClient
from calendar_bridge.providers.icloud.client import CalDAVClient, Discovery
from calendar_bridge.sync.context import EngineContext


def refresh_google_calendars(ctx: EngineContext, client: GoogleCalendarClient) -> list[dict[str, Any]]:
    entries = client.calendar_list()
    repo = ctx.repo
    with ctx.db.tx():
        for entry in entries:
            access = entry.get("accessRole", "reader")
            repo.upsert_calendar(
                provider="google",
                remote_id=entry["id"],
                display_name=entry.get("summaryOverride") or entry.get("summary"),
                access=access,
                writable=access in ("owner", "writer"),
                supports_events=True,
                is_primary=bool(entry.get("primary")),
                timezone=entry.get("timeZone"),
                default_reminders=entry.get("defaultReminders") or [],
            )
        repo.mark_calendars_missing("google", [e["id"] for e in entries])
        repo.set_meta("google_calendars_refreshed_at", repo.now())
    return entries


def google_account_identity(entries: list[dict[str, Any]]) -> str | None:
    """The primary calendar's id is the account's address."""
    for entry in entries:
        if entry.get("primary"):
            return str(entry["id"]).lower()
    return None


def refresh_icloud_calendars(ctx: EngineContext, client: CalDAVClient) -> Discovery:
    discovery = client.discover()
    repo = ctx.repo
    with ctx.db.tx():
        for calendar in discovery.calendars:
            repo.upsert_calendar(
                provider="icloud",
                remote_id=calendar.url,
                display_name=calendar.display_name,
                access="read-write" if calendar.writable else "read-only",
                writable=calendar.writable,
                supports_events=calendar.supports_events,
                is_primary=calendar.is_default,
                timezone=calendar.timezone,
            )
        repo.mark_calendars_missing("icloud", [c.url for c in discovery.calendars])
        repo.set_meta_json("icloud_addresses", discovery.addresses)
        repo.set_meta("icloud_principal", discovery.principal_url)
        repo.set_meta("icloud_calendars_refreshed_at", repo.now())
    return discovery


def find_calendar(rows: list[CalendarRow], selector: str) -> CalendarRow | None:
    """Select by numeric id, exact remote id/URL, 'primary'/'default', or display name."""
    if selector.isdigit():
        return next((r for r in rows if r.id == int(selector)), None)
    if selector in ("primary", "default"):
        return next((r for r in rows if r.is_primary), None)
    exact = [r for r in rows if r.remote_id == selector]
    if exact:
        return exact[0]
    named = [r for r in rows if (r.display_name or "").casefold() == selector.casefold()]
    return named[0] if len(named) == 1 else None
