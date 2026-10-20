"""Apply a conflict resolution chosen by the user (``calendar-bridge conflicts resolve``).

Resolutions are recorded in the database by the CLI and applied by the engine on its next cycle,
through the same journaled, verified write path as ordinary synchronization. A resolution is the
user's explicit confirmation, so it may change an iCloud meeting the user organizes (iCloud may
then notify its guests, as the CLI warns before recording such a choice).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from calendar_bridge.db.repo import ConflictRow
from calendar_bridge.errors import BridgeError
from calendar_bridge.model.canonical import INFO_GROUPS, CanonicalEvent, with_groups
from calendar_bridge.model.merge import POLICY_PROPAGATE, SideState, decide, differing_groups
from calendar_bridge.sync.views import DELETED, PRESENT

if TYPE_CHECKING:
    from calendar_bridge.sync.reconcile import Plan, Reconciler

RESOLUTIONS = {
    "keep_google": "make both calendars match the Google version",
    "keep_icloud": "make both calendars match the iCloud version",
    "merge": "choose a side per conflicting field (--field NAME=google|icloud)",
    "accept_delete": "accept the deletion and delete the remaining copy",
    "restore": "re-create the deleted copy from the remaining one",
    "delete_duplicate": "delete this duplicate copy",
    "keep_separate": "treat the events as separate and mirror each of them",
    "link": "link the two given events as the same event",
}


class ResolutionNotApplicable(BridgeError):
    """The chosen resolution does not fit the event's current state; the conflict is reopened."""


def apply_resolution(rec: Reconciler, plan: Plan, conflict: ConflictRow) -> None:
    resolution = conflict.resolution or ""
    if conflict.kind == "duplicate":
        _resolve_duplicate(rec, plan, resolution)
    elif resolution in ("keep_google", "keep_icloud"):
        _keep(rec, plan, "google" if resolution == "keep_google" else "icloud")
    elif resolution == "merge":
        _merge(rec, plan, conflict)
    elif resolution == "accept_delete":
        _accept_delete(rec, plan)
    elif resolution == "restore":
        _restore(rec, plan)
    else:
        raise ResolutionNotApplicable(f"resolution '{resolution}' does not apply to a {conflict.kind} conflict")
    with rec.db.tx():
        rec.repo.update_conflict(conflict.id, state="resolved", resolved_at=rec.repo.now())
        rec.repo.audit(
            "conflict_resolved", mapping_id=rec.mc.id, item_id=plan.item.id, detail=f"#{conflict.id} {resolution}"
        )


def _uid(plan: Plan) -> str:
    for conversion in (plan.i.conversion, plan.g.conversion):
        if conversion is not None and conversion.uid:
            return conversion.uid
    return plan.item.icloud_uid or plan.item.google_ical_uid or f"{plan.item.google_event_id}@google.com"


def _keep(rec: Reconciler, plan: Plan, winner: str) -> None:
    item, g, i, ex = plan.item, plan.g, plan.i, rec.executor
    win, lose = (g, i) if winner == "google" else (i, g)
    loser = "icloud" if winner == "google" else "google"
    if win.state == DELETED:
        if lose.state == PRESENT:
            if loser == "icloud":
                ex.delete_icloud(item, i, confirmed=True)
            else:
                ex.delete_google(item, g)
        rec._tombstone(item, winner, touched={loser} if lose.state == PRESENT else set())
        return
    source = win.event
    assert source is not None
    if lose.state != PRESENT:
        _recreate(rec, plan, loser, source)
        return
    target_current = lose.event
    assert target_current is not None
    project = rec.mc.project_icloud if loser == "icloud" else rec.mc.project_google
    groups = differing_groups(project(source), target_current)
    if loser == item.authority:
        groups -= set(INFO_GROUPS)
    target = with_groups(target_current, source, groups)
    if loser == "icloud":
        i_after = ex.update_icloud(item, i, target, groups, confirmed=True)
        _commit_after(rec, plan, g.event, i_after.event, set(), groups)
    else:
        g_after, notes = ex.update_google(item, g, target, groups)
        _commit_after(rec, plan, g_after.event, i.event, groups, set(), notes)


def _commit_after(
    rec: Reconciler,
    plan: Plan,
    g_after: CanonicalEvent | None,
    i_after: CanonicalEvent | None,
    google_groups: set[str],
    icloud_groups: set[str],
    notes: list[str] | None = None,
) -> None:
    assert g_after is not None and i_after is not None
    g_base, i_base, concurrent = rec._post_write_baselines(plan, g_after, i_after, google_groups, icloud_groups)
    touched = ({"google"} if google_groups else set()) | ({"icloud"} if icloud_groups else set())
    rec._commit(plan.item, g_base, i_base, notes=notes, touched=touched, dirty=concurrent)


