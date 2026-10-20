"""The bridge's status as a versioned, machine-readable document.

``calendar-bridge status --json`` prints it and the daemon writes it to ``<data>/status.json``
after every cycle. The schema (``calendar-bridge.status/v1``) is the integration contract for
other systems; fields are only ever added, never renamed or removed, within v1.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

from calendar_bridge import __version__
from calendar_bridge.clock import from_iso, seconds_since
from calendar_bridge.credentials import GOOGLE_TOKEN, ICLOUD
from calendar_bridge.db.repo import ItemRow, Repo
from calendar_bridge.model.canonical import CanonicalEvent
from calendar_bridge.providers.google.convert import GoogleContext, google_to_canonical
from calendar_bridge.providers.icloud.convert import ICloudContext, icloud_to_canonical
from calendar_bridge.sync.context import EngineContext

SCHEMA = "calendar-bridge.status/v1"


def _auth(repo: Repo, provider: str, credential: str) -> dict[str, Any]:
    account = repo.get_account(provider)
    if credential not in repo.credential_names():
        return {"state": "missing", "account": None, "error": None, "since": None}
    if account is None:
        return {"state": "unverified", "account": None, "error": None, "since": None}
    return {
        "state": account["auth_state"],
        "account": account["display"] or account["identity"],
        "error": account["auth_error"],
        "since": account["auth_state_since"],
        "last_verified_at": account["last_verified_at"],
    }


def event_brief(repo: Repo, item: ItemRow, default_tz: str, include_titles: bool) -> dict[str, Any]:
    """Title and start of an item, from its baselines or, before its first sync, from the
    observed provider copy."""
    event: CanonicalEvent | None = None
    synced = item.google_synced_json or item.icloud_synced_json
    if synced:
        event = CanonicalEvent.loads(synced)
    elif item.google_event_id:
        master, instances = repo.google_family(item.mapping_id, item.google_event_id)
        if master is not None and master.status != "cancelled":
            event = google_to_canonical(master.raw, [i.raw for i in instances], GoogleContext(default_tz)).event
    elif item.icloud_href:
        row = repo.icloud_resource(item.mapping_id, item.icloud_href)
        if row is not None and row.ics:
            try:
                event = icloud_to_canonical(row.ics, ICloudContext(default_tz)).event
            except ValueError:
                event = None
    if event is None:
        return {}
    brief: dict[str, Any] = {"start": event.master.start.describe()}
    if include_titles:
        brief["title"] = event.master.summary or "(no title)"
    return brief


def build_status(ctx: EngineContext, *, include_titles: bool = True) -> dict[str, Any]:
    repo = ctx.repo
    now = ctx.now()
    settings = ctx.settings
    tz = ctx.default_tz
    heartbeat = repo.get_meta("heartbeat_at")
    last_ok = repo.get_meta("last_ok_cycle_at")
    status: dict[str, Any] = {
        "schema": SCHEMA,
        "version": __version__,
        "generated_at": repo.now(),
        "daemon": {
            "heartbeat_at": heartbeat,
            "heartbeat_age_seconds": seconds_since(heartbeat, now),
            "started_at": repo.get_meta("daemon_started_at"),
            "last_cycle_at": repo.get_meta("last_cycle_at"),
            "last_ok_cycle_at": last_ok,
            "sync_interval_seconds": settings.sync_interval,
        },
        "google": _auth(repo, "google", GOOGLE_TOKEN),
        "icloud": _auth(repo, "icloud", ICLOUD),
        "global_hold": repo.get_meta("global_hold"),
        "mappings": [],
    }
    since_24h = (now - dt.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    total_counts: dict[str, int] = {}
    for mapping in repo.mappings(("active", "pending_initial", "paused")):
        google_cal = repo.calendar(mapping.google_calendar_id)
        icloud_cal = repo.calendar(mapping.icloud_calendar_id)
        cp_g = repo.checkpoint(mapping.id, "google")
        cp_i = repo.checkpoint(mapping.id, "icloud")
        counts = repo.item_counts(mapping.id)
        for key, value in counts.items():
            total_counts[key] = total_counts.get(key, 0) + value
        reads = [cp_g.last_read_ok_at, cp_i.last_read_ok_at]
        lag = None if None in reads else max(seconds_since(r, now) or 0 for r in reads)
        pending = []
        for item in repo.items(mapping.id, "pending")[:20]:
            if item.origin == "google" and item.last_icloud_verified_at is None:
                waiting_for = "icloud"  # never confirmed in iCloud yet
            elif item.origin == "icloud" and item.last_google_verified_at is None:
                waiting_for = "google"
            else:
                waiting_for = "update"
            entry = {
                "item_id": item.id,
                "google_event_id": item.google_event_id,
                "waiting_for": waiting_for,
                "reason": item.state_reason,
            }
            entry.update(event_brief(repo, item, tz, include_titles))
            pending.append(entry)
        verified = []
        for row in repo.recent_verified(since_24h, "icloud"):
            if row["mapping_id"] != mapping.id:
                continue
            item = repo.item(row["id"])
            entry = {
                "item_id": item.id,
                "google_event_id": item.google_event_id,
                "verified_in_icloud_at": item.last_icloud_verified_at,
            }
            entry.update(event_brief(repo, item, tz, include_titles))
            verified.append(entry)
        status["mappings"].append(
            {
                "id": mapping.id,
                "name": mapping.name,
                "state": mapping.state,
                "google_calendar": {
                    "id": google_cal.remote_id,
                    "name": google_cal.label(),
                    "primary": google_cal.is_primary,
                },
                "icloud_calendar": {"name": icloud_cal.label()},
                "last_google_read_at": cp_g.last_read_ok_at,
                "last_icloud_read_at": cp_i.last_read_ok_at,
                "last_google_write_verified_at": cp_g.last_write_verified_at,
                "last_icloud_write_verified_at": cp_i.last_write_verified_at,
                "last_full_reconcile_at": mapping.last_full_reconcile_at,
                "lag_seconds": lag,
                "read_errors": {
                    "google": cp_g.last_error if cp_g.consecutive_failures else None,
                    "icloud": cp_i.last_error if cp_i.consecutive_failures else None,
                },
                "consecutive_read_failures": {"google": cp_g.consecutive_failures, "icloud": cp_i.consecutive_failures},
                "items": counts,
                "open_write_intents": len(repo.open_intents(mapping.id)),
                "oldest_open_write_intent_at": repo.oldest_open_intent_at(mapping.id),
                "skipped": repo.skipped_counts(mapping.id),
                "destructive_writes": "suspended" if mapping.destructive_hold else "allowed",
                "destructive_hold_reason": mapping.destructive_hold_reason,
                "pending": pending,
                "recently_verified_in_icloud": verified[:20],
            }
        )
    conflicts = repo.conflicts()
    status["conflicts"] = {
        "open": len(conflicts),
        "items": [
            {
                "id": c.id,
                "item_id": c.item_id,
                "kind": c.kind,
                "fields": c.groups,
                "since": c.created_at,
                "reason": c.reason,
            }
            for c in conflicts[:50]
        ],
    }
    status["totals"] = total_counts
    status["failures"] = {
        "retryable": total_counts.get("pending", 0),
        "permanent": total_counts.get("failed", 0) + total_counts.get("rejected", 0),
    }
    status["alerts"] = [dict(row) for row in repo.alerts("active")]
    status["overall"] = overall(status, settings.lag_alert_seconds)
    return status


def overall(status: dict[str, Any], lag_limit: int) -> str:
    """ok | degraded | error | not_configured"""
    mappings = [m for m in status["mappings"] if m["state"] == "active"]
    if not mappings:
        return "not_configured"
    if status["google"]["state"] not in ("ok",) or status["icloud"]["state"] not in ("ok",):
        return "error"
    if status["global_hold"] or any(m["destructive_writes"] == "suspended" for m in mappings):
        return "error"
    last_ok = status["daemon"]["last_ok_cycle_at"]
    if last_ok is None:
        return "degraded"
    generated = from_iso(status["generated_at"])
    if (generated - from_iso(last_ok)).total_seconds() > lag_limit:
        return "error"
    if status["conflicts"]["open"] or status["failures"]["permanent"]:
        return "degraded"
    if any(m["lag_seconds"] is None or m["lag_seconds"] > lag_limit for m in mappings):
        return "degraded"
    return "ok"


def write_status_file(path: Path, status: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(status, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    if os.name == "posix":
        tmp.chmod(0o600)
    tmp.replace(path)
