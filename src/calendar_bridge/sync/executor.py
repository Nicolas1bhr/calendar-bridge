"""Remote writes with a durable intent journal and read-back verification.

Protocol for every remote mutation:

1. record a write intent (and, for creations, the planned remote id) and commit;
2. perform the conditional / deterministic-id request;
3. mark the intent applied;
4. read the resulting remote state back and store it as the latest observation;
5. the reconciler then commits the item's new baselines and closes the intents.

A crash anywhere leaves either no intent (nothing happened) or an open intent naming the exact
remote object, which recovery inspects before anything else touches the item. Creations use a
pre-chosen Google event id / a UID-derived CalDAV URL with ``If-None-Match: *``, so repeating a
create can never produce a second copy; updates and deletes are conditional on the ETag that
was observed, so a concurrent edit is never overwritten.
"""

from __future__ import annotations

import dataclasses
import functools
from collections.abc import Callable
from typing import Any

from calendar_bridge.db.repo import ItemRow
from calendar_bridge.errors import (
    AlreadyExists,
    BridgeError,
    InvalidRequest,
    NotFound,
    PreconditionFailed,
    ProviderError,
    TransientError,
    UidConflict,
)
from calendar_bridge.logs import get_logger
from calendar_bridge.model.canonical import PERSONAL_GROUPS, CanonicalEvent, is_override_group, override_key
from calendar_bridge.model.recurrence import expected_occurrence, google_instance_id
from calendar_bridge.providers.google.client import GoogleCalendarClient, new_event_id
from calendar_bridge.providers.google.convert import (
    eventdata_from_google,
    exdate_line_keys,
    insert_body,
    instance_patch_body,
    marker_dict,
    master_patch_body,
    recurrence_lines,
)
from calendar_bridge.providers.icloud.client import CalDAVClient, normalize_path, resource_url
from calendar_bridge.providers.icloud.convert import ICloudContext, apply_to_ics, build_ics, resource_name_for_uid
from calendar_bridge.sync.context import EngineContext, MappingContext
from calendar_bridge.sync.observe import extract_uid, store_google_raw
from calendar_bridge.sync.views import (
    GoogleView,
    ICloudView,
    google_view_from_rows,
    icloud_view_from_row,
)

log = get_logger("executor")


class VerificationFailed(ProviderError):
    """The read-back after a write did not show the expected result."""


class GuestProtected(BridgeError):
    """Refusal to change or delete an iCloud meeting the user organizes: iCloud's server would
    e-mail the guests. Only a confirmed resolution may do that."""

    def __init__(self, message: str, groups: set[str]) -> None:
        super().__init__(message)
        self.groups = groups


def icloud_schedules_guests(view: ICloudView, ctx: ICloudContext) -> bool:
    """True when iCloud would send invitations/updates/cancellations for writes to this
    resource: the user organizes it and it has other guests. With unknown own addresses any
    organized meeting is treated as the user's (the safe assumption)."""
    event = view.event
    if event is None or event.master.organizer is None:
        return False
    organizer = event.master.organizer
    others = [p for p in event.master.attendees if not p.is_self and p.email != organizer.email]
    return bool(others) and (organizer.is_self or not ctx.self_addresses)


@dataclasses.dataclass
class ExecResult:
    notes: list[str] = dataclasses.field(default_factory=list)


