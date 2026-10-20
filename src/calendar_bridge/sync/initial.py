"""Initial reconciliation of a newly mapped calendar pair.

Before anything is copied, both calendars are read completely and existing events are paired:

1. by identity - same UID / iCalUID, or the bridge's own markers (a previous installation);
2. by exact content - same title, start, end and recurrence, when exactly one candidate exists
   on each side.

Several equally good candidates make a match *ambiguous*: those events are neither merged nor
copied until the user decides. The result is a plan (``calendar-bridge reconcile``) with a
safety verdict; nothing is written to either provider until it is applied with ``--apply``,
and even then all copying goes through the normal journaled write path.
"""

from __future__ import annotations

import dataclasses
from collections import defaultdict
from typing import Any

from calendar_bridge.db.repo import GoogleEventRow, ICloudResourceRow
from calendar_bridge.errors import BridgeError
from calendar_bridge.model.canonical import CanonicalEvent, ConversionResult
from calendar_bridge.model.merge import pairing_differences
from calendar_bridge.model.recurrence import in_window
from calendar_bridge.providers.google.client import GoogleCalendarClient
from calendar_bridge.providers.google.convert import google_to_canonical
from calendar_bridge.providers.icloud.client import CalDAVClient
from calendar_bridge.providers.icloud.convert import ICloudConversion, icloud_to_canonical
from calendar_bridge.sync.context import HOME_TIMEZONE, EngineContext, MappingContext
from calendar_bridge.sync.observe import GoogleObserver, ICloudObserver
from calendar_bridge.sync.reconcile import owner_of

LARGE_COPY = 250


@dataclasses.dataclass
class Side:
    provider: str
    key: str  # google event id / icloud href
    uid: str | None
    conversion: ConversionResult
    markers: dict[str, str]

    @property
    def event(self) -> CanonicalEvent | None:
        return self.conversion.event

    def label(self) -> str:
        return self.event.describe() if self.event is not None else "(unreadable)"


@dataclasses.dataclass
class Pair:
    google: Side
    icloud: Side
    how: str  # identity | content
    identical: bool
    differences: list[str]


@dataclasses.dataclass
class InitialPlan:
    mapping_id: int
    google_total: int = 0
    icloud_total: int = 0
    pairs: list[Pair] = dataclasses.field(default_factory=list)
    ambiguous: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    copy_to_icloud: list[Side] = dataclasses.field(default_factory=list)
    copy_to_google: list[Side] = dataclasses.field(default_factory=list)
    unsupported: list[tuple[Side, str]] = dataclasses.field(default_factory=list)
    out_of_window: dict[str, int] = dataclasses.field(default_factory=lambda: {"google": 0, "icloud": 0})
    not_events: int = 0
    blockers: list[str] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)

    @property
    def safe(self) -> bool:
        return not self.blockers

    def summary(self) -> dict[str, Any]:
        return {
            "mapping_id": self.mapping_id,
            "google_events": self.google_total,
            "icloud_events": self.icloud_total,
            "already_matching": sum(1 for p in self.pairs if p.identical),
            "matched_with_differences": sum(1 for p in self.pairs if not p.identical),
            "ambiguous": len(self.ambiguous),
            "copy_to_icloud": len(self.copy_to_icloud),
            "copy_to_google": len(self.copy_to_google),
            "unsupported": len(self.unsupported),
            "outside_history_window": dict(self.out_of_window),
            "not_events": self.not_events,
            "verdict": "safe" if self.safe else "blocked",
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
        }


def _content_key(event: CanonicalEvent, default_tz: str) -> tuple[Any, ...]:
    m = event.master
    return (
        m.summary.casefold().strip(),
        m.start.instant(default_tz).isoformat(),
        m.end.instant(default_tz).isoformat(),
        m.start.is_all_day,
        tuple(event.recurrence.rrules),
    )


