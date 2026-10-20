"""Change detection for both providers.

Each observer reads changes, then records them and advances the provider checkpoint in *one*
transaction. If anything fails before that commit, nothing is recorded and the old checkpoint
is reused next cycle, so no change can be skipped. Recording is idempotent, so re-reading the
same changes is harmless.

Deletion evidence rules:

* Google: an explicit ``status: cancelled`` record from the API, or a direct GET of that event
  returning 404/410 (used only to explain an event missing from a *complete* listing).
* iCloud: a direct request for that exact resource URL returning 404/410. Resources reported
  deleted by sync-collection or missing from a full listing are verified one by one; anything
  that cannot be verified is parked as ``unverified_missing`` and re-checked later - it is never
  treated as deleted.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any

from calendar_bridge.db.repo import Repo
from calendar_bridge.errors import NotFound, SyncTokenInvalid, TransientError, UnsupportedFeature
from calendar_bridge.logs import get_logger
from calendar_bridge.model.recurrence import key_for_google_time
from calendar_bridge.providers.google.client import GoogleCalendarClient
from calendar_bridge.providers.icloud.client import CalDAVClient, resource_url
from calendar_bridge.sync.context import EngineContext, MappingContext

log = get_logger("observe")
_UID_LINE = re.compile(r"^UID(?:;[^:]*)?:(.*)$", re.IGNORECASE)


@dataclasses.dataclass
class ObserveStats:
    provider: str
    full: bool = False
    changed: int = 0
    deleted: int = 0
    unverified: int = 0
    skipped: bool = False

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def extract_uid(ics: str) -> str | None:
    unfolded = re.sub(r"\r?\n[ \t]", "", ics)
    in_event = False
    for line in unfolded.splitlines():
        upper = line.upper()
        if upper.startswith("BEGIN:VEVENT"):
            in_event = True
        elif in_event:
            match = _UID_LINE.match(line)
            if match:
                return match.group(1).strip()
    return None


class GoogleObserver:
    def __init__(self, engine: EngineContext, mc: MappingContext, client: GoogleCalendarClient) -> None:
        self.engine = engine
        self.mc = mc
        self.client = client
        self.repo = engine.repo

    def observe(self, *, force_full: bool = False) -> ObserveStats:
        checkpoint = self.repo.checkpoint(self.mc.id, "google")
        full = force_full or checkpoint.needs_full or not checkpoint.sync_token
        calendar_id = self.mc.google_cal.remote_id
        events: list[dict[str, Any]] = []
        token: str | None = None
        if not full:
            try:
                events, token = self._fetch(calendar_id, checkpoint.sync_token)
            except SyncTokenInvalid:
                log.info("google sync token expired; full resync", extra={"mapping_id": self.mc.id})
                full = True
        if full:
            events, token = self._fetch(calendar_id, None)

        missing_verdicts: dict[str, dict[str, Any] | None] = {}
        unverified = 0
        if full:
            seen = {e["id"] for e in events}
            for event_id in sorted(self.repo.google_event_ids(self.mc.id) - seen):
                try:
                    missing_verdicts[event_id] = self.client.get_event(calendar_id, event_id)
                except NotFound:
                    missing_verdicts[event_id] = None
                except TransientError as exc:
                    # Unverifiable for now: keep what is stored (never a deletion) and check it
                    # again at the next complete read, without holding up everything else.
                    unverified += 1
                    log.warning(
                        "could not verify an event missing from a complete listing",
                        extra={"mapping_id": self.mc.id, "event_id": event_id, "error": exc.short()},
                    )

        stats = ObserveStats("google", full=full, unverified=unverified)
        with self.engine.db.tx():
            for raw in events:
                if self._store(raw):
                    stats.changed += 1
                    if raw.get("status") == "cancelled":
                        stats.deleted += 1
            for event_id, fetched in missing_verdicts.items():
                if fetched is None or fetched.get("status") == "cancelled":
                    row = self.repo.google_event(self.mc.id, event_id)
                    if self.repo.mark_google_cancelled(self.mc.id, event_id):
                        stats.deleted += 1
                        self.repo.mark_dirty_google(self.mc.id, event_id, row.master_id if row else None)
                else:
                    log.warning(
                        "event missing from complete listing but still exists",
                        extra={"mapping_id": self.mc.id, "event_id": event_id},
                    )
                    if self._store(fetched):
                        stats.changed += 1
            fields: dict[str, Any] = {"sync_token": token, "needs_full": 0}
            if full:
                fields["last_full_at"] = self.repo.now()
            self.repo.checkpoint_ok(self.mc.id, "google", **fields)
        return stats

    def _fetch(self, calendar_id: str, sync_token: str | None) -> tuple[list[dict[str, Any]], str | None]:
        events: list[dict[str, Any]] = []
        token = None
        for page in self.client.iter_event_pages(calendar_id, sync_token=sync_token):
            events.extend(page.get("items", []))
            token = page.get("nextSyncToken") or token
        if token is None:
            raise TransientError("Google listing ended without nextSyncToken", provider="google")
        return events, token

    def _store(self, raw: dict[str, Any]) -> bool:
        return store_google_raw(self.repo, self.mc.id, raw)


def store_google_raw(repo: Repo, mapping_id: int, raw: dict[str, Any]) -> bool:
    """Record one Google event resource; marks the owning item dirty if it changed.

    Google only guarantees ``id`` and ``status`` on deleted events, so a bare cancellation keeps
    the series link and original start already known for that id."""
    if raw.get("status") == "cancelled":
        known = repo.google_event(mapping_id, raw["id"])
        if known is not None:
            raw = dict(raw)
            for field in ("recurringEventId", "originalStartTime", "iCalUID"):
                if field not in raw and field in known.raw:
                    raw[field] = known.raw[field]
    master_id = raw.get("recurringEventId")
    original = raw.get("originalStartTime")
    key = key_for_google_time(original) if original else None
    changed = repo.upsert_google_event(mapping_id, raw, master_id=master_id, recurrence_key=key)
    if changed:
        repo.mark_dirty_google(mapping_id, raw["id"], master_id)
    return changed


class ICloudObserver:
    def __init__(self, engine: EngineContext, mc: MappingContext, client: CalDAVClient) -> None:
        self.engine = engine
        self.mc = mc
        self.client = client
        self.repo = engine.repo

    @property
    def url(self) -> str:
        return self.mc.icloud_cal.remote_id

    def observe(self, *, force_full: bool = False) -> ObserveStats:
        checkpoint = self.repo.checkpoint(self.mc.id, "icloud")
        full = force_full or checkpoint.needs_full or not checkpoint.sync_token
        stats = ObserveStats("icloud", full=full)
        token_now, ctag_now = self.client.collection_state(self.url)
        pending_verification = self.repo.icloud_unverified(self.mc.id)
        if (
            not full
            and token_now is not None
            and token_now == checkpoint.sync_token
            and ctag_now == checkpoint.ctag
            and not pending_verification
        ):
            with self.engine.db.tx():
                self.repo.checkpoint_ok(self.mc.id, "icloud")
            stats.skipped = True
            return stats

        known = self.repo.icloud_etags(self.mc.id)
        changed: dict[str, str | None] = {}
        candidates: set[str] = set(pending_verification)
        new_token: str | None = None
        if not full:
            try:
                result = self.client.sync_collection(self.url, checkpoint.sync_token)
                changed, new_token = result.changed, result.token
                candidates |= result.deleted
            except (SyncTokenInvalid, UnsupportedFeature):
                log.info("icloud sync token rejected; full listing", extra={"mapping_id": self.mc.id})
                full = True
                stats.full = True
        if full:
            try:
                result = self.client.sync_collection(self.url, None)
                listing, new_token = result.changed, result.token
            except UnsupportedFeature:
                listing, new_token = self.client.list_etags(self.url), token_now
            changed = listing
            candidates |= set(known) - set(listing)

        to_fetch = sorted(p for p, etag in changed.items() if etag is None or known.get(p) != etag)
        fetched = self.client.multiget(self.url, to_fetch) if to_fetch else {}
        present: dict[str, tuple[str | None, str]] = {}
        for path, value in fetched.items():
            if value is None:
                candidates.add(path)
            else:
                present[path] = value

        confirmed: set[str] = set()
        unverified: set[str] = set()
        for path in sorted(candidates - set(present)):
            if path not in known and path not in pending_verification:
                continue  # never stored: nothing to delete
            try:
                current = self.client.get(resource_url(self.url, path))
            except TransientError:
                unverified.add(path)
                continue
            if current is None:
                confirmed.add(path)
            else:
                present[path] = current

        with self.engine.db.tx():
            for path, (etag, ics) in present.items():
                if self.repo.upsert_icloud_resource(self.mc.id, path, uid=extract_uid(ics), etag=etag, ics=ics):
                    stats.changed += 1
                    self.repo.mark_dirty_icloud(self.mc.id, path)
            for path in confirmed:
                if self.repo.mark_icloud_deleted(self.mc.id, path):
                    stats.deleted += 1
                    self.repo.mark_dirty_icloud(self.mc.id, path)
            for path in unverified:
                self.repo.mark_icloud_missing(self.mc.id, path)
            stats.unverified = len(unverified)
            fields: dict[str, Any] = {"sync_token": new_token, "ctag": ctag_now, "needs_full": 0}
            if full:
                fields["last_full_at"] = self.repo.now()
            self.repo.checkpoint_ok(self.mc.id, "icloud", **fields)
        return stats
