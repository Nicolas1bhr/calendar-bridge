"""Crash, restart, outage and safety scenarios: nothing is lost, duplicated or wrongly deleted."""

from __future__ import annotations

from typing import Any

import pytest

from calendar_bridge.db.backup import create_backup, restore_backup
from calendar_bridge.providers.icloud.client import SyncResult
from tests.conftest import Bridge, reopen
from tests.fakes.caldav_server import DavFault
from tests.fakes.google_api import SimulatedCrash


def summaries_google(b: Bridge) -> list[str]:
    return sorted(e.get("summary", "") for e in b.g_live())


def summaries_icloud(b: Bridge) -> list[str]:
    return sorted(e.master.summary for e in b.i_events().values())


# ------------------------------------------------------------------ interrupted writes


def test_crash_after_google_insert_before_commit(bridge: Bridge) -> None:
    bridge.map()
    bridge.i_create("Physio", "20300401T090000", "20300401T100000")
    bridge.google.fail("POST", "/events", crash_after_apply=True)
    with pytest.raises(SimulatedCrash):
        bridge.sync()
    assert summaries_google(bridge) == ["Physio"]  # the write reached Google...
    restarted = reopen(bridge)  # ...but the process died before recording it.
    restarted.sync_ok()
    restarted.sync_ok()
    assert summaries_google(restarted) == ["Physio"], "recovery must adopt, not re-create"
    (item,) = restarted.items()
    assert item.state == "synced"
    intents = restarted.app.repo.intents_for_item(item.id)
    assert any(i.op == "create" and i.state == "verified" for i in intents)


