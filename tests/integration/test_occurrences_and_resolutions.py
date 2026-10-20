"""Occurrence restore/revert flows, series changes, every conflict resolution, crash-before-write."""

from __future__ import annotations

import pytest

from tests.conftest import TZ, Bridge, reopen
from tests.fakes.google_api import SimulatedCrash


def unfold(ics: str) -> str:
    return ics.replace("\r\n ", "").replace("\r\n", "\n")


def resolve(b: Bridge, conflict_id: int, resolution: str, data: dict | None = None) -> None:
    with b.app.db.tx():
        b.app.repo.update_conflict(conflict_id, state="resolving", resolution=resolution, resolution_data=data or {})


def test_restoring_a_cancelled_occurrence_on_each_side(bridge: Bridge) -> None:
    bridge.map()
    master = bridge.g_create(
        "Swim", "2030-09-02T07:00:00", "2030-09-02T08:00:00", recurrence=["RRULE:FREQ=DAILY;COUNT=5"]
    )
    bridge.sync_ok()
    # Google: cancel then restore an occurrence.
    bridge.g_delete(f"{master}_20300903T050000Z")
    bridge.sync_ok()
    assert "20300903T070000" in unfold(bridge.i_by_summary("Swim")[1]).split("EXDATE")[1]
    bridge.g_patch(f"{master}_20300903T050000Z", {"status": "confirmed"})
    bridge.sync_ok()
    assert "EXDATE" not in unfold(bridge.i_by_summary("Swim")[1])

    # iPhone: cancel an occurrence, then restore it by removing the EXDATE.
    bridge.i_edit(
        "Swim",
        lambda ics: ics.replace(
            "RRULE:FREQ=DAILY;COUNT=5", f"RRULE:FREQ=DAILY;COUNT=5\r\nEXDATE;TZID={TZ}:20300904T070000"
        ),
    )
    bridge.sync_ok()
    cancelled = {e["id"]: e["status"] for e in bridge.google.instances_of(bridge.google_cal, master)}
    assert cancelled.get(f"{master}_20300904T050000Z") == "cancelled"
    bridge.i_edit("Swim", lambda ics: ics.replace(f"EXDATE;TZID={TZ}:20300904T070000\r\n", ""))
    bridge.sync_ok()
    statuses = {e["id"]: e["status"] for e in bridge.google.instances_of(bridge.google_cal, master)}
    assert statuses[f"{master}_20300904T050000Z"] == "confirmed"
    assert {i.state for i in bridge.items()} == {"synced"}


def test_reverting_an_override_and_moving_the_series(bridge: Bridge) -> None:
    bridge.map()
    master = bridge.g_create(
        "Standup", "2030-09-09T09:00:00", "2030-09-09T09:15:00", recurrence=["RRULE:FREQ=DAILY;COUNT=4"]
    )
    bridge.sync_ok()
    bridge.g_patch(f"{master}_20300910T070000Z", {"summary": "Standup (remote)"})
    bridge.sync_ok()
    assert "Standup (remote)" in bridge.i_by_summary("Standup")[1]

    # iPhone reverts the override (deletes the RECURRENCE-ID component).
    def drop_override(ics: str) -> str:
        head, _, rest = ics.partition("BEGIN:VEVENT")
        first, _, _tail = rest.partition("END:VEVENT")
        return head + "BEGIN:VEVENT" + first + "END:VEVENT\r\nEND:VCALENDAR\r\n"

    bridge.i_edit("Standup", drop_override)
    bridge.sync_ok()
    instance = bridge.google.event(bridge.google_cal, f"{master}_20300910T070000Z")
    assert instance["summary"] == "Standup"
    # Google moves the whole series one hour later.
    bridge.g_patch(
        master,
        {
            "start": {"dateTime": "2030-09-09T10:00:00", "timeZone": TZ},
            "end": {"dateTime": "2030-09-09T10:15:00", "timeZone": TZ},
        },
    )
    bridge.sync_ok()
    assert "DTSTART;TZID=Europe/Brussels:20300909T100000" in unfold(bridge.i_by_summary("Standup")[1])


def test_merge_resolution_with_per_field_choices(bridge: Bridge) -> None:
    bridge.map()
    event_id = bridge.g_create("Offsite", "2030-09-11T09:00:00", "2030-09-11T17:00:00")
    bridge.sync_ok()
    bridge.g_patch(event_id, {"summary": "Offsite (Google)", "location": "Antwerp"})
    bridge.i_edit("Offsite", lambda ics: ics.replace("SUMMARY:Offsite", "SUMMARY:Offsite (iPhone)\r\nLOCATION:Ghent"))
    bridge.sync_ok()
    (conflict,) = bridge.conflicts()
    assert set(conflict.groups) == {"summary", "location"}
    resolve(bridge, conflict.id, "merge", {"groups": {"summary": "icloud", "location": "google"}})
    bridge.sync_ok()
    event = bridge.google.event(bridge.google_cal, event_id)
    assert event["summary"] == "Offsite (iPhone)" and event["location"] == "Antwerp"
    assert "LOCATION:Antwerp" in bridge.i_by_summary("Offsite (iPhone)")[1]