class Executor:
    def __init__(
        self,
        engine: EngineContext,
        mc: MappingContext,
        google: GoogleCalendarClient,
        icloud: CalDAVClient,
    ) -> None:
        self.engine = engine
        self.mc = mc
        self.google = google
        self.icloud = icloud
        self.repo = engine.repo
        self.db = engine.db
        self.calendar_id = mc.google_cal.remote_id
        self.collection = mc.icloud_cal.remote_id
        self.intents: list[int] = []

    # ------------------------------------------------------------ journal

    def journal(
        self,
        item: ItemRow,
        provider: str,
        op: str,
        target: str,
        *,
        precondition: str | None = None,
        payload: dict[str, Any] | None = None,
        item_updates: dict[str, Any] | None = None,
    ) -> int:
        with self.db.tx():
            intent_id = self.repo.create_intent(
                mapping_id=self.mc.id,
                item_id=item.id,
                provider=provider,
                op=op,
                target=target,
                precondition=precondition,
                payload=payload,
            )
            if item_updates:
                self.repo.update_item(item.id, **item_updates)
        self.intents.append(intent_id)
        return intent_id

    def call[T](self, intent_id: int, fn: Callable[[], T]) -> T:
        try:
            result = fn()
        except PreconditionFailed as exc:
            with self.db.tx():
                self.repo.record_attempt(intent_id, state="abandoned", error=exc.short())
            raise
        except TransientError as exc:
            # Outcome unknown (e.g. timeout after sending): leave the intent open for recovery.
            with self.db.tx():
                self.repo.record_attempt(intent_id, error=exc.short())
            raise
        except ProviderError as exc:
            with self.db.tx():
                self.repo.record_attempt(intent_id, state="failed", error=exc.short())
            raise
        with self.db.tx():
            self.repo.record_attempt(intent_id, state="applied")
        return result

    def create_call[T](self, item: ItemRow, intent_id: int, fn: Callable[[], T], **planned: str) -> T:
        """Run a creation. A definite refusal (4xx other than the handled races) means nothing was
        created, so the planned identity is released; an unknown outcome keeps it for recovery."""
        try:
            return self.call(intent_id, fn)
        except TransientError:
            raise
        except ProviderError:
            with self.db.tx():
                current = self.repo.item(item.id)
                release = {k: None for k, v in planned.items() if getattr(current, k) == v}
                if release:
                    self.repo.update_item(item.id, **release)
            raise

    def close(self, state: str = "verified") -> None:
        if self.intents:
            with self.db.tx():
                self.repo.close_intents(self.intents, state)
            self.intents.clear()

    # ------------------------------------------------------------ google

    def google_readback(self, master_id: str, touched_instances: set[str]) -> GoogleView:
        master = self.google.get_event(self.calendar_id, master_id)
        fetched: dict[str, dict[str, Any]] = {}
        if master.get("recurrence") and master.get("iCalUID"):
            for event in self.google.events_by_ical_uid(self.calendar_id, master["iCalUID"]):
                if event.get("recurringEventId") == master_id:
                    fetched[event["id"]] = event
        for instance_id in touched_instances - set(fetched):
            try:
                fetched[instance_id] = self.google.get_event(self.calendar_id, instance_id)
            except NotFound:
                continue
        with self.db.tx():
            store_google_raw(self.repo, self.mc.id, master)
            for raw in fetched.values():
                store_google_raw(self.repo, self.mc.id, raw)
        master_row, instance_rows = self.repo.google_family(self.mc.id, master_id)
        return google_view_from_rows(self.mc, master_row, instance_rows)

    def create_google(self, item: ItemRow, source: CanonicalEvent, uid: str) -> tuple[GoogleView, list[str]]:
        notes: list[str] = []
        target = self.mc.project_google(source)
        event_id = new_event_id()
        footer = self.mc.footer("google", item.authority)
        origin = "google" if item.origin == "google" else "icloud"
        body = insert_body(
            target,
            self.mc.gctx,
            event_id=event_id,
            footer=footer,
            markers=marker_dict(origin=origin, uid=uid, mapping_id=self.mc.id),
        )
        intent = self.journal(
            item,
            "google",
            "create",
            event_id,
            payload={"source": source.to_json(), "uid": uid},
            item_updates={"google_event_id": event_id},
        )

        def insert() -> dict[str, Any]:
            try:
                return self.google.insert_event(self.calendar_id, body)
            except AlreadyExists:
                # Our own earlier attempt succeeded: the id was chosen by us and is unique.
                return self.google.get_event(self.calendar_id, event_id)

        self.create_call(item, intent, insert, google_event_id=event_id)
        touched: set[str] = set()
        for key, data in target.overrides:
            instance_id = google_instance_id(event_id, key)
            patch = instance_patch_body(data, None, self.mc.gctx, footer)
            step = self.journal(item, "google", "patch_instance", instance_id)
            try:
                self.call(step, functools.partial(self.google.patch_event, self.calendar_id, instance_id, patch))
                touched.add(instance_id)
            except NotFound:
                notes.append(f"occurrence {key} could not be modified in Google (not part of the series)")
        return self.google_readback(event_id, touched), notes

    def update_google(
        self, item: ItemRow, view: GoogleView, target: CanonicalEvent, groups: set[str]
    ) -> tuple[GoogleView, list[str]]:
        notes: list[str] = []
        assert view.master is not None and view.event is not None
        master = view.master
        master_id = master.event_id
        current = view.event
        footer = self.mc.footer("google", item.authority)
        gctx = self.mc.gctx
        rows = view.instance_by_key()
        line_keys = exdate_line_keys(master.raw, gctx) if master.raw.get("recurrence") else set()
        target_ex = set(target.recurrence.exdates)
        keep_lines = line_keys & target_ex

        operations: list[tuple[str, str, str, Any]] = []
        if "exdates" in groups:
            for key in sorted(target_ex - set(current.recurrence.exdates)):
                row = rows.get(key)
                operations.append(("cancel", key, row.event_id if row else google_instance_id(master_id, key), row))
            for key in sorted(set(current.recurrence.exdates) - target_ex):
                row = rows.get(key)
                if key not in line_keys and row is not None and row.status == "cancelled":
                    operations.append(("restore", key, row.event_id, row))
        target_overrides = target.override_map()
        for group in sorted(g for g in groups if is_override_group(g)):
            key = override_key(group)
            if key in target_ex:
                continue
            row = rows.get(key)
            instance_id = row.event_id if row else google_instance_id(master_id, key)
            data = target_overrides.get(key)
            if data is None:
                if row is None or row.status == "cancelled":
                    continue
                start, end = expected_occurrence(target.master, key, gctx.default_tz)
                data = dataclasses.replace(target.master, start=start, end=end)
            operations.append(("patch", key, instance_id, (row, data)))

        master_groups = {g for g in groups if not is_override_group(g)}
        body = master_patch_body(target, master_groups, master.raw, gctx, footer, exdate_lines=keep_lines)
        if body:
            intent = self.journal(item, "google", "update", master_id, precondition=master.etag)
            self.call(
                intent, functools.partial(self.google.patch_event, self.calendar_id, master_id, body, etag=master.etag)
            )

        touched: set[str] = set()
        fallback_lines: set[str] = set()
        for kind, key, instance_id, extra in operations:
            if kind == "cancel":
                row = extra
                etag = row.etag if row is not None and row.status != "cancelled" else None
                step = self.journal(item, "google", "cancel_instance", instance_id, precondition=etag)
                try:
                    self.call(
                        step, functools.partial(self.google.delete_event, self.calendar_id, instance_id, etag=etag)
                    )
                    touched.add(instance_id)
                except NotFound:
                    fallback_lines.add(key)
            elif kind == "restore":
                row = extra
                step = self.journal(item, "google", "restore_instance", instance_id)
                self.call(
                    step,
                    functools.partial(self.google.patch_event, self.calendar_id, instance_id, {"status": "confirmed"}),
                )
                touched.add(instance_id)
            else:
                row, data = extra
                live = row is not None and row.status != "cancelled"
                current_data = eventdata_from_google(row.raw, gctx) if live else None
                patch = instance_patch_body(data, current_data, gctx, footer, restore=row is not None and not live)
                if not patch:
                    continue
                etag = row.etag if live else None
                step = self.journal(item, "google", "patch_instance", instance_id, precondition=etag)
                try:
                    self.call(
                        step,
                        functools.partial(self.google.patch_event, self.calendar_id, instance_id, patch, etag=etag),
                    )
                    touched.add(instance_id)
                except NotFound:
                    notes.append(f"occurrence {key} could not be modified in Google (not part of the series)")

        if fallback_lines:
            refreshed = self.google.get_event(self.calendar_id, master_id)
            lines = recurrence_lines(target, gctx, keep_lines | fallback_lines)
            step = self.journal(item, "google", "update", master_id, precondition=refreshed.get("etag"))
            self.call(
                step,
                functools.partial(
                    self.google.patch_event,
                    self.calendar_id,
                    master_id,
                    {"recurrence": lines},
                    etag=refreshed.get("etag"),
                ),
            )
        return self.google_readback(master_id, touched), notes

    def delete_google(self, item: ItemRow, view: GoogleView) -> None:
        assert view.master is not None
        event_id = view.master.event_id
        etag = view.master.etag
        intent = self.journal(item, "google", "delete", event_id, precondition=etag)

        def delete() -> None:
            try:
                self.google.delete_event(self.calendar_id, event_id, etag=etag)
            except NotFound:
                return

        self.call(intent, delete)
        try:
            after = self.google.get_event(self.calendar_id, event_id)
            gone = after.get("status") == "cancelled"
        except NotFound:
            gone, after = True, None
        if not gone:
            raise VerificationFailed("event still exists after delete", provider="google")
        with self.db.tx():
            if after is not None:
                store_google_raw(self.repo, self.mc.id, after)
            self.repo.mark_google_cancelled(self.mc.id, event_id)

    # ------------------------------------------------------------ icloud

    def _url(self, path: str) -> str:
        return resource_url(self.collection, path)

    def icloud_readback(self, path: str) -> ICloudView:
        fetched = self.icloud.get(self._url(path))
        with self.db.tx():
            if fetched is None:
                self.repo.mark_icloud_deleted(self.mc.id, path)
            else:
                etag, ics = fetched
                if etag is None:
                    etag = self._etag(path)
                self.repo.upsert_icloud_resource(self.mc.id, path, uid=extract_uid(ics), etag=etag, ics=ics)
        return icloud_view_from_row(self.mc, self.repo.icloud_resource(self.mc.id, path))

    def _etag(self, path: str) -> str | None:
        return self.icloud.etag(self._url(path))

    def create_icloud(
        self, item: ItemRow, source: CanonicalEvent, uid: str, google_id: str | None
    ) -> tuple[ICloudView, list[str]]:
        target = self.mc.project_icloud(source)
        collection_path = normalize_path(self.collection, self.collection)
        path = collection_path.rstrip("/") + "/" + resource_name_for_uid(uid)
        markers = {"ORIGIN": "google" if item.origin == "google" else "icloud", "MAPPING": str(self.mc.id)}
        if google_id:
            markers["GOOGLE-ID"] = google_id
        ics = build_ics(
            target,
            uid=uid,
            ctx=self.mc.ictx,
            footer=self.mc.footer("icloud", item.authority),
            markers=markers,
            now=self.engine.now(),
            mirror_conference=item.authority == "google",
        )
        intent = self.journal(
            item,
            "icloud",
            "create",
            path,
            precondition="*",
            payload={"source": source.to_json(), "uid": uid},
            item_updates={"icloud_href": path, "icloud_uid": uid},
        )

        def put() -> None:
            try:
                self.icloud.put(self._url(path), ics, create=True)
            except PreconditionFailed:
                existing = self.icloud.get(self._url(path))
                if existing is None:
                    raise TransientError("resource vanished during create", provider="icloud") from None
                if extract_uid(existing[1]) != uid:
                    raise UidConflict("unrelated resource occupies the target URL", provider="icloud") from None
                # Our own earlier attempt already created it.

        self.create_call(item, intent, put, icloud_href=path)
        return self.icloud_readback(path), []

    def update_icloud(
        self, item: ItemRow, view: ICloudView, target: CanonicalEvent, groups: set[str], *, confirmed: bool = False
    ) -> ICloudView:
        assert view.row is not None and view.conversion is not None
        shared = groups - PERSONAL_GROUPS
        if shared and not confirmed and icloud_schedules_guests(view, self.mc.ictx):
            raise GuestProtected("changing an iCloud meeting you organize would notify its guests", shared)
        row = view.row
        new_ics = apply_to_ics(
            row.ics or "",
            target,
            groups,
            ctx=self.mc.ictx,
            footer=self.mc.footer("icloud", item.authority),
            now=self.engine.now(),
            mirror_conference=item.authority == "google",
            detached_key=view.conversion.detached_key,
        )
        if new_ics == row.ics:
            return view
        etag = row.etag or self._etag(row.href)
        if etag is None:
            raise InvalidRequest("server did not provide an ETag; refusing an unconditional write", provider="icloud")
        intent = self.journal(item, "icloud", "update", row.href, precondition=etag)
        self.call(intent, functools.partial(self.icloud.put, self._url(row.href), new_ics, if_match=etag))
        return self.icloud_readback(row.href)

    def delete_icloud(self, item: ItemRow, view: ICloudView, *, confirmed: bool = False) -> None:
        assert view.row is not None
        if not confirmed and icloud_schedules_guests(view, self.mc.ictx):
            raise GuestProtected(
                "deleting an iCloud meeting you organize would cancel it for its guests", {"*deleted*"}
            )
        row = view.row
        etag = row.etag or self._etag(row.href)
        if etag is None:
            raise InvalidRequest("server did not provide an ETag; refusing an unconditional delete", provider="icloud")
        intent = self.journal(item, "icloud", "delete", row.href, precondition=etag)

        def delete() -> None:
            try:
                self.icloud.delete(self._url(row.href), if_match=etag)
            except NotFound:
                return

        self.call(intent, delete)
        if self.icloud.get(self._url(row.href)) is not None:
            raise VerificationFailed("resource still exists after delete", provider="icloud")
        with self.db.tx():
            self.repo.mark_icloud_deleted(self.mc.id, row.href)