def test_crash_after_icloud_put_before_commit(bridge: Bridge, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge.map()
    bridge.g_create("Optician", "2030-04-02T09:00:00", "2030-04-02T10:00:00")
    original_put = bridge.icloud_client.put

    def crashing_put(*args: Any, **kwargs: Any) -> Any:
        original_put(*args, **kwargs)
        raise SimulatedCrash("after PUT")

    monkeypatch.setattr(bridge.icloud_client, "put", crashing_put)
    with pytest.raises(SimulatedCrash):
        bridge.sync()
    monkeypatch.setattr(bridge.icloud_client, "put", original_put)
    restarted = reopen(bridge)
    restarted.sync_ok()
    restarted.sync_ok()
    assert summaries_icloud(restarted) == ["Optician"]
    assert restarted.items()[0].state == "synced"


def test_crash_after_update_and_after_delete(bridge: Bridge) -> None:
    bridge.map()
    a = bridge.g_create("Update me", "2030-04-03T09:00:00", "2030-04-03T10:00:00")
    bridge.i_create("Delete me", "20300403T110000", "20300403T120000")
    bridge.sync_ok()
    bridge.i_edit("Update me", lambda ics: ics.replace("SUMMARY:Update me", "SUMMARY:Updated"))
    bridge.google.fail("PATCH", "/events", crash_after_apply=True)
    with pytest.raises(SimulatedCrash):
        bridge.sync()
    restarted = reopen(bridge)
    restarted.sync_ok()
    assert restarted.google.event(restarted.google_cal, a)["summary"] == "Updated"
    assert restarted.conflicts() == [], "our own applied write must be recognised as converged"

    url, _ = restarted.i_by_summary("Delete me")
    restarted.server.delete(url)
    restarted.google.fail("DELETE", "/events", crash_after_apply=True)
    with pytest.raises(SimulatedCrash):
        restarted.sync()
    again = reopen(restarted)
    again.sync_ok()
    assert summaries_google(again) == ["Updated"]
    assert sorted(i.state for i in again.items()) == ["deleted", "synced"]


def test_restart_repeats_nothing(bridge: Bridge) -> None:
    bridge.map()
    for n in range(5):
        bridge.g_create(f"G{n}", f"2030-04-0{n + 1}T09:00:00", f"2030-04-0{n + 1}T10:00:00")
        bridge.i_create(f"I{n}", f"2030040{n + 1}T140000", f"2030040{n + 1}T150000")
    bridge.sync_ok()
    writes = (
        len(bridge.google.writes()),
        len([r for r in bridge.server.middleware.requests if r[0] in ("PUT", "DELETE")]),
    )
    restarted = reopen(bridge)
    for _ in range(3):
        restarted.sync_ok()
    restarted.sync_ok(force_full=True)
    after = (
        len(restarted.google.writes()),
        len([r for r in restarted.server.middleware.requests if r[0] in ("PUT", "DELETE")]),
    )
    assert writes == after
    assert len(restarted.g_live()) == 10 and len(restarted.i_resources()) == 10


# ------------------------------------------------------------------ provider outages


def test_google_outage_defers_then_catches_up(bridge: Bridge) -> None:
    bridge.map()
    bridge.sync_ok()
    bridge.google.fail("GET", "/events", status=503, reason="backendError", remaining=3)
    bridge.i_create("Made during outage", "20300405T090000", "20300405T100000")
    report = bridge.sync()
    assert not report.ok and any("google read failed" in e for e in report.mappings[0].errors)
    assert bridge.g_live() == []
    bridge.google.faults.clear()
    bridge.sync_ok()
    assert summaries_google(bridge) == ["Made during outage"]


def test_icloud_outage_defers_then_catches_up(bridge: Bridge) -> None:
    bridge.map()
    bridge.sync_ok()
    bridge.server.middleware.faults.append(
        DavFault(lambda m, p, b: m in ("PROPFIND", "REPORT"), "503 Service Unavailable", remaining=10)
    )
    bridge.g_create("Made while iCloud is down", "2030-04-06T09:00:00", "2030-04-06T10:00:00")
    report = bridge.sync()
    assert not report.ok
    bridge.server.middleware.faults.clear()
    assert bridge.i_resources() == {}
    bridge.sync_ok()
    assert summaries_icloud(bridge) == ["Made while iCloud is down"]


def test_expired_google_sync_token_triggers_safe_full_resync(bridge: Bridge) -> None:
    bridge.map()
    a = bridge.g_create("Keep", "2030-04-07T09:00:00", "2030-04-07T10:00:00")
    bridge.sync_ok()
    bridge.google.expire_tokens(bridge.google_cal)
    bridge.g_patch(a, {"summary": "Keep (edited)"})
    bridge.sync_ok()
    assert summaries_icloud(bridge) == ["Keep (edited)"]
    assert len(bridge.items()) == 1


def test_rejected_icloud_sync_token_falls_back_to_full_listing(bridge: Bridge) -> None:
    bridge.map()
    bridge.i_create("Before", "20300408T090000", "20300408T100000")
    bridge.sync_ok()
    body = b'<?xml version="1.0" encoding="utf-8"?><D:error xmlns:D="DAV:"><D:valid-sync-token/></D:error>'
    bridge.server.middleware.faults.append(
        DavFault(lambda m, p, b: m == "REPORT" and b"sync-token>http" in b, "403 Forbidden", body)
    )
    bridge.i_create("After", "20300408T110000", "20300408T120000")
    bridge.sync_ok()
    assert summaries_google(bridge) == ["After", "Before"]


# ------------------------------------------------------------------ deletion evidence


def test_unverifiable_icloud_deletion_is_never_propagated(bridge: Bridge) -> None:
    bridge.map()
    bridge.i_create("Fragile", "20300409T090000", "20300409T100000")
    bridge.sync_ok()
    url, _ = bridge.i_by_summary("Fragile")
    bridge.server.delete(url)
    path = url.split(":", 2)[-1].split("/", 1)[-1]
    # The confirming GET fails transiently: the deletion must stay unverified.
    bridge.server.middleware.faults.append(
        DavFault(
            lambda m, p, b: m == "GET" and p.endswith(path.rsplit("/", 1)[-1]), "503 Service Unavailable", remaining=5
        )
    )
    bridge.sync_ok()
    assert summaries_google(bridge) == ["Fragile"], "no deletion without verified evidence"
    bridge.server.middleware.faults.clear()
    bridge.sync_ok()
    assert summaries_google(bridge) == []


def test_listing_gap_for_an_existing_resource_is_not_a_deletion(
    bridge: Bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge.map()
    bridge.i_create("Still here", "20300410T090000", "20300410T100000")
    bridge.sync_ok()
    url, _ = bridge.i_by_summary("Still here")
    path = "/" + url.split("/", 3)[3]
    real = bridge.icloud_client.sync_collection

    def lying_sync(collection: str, token: str | None) -> SyncResult:
        result = real(collection, token)
        result.deleted.add(path)  # server claims it is gone...
        return result

    monkeypatch.setattr(bridge.icloud_client, "sync_collection", lying_sync)
    bridge.sync_ok()
    assert summaries_google(bridge) == ["Still here"]  # ...but a direct read proves otherwise.


def test_mass_deletion_guard_holds_then_releases(bridge: Bridge) -> None:
    bridge.map()
    ids = [
        bridge.g_create(f"Event {n}", f"2030-05-{n + 1:02d}T09:00:00", f"2030-05-{n + 1:02d}T10:00:00")
        for n in range(15)
    ]
    bridge.sync_ok()
    assert len(bridge.i_resources()) == 15
    for event_id in ids:
        bridge.g_delete(event_id)
    bridge.sync_ok()
    assert len(bridge.i_resources()) == 15, "bulk deletion must be held for review"
    mapping = bridge.app.repo.mapping(bridge.mapping_id)
    assert mapping.destructive_hold and "deletions" in (mapping.destructive_hold_reason or "")
    release(bridge)
    bridge.sync_ok()
    assert bridge.i_resources() == {}
    assert not bridge.app.repo.get_meta(f"release_once:{bridge.mapping_id}"), "a release is used up"


def release(bridge: Bridge) -> list[int]:
    """What `calendar-bridge safety release` records: exactly the deletions held for review."""
    held = [i.id for i in bridge.items("pending") if (i.state_reason or "").startswith("held for review")]
    with bridge.app.db.tx():
        bridge.app.repo.clear_hold(bridge.mapping_id)
        bridge.app.repo.set_meta_json(f"release_once:{bridge.mapping_id}", {"at": bridge.app.repo.now(), "items": held})
    return held


def test_a_release_covers_only_the_reviewed_deletions(bridge: Bridge) -> None:
    bridge.map()
    ids = [
        bridge.g_create(
            f"Event {n}",
            f"2030-05-{n % 28 + 1:02d}T{9 + n // 28:02d}:00:00",
            f"2030-05-{n % 28 + 1:02d}T{9 + n // 28:02d}:30:00",
        )
        for n in range(30)
    ]
    bridge.sync_ok()
    for event_id in ids[:13]:
        bridge.g_delete(event_id)
    bridge.sync_ok()
    assert len(release(bridge)) == 13
    for event_id in ids[13:28]:  # more deletions arrive before the next cycle
        bridge.g_delete(event_id)
    bridge.sync_ok()
    assert len(bridge.i_resources()) == 30 - 13, "only the reviewed deletions ran"
    assert bridge.app.repo.mapping(bridge.mapping_id).destructive_hold, "the new batch is held for review"


def test_a_release_without_pending_deletions_never_disarms_the_guard(bridge: Bridge) -> None:
    bridge.map()
    ids = [
        bridge.g_create(f"Event {n}", f"2030-05-{n + 1:02d}T09:00:00", f"2030-05-{n + 1:02d}T10:00:00")
        for n in range(15)
    ]
    bridge.sync_ok()
    with bridge.app.db.tx():  # e.g. released after the held events were restored
        bridge.app.repo.set_meta_json(
            f"release_once:{bridge.mapping_id}",
            {"at": bridge.app.repo.now(), "items": [i.id for i in bridge.items()]},
        )
    bridge.sync_ok()
    assert not bridge.app.repo.get_meta(f"release_once:{bridge.mapping_id}"), (
        "consumed by a cycle with nothing to delete"
    )
    for event_id in ids:  # a later accident
        bridge.g_delete(event_id)
    bridge.sync_ok()
    assert len(bridge.i_resources()) == 15, "the guard still holds a later mass deletion"


def test_a_hold_clears_itself_once_nothing_is_waiting(bridge: Bridge) -> None:
    bridge.map()
    ids = [
        bridge.g_create(f"Event {n}", f"2030-05-{n + 1:02d}T09:00:00", f"2030-05-{n + 1:02d}T10:00:00")
        for n in range(15)
    ]
    bridge.sync_ok()
    deleted = {event_id: bridge.google.event(bridge.google_cal, event_id) for event_id in ids}
    for event_id in ids:
        bridge.g_delete(event_id)
    bridge.sync_ok()
    assert bridge.app.repo.mapping(bridge.mapping_id).destructive_hold
    bridge.app.settings.max_items_per_cycle = 5  # fewer than the 15 held deletions
    other = bridge.g_create("Unrelated", "2030-06-01T09:00:00", "2030-06-01T10:00:00")
    bridge.sync_ok()
    assert len(bridge.i_resources()) == 16, "held deletions do not stop other changes"
    for event_id, body in deleted.items():  # the user restores the events in Google
        assert body is not None
        bridge.g_patch(event_id, {"status": "confirmed", "summary": body["summary"]})
    bridge.sync_ok()
    bridge.sync_ok()
    assert not bridge.app.repo.mapping(bridge.mapping_id).destructive_hold
    assert len(bridge.i_resources()) == 16 and other


def test_concurrent_edit_during_write_is_retried_not_overwritten(bridge: Bridge) -> None:
    bridge.map()
    a = bridge.g_create("Race", "2030-04-11T09:00:00", "2030-04-11T10:00:00")
    bridge.sync_ok()
    bridge.i_edit("Race", lambda ics: ics.replace("SUMMARY:Race", "SUMMARY:Race (iPhone)"))
    bridge.google.fail("PATCH", "/events", status=412, reason="conditionNotMet")
    bridge.sync()
    assert bridge.google.event(bridge.google_cal, a)["summary"] == "Race"
    assert bridge.items()[0].state == "pending"
    bridge.sync_ok()
    assert bridge.google.event(bridge.google_cal, a)["summary"] == "Race (iPhone)"


def test_google_authorization_failure_stops_writes_and_is_reported(bridge: Bridge) -> None:
    bridge.map()
    bridge.sync_ok()
    bridge.google.fail(None, "/", status=401, reason="authError", remaining=50)
    bridge.i_create("Waiting for auth", "20300412T090000", "20300412T100000")
    report = bridge.sync()
    assert not report.ok
    account = bridge.app.repo.get_account("google")
    assert account["auth_state"] == "invalid"
    bridge.google.faults.clear()
    bridge.sync_ok()
    assert bridge.app.repo.get_account("google")["auth_state"] == "ok"
    assert summaries_google(bridge) == ["Waiting for auth"]


# ------------------------------------------------------------------ backup / restore


def test_restoring_an_older_backup_relinks_instead_of_duplicating(bridge: Bridge) -> None:
    bridge.map()
    bridge.g_create("Old G", "2030-04-13T09:00:00", "2030-04-13T10:00:00")
    bridge.sync_ok()
    backup = create_backup(bridge.app.db, bridge.app.settings.backup_dir, box=bridge.app.maybe_box(), keep=5)
    bridge.g_create("New G", "2030-04-14T09:00:00", "2030-04-14T10:00:00")
    bridge.i_create("New I", "20300414T110000", "20300414T120000")
    bridge.sync_ok()
    assert len(bridge.g_live()) == 3 and len(bridge.i_resources()) == 3

    settings = bridge.app.settings
    bridge.app.close()
    restore_backup(backup, settings.db_path, box=bridge.app.maybe_box(), clock=bridge.app.clock)
    restored = reopen(bridge)
    with restored.app.db.tx():
        restored.app.repo.request_full_resync()
    restored.sync_ok()
    restored.sync_ok()
    assert summaries_google(restored) == ["New G", "New I", "Old G"]
    assert summaries_icloud(restored) == ["New G", "New I", "Old G"]
    assert {i.state for i in restored.items()} == {"synced"}


def test_large_backlogs_are_processed_in_bounded_batches(bridge: Bridge) -> None:
    bridge.app.settings.max_items_per_cycle = 4
    bridge.map()
    for n in range(10):
        bridge.g_create(f"Batch {n}", f"2030-04-{n + 15:02d}T09:00:00", f"2030-04-{n + 15:02d}T10:00:00")
    bridge.sync_ok()
    assert len(bridge.i_resources()) == 4
    bridge.sync_ok()
    bridge.sync_ok()
    assert len(bridge.i_resources()) == 10
    assert {i.state for i in bridge.items()} == {"synced"}


def test_server_without_sync_collection_uses_full_listing(bridge: Bridge) -> None:
    bridge.map()
    bridge.i_create("Listed", "20300420T090000", "20300420T100000")
    bridge.server.middleware.faults.append(
        DavFault(lambda m, p, b: m == "REPORT" and b"sync-collection" in b, "501 Not Implemented", remaining=20)
    )
    bridge.sync_ok()
    assert summaries_google(bridge) == ["Listed"]
    url, _ = bridge.i_by_summary("Listed")
    bridge.server.delete(url)
    bridge.sync_ok()
    assert summaries_google(bridge) == [], "deletion found by listing comparison, then verified directly"


def test_unsupported_icloud_resource_is_isolated(bridge: Bridge) -> None:
    bridge.map()
    bridge.i_create("Healthy", "20300421T090000", "20300421T100000")
    bridge.i_create(
        "Excluded by rule",
        "20300422T090000",
        "20300422T100000",
        extra="RRULE:FREQ=DAILY;COUNT=5\r\nEXRULE:FREQ=DAILY;INTERVAL=2;COUNT=2\r\n",
    )
    bridge.sync_ok()
    assert summaries_google(bridge) == ["Healthy"]
    states = sorted(i.state for i in bridge.items())
    assert states == ["synced", "unsupported"]
    (unsupported,) = bridge.items("unsupported")
    assert "EXRULE" in (unsupported.state_reason or "")
