"""Shared context objects for one engine run and one calendar mapping."""

from __future__ import annotations

import dataclasses
import datetime as dt
import threading

from calendar_bridge.clock import Clock
from calendar_bridge.config import Settings
from calendar_bridge.db.database import Database
from calendar_bridge.db.repo import CalendarRow, MappingRow, Repo
from calendar_bridge.model import timeutil
from calendar_bridge.model.canonical import Alarm, CanonicalEvent, EventData
from calendar_bridge.model.text import FooterFn, no_footer, render_footer
from calendar_bridge.providers.google.convert import GoogleContext, project_for_google
from calendar_bridge.providers.icloud.convert import ICloudContext, project_for_icloud

OWNER_LABEL = {"google": "Google Calendar", "icloud": "iCloud"}
HOME_TIMEZONE = "home_timezone"  # meta key: the zone recorded when the first calendar pair started


def home_timezone(settings: Settings, repo: Repo) -> str:
    """Zone for times that carry none (floating times): the configured ``default_timezone``. With
    "auto", the zone recorded when the first calendar pair started, so that it never drifts under
    events already synchronized (occurrence identities of floating recurring events depend on it);
    before that, the Google account's own zone (its primary calendar's), which Google itself uses."""
    if settings.default_timezone != "auto":
        return timeutil.default_zone_name(settings.default_timezone)
    recorded = repo.get_meta(HOME_TIMEZONE)
    if recorded:
        return recorded
    primary = next((c for c in repo.calendars("google") if c.is_primary and not c.missing_since), None)
    return (primary is not None and timeutil.canonical_tzid(primary.timezone)) or "UTC"


@dataclasses.dataclass
class EngineContext:
    settings: Settings
    db: Database
    repo: Repo
    clock: Clock
    stop: threading.Event | None = None

    def now(self) -> dt.datetime:
        return self.clock.now()

    @property
    def stopping(self) -> bool:
        return self.stop is not None and self.stop.is_set()

    @property
    def default_tz(self) -> str:
        return home_timezone(self.settings, self.repo)


@dataclasses.dataclass
class MappingContext:
    mapping: MappingRow
    google_cal: CalendarRow
    icloud_cal: CalendarRow
    gctx: GoogleContext
    ictx: ICloudContext
    settings: Settings

    @classmethod
    def build(cls, engine: EngineContext, mapping: MappingRow) -> MappingContext:
        repo = engine.repo
        settings = engine.settings
        google_cal = repo.calendar(mapping.google_calendar_id)
        icloud_cal = repo.calendar(mapping.icloud_calendar_id)
        default_tz = engine.default_tz
        reminders = tuple(
            Alarm("email" if r.get("method") == "email" else "display", int(r.get("minutes", 0)))
            for r in google_cal.default_reminders
        )
        google_account = repo.get_account("google")
        addresses = frozenset(
            a.strip().lower()
            for a in (repo.get_meta_json("icloud_addresses") or [])
            if isinstance(a, str) and a.strip()
        )
        gctx = GoogleContext(
            default_tz=default_tz,
            calendar_tz=google_cal.timezone,
            default_reminders=reminders,
            self_email=(google_account["identity"].lower() if google_account else None),
            mirror_event_types=frozenset(settings.mirror_google_event_types),
        )
        ictx = ICloudContext(default_tz=default_tz, self_addresses=addresses)
        return cls(mapping, google_cal, icloud_cal, gctx, ictx, settings)

    @property
    def id(self) -> int:
        return self.mapping.id

    def window_start(self, now: dt.datetime) -> dt.datetime | None:
        if self.mapping.history_days is None:
            return None
        return now - dt.timedelta(days=self.mapping.history_days)

    def footer(self, target_provider: str, authority: str) -> FooterFn:
        """Mirrors get the owner's guest list / call links as text; owners never do."""
        if target_provider == authority or not self.settings.mirror_info_block:
            return no_footer
        label = OWNER_LABEL[authority]

        def footer_for(data: EventData) -> str:
            return render_footer(data, label)

        return footer_for

    def project_google(self, event: CanonicalEvent) -> CanonicalEvent:
        return project_for_google(event, self.gctx)

    def project_icloud(self, event: CanonicalEvent) -> CanonicalEvent:
        return project_for_icloud(event, self.ictx)
