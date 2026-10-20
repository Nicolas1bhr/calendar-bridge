"""Three-way change detection and merge decisions.

Each side keeps its *own* baseline: the canonical form of that provider's representation as it
was the last time both sides were verified in agreement. A side "changed" when its current
canonical form differs from its own baseline. Comparing each provider only against itself means
provider normalizations (Google rewriting a time zone, iCloud adding properties) can never be
mistaken for user edits, and the bridge's own writes come back as "no change".

Rules, per field group:

* changed on one side only            -> copy that group to the other side
* changed on both sides, same result  -> nothing to do
* changed on both sides, different    -> conflict (whole event; nothing is written)
* ``exdates`` changed on both sides   -> union of both sides' edits, if timing is untouched
* ``timing`` on one side combined with occurrence edits (exdates/overrides) on the other is a
  conflict, because occurrence identities depend on the series timing.
* informational groups (people, conference) only flow from the owning provider to the mirror.
* for meetings under the "authoritative" policy, edits made on the mirror to shared fields are
  conflicts of kind ``guest_managed`` rather than silently applied, so guest-visible changes are
  always made where the guest list lives.

The decision is all-or-nothing: if any group conflicts, no group is written.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

from calendar_bridge.model.canonical import (
    INFO_GROUPS,
    PERSONAL_GROUPS,
    CanonicalEvent,
    all_groups,
    changed_groups,
    group_values,
    is_override_group,
    with_exdates,
    with_groups,
)

Projection = Callable[[CanonicalEvent], CanonicalEvent]

POLICY_AUTHORITATIVE = "authoritative"
POLICY_PROPAGATE = "propagate"


@dataclasses.dataclass
class SideState:
    base: CanonicalEvent | None
    current: CanonicalEvent | None  # None when deleted

    @property
    def deleted(self) -> bool:
        return self.current is None


@dataclasses.dataclass
class Decision:
    action: str  # noop | write | conflict | delete_google | delete_icloud | tombstone
    google_target: CanonicalEvent | None = None
    google_groups: set[str] = dataclasses.field(default_factory=set)
    icloud_target: CanonicalEvent | None = None
    icloud_groups: set[str] = dataclasses.field(default_factory=set)
    conflict_kind: str | None = None
    conflict_groups: set[str] = dataclasses.field(default_factory=set)
    ignored_groups: set[str] = dataclasses.field(default_factory=set)
    reason: str = ""

    @property
    def writes(self) -> bool:
        return bool(self.google_groups or self.icloud_groups)


def _values(event: CanonicalEvent | None) -> dict[str, object]:
    return group_values(event) if event is not None else {}


def _occurrence_groups(groups: set[str]) -> set[str]:
    return {g for g in groups if g == "exdates" or is_override_group(g)}


def decide(
    google: SideState,
    icloud: SideState,
    *,
    authority: str,
    meeting_policy: str,
    project_google: Projection,
    project_icloud: Projection,
) -> Decision:
    """Decide what to do for one linked item. ``authority`` is "google" or "icloud"."""
    if google.deleted and icloud.deleted:
        return Decision("tombstone", reason="deleted on both sides")
    if google.deleted or icloud.deleted:
        return _decide_deletion(google, icloud, authority=authority, meeting_policy=meeting_policy)

    assert google.current is not None and icloud.current is not None
    g_changed = changed_groups(google.base, google.current)
    i_changed = changed_groups(icloud.base, icloud.current)
    if not g_changed and not i_changed:
        return Decision("noop")

    g_now = _values(google.current)
    i_now = _values(icloud.current)
    g_as_icloud = _values(project_icloud(google.current))
    i_as_google = _values(project_google(icloud.current))

    def converged(group: str) -> bool:
        return g_as_icloud.get(group) == i_now.get(group) or i_as_google.get(group) == g_now.get(group)

    owner_current = google.current if authority == "google" else icloud.current
    meeting = owner_current.is_meeting
    owner_changed, mirror_changed = (g_changed, i_changed) if authority == "google" else (i_changed, g_changed)

    to_icloud: set[str] = set()
    to_google: set[str] = set()
    ignored: set[str] = set()
    conflicts: set[str] = set()
    guest_conflicts: set[str] = set()

    # Informational groups are never present on the mirror as data (only as rendered text), so
    # convergence cannot be judged on them: an owner-side change always refreshes the mirror.
    for group in INFO_GROUPS:
        if group in owner_changed:
            (to_icloud if authority == "google" else to_google).add(group)
        if group in mirror_changed:
            ignored.add(group)
    g_changed -= set(INFO_GROUPS)
    i_changed -= set(INFO_GROUPS)
    mirror_changed = mirror_changed - set(INFO_GROUPS)

    if meeting and meeting_policy == POLICY_AUTHORITATIVE:
        for group in mirror_changed:
            if group not in PERSONAL_GROUPS and not converged(group):
                guest_conflicts.add(group)
    if guest_conflicts:
        return Decision(
            "conflict",
            conflict_kind="guest_managed",
            conflict_groups=guest_conflicts,
            ignored_groups=ignored,
            reason=f"meeting guests are managed in {authority}; mirror edits need confirmation",
        )

    if "timing" in g_changed and _occurrence_groups(i_changed):
        conflicts |= {"timing"} | _occurrence_groups(i_changed)
    if "timing" in i_changed and _occurrence_groups(g_changed):
        conflicts |= {"timing"} | _occurrence_groups(g_changed)

    merged_exdates: tuple[str, ...] | None = None
    for group in sorted(g_changed | i_changed):
        in_g, in_i = group in g_changed, group in i_changed
        if in_g and in_i:
            if converged(group):
                continue
            if group == "exdates" and "timing" not in (g_changed | i_changed):
                merged_exdates = _merge_exdates(google, icloud)
                continue
            conflicts.add(group)
        elif in_g:
            if not converged(group):
                to_icloud.add(group)
        elif not converged(group):
            to_google.add(group)

    if conflicts:
        return Decision(
            "conflict",
            conflict_kind="field_conflict",
            conflict_groups=conflicts,
            ignored_groups=ignored,
            reason="both providers changed the same fields differently",
        )

    google_target = with_groups(google.current, icloud.current, to_google)
    icloud_target = with_groups(icloud.current, google.current, to_icloud)
    if merged_exdates is not None:
        if tuple(google.current.recurrence.exdates) != merged_exdates:
            google_target = with_exdates(google_target, merged_exdates)
            to_google.add("exdates")
        if tuple(icloud.current.recurrence.exdates) != merged_exdates:
            icloud_target = with_exdates(icloud_target, merged_exdates)
            to_icloud.add("exdates")

    if not to_google and not to_icloud:
        return Decision("noop", ignored_groups=ignored, reason="changes already converged")
    return Decision(
        "write",
        google_target=google_target if to_google else None,
        google_groups=to_google,
        icloud_target=icloud_target if to_icloud else None,
        icloud_groups=to_icloud,
        ignored_groups=ignored,
    )


def _merge_exdates(google: SideState, icloud: SideState) -> tuple[str, ...]:
    assert google.current is not None and icloud.current is not None
    g_base = set(google.base.recurrence.exdates) if google.base else set()
    i_base = set(icloud.base.recurrence.exdates) if icloud.base else set()
    g_now = set(google.current.recurrence.exdates)
    i_now = set(icloud.current.recurrence.exdates)
    removed = (g_base - g_now) | (i_base - i_now)
    return tuple(sorted((g_now | i_now) - removed))


def _decide_deletion(google: SideState, icloud: SideState, *, authority: str, meeting_policy: str) -> Decision:
    deleted_side = "google" if google.deleted else "icloud"
    survivor = icloud if google.deleted else google
    assert survivor.current is not None
    survivor_changes = changed_groups(survivor.base, survivor.current) - set(INFO_GROUPS)
    if survivor.base is None or survivor_changes:
        return Decision(
            "conflict",
            conflict_kind="delete_vs_modify",
            conflict_groups=survivor_changes or {"*"},
            reason=f"deleted in {deleted_side} but changed in the other provider",
        )
    if survivor.current.is_meeting and authority != deleted_side and meeting_policy == POLICY_AUTHORITATIVE:
        return Decision(
            "conflict",
            conflict_kind="guest_managed",
            conflict_groups={"*deleted*"},
            reason=(
                f"meeting deleted from the {deleted_side} mirror; deleting it in {authority} "
                "could notify guests, so it needs confirmation"
            ),
        )
    if google.deleted:
        return Decision("delete_icloud", reason="deleted in google")
    return Decision("delete_google", reason="deleted in icloud")


def identical(a: CanonicalEvent, b: CanonicalEvent, *, ignore: set[str] | None = None) -> bool:
    va, vb = group_values(a), group_values(b)
    keys = (set(va) | set(vb)) - (ignore or set())
    return all(va.get(k) == vb.get(k) for k in keys)


def differing_groups(a: CanonicalEvent, b: CanonicalEvent) -> set[str]:
    va, vb = group_values(a), group_values(b)
    return {k for k in set(va) | set(vb) if va.get(k) != vb.get(k)}


def pairing_differences(
    google: CanonicalEvent, icloud: CanonicalEvent, *, project_google: Projection, project_icloud: Projection
) -> tuple[set[str], set[str]]:
    """Differences between two copies that are being linked for the first time, split into
    content groups (need a human decision) and personal groups (reminders, free/busy,
    visibility), which each side may legitimately keep different: with per-side baselines a
    later change on either side still propagates."""
    one_way = differing_groups(project_icloud(google), icloud) - set(INFO_GROUPS)
    other_way = differing_groups(google, project_google(icloud)) - set(INFO_GROUPS)
    best = one_way if len(one_way) <= len(other_way) else other_way
    return best - PERSONAL_GROUPS, best & PERSONAL_GROUPS


def baseline_after_write(
    before: CanonicalEvent, after: CanonicalEvent, written: set[str]
) -> tuple[CanonicalEvent, bool]:
    """New baseline for a provider after the bridge wrote ``written`` groups to it.

    Only the written groups (plus occurrence groups a timing change can reshape as a provider
    side effect) are taken from the read-back. Anything else that differs was changed by someone
    else between observation and read-back; it keeps its old baseline value so the next cycle
    sees it as a change and propagates it instead of absorbing it. Returns (baseline, concurrent).
    """
    absorb = set(written)
    if "timing" in written:
        absorb |= {"exdates"} | {g for g in all_groups(before, after) if is_override_group(g)}
    changed = changed_groups(before, after)
    baseline = with_groups(before, after, changed & absorb)
    return baseline, bool(changed - absorb)
