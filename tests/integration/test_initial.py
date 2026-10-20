"""Initial reconciliation: pair existing events before copying anything."""

from __future__ import annotations

import pytest

from calendar_bridge.errors import BridgeError
from calendar_bridge.sync.context import MappingContext
from calendar_bridge.sync.initial import apply_plan
from tests.conftest import Bridge


def writes(b: Bridge) -> tuple[int, int]:
    return len(b.google.writes()), len([r for r in b.server.middleware.requests if r[0] in ("PUT", "DELETE")])


@pytest.fixture
def prepared(bridge: Bridge) -> Bridge:
    bridge.icloud_url = bridge.server.calendar_url("home")
    return bridge


def test_identical_existing_events_are_linked_without_copying(prepared: Bridge) -> None:
    b = prepared
    b.g_create("Yoga", "2030-06-01T08:00:00", "2030-06-01T09:00:00")
    b.i_create("Yoga", "20300601T080000", "20300601T090000")
    before = writes(b)
    plan = b.map(apply=False)
    summary = plan.summary()
    assert summary["already_matching"] == 1
    assert summary["copy_to_icloud"] == summary["copy_to_google"] == 0
    assert summary["verdict"] == "safe"
    assert writes(b) == before, "planning is read-only"
    mc = MappingContext.build(b.app.ctx, b.app.repo.mapping(b.mapping_id))
    apply_plan(b.app.ctx, mc, plan, accept_ambiguous=False)
    b.sync_ok()
    assert writes(b) == before
    (item,) = b.items()
    assert item.state == "synced" and item.origin == "both"


def test_one_sided_events_are_copied_after_apply(prepared: Bridge) -> None:
    b = prepared
    b.g_create("Only in Google", "2030-06-02T08:00:00", "2030-06-02T09:00:00")
    b.i_create("Only in iCloud", "20300602T100000", "20300602T110000")
    plan = b.map()
    assert plan.summary()["copy_to_icloud"] == 1 and plan.summary()["copy_to_google"] == 1
    b.sync_ok()
    assert sorted(e["summary"] for e in b.g_live()) == ["Only in Google", "Only in iCloud"]
    assert len(b.i_resources()) == 2


def test_ambiguous_matches_block_and_are_never_merged(prepared: Bridge) -> None:
    b = prepared
    for _ in range(2):
        b.g_create("Lunch", "2030-06-03T12:00:00", "2030-06-03T13:00:00")
        b.i_create("Lunch", "20300603T120000", "20300603T130000")
    plan = b.map(apply=False)
    assert plan.summary()["ambiguous"] == 1 and not plan.safe
    mc = MappingContext.build(b.app.ctx, b.app.repo.mapping(b.mapping_id))
    with pytest.raises(BridgeError):
        apply_plan(b.app.ctx, mc, plan, accept_ambiguous=False)
    apply_plan(b.app.ctx, mc, plan, accept_ambiguous=True)
    before = writes(b)
    b.sync_ok()
    assert writes(b) == before, "ambiguous events are neither merged nor copied"
    (conflict,) = b.conflicts()
    assert conflict.kind == "ambiguous_match" and len(conflict.candidates) == 4

    google_ids = [c["id"] for c in conflict.candidates if c["provider"] == "google"]
    icloud_hrefs = [c["id"] for c in conflict.candidates if c["provider"] == "icloud"]
    with b.app.db.tx():
        b.app.repo.update_conflict(
            conflict.id,
            state="resolving",
            resolution="link",
            resolution_data={"google_event_id": google_ids[0], "icloud_href": icloud_hrefs[0]},
        )
    b.sync_ok()
    b.sync_ok()
    # The linked pair is in sync; the other two became ordinary events and were mirrored.
    assert len(b.g_live()) == 3 and len(b.i_resources()) == 3
    assert b.app.repo.conflict(conflict.id).state == "resolved"


def test_same_uid_with_different_details_is_a_conflict(prepared: Bridge) -> None:
    b = prepared
    event = b.google.user_insert(
        b.google_cal,
        {
            "id": "inv0001",
            "iCalUID": "meeting-123@example.org",
            "summary": "Board meeting",
            "start": {"dateTime": "2030-06-04T09:00:00", "timeZone": "Europe/Brussels"},
            "end": {"dateTime": "2030-06-04T10:00:00", "timeZone": "Europe/Brussels"},
        },
    )
    b.i_create("Board meeting (moved room)", "20300604T090000", "20300604T100000", uid="meeting-123@example.org")
    b.map()
    b.sync_ok()
    (conflict,) = b.conflicts()
    assert conflict.kind == "initial_divergence" and "summary" in conflict.groups
    assert b.google.event(b.google_cal, event["id"])["summary"] == "Board meeting"


def test_history_window_excludes_old_events(prepared: Bridge) -> None:
    b = prepared
    b.g_create("Ancient", "2020-01-01T09:00:00", "2020-01-01T10:00:00")
    b.g_create("Recent", "2030-06-05T09:00:00", "2030-06-05T10:00:00")
    b.g_create("Long series", "2020-01-06T09:00:00", "2020-01-06T10:00:00", recurrence=["RRULE:FREQ=WEEKLY"])
    plan = b.map(history_days=365)
    assert plan.summary()["outside_history_window"]["google"] == 1
    b.sync_ok()
    assert sorted(e.master.summary for e in b.i_events().values()) == ["Long series", "Recent"]


def test_read_only_calendar_is_blocked(prepared: Bridge) -> None:
    b = prepared
    b.google_cal = "en.be#holiday@group.v.calendar.google.com"
    plan = b.map(apply=False)
    assert not plan.safe and any("not writable" in x for x in plan.blockers)


def test_dry_run_cycle_writes_nothing(prepared: Bridge) -> None:
    b = prepared
    b.map()
    b.g_create("Planned only", "2030-06-06T09:00:00", "2030-06-06T10:00:00")
    before = writes(b)
    report = b.sync(dry_run=True)
    assert writes(b) == before
    assert report.mappings[0].plans == [] or all(p["action"] != "noop" for p in report.mappings[0].plans)
