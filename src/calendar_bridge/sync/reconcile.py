"""Per-mapping reconciliation.

1. Link newly observed events to existing items by identity (UIDs and the bridge's markers),
   or create items for genuinely new events.
2. Plan every item that needs attention - pure computation, no network.
3. Apply the mass-deletion guard to the whole plan before anything destructive runs.
4. Execute plans one item at a time; each item's failure is isolated and classified.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import random
import sqlite3
from collections import Counter
from typing import Any

from calendar_bridge.clock import is_due, to_iso
from calendar_bridge.db.repo import ConflictRow, ItemRow
from calendar_bridge.errors import (
    AuthError,
    InvalidRequest,
    PermissionDenied,
    PreconditionFailed,
    ProviderError,
    TransientError,
    UidConflict,
)
from calendar_bridge.logs import get_logger
from calendar_bridge.model.canonical import INFO_GROUPS, CanonicalEvent, changed_groups
from calendar_bridge.model.merge import Decision, SideState, baseline_after_write, decide, pairing_differences
from calendar_bridge.model.recurrence import in_window
from calendar_bridge.providers.google.convert import google_to_canonical, write_notes
from calendar_bridge.providers.icloud.convert import icloud_to_canonical
from calendar_bridge.sync.context import EngineContext, MappingContext
from calendar_bridge.sync.executor import Executor, GuestProtected, VerificationFailed
from calendar_bridge.sync.observe import store_google_raw
from calendar_bridge.sync.views import (
    ABSENT,
    DELETED,
    PRESENT,
    UNKNOWN,
    GoogleView,
    ICloudView,
    google_view,
    icloud_view,
)

log = get_logger("reconcile")
DESTRUCTIVE = frozenset({"delete_google", "delete_icloud", "suppress_google"})
RELEASE_VALID_FOR = dt.timedelta(hours=1)
HELD = "held for review: "  # state_reason prefix of an item whose deletion is held


@dataclasses.dataclass
class Plan:
    item: ItemRow
    kind: str
    g: GoogleView
    i: ICloudView
    decision: Decision | None = None
    reason: str = ""
    conflict: ConflictRow | None = None

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"item_id": self.item.id, "action": self.kind}
        if self.reason:
            out["reason"] = self.reason
        if self.decision is not None:
            if self.decision.google_groups:
                out["to_google"] = sorted(self.decision.google_groups)
            if self.decision.icloud_groups:
                out["to_icloud"] = sorted(self.decision.icloud_groups)
            if self.decision.conflict_groups:
                out["conflict"] = sorted(self.decision.conflict_groups)
        return out


@dataclasses.dataclass
class ReconcileStats:
    planned: Counter[str] = dataclasses.field(default_factory=Counter)
    done: Counter[str] = dataclasses.field(default_factory=Counter)
    errors: int = 0
    held: int = 0
    linked: int = 0
    new_items: int = 0
    plans: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "planned": dict(self.planned),
            "done": dict(self.done),
            "errors": self.errors,
            "held": self.held,
            "linked": self.linked,
            "new_items": self.new_items,
        }


def organized_by_user(event: CanonicalEvent | None) -> bool:
    return bool(event and event.is_meeting and event.master.organizer and event.master.organizer.is_self)


def owner_of(google_event: CanonicalEvent | None, icloud_event: CanonicalEvent | None, default: str) -> str:
    """The provider that owns the guest list when both sides already had the event: first the
    side where the user is the organizer (a meeting organized in iCloud and also delivered to
    Gmail as an invitation is owned by iCloud), then the side that has guests at all."""
    g_organizer, i_organizer = organized_by_user(google_event), organized_by_user(icloud_event)
    if i_organizer and not g_organizer:
        return "icloud"
    if g_organizer and not i_organizer:
        return "google"
    g_meeting = bool(google_event and google_event.is_meeting)
    i_meeting = bool(icloud_event and icloud_event.is_meeting)
    if g_meeting and not i_meeting:
        return "google"
    if i_meeting and not g_meeting:
        return "icloud"
    return default


class Reconciler:
    def __init__(
        self,
        engine: EngineContext,
        mc: MappingContext,
        executor: Executor,
        *,
        dry_run: bool = False,
        blocked_items: set[int] | None = None,
    ) -> None:
        self.engine = engine
        self.mc = mc
        self.executor = executor
        self.repo = engine.repo
        self.db = engine.db
        self.settings = engine.settings
        self.dry_run = dry_run
        self.blocked = blocked_items or set()
        self.stats = ReconcileStats()

    # ================================================================== entry point

    def run(self) -> ReconcileStats:
        if not self.dry_run:
            self.apply_unlinked_resolutions()
            self.link_new_rows()
        now = self.repo.now()
        plans = [self.safe_plan(item) for item in self.repo.items_to_process(self.mc.id, now)]
        for plan in plans:
            self.stats.planned[plan.kind] += 1
            self.stats.plans.append(plan.describe())
        if self.dry_run:
            return self.stats
        self.apply_destructive_guard(plans)
        # Bounded work per cycle keeps cycles short (well inside the watchdog timeout) during
        # large initial copies; items not reached stay dirty and are processed next cycle.
        budget = self.settings.max_items_per_cycle
        for plan in plans:
            if self.engine.stopping or budget <= 0:
                break  # remaining items stay dirty
            if plan.kind not in ("wait", "held"):  # held deletions must not crowd out other work
                budget -= 1
            self.execute(plan)
        return self.stats

    # ================================================================== linking

    def pair_unlinked_mirrors(self) -> None:
        """Link a mirror and its original when *both* are unknown to the database (after a
        restore from an older backup, or a lost database), using the markers the bridge writes
        on every mirror. This runs before anything else so neither can be mistaken for a new
        event and copied a second time."""
        icloud_rows = self.repo.unlinked_icloud(self.mc.id)
        google_rows = self.repo.unlinked_google_roots(self.mc.id)
        if not icloud_rows or not google_rows:
            return
        by_uid: dict[str, list[Any]] = {}
        for res in icloud_rows:
            if res.uid:
                by_uid.setdefault(res.uid, []).append(res)
        google_by_id = {g.event_id: g for g in google_rows}
        used_icloud: set[str] = set()
        used_google: set[str] = set()
        mapping_marker = str(self.mc.id)
        with self.db.tx():
            for g in google_rows:
                private = (g.raw.get("extendedProperties") or {}).get("private") or {}
                uid = private.get("cbUid")
                matches = [r for r in by_uid.get(uid or "", []) if r.href not in used_icloud]
                if not uid or private.get("cbMapping") != mapping_marker or len(matches) != 1:
                    continue
                self._paired_item("icloud", g, matches[0], uid)
                used_icloud.add(matches[0].href)
                used_google.add(g.event_id)
            for res in icloud_rows:
                if res.href in used_icloud or not res.ics or "X-CALENDAR-BRIDGE-" not in res.ics:
                    continue
                markers = icloud_to_canonical(res.ics, self.mc.ictx).markers
                original = google_by_id.get(markers.get("GOOGLE-ID", ""))
                if original is None or original.event_id in used_google or markers.get("MAPPING") != mapping_marker:
                    continue
                self._paired_item("google", original, res, res.uid)
                used_google.add(original.event_id)
                used_icloud.add(res.href)

    def _paired_item(self, origin: str, g: Any, res: Any, uid: str | None) -> None:
        item_id = self.repo.create_item(
            mapping_id=self.mc.id,
            origin=origin,
            authority=origin,
            google_event_id=g.event_id,
            google_ical_uid=g.ical_uid,
            icloud_href=res.href,
            icloud_uid=uid,
        )
        self.repo.audit("relinked_by_marker", mapping_id=self.mc.id, item_id=item_id)
        self.stats.linked += 1

    def link_new_rows(self) -> None:
        try:
            self.pair_unlinked_mirrors()
        except sqlite3.IntegrityError:
            log.exception("pairing mirrors failed; continuing with normal linking", extra={"mapping_id": self.mc.id})
        window = self.mc.window_start(self.engine.now())
        for row in self.repo.unlinked_google_roots(self.mc.id):
            try:
                conversion = google_to_canonical(row.raw, [], self.mc.gctx)
            except (KeyError, ValueError, TypeError):
                with self.db.tx():
                    self.repo.set_google_skip(self.mc.id, row.event_id, "unreadable")
                continue
            markers = conversion.markers
            with self.db.tx():
                candidate = None
                if markers.get("cbUid") and markers.get("cbMapping") == str(self.mc.id):
                    candidate = self._single(
                        i
                        for i in self.repo.items_by_uid(self.mc.id, markers["cbUid"])
                        if i.icloud_uid == markers["cbUid"]
                    )
                if candidate is None and row.ical_uid:
                    candidate = self._single(
                        i for i in self.repo.items_by_uid(self.mc.id, row.ical_uid) if i.icloud_uid == row.ical_uid
                    )
                if candidate is not None and candidate.google_event_id is None:
                    self.repo.update_item(
                        candidate.id, google_event_id=row.event_id, google_ical_uid=row.ical_uid, dirty=1
                    )
                    self.repo.audit("linked", mapping_id=self.mc.id, item_id=candidate.id, provider="google")
                    self.stats.linked += 1
                    continue
                if candidate is not None:
                    self._new_item(
                        "google",
                        google_event_id=row.event_id,
                        google_ical_uid=row.ical_uid,
                        state="conflicted",
                        reason="second Google copy of an event that is already mirrored",
                        duplicate_of=candidate.id,
                    )
                    continue
                if (
                    conversion.supported
                    and conversion.event is not None
                    and not in_window(conversion.event, window, self.mc.gctx.default_tz)
                ):
                    self.repo.set_google_skip(self.mc.id, row.event_id, "outside_history_window")
                    continue
                state, reason = (
                    ("pending", None)
                    if conversion.supported
                    else ("unsupported", "; ".join(conversion.unsupported) or "unsupported event")
                )
                self._new_item(
                    "google",
                    google_event_id=row.event_id,
                    google_ical_uid=row.ical_uid,
                    state=state,
                    reason=reason,
                )

        for res in self.repo.unlinked_icloud(self.mc.id):
            conversion = icloud_to_canonical(res.ics or "", self.mc.ictx)
            with self.db.tx():
                if not conversion.is_event:
                    self.repo.set_icloud_skip(self.mc.id, res.href, "not_an_event")
                    continue
                uid = conversion.uid
                markers = conversion.markers
                candidate = None
                if uid:
                    candidate = self._single(i for i in self.repo.items_by_uid(self.mc.id, uid) if i.icloud_uid == uid)
                    if candidate is not None and candidate.icloud_href is not None:
                        old = self.repo.icloud_resource(self.mc.id, candidate.icloud_href)
                        if old is not None and old.status == "present":
                            self._new_item(
                                "icloud",
                                icloud_href=res.href,
                                icloud_uid=uid,
                                state="conflicted",
                                reason="two iCloud resources share one UID",
                                duplicate_of=candidate.id,
                            )
                            continue
                if candidate is None and markers.get("GOOGLE-ID") and markers.get("MAPPING") == str(self.mc.id):
                    candidate = self.repo.item_by_google(self.mc.id, markers["GOOGLE-ID"])
                if candidate is None and uid:
                    candidate = self._single(
                        i
                        for i in self.repo.items_by_uid(self.mc.id, uid)
                        if i.google_ical_uid == uid and i.icloud_href is None
                    )
                if candidate is not None:
                    # New link, or the resource moved/was renamed: keep the item's history.
                    self.repo.update_item(
                        candidate.id,
                        icloud_href=res.href,
                        icloud_uid=uid,
                        icloud_detached_key=conversion.detached_key,
                        dirty=1,
                    )
                    self.repo.audit("linked", mapping_id=self.mc.id, item_id=candidate.id, provider="icloud")
                    self.stats.linked += 1
                    continue
                if (
                    conversion.supported
                    and conversion.event is not None
                    and not in_window(conversion.event, window, self.mc.ictx.default_tz)
                ):
                    self.repo.set_icloud_skip(self.mc.id, res.href, "outside_history_window")
                    continue
                state, reason = (
                    ("pending", None)
                    if conversion.supported
                    else ("unsupported", "; ".join(conversion.unsupported) or "unsupported event")
                )
                self._new_item(
                    "icloud",
                    icloud_href=res.href,
                    icloud_uid=uid,
                    state=state,
                    reason=reason,
                    icloud_detached_key=conversion.detached_key,
                )

    def _single(self, items: Any) -> ItemRow | None:
        found = list(items)
        return found[0] if len(found) == 1 else None

    def _new_item(
        self, origin: str, *, state: str, reason: str | None, duplicate_of: int | None = None, **ids: Any
    ) -> int:
        item_id = self.repo.create_item(
            mapping_id=self.mc.id, origin=origin, authority=origin, state=state, state_reason=reason, **ids
        )
        self.stats.new_items += 1
        self.repo.audit(f"new_{origin}_event", mapping_id=self.mc.id, item_id=item_id, provider=origin)
        if duplicate_of is not None:
            self.repo.create_conflict(
                mapping_id=self.mc.id,
                item_id=item_id,
                kind="duplicate",
                groups=[],
                google=None,
                icloud=None,
                google_base=None,
                icloud_base=None,
                reason=reason or "duplicate",
                candidates=[{"item_id": duplicate_of}],
            )
            self.repo.update_item(item_id, dirty=0)
        return item_id

    def apply_unlinked_resolutions(self) -> None:
        """Resolutions for ambiguous initial matches (conflicts without an item)."""
        for conflict in self.repo.conflicts(("resolving",)):
            if conflict.mapping_id != self.mc.id or conflict.item_id is not None:
                continue
            data = conflict.resolution_data or {}
            if conflict.resolution == "link" and (
                self.repo.item_by_google(self.mc.id, data.get("google_event_id", "")) is not None
                or self.repo.item_by_icloud(self.mc.id, data.get("icloud_href", "")) is not None
            ):
                with self.db.tx():
                    self.repo.update_conflict(
                        conflict.id,
                        state="open",
                        resolution=None,
                        reason="cannot link: one of the chosen events is already synchronized with another event",
                    )
                continue
            with self.db.tx():
                for cand in conflict.candidates:
                    if cand.get("provider") == "google":
                        self.repo.set_google_skip(self.mc.id, cand["id"], None)
                    else:
                        self.repo.set_icloud_skip(self.mc.id, cand["id"], None)
                if conflict.resolution == "link":
                    self.repo.create_item(
                        mapping_id=self.mc.id,
                        origin="both",
                        authority=data.get("authority", "google"),
                        google_event_id=data["google_event_id"],
                        icloud_href=data["icloud_href"],
                        google_ical_uid=data.get("google_ical_uid"),
                        icloud_uid=data.get("icloud_uid"),
                    )
                # "keep_separate": clearing the skip makes the next linking pass mirror each one.
                self.repo.update_conflict(conflict.id, state="resolved", resolved_at=self.repo.now())
                self.repo.audit(
                    "conflict_resolved", mapping_id=self.mc.id, detail=f"#{conflict.id} {conflict.resolution}"
                )

    # ================================================================== planning

    def safe_plan(self, item: ItemRow) -> Plan:
        """Plan one item; a failure is confined to that item and never stops the cycle."""
        try:
            return self.plan(item)
        except Exception as exc:
            log.exception("could not plan item", extra={"item_id": item.id})
            return Plan(
                item,
                "error",
                GoogleView(UNKNOWN),
                ICloudView(UNKNOWN),
                reason=f"internal error while planning: {type(exc).__name__}",
            )

    def plan(self, item: ItemRow) -> Plan:
        g = google_view(self.repo, self.mc, item)
        i = icloud_view(self.repo, self.mc, item)
        if item.id in self.blocked:
            return Plan(item, "wait", g, i, reason="unresolved write intent")
        if UNKNOWN in (g.state, i.state):
            return Plan(item, "wait", g, i, reason="waiting for verified provider state")
        conflict = self.repo.active_conflict_for_item(item.id)
        if conflict is not None and conflict.state == "resolving":
            return Plan(item, "resolve", g, i, conflict=conflict)
        if conflict is not None and conflict.kind == "duplicate":
            return Plan(item, "wait", g, i, conflict=conflict, reason="duplicate awaiting decision")
        plan = self._plan_states(item, g, i)
        plan.conflict = conflict
        return plan

    def _plan_states(self, item: ItemRow, g: GoogleView, i: ICloudView) -> Plan:
        if g.state == ABSENT and i.state == ABSENT:
            return Plan(item, "tombstone", g, i, reason="no remaining copies")
        if g.state == ABSENT:
            if i.state == DELETED:
                return Plan(item, "tombstone", g, i, reason="deleted in iCloud before mirroring")
            if not i.supported:
                return Plan(item, "unsupported", g, i, reason=self._reasons(i.conversion))
            assert i.event is not None
            if i.event.master.status == "cancelled":
                return Plan(item, "hold_suppressed", g, i, reason="cancelled in iCloud; not shown in Google")
            return Plan(item, "create_google", g, i)
        if i.state == ABSENT:
            if g.state == DELETED:
                return Plan(item, "tombstone", g, i, reason="deleted in Google before mirroring")
            if not g.supported:
                return Plan(item, "unsupported", g, i, reason=self._reasons(g.conversion))
            return Plan(item, "create_icloud", g, i)

        unsupported = [v.conversion for v in (g, i) if v.state == PRESENT and not v.supported]
        if unsupported:
            return Plan(item, "unsupported", g, i, reason="; ".join(self._reasons(c) for c in unsupported))

        if item.google_synced_json is None or item.icloud_synced_json is None:
            if g.state == PRESENT and i.state == PRESENT:
                return Plan(item, "initial_link", g, i)
            if g.state == DELETED and i.state == DELETED:
                return Plan(item, "tombstone", g, i, reason="deleted on both sides")
            return Plan(
                item,
                "conflict",
                g,
                i,
                reason="one side deleted before the pair was ever in sync",
                decision=Decision("conflict", conflict_kind="delete_vs_modify", conflict_groups={"*"}),
            )

        if i.state == PRESENT and i.event is not None and i.event.master.status == "cancelled":
            base = item.icloud_base
            if base is None or base.master.status != "cancelled":
                if item.authority == "icloud" and g.state == PRESENT:
                    return Plan(item, "suppress_google", g, i, reason="cancelled in iCloud")
                if item.authority == "google":
                    return Plan(
                        item,
                        "unsupported",
                        g,
                        i,
                        reason="'cancelled' status set on the iCloud copy is not propagated to Google",
                    )

        decision = decide(
            SideState(item.google_base, g.event),
            SideState(item.icloud_base, i.event),
            authority=item.authority,
            meeting_policy=self.settings.meeting_policy,
            project_google=self.mc.project_google,
            project_icloud=self.mc.project_icloud,
        )
        kind = {
            "noop": "noop",
            "write": "write",
            "conflict": "conflict",
            "tombstone": "tombstone",
            "delete_google": "delete_google",
            "delete_icloud": "delete_icloud",
        }[decision.action]
        return Plan(item, kind, g, i, decision=decision, reason=decision.reason)

    @staticmethod
    def _reasons(conversion: Any) -> str:
        if conversion is None:
            return "unreadable"
        return "; ".join(conversion.unsupported) or "unsupported"

    # ================================================================== safety

    def apply_destructive_guard(self, plans: list[Plan]) -> None:
        destructive = [p for p in plans if p.kind in DESTRUCTIVE]
        mapping = self.repo.mapping(self.mc.id)
        # A release (`safety release`) covers exactly the deletions the user reviewed; any other
        # deletion goes through the guard as usual.
        released = self._released_items()
        reviewed = [p for p in destructive if p.item.id in released]
        others = [p for p in destructive if p.item.id not in released]
        if released and not reviewed:
            self._end_release()
        if not destructive:
            if mapping.destructive_hold:
                # Nothing held any more (e.g. the events were restored): the hold has done its job.
                with self.db.tx():
                    self.repo.clear_hold(self.mc.id)
                    self.repo.audit("destructive_hold_cleared", mapping_id=self.mc.id, detail="no deletion waiting")
            return
        global_hold = self.repo.get_meta("global_hold")
        reason, held = None, others
        if global_hold:
            reason, held = f"destructive writes suspended globally: {global_hold}", destructive
        elif others and mapping.destructive_hold:
            reason = f"destructive writes suspended for this mapping: {mapping.destructive_hold_reason}"
        elif others:
            live = self.repo.live_item_count(self.mc.id)
            threshold = max(self.settings.mass_delete_count, int(self.settings.mass_delete_ratio * live))
            if len(others) > threshold:
                reason = (
                    f"{len(others)} deletions requested in one cycle (threshold {threshold}); review and run "
                    f"`calendar-bridge safety release --mapping {self.mc.id}`"
                )
                with self.db.tx():
                    self.repo.set_hold(self.mc.id, reason)
                    self.repo.audit("destructive_hold", mapping_id=self.mc.id, detail=reason)
                log.warning("mass deletion guard tripped", extra={"mapping_id": self.mc.id, "count": len(others)})
        if reason is not None:
            for plan in held:
                plan.kind = "held"
                plan.reason = reason
                self.stats.held += 1
        if reviewed and not global_hold and len(reviewed) <= self.settings.max_items_per_cycle:
            self._end_release()  # the reviewed deletions all run in this cycle

    def _released_items(self) -> set[int]:
        raw = self.repo.get_meta(f"release_once:{self.mc.id}")
        if not raw:
            return set()
        try:
            release = json.loads(raw)
            if not is_due(release["at"], self.engine.now(), RELEASE_VALID_FOR):
                return {int(item_id) for item_id in release["items"]}
        except (ValueError, KeyError, TypeError):
            pass  # unreadable, or a release from an older version: not honoured
        self._end_release()
        return set()

    def _end_release(self) -> None:
        with self.db.tx():
            self.repo.set_meta(f"release_once:{self.mc.id}", None)

    # ================================================================== execution

    def execute(self, plan: Plan) -> None:
        item = plan.item
        try:
            handler = getattr(self, f"_do_{plan.kind}")
            handler(plan)
            self.stats.done[plan.kind] += 1
            self.executor.close("verified")
        except AuthError:
            self.executor.intents.clear()
            raise
        except GuestProtected as exc:
            self.executor.close("abandoned")
            self._record_conflict(plan, "guest_managed", exc.groups, str(exc))
        except PreconditionFailed as exc:
            self._retry_soon(item, f"concurrent change detected ({exc.short()})")
        except UidConflict as exc:
            self._handle_uid_conflict(plan, exc)
        except (PermissionDenied, InvalidRequest) as exc:
            self._finish_error(item, "rejected", f"provider rejected the change: {exc.short()}")
        except (TransientError, VerificationFailed) as exc:
            self._schedule_retry(item, exc.short())
        except ProviderError as exc:
            self._finish_error(item, "failed", exc.short())
        except Exception as exc:
            log.exception("unexpected error while syncing item", extra={"item_id": item.id})
            self._finish_error(item, "failed", f"internal error: {type(exc).__name__}")
        finally:
            self.executor.intents.clear()

    # ---------------------------------------------------------------- handlers

    def _do_wait(self, plan: Plan) -> None:
        return

    def _do_error(self, plan: Plan) -> None:
        self._finish_error(plan.item, "failed", plan.reason)

    def _do_held(self, plan: Plan) -> None:
        reason = HELD + plan.reason
        if plan.item.state == "pending" and plan.item.state_reason == reason:
            return  # already recorded: no write every cycle while it waits for review
        with self.db.tx():
            self.repo.update_item(plan.item.id, state="pending", state_reason=reason)

    def _do_noop(self, plan: Plan) -> None:
        self._commit(plan.item, plan.g.event, plan.i.event, notes=self._notes(plan))
        if plan.conflict is not None:
            self._close_conflict(plan.conflict, "superseded")

    def _do_hold_suppressed(self, plan: Plan) -> None:
        with self.db.tx():
            self.repo.update_item(
                plan.item.id,
                state="synced",
                state_reason=plan.reason,
                suppressed="google",
                dirty=0,
                icloud_synced_json=plan.i.event.dumps() if plan.i.event else None,
            )

    def _do_tombstone(self, plan: Plan) -> None:
        deleted_by = {
            (DELETED, DELETED): "both",
            (DELETED, ABSENT): "google",
            (ABSENT, DELETED): "icloud",
        }.get((plan.g.state, plan.i.state), "both")
        self._tombstone(plan.item, deleted_by)

    def _do_unsupported(self, plan: Plan) -> None:
        with self.db.tx():
            self.repo.update_item(plan.item.id, state="unsupported", state_reason=plan.reason[:500], dirty=0)

    def _do_create_google(self, plan: Plan) -> None:
        item, source = plan.item, plan.i.event
        assert source is not None and plan.i.conversion is not None
        uid = plan.i.conversion.uid or item.icloud_uid or ""
        existing = self._find_existing_google(uid)
        if existing is not None:
            # Adopt it; the next pass compares both copies before anything is written.
            with self.db.tx():
                store_google_raw(self.repo, self.mc.id, existing)
                self.repo.update_item(
                    item.id, google_event_id=existing["id"], google_ical_uid=existing.get("iCalUID"), dirty=1
                )
                self.repo.audit("linked_existing", mapping_id=self.mc.id, item_id=item.id, provider="google")
            return
        g_after, notes = self.executor.create_google(item, source, uid)
        if g_after.event is None:
            raise VerificationFailed("created Google event could not be read back", provider="google")
        notes += write_notes(source, self.mc.gctx) + self._lossy_notes(source, g_after.event, "google")
        self._commit(
            item,
            g_after.event,
            source,
            notes=notes,
            touched={"google"},
            extra={"google_ical_uid": g_after.master.ical_uid if g_after.master else None, "suppressed": None},
        )
        self._audit(item, "created", "google")

    def _do_create_icloud(self, plan: Plan) -> None:
        item, source = plan.item, plan.g.event
        assert source is not None and plan.g.conversion is not None
        uid = plan.g.conversion.uid or f"{item.google_event_id}@google.com"
        i_after, notes = self.executor.create_icloud(item, source, uid, item.google_event_id)
        if i_after.event is None:
            raise VerificationFailed("created iCloud event could not be read back", provider="icloud")
        notes += self._lossy_notes(source, i_after.event, "icloud")
        self._commit(item, source, i_after.event, notes=notes, touched={"icloud"})
        self._audit(item, "created", "icloud")

    def _do_initial_link(self, plan: Plan) -> None:
        g_event, i_event = plan.g.event, plan.i.event
        assert g_event is not None and i_event is not None
        authority = owner_of(g_event, i_event, plan.item.authority)
        groups, personal = pairing_differences(
            g_event, i_event, project_google=self.mc.project_google, project_icloud=self.mc.project_icloud
        )
        if not groups:
            notes = self._notes(plan)
            if personal:
                notes.append("linked copies keep their own " + ", ".join(sorted(personal)))
            self._commit(plan.item, g_event, i_event, notes=notes, extra={"authority": authority})
            self._audit(plan.item, "linked_identical", None)
            return
        with self.db.tx():
            self.repo.update_item(plan.item.id, authority=authority)
        self._record_conflict(
            plan, "initial_divergence", groups, "both calendars already had this event with different details"
        )

    def _do_write(self, plan: Plan) -> None:
        decision = plan.decision
        assert decision is not None
        item = plan.item
        g_after, i_after = plan.g, plan.i
        notes: list[str] = []
        touched: set[str] = set()
        mismatches: list[str] = []
        if decision.google_groups and decision.google_target is not None:
            g_after, extra_notes = self.executor.update_google(
                item, plan.g, decision.google_target, decision.google_groups
            )
            notes += extra_notes + write_notes(decision.google_target, self.mc.gctx)
            touched.add("google")
            mismatches += self._verify(
                self.mc.project_google(decision.google_target), g_after.event, decision.google_groups, "Google"
            )
        if decision.icloud_groups and decision.icloud_target is not None:
            i_after = self.executor.update_icloud(item, plan.i, decision.icloud_target, decision.icloud_groups)
            touched.add("icloud")
            mismatches += self._verify(
                self.mc.project_icloud(decision.icloud_target), i_after.event, decision.icloud_groups, "iCloud"
            )
        if g_after.event is None or i_after.event is None:
            raise VerificationFailed("event missing after update", provider="/".join(sorted(touched)))
        if decision.ignored_groups:
            notes.append(
                "guest list / call details edited on the mirror are not copied back: "
                + ", ".join(sorted(decision.ignored_groups))
            )
        state, reason = "synced", None
        if mismatches:
            state, reason = "unsupported", "; ".join(mismatches)
        g_base, i_base, concurrent = self._post_write_baselines(
            plan, g_after.event, i_after.event, decision.google_groups, decision.icloud_groups
        )
        self._commit(
            item,
            g_base,
            i_base,
            notes=notes + self._notes(plan),
            touched=touched,
            state=state,
            reason=reason,
            dirty=concurrent,
        )
        self._audit(
            item,
            "updated",
            ",".join(sorted(touched)),
            detail=",".join(sorted(decision.google_groups | decision.icloud_groups)),
        )
        if plan.conflict is not None:
            self._close_conflict(plan.conflict, "superseded")

    def _do_delete_google(self, plan: Plan) -> None:
        self.executor.delete_google(plan.item, plan.g)
        self._tombstone(plan.item, "icloud", touched={"google"})

    def _do_delete_icloud(self, plan: Plan) -> None:
        self.executor.delete_icloud(plan.item, plan.i)
        self._tombstone(plan.item, "google", touched={"icloud"})

    def _do_suppress_google(self, plan: Plan) -> None:
        self.executor.delete_google(plan.item, plan.g)
        with self.db.tx():
            self.repo.update_item(
                plan.item.id,
                google_event_id=None,
                suppressed="google",
                google_synced_json=None,
                google_synced_fp=None,
                icloud_synced_json=plan.i.event.dumps() if plan.i.event else None,
                state="synced",
                state_reason="cancelled in iCloud; removed from Google until reinstated",
                dirty=0,
                last_synced_at=self.repo.now(),
            )
            self.repo.audit("suppressed", mapping_id=self.mc.id, item_id=plan.item.id, provider="google")

    def _do_conflict(self, plan: Plan) -> None:
        decision = plan.decision
        assert decision is not None
        self._record_conflict(plan, decision.conflict_kind or "field_conflict", decision.conflict_groups, plan.reason)

    def _do_resolve(self, plan: Plan) -> None:
        conflict = plan.conflict
        assert conflict is not None
        from calendar_bridge.sync.resolve import ResolutionNotApplicable, apply_resolution

        try:
            apply_resolution(self, plan, conflict)
        except ResolutionNotApplicable as exc:
            # The event changed since the decision was taken: ask again instead of guessing.
            with self.db.tx():
                self.repo.update_conflict(
                    conflict.id, state="open", resolution=None, reason=f"resolution not applied: {exc}"
                )
                self.repo.update_item(plan.item.id, state="conflicted", state_reason=str(exc), dirty=0)

    # ---------------------------------------------------------------- helpers

    def _find_existing_google(self, uid: str) -> dict[str, Any] | None:
        """Before creating a Google copy, look for one that already exists (a copy made before
        a crash or database restore, or the same invitation delivered to both providers)."""
        if not uid:
            return None
        client = self.executor.google
        candidates = [
            e
            for e in client.events_by_private_property(self.mc.google_cal.remote_id, "cbUid", uid)
            if e.get("status") != "cancelled"
            and not e.get("recurringEventId")
            and (e.get("extendedProperties") or {}).get("private", {}).get("cbMapping") == str(self.mc.id)
        ]
        if not candidates:
            candidates = [
                e
                for e in client.events_by_ical_uid(self.mc.google_cal.remote_id, uid)
                if e.get("status") != "cancelled" and not e.get("recurringEventId")
            ]
        taken = {row.google_event_id for row in self.repo.items(self.mc.id) if row.state != "deleted"}
        candidates = [e for e in candidates if e["id"] not in taken]
        return candidates[0] if len(candidates) == 1 else None

    def _verify(
        self, expected: CanonicalEvent, actual: CanonicalEvent | None, groups: set[str], label: str
    ) -> list[str]:
        if actual is None:
            return [f"{label} copy missing after write"]
        bad = sorted(g for g in changed_groups(expected, actual) & groups if g not in INFO_GROUPS)
        return [f"{label} did not keep: {', '.join(bad)}"] if bad else []

    def _lossy_notes(self, source: CanonicalEvent, written: CanonicalEvent, provider: str) -> list[str]:
        project = self.mc.project_google if provider == "google" else self.mc.project_icloud
        lost = sorted(g for g in changed_groups(project(source), written) if g not in INFO_GROUPS)
        return [f"{provider} copy differs in: {', '.join(lost)}"] if lost else []

    def _notes(self, plan: Plan) -> list[str]:
        notes: list[str] = []
        for view in (plan.g, plan.i):
            if view.conversion is not None:
                notes += view.conversion.notes
        return list(dict.fromkeys(notes))

    def _post_write_baselines(
        self,
        plan: Plan,
        g_after: CanonicalEvent,
        i_after: CanonicalEvent,
        google_groups: set[str],
        icloud_groups: set[str],
    ) -> tuple[CanonicalEvent, CanonicalEvent, bool]:
        """Baselines after writing: a side that was written takes only what was written from its
        read-back; other differences are concurrent edits that must still propagate."""
        concurrent = False
        g_base, i_base = g_after, i_after
        if google_groups and plan.g.event is not None:
            g_base, g_concurrent = baseline_after_write(plan.g.event, g_after, google_groups)
            concurrent |= g_concurrent
        if icloud_groups and plan.i.event is not None:
            i_base, i_concurrent = baseline_after_write(plan.i.event, i_after, icloud_groups)
            concurrent |= i_concurrent
        return g_base, i_base, concurrent

    def _commit(
        self,
        item: ItemRow,
        google_event: CanonicalEvent | None,
        icloud_event: CanonicalEvent | None,
        *,
        notes: list[str] | None = None,
        touched: set[str] | None = None,
        state: str = "synced",
        reason: str | None = None,
        extra: dict[str, Any] | None = None,
        dirty: bool = False,
    ) -> None:
        now = self.repo.now()
        authority = (extra or {}).get("authority", item.authority)
        owner = google_event if authority == "google" else icloud_event
        fields: dict[str, Any] = {
            "state": state,
            "state_reason": reason,
            "dirty": int(dirty),
            "retry_count": 0,
            "next_attempt_at": None,
            "last_error": None,
            "notes_json": json.dumps(notes) if notes else None,
            "last_synced_at": now,
            "meeting": int(bool(owner and owner.is_meeting)),
        }
        if google_event is not None:
            fields.update(google_synced_json=google_event.dumps(), google_synced_fp=google_event.fingerprint())
        if icloud_event is not None:
            fields.update(icloud_synced_json=icloud_event.dumps(), icloud_synced_fp=icloud_event.fingerprint())
        for provider in touched or ():
            fields[f"last_{provider}_verified_at"] = now
        fields.update(extra or {})
        with self.db.tx():
            self.repo.update_item(item.id, **fields)
            for provider in touched or ():
                self.repo.update_checkpoint(self.mc.id, provider, last_write_verified_at=now)

    def _tombstone(self, item: ItemRow, deleted_by: str, touched: set[str] | None = None) -> None:
        now = self.repo.now()
        with self.db.tx():
            self.repo.update_item(
                item.id,
                state="deleted",
                state_reason=f"deleted in {deleted_by}",
                deleted_at=now,
                deleted_by=deleted_by,
                dirty=0,
            )
            for provider in touched or ():
                self.repo.update_item(item.id, **{f"last_{provider}_verified_at": now})
                self.repo.update_checkpoint(self.mc.id, provider, last_write_verified_at=now)
            self.repo.audit(
                "deleted",
                mapping_id=self.mc.id,
                item_id=item.id,
                provider=",".join(sorted(touched or ())) or None,
                detail=f"by {deleted_by}",
            )
            conflict = self.repo.active_conflict_for_item(item.id)
            if conflict is not None:
                self.repo.update_conflict(conflict.id, state="superseded", resolved_at=now)

    def _record_conflict(self, plan: Plan, kind: str, groups: set[str], reason: str) -> None:
        item = plan.item
        now = self.repo.now()
        with self.db.tx():
            existing = plan.conflict or self.repo.active_conflict_for_item(item.id)
            if existing is not None and existing.kind == kind and existing.state == "open":
                self.repo.update_conflict(
                    existing.id,
                    groups=groups,
                    google_json=plan.g.event.dumps() if plan.g.event else None,
                    icloud_json=plan.i.event.dumps() if plan.i.event else None,
                    reason=reason,
                )
                conflict_id = existing.id
            else:
                if existing is not None:
                    self.repo.update_conflict(existing.id, state="superseded", resolved_at=now)
                conflict_id = self.repo.create_conflict(
                    mapping_id=self.mc.id,
                    item_id=item.id,
                    kind=kind,
                    groups=groups,
                    google=plan.g.event,
                    icloud=plan.i.event,
                    google_base=item.google_base,
                    icloud_base=item.icloud_base,
                    reason=reason,
                )
                self.repo.audit("conflict", mapping_id=self.mc.id, item_id=item.id, detail=f"#{conflict_id} {kind}")
            self.repo.update_item(
                item.id, state="conflicted", state_reason=f"conflict #{conflict_id}: {reason}", dirty=0
            )

    def _close_conflict(self, conflict: ConflictRow, state: str) -> None:
        with self.db.tx():
            self.repo.update_conflict(conflict.id, state=state, resolved_at=self.repo.now())

    def _audit(self, item: ItemRow, action: str, provider: str | None, detail: str | None = None) -> None:
        with self.db.tx():
            self.repo.audit(action, mapping_id=self.mc.id, item_id=item.id, provider=provider, detail=detail)

    def _retry_soon(self, item: ItemRow, error: str) -> None:
        with self.db.tx():
            self.repo.update_item(
                item.id, state="pending", state_reason=error, last_error=error, dirty=1, next_attempt_at=None
            )

    def _schedule_retry(self, item: ItemRow, error: str) -> None:
        current = self.repo.item(item.id)
        attempts = current.retry_count + 1
        if attempts >= self.settings.item_max_attempts:
            self._finish_error(item, "failed", f"gave up after {attempts} attempts: {error}")
            return
        delay = min(self.settings.item_retry_max, self.settings.item_retry_base * 2 ** (attempts - 1))
        delay = delay * (0.75 + random.random() / 2)
        next_at = self.engine.now().timestamp() + delay
        with self.db.tx():
            self.repo.update_item(
                item.id,
                state="pending",
                state_reason=f"will retry: {error}",
                last_error=error,
                dirty=0,  # picked up again once next_attempt_at is due (or when it changes)
                retry_count=attempts,
                next_attempt_at=to_iso(dt.datetime.fromtimestamp(next_at, dt.UTC)),
            )
        self.stats.errors += 1

    def _finish_error(self, item: ItemRow, state: str, error: str) -> None:
        with self.db.tx():
            self.repo.update_item(item.id, state=state, state_reason=error, last_error=error, dirty=0)
            self.repo.audit(state, mapping_id=self.mc.id, item_id=item.id, detail=error[:300])
        self.stats.errors += 1

    def _handle_uid_conflict(self, plan: Plan, exc: UidConflict) -> None:
        item = plan.item
        with self.db.tx():
            if exc.existing_href and self.repo.item_by_icloud(self.mc.id, exc.existing_href) is None:
                # An event with this UID already exists in the iCloud calendar: link to it and
                # compare, instead of creating a second copy.
                self.repo.update_item(item.id, icloud_href=exc.existing_href, dirty=1)
                self.repo.request_full_resync(self.mc.id)
                self.repo.audit("linked_existing_uid", mapping_id=self.mc.id, item_id=item.id, provider="icloud")
                return
        self._finish_error(item, "failed", f"UID already used by another iCloud event: {exc.short()}")