def build_plan(
    engine: EngineContext,
    mc: MappingContext,
    google: GoogleCalendarClient,
    icloud: CalDAVClient,
) -> InitialPlan:
    repo = engine.repo
    mapping = repo.mapping(mc.id)
    if mapping.state not in ("pending_initial", "active"):
        raise BridgeError(f"mapping {mc.id} is {mapping.state}")
    GoogleObserver(engine, mc, google).observe(force_full=True)
    ICloudObserver(engine, mc, icloud).observe(force_full=True)
    plan = InitialPlan(mapping_id=mc.id)
    _check_calendars(engine, mc, plan)

    window = mc.window_start(engine.now())
    tz = mc.gctx.default_tz
    g_sides: list[Side] = []
    for row in repo.unlinked_google_roots(mc.id):
        conversion = _google_conversion(mc, repo, row)
        g_sides.append(Side("google", row.event_id, row.ical_uid, conversion, conversion.markers))
    i_sides: list[Side] = []
    for res in repo.unlinked_icloud(mc.id):
        conversion = _icloud_conversion(mc, res)
        if not conversion.is_event:
            plan.not_events += 1
            continue
        i_sides.append(Side("icloud", res.href, conversion.uid, conversion, conversion.markers))
    plan.google_total, plan.icloud_total = len(g_sides), len(i_sides)

    # 1. identity
    i_by_uid = defaultdict(list)
    for side in i_sides:
        if side.uid:
            i_by_uid[side.uid].append(side)
    matched_g: set[str] = set()
    matched_i: set[str] = set()
    for g_side in g_sides:
        keys = {g_side.uid, g_side.markers.get("cbUid")} - {None}
        found = [s for k in keys for s in i_by_uid.get(k or "", [])]
        found += [s for s in i_sides if s.markers.get("GOOGLE-ID") == g_side.key and s not in found]
        found = [s for s in found if s.key not in matched_i]
        if len(found) == 1:
            plan.pairs.append(_pair(mc, g_side, found[0], "identity"))
            matched_g.add(g_side.key)
            matched_i.add(found[0].key)
        elif len(found) > 1:
            plan.ambiguous.append(_ambiguous("same UID", [g_side], found))
            matched_g.add(g_side.key)
            matched_i.update(s.key for s in found)

    # 2. exact content
    buckets: dict[tuple[Any, ...], tuple[list[Side], list[Side]]] = defaultdict(lambda: ([], []))
    for side in g_sides:
        if side.key not in matched_g and side.event is not None:
            buckets[_content_key(side.event, tz)][0].append(side)
    for side in i_sides:
        if side.key not in matched_i and side.event is not None:
            buckets[_content_key(side.event, tz)][1].append(side)
    for g_list, i_list in buckets.values():
        if not g_list or not i_list:
            continue
        if len(g_list) == 1 and len(i_list) == 1:
            plan.pairs.append(_pair(mc, g_list[0], i_list[0], "content"))
        else:
            plan.ambiguous.append(_ambiguous("same title and time", g_list, i_list))
        matched_g.update(s.key for s in g_list)
        matched_i.update(s.key for s in i_list)

    # 3. the rest is copied (if supported and recent enough)
    for sides, target, provider in ((g_sides, plan.copy_to_icloud, "google"), (i_sides, plan.copy_to_google, "icloud")):
        matched = matched_g if provider == "google" else matched_i
        for side in sides:
            if side.key in matched:
                continue
            if not side.conversion.supported:
                plan.unsupported.append((side, "; ".join(side.conversion.unsupported) or "unsupported"))
            elif side.event is not None and not in_window(side.event, window, tz):
                plan.out_of_window[provider] += 1
            else:
                target.append(side)

    if plan.ambiguous:
        plan.blockers.append(
            f"{len(plan.ambiguous)} ambiguous match group(s): look-alike events are never paired automatically. "
            "Start with --accept-ambiguous; they then wait for your decision (`calendar-bridge conflicts list`, "
            "then `conflicts resolve <id> --link ...` or `--keep-separate`)"
        )
    divergent = sum(1 for p in plan.pairs if not p.identical)
    if divergent:
        plan.warnings.append(f"{divergent} matched event(s) differ and will be recorded as conflicts")
    copies = len(plan.copy_to_icloud) + len(plan.copy_to_google)
    if copies > LARGE_COPY:
        plan.warnings.append(f"{copies} events will be copied; consider --history-days to limit history")
    return plan