def test_accept_delete_resolution(bridge: Bridge) -> None:
    bridge.map()
    event_id = bridge.g_create("Maybe", "2030-09-12T09:00:00", "2030-09-12T10:00:00")
    bridge.sync_ok()
    bridge.g_delete(event_id)
    bridge.i_edit("Maybe", lambda ics: ics.replace("SUMMARY:Maybe", "SUMMARY:Maybe edited"))
    bridge.sync_ok()
    (conflict,) = bridge.conflicts()
    resolve(bridge, conflict.id, "accept_delete")
    bridge.sync_ok()
    assert bridge.i_resources() == {} and bridge.items()[0].state == "deleted"


def test_keep_google_after_mirror_deleted_meeting(bridge: Bridge) -> None:
    bridge.map()
    bridge.g_create(
        "Interview",
        "2030-09-13T09:00:00",
        "2030-09-13T10:00:00",
        attendees=[{"email": "me@gmail.com", "self": True}, {"email": "candidate@example.com"}],
    )
    bridge.sync_ok()
    url, _ = bridge.i_by_summary("Interview")
    bridge.server.delete(url)
    bridge.sync_ok()
    (conflict,) = bridge.conflicts()
    assert conflict.kind == "guest_managed"
    assert bridge.g_by_summary("Interview"), "the guest-managed original is untouched"
    resolve(bridge, conflict.id, "keep_google")
    bridge.sync_ok()
    bridge.i_by_summary("Interview")  # mirror re-created


def test_duplicate_mirror_is_detected_and_can_be_deleted(bridge: Bridge) -> None:
    bridge.map()
    _, _uid = bridge.i_create("Dup", "20300914T090000", "20300914T100000")
    bridge.sync_ok()
    original = bridge.g_by_summary("Dup")
    clone = dict(original)
    for key in ("id", "etag", "iCalUID", "updated", "created", "organizer", "creator", "sequence", "kind"):
        clone.pop(key, None)
    bridge.google.user_insert(bridge.google_cal, clone)  # e.g. left behind by an old tool or a lost database
    bridge.sync_ok()
    conflicts = [c for c in bridge.conflicts() if c.kind == "duplicate"]
    assert len(conflicts) == 1
    resolve(bridge, conflicts[0].id, "delete_duplicate")
    bridge.sync_ok()
    assert [e["id"] for e in bridge.g_live()] == [original["id"]]


def test_crash_before_google_insert_recreates_exactly_once(bridge: Bridge) -> None:
    bridge.map()
    bridge.i_create("Vet", "20300915T090000", "20300915T100000")
    bridge.google.fail("POST", "/events", crash_before_apply=True)
    with pytest.raises(SimulatedCrash):
        bridge.sync()
    assert bridge.g_live() == []
    restarted = reopen(bridge)
    restarted.sync_ok()
    restarted.sync_ok()
    assert [e["summary"] for e in restarted.g_live()] == ["Vet"]
    intents = restarted.app.repo.intents_for_item(restarted.items()[0].id)
    assert {i.state for i in intents} >= {"abandoned", "verified"}


def test_transient_write_failure_is_retried_then_succeeds(bridge: Bridge) -> None:
    bridge.map()
    bridge.i_create("Retry me", "20300916T090000", "20300916T100000")
    bridge.google.fail("POST", "/events", status=503, reason="backendError")
    bridge.sync()
    (item,) = bridge.items()
    assert item.state == "pending" and item.retry_count == 1
    bridge.sync_ok()
    bridge.sync_ok()
    assert [e["summary"] for e in bridge.g_live()] == ["Retry me"]
    assert bridge.items()[0].state == "synced"


def test_rejected_write_is_reported_not_retried_forever(bridge: Bridge) -> None:
    bridge.map()
    bridge.i_create("Forbidden", "20300917T090000", "20300917T100000")
    bridge.google.fail("POST", "/events", status=403, reason="forbidden")
    bridge.sync()
    (item,) = bridge.items()
    assert item.state == "rejected" and "rejected" in (item.state_reason or "")
    writes = len(bridge.google.writes())
    bridge.sync_ok()
    assert len(bridge.google.writes()) == writes, "a rejected item waits for a change or a manual retry"


def test_refused_create_can_be_retried_after_the_cause_is_fixed(bridge: Bridge) -> None:
    bridge.map()
    bridge.i_create("Refused once", "20300918T090000", "20300918T100000")
    bridge.google.fail("POST", "/events", status=400, reason="invalid")
    bridge.sync()
    (item,) = bridge.items()
    assert item.state == "rejected" and item.google_event_id is None, "nothing was created, nothing is reserved"
    with bridge.app.db.tx():
        bridge.app.repo.update_item(item.id, state="pending", dirty=1, retry_count=0)
    bridge.sync_ok()
    assert [e["summary"] for e in bridge.g_live()] == ["Refused once"]
    assert bridge.items()[0].state == "synced"
