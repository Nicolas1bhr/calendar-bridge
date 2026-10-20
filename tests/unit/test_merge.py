from __future__ import annotations

import dataclasses
import datetime as dt

from calendar_bridge.model.canonical import (
    Alarm,
    CanonicalEvent,
    EventData,
    EventTime,
    Person,
    Recurrence,
    changed_groups,
    with_groups,
)
from calendar_bridge.model.merge import SideState, decide


def ident(event: CanonicalEvent) -> CanonicalEvent:
    return event


def ev(summary: str = "A", hour: int = 10, **kw: object) -> CanonicalEvent:
    start = EventTime.zoned(dt.datetime(2030, 1, 7, hour), "Europe/Brussels")
    end = EventTime.zoned(dt.datetime(2030, 1, 7, hour + 1), "Europe/Brussels")
    recurrence = kw.pop("recurrence", Recurrence())
    overrides = kw.pop("overrides", ())
    return CanonicalEvent(EventData(start=start, end=end, summary=summary, **kw), recurrence, overrides)  # type: ignore[arg-type]


def run(
    g_base: CanonicalEvent | None,
    g_now: CanonicalEvent | None,
    i_base: CanonicalEvent | None,
    i_now: CanonicalEvent | None,
    authority: str = "google",
    policy: str = "authoritative",
) -> object:
    return decide(
        SideState(g_base, g_now),
        SideState(i_base, i_now),
        authority=authority,
        meeting_policy=policy,
        project_google=ident,
        project_icloud=ident,
    )


def test_no_change_is_noop() -> None:
    base = ev()
    assert run(base, base, base, base).action == "noop"


def test_one_sided_change_propagates_only_that_group() -> None:
    base = ev()
    decision = run(base, ev("B"), base, base)
    assert decision.action == "write"
    assert decision.icloud_groups == {"summary"} and not decision.google_groups
    assert decision.icloud_target.master.summary == "B"


def test_convergent_changes_are_noop() -> None:
    base = ev()
    assert run(base, ev("B"), base, ev("B")).action == "noop"


def test_conflicting_same_group_writes_nothing() -> None:
    base = ev()
    decision = run(base, ev("B"), base, ev("C"))
    assert decision.action == "conflict" and decision.conflict_groups == {"summary"}
    assert not decision.writes


def test_independent_groups_merge() -> None:
    base = ev()
    decision = run(base, ev("B"), base, ev(location="Room 1"))
    assert decision.action == "write"
    assert decision.icloud_groups == {"summary"} and decision.google_groups == {"location"}


def test_exdates_from_both_sides_are_unioned() -> None:
    rec = Recurrence(("FREQ=DAILY;COUNT=10",))
    base = ev(recurrence=rec)
    g = ev(recurrence=dataclasses.replace(rec, exdates=("20300108T090000Z",)))
    i = ev(recurrence=dataclasses.replace(rec, exdates=("20300109T090000Z",)))
    decision = run(base, g, base, i)
    assert decision.action == "write"
    assert decision.google_target.recurrence.exdates == ("20300108T090000Z", "20300109T090000Z")
    assert decision.icloud_target.recurrence.exdates == ("20300108T090000Z", "20300109T090000Z")


def test_timing_change_against_occurrence_edit_conflicts() -> None:
    rec = Recurrence(("FREQ=DAILY;COUNT=10",))
    base = ev(recurrence=rec)
    moved_series = ev(hour=12, recurrence=rec)
    cancelled_one = ev(recurrence=dataclasses.replace(rec, exdates=("20300108T090000Z",)))
    decision = run(base, moved_series, base, cancelled_one)
    assert decision.action == "conflict" and {"timing", "exdates"} <= decision.conflict_groups


def test_people_only_flow_from_owner_to_mirror() -> None:
    attendee = Person("guest@example.com", "Guest", "accepted")
    me = Person("me@example.com", is_self=True)
    base = ev(attendees=(me, attendee))
    owner_changed = ev(attendees=(me, dataclasses.replace(attendee, status="declined")))
    decision = run(base, owner_changed, base, base, authority="google")
    assert decision.icloud_groups == {"people"}
    mirror_changed = run(base, base, base, owner_changed, authority="google")
    assert mirror_changed.action == "noop" and "people" in mirror_changed.ignored_groups


def test_mirror_edit_of_meeting_is_guest_managed_conflict() -> None:
    guests = (Person("me@example.com", is_self=True), Person("guest@example.com"))
    base = ev(attendees=guests)
    mirror_moved = ev(hour=14, attendees=guests)
    decision = run(base, base, base, mirror_moved, authority="google")
    assert decision.action == "conflict" and decision.conflict_kind == "guest_managed"
    propagate = run(base, base, base, mirror_moved, authority="google", policy="propagate")
    assert propagate.action == "write" and propagate.google_groups == {"timing"}


def test_personal_fields_on_meeting_mirror_still_propagate() -> None:
    guests = (Person("me@example.com", is_self=True), Person("guest@example.com"))
    base = ev(attendees=guests)
    mirror = ev(attendees=guests, alarms=(Alarm("display", 15),))
    decision = run(base, base, base, mirror, authority="google")
    assert decision.action == "write" and decision.google_groups == {"alarms"}


def test_deletions() -> None:
    base = ev()
    assert run(base, None, base, base).action == "delete_icloud"
    assert run(base, base, base, None).action == "delete_google"
    assert run(base, None, base, None).action == "tombstone"
    conflict = run(base, None, base, ev("edited"))
    assert conflict.action == "conflict" and conflict.conflict_kind == "delete_vs_modify"


def test_mirror_deletion_of_meeting_needs_confirmation() -> None:
    guests = (Person("me@example.com", is_self=True), Person("guest@example.com"))
    base = ev(attendees=guests)
    decision = run(base, None, base, base, authority="icloud")
    assert decision.action == "conflict" and decision.conflict_kind == "guest_managed"
    owner_deleted = run(base, None, base, base, authority="google")
    assert owner_deleted.action == "delete_icloud"


def test_with_groups_and_changed_groups() -> None:
    a, b = ev("A", location="X"), ev("B", location="Y", alarms=(Alarm("display", 5),))
    assert changed_groups(a, a) == set()
    assert changed_groups(a, b) == {"summary", "location", "alarms"}
    mixed = with_groups(a, b, {"summary"})
    assert mixed.master.summary == "B" and mixed.master.location == "X"
    assert changed_groups(with_groups(a, b, changed_groups(a, b)), b) == set()


def test_override_groups_are_independent_per_occurrence() -> None:
    rec = Recurrence(("FREQ=DAILY;COUNT=10",))
    base = ev(recurrence=rec)
    moved = dataclasses.replace(base.master, summary="moved")
    g = ev(recurrence=rec, overrides=(("20300108T090000Z", moved),))
    i = ev(recurrence=rec, overrides=(("20300110T090000Z", moved),))
    decision = run(base, g, base, i)
    assert decision.action == "write"
    assert decision.icloud_groups == {"override:20300108T090000Z"}
    assert decision.google_groups == {"override:20300110T090000Z"}