def _google_conversion(mc: MappingContext, repo: Any, row: GoogleEventRow) -> ConversionResult:
    _master, instances = repo.google_family(mc.id, row.event_id)
    return google_to_canonical(row.raw, [i.raw for i in instances], mc.gctx)


def _icloud_conversion(mc: MappingContext, row: ICloudResourceRow) -> ICloudConversion:
    return icloud_to_canonical(row.ics or "", mc.ictx)


def _pair(mc: MappingContext, g_side: Side, i_side: Side, how: str) -> Pair:
    if g_side.event is None or i_side.event is None:
        return Pair(g_side, i_side, how, False, ["unreadable"])
    content, _personal = pairing_differences(
        g_side.event, i_side.event, project_google=mc.project_google, project_icloud=mc.project_icloud
    )
    return Pair(g_side, i_side, how, not content, sorted(content))


def _ambiguous(why: str, g_list: list[Side], i_list: list[Side]) -> dict[str, Any]:
    return {
        "why": why,
        "candidates": [{"provider": s.provider, "id": s.key, "uid": s.uid} for s in g_list + i_list],
        "labels": [s.label() for s in g_list + i_list],
    }


def _check_calendars(engine: EngineContext, mc: MappingContext, plan: InitialPlan) -> None:
    g, i = mc.google_cal, mc.icloud_cal
    if not g.writable or g.access not in ("owner", "writer"):
        plan.blockers.append(f"Google calendar '{g.label()}' is not writable ({g.access})")
    if g.role == "status":
        plan.blockers.append("the Calendar Bridge status calendar cannot be mapped")
    if not i.writable:
        plan.blockers.append(f"iCloud calendar '{i.label()}' is read-only")
    if not i.supports_events:
        plan.blockers.append(f"iCloud calendar '{i.label()}' does not hold events")
    if g.missing_since or i.missing_since:
        plan.blockers.append("one of the calendars was not found in the latest discovery")
    del engine


def apply_plan(
    engine: EngineContext, mc: MappingContext, plan: InitialPlan, *, accept_ambiguous: bool
) -> dict[str, int]:
    """Record the matches; the normal cycle then copies the rest through the journaled path."""
    repo = engine.repo
    blockers = [b for b in plan.blockers if not (accept_ambiguous and b.startswith(f"{len(plan.ambiguous)} ambiguous"))]
    if blockers:
        raise BridgeError("initial reconciliation is blocked: " + "; ".join(blockers))
    counts = {"linked": 0, "ambiguous": 0}
    with engine.db.tx():
        for pair in plan.pairs:
            authority = owner_of(pair.google.event, pair.icloud.event, "google")
            repo.create_item(
                mapping_id=mc.id,
                origin="both",
                authority=authority,
                google_event_id=pair.google.key,
                google_ical_uid=pair.google.uid,
                icloud_href=pair.icloud.key,
                icloud_uid=pair.icloud.uid,
                icloud_detached_key=getattr(pair.icloud.conversion, "detached_key", None),
            )
            counts["linked"] += 1
        for group in plan.ambiguous:
            for cand in group["candidates"]:
                if cand["provider"] == "google":
                    repo.set_google_skip(mc.id, cand["id"], "ambiguous_match")
                else:
                    repo.set_icloud_skip(mc.id, cand["id"], "ambiguous_match")
            repo.create_conflict(
                mapping_id=mc.id,
                item_id=None,
                kind="ambiguous_match",
                groups=[],
                google=None,
                icloud=None,
                google_base=None,
                icloud_base=None,
                reason=f"several possible matches ({group['why']})",
                candidates=group["candidates"],
            )
            counts["ambiguous"] += 1
        repo.update_mapping(mc.id, state="active", activated_at=repo.now(), last_full_reconcile_at=repo.now())
        if repo.get_meta(HOME_TIMEZONE) is None:
            repo.set_meta(HOME_TIMEZONE, engine.default_tz)  # from now on, floating times keep this zone
        repo.audit("mapping_activated", mapping_id=mc.id, detail=str(plan.summary()))
    return counts