def _recreate(rec: Reconciler, plan: Plan, side: str, source: CanonicalEvent) -> None:
    item, ex = plan.item, rec.executor
    uid = _uid(plan)
    if side == "icloud":
        with rec.db.tx():
            rec.repo.update_item(item.id, icloud_href=None)
        i_after, notes = ex.create_icloud(rec.repo.item(item.id), source, uid, item.google_event_id)
        rec._commit(item, source, i_after.event, notes=notes, touched={"icloud"})
    else:
        with rec.db.tx():
            rec.repo.update_item(item.id, google_event_id=None, suppressed=None)
        g_after, notes = ex.create_google(rec.repo.item(item.id), source, uid)
        rec._commit(item, g_after.event, source, notes=notes, touched={"google"})


def _merge(rec: Reconciler, plan: Plan, conflict: ConflictRow) -> None:
    """Per-field choice for the conflicting groups; everything else follows the normal rules
    (one-sided changes propagate, cancelled occurrences from both sides are combined, converged
    fields are left alone)."""
    item, g, i, ex = plan.item, plan.g, plan.i, rec.executor
    if g.event is None or i.event is None:
        raise ResolutionNotApplicable("merge needs both versions to exist; use keep_*, accept_delete or restore")
    choices: dict[str, str] = (conflict.resolution_data or {}).get("groups", {})
    unresolved = set(conflict.groups) - set(choices)
    if unresolved:
        raise ResolutionNotApplicable("no side chosen for: " + ", ".join(sorted(unresolved)))
    from_google = {grp for grp, side in choices.items() if side == "google"}
    from_icloud = {grp for grp, side in choices.items() if side == "icloud"}

    if item.google_base is None or item.icloud_base is None:
        # Linked without a common history: write exactly what the user chose, nothing else.
        to_icloud, to_google = from_google, from_icloud
        google_target = with_groups(g.event, i.event, to_google)
        icloud_target = with_groups(i.event, g.event, to_icloud)
    else:
        # Undo the losing side's edit of each chosen group, then let the normal rules decide.
        g_now = with_groups(g.event, item.google_base, from_icloud)
        i_now = with_groups(i.event, item.icloud_base, from_google)
        decision = decide(
            SideState(item.google_base, g_now),
            SideState(item.icloud_base, i_now),
            authority=item.authority,
            meeting_policy=POLICY_PROPAGATE,
            project_google=rec.mc.project_google,
            project_icloud=rec.mc.project_icloud,
        )
        if decision.action == "conflict":
            raise ResolutionNotApplicable(
                "other fields changed since this conflict was recorded: " + ", ".join(sorted(decision.conflict_groups))
            )
        to_google, to_icloud = decision.google_groups, decision.icloud_groups
        google_target = decision.google_target or g.event
        icloud_target = decision.icloud_target or i.event
    g_after: CanonicalEvent | None = g.event
    i_after: CanonicalEvent | None = i.event
    notes: list[str] = []
    if to_google:
        g_view, notes = ex.update_google(item, g, google_target, to_google)
        g_after = g_view.event
    if to_icloud:
        i_after = ex.update_icloud(item, i, icloud_target, to_icloud, confirmed=True).event
    _commit_after(rec, plan, g_after, i_after, to_google, to_icloud, notes)


def _accept_delete(rec: Reconciler, plan: Plan) -> None:
    item, g, i, ex = plan.item, plan.g, plan.i, rec.executor
    if g.state == DELETED and i.state == PRESENT:
        ex.delete_icloud(item, i, confirmed=True)
        rec._tombstone(item, "google", touched={"icloud"})
    elif i.state == DELETED and g.state == PRESENT:
        ex.delete_google(item, g)
        rec._tombstone(item, "icloud", touched={"google"})
    elif g.state == PRESENT and i.state == PRESENT:
        raise ResolutionNotApplicable("neither copy is deleted any more; choose keep_google or keep_icloud")
    else:
        rec._tombstone(item, "both")


def _restore(rec: Reconciler, plan: Plan) -> None:
    g, i = plan.g, plan.i
    if g.state == DELETED and i.event is not None:
        _recreate(rec, plan, "google", i.event)
    elif i.state == DELETED and g.event is not None:
        _recreate(rec, plan, "icloud", g.event)
    else:
        raise ResolutionNotApplicable("nothing to restore: no copy is deleted")


def _resolve_duplicate(rec: Reconciler, plan: Plan, resolution: str) -> None:
    item, g, i, ex = plan.item, plan.g, plan.i, rec.executor
    if resolution == "delete_duplicate":
        if g.state == PRESENT:
            ex.delete_google(item, g)
            rec._tombstone(item, "user", touched={"google"})
        elif i.state == PRESENT:
            ex.delete_icloud(item, i, confirmed=True)
            rec._tombstone(item, "user", touched={"icloud"})
        return
    if resolution == "keep_separate":
        with rec.db.tx():
            rec.repo.update_item(item.id, state="pending", state_reason=None, dirty=1)
        return
    raise ResolutionNotApplicable("a duplicate can only be resolved with delete_duplicate or keep_separate")


__all__ = ["RESOLUTIONS", "ResolutionNotApplicable", "apply_resolution"]
