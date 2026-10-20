"""Typed data access for the bridge's persistent state."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Iterable
from typing import Any

from calendar_bridge.db.database import Database
from calendar_bridge.model.canonical import CanonicalEvent

ITEM_STATES = ("synced", "pending", "conflicted", "rejected", "unsupported", "failed", "deleted")


def _loads(text: str | None) -> Any:
    return None if text is None else json.loads(text)


def _dumps(value: Any) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True, ensure_ascii=False)


@dataclasses.dataclass
class CalendarRow:
    id: int
    provider: str
    remote_id: str
    display_name: str | None
    access: str
    writable: bool
    supports_events: bool
    is_primary: bool
    timezone: str | None
    role: str | None
    default_reminders: list[dict[str, Any]]
    last_discovered_at: str
    missing_since: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> CalendarRow:
        return cls(
            id=row["id"],
            provider=row["provider"],
            remote_id=row["remote_id"],
            display_name=row["display_name"],
            access=row["access"],
            writable=bool(row["writable"]),
            supports_events=bool(row["supports_events"]),
            is_primary=bool(row["is_primary"]),
            timezone=row["timezone"],
            role=row["role"],
            default_reminders=_loads(row["default_reminders_json"]) or [],
            last_discovered_at=row["last_discovered_at"],
            missing_since=row["missing_since"],
        )

    def label(self) -> str:
        return self.display_name or self.remote_id


@dataclasses.dataclass
class MappingRow:
    id: int
    name: str
    google_calendar_id: int
    icloud_calendar_id: int
    state: str
    history_days: int | None
    created_at: str
    activated_at: str | None
    destructive_hold: bool
    destructive_hold_reason: str | None
    destructive_hold_since: str | None
    last_cycle_ok_at: str | None
    last_full_reconcile_at: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> MappingRow:
        data = dict(row)
        data["destructive_hold"] = bool(data["destructive_hold"])
        return cls(**data)


@dataclasses.dataclass
class CheckpointRow:
    mapping_id: int
    provider: str
    sync_token: str | None
    ctag: str | None
    needs_full: bool
    last_full_at: str | None
    last_read_ok_at: str | None
    last_write_verified_at: str | None
    last_error: str | None
    last_error_at: str | None
    consecutive_failures: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> CheckpointRow:
        data = dict(row)
        data["needs_full"] = bool(data["needs_full"])
        return cls(**data)


@dataclasses.dataclass
class ItemRow:
    id: int
    mapping_id: int
    origin: str
    authority: str
    google_event_id: str | None
    google_ical_uid: str | None
    icloud_href: str | None
    icloud_uid: str | None
    icloud_detached_key: str | None
    state: str
    state_reason: str | None
    dirty: bool
    suppressed: str | None
    google_synced_json: str | None
    icloud_synced_json: str | None
    google_synced_fp: str | None
    icloud_synced_fp: str | None
    meeting: bool
    notes_json: str | None
    retry_count: int
    next_attempt_at: str | None
    last_error: str | None
    created_at: str
    updated_at: str
    last_synced_at: str | None
    last_google_verified_at: str | None
    last_icloud_verified_at: str | None
    deleted_at: str | None
    deleted_by: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ItemRow:
        data = dict(row)
        data["dirty"] = bool(data["dirty"])
        data["meeting"] = bool(data["meeting"])
        return cls(**data)

    @property
    def google_base(self) -> CanonicalEvent | None:
        return CanonicalEvent.loads(self.google_synced_json) if self.google_synced_json else None

    @property
    def icloud_base(self) -> CanonicalEvent | None:
        return CanonicalEvent.loads(self.icloud_synced_json) if self.icloud_synced_json else None

    @property
    def notes(self) -> list[str]:
        return list(_loads(self.notes_json) or [])


@dataclasses.dataclass
class GoogleEventRow:
    mapping_id: int
    event_id: str
    master_id: str | None
    ical_uid: str | None
    recurrence_key: str | None
    status: str
    etag: str | None
    updated: str | None
    raw: dict[str, Any]
    observed_at: str
    skip_reason: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> GoogleEventRow:
        data = dict(row)
        data["raw"] = json.loads(data.pop("raw_json"))
        return cls(**data)


@dataclasses.dataclass
class ICloudResourceRow:
    mapping_id: int
    href: str
    uid: str | None
    etag: str | None
    ics: str | None
    status: str
    observed_at: str
    deleted_verified_at: str | None
    skip_reason: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ICloudResourceRow:
        return cls(**dict(row))


@dataclasses.dataclass
class IntentRow:
    id: int
    mapping_id: int
    item_id: int | None
    provider: str
    op: str
    target: str
    precondition: str | None
    payload: dict[str, Any] | None
    state: str
    attempts: int
    last_error: str | None
    result_etag: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> IntentRow:
        data = dict(row)
        data["payload"] = _loads(data.pop("payload_json"))
        return cls(**data)


@dataclasses.dataclass
class ConflictRow:
    id: int
    mapping_id: int
    item_id: int | None
    kind: str
    groups: list[str]
    google_json: str | None
    icloud_json: str | None
    google_base_json: str | None
    icloud_base_json: str | None
    candidates: list[dict[str, Any]]
    reason: str | None
    state: str
    resolution: str | None
    resolution_data: dict[str, Any] | None
    created_at: str
    resolved_at: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ConflictRow:
        data = dict(row)
        data["groups"] = _loads(data.pop("groups_json")) or []
        data["candidates"] = _loads(data.pop("candidates_json")) or []
        data["resolution_data"] = _loads(data.pop("resolution_json"))
        return cls(**data)


class Repo:
    """All SQL lives here; callers group calls into transactions with ``db.tx()``."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def now(self) -> str:
        return self.db.now()

    # ------------------------------------------------------------ meta

    def get_meta(self, key: str) -> str | None:
        value = self.db.scalar("SELECT value FROM meta WHERE key = ?", (key,))
        return None if value is None else str(value)

    def set_meta(self, key: str, value: str | None) -> None:
        self.db.execute(
            "INSERT INTO meta(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, self.now()),
        )

    def get_meta_json(self, key: str) -> Any:
        return _loads(self.get_meta(key))

    def set_meta_json(self, key: str, value: Any) -> None:
        self.set_meta(key, _dumps(value))

    # ------------------------------------------------------------ credentials

    def get_credential(self, name: str) -> str | None:
        value = self.db.scalar("SELECT ciphertext FROM credentials WHERE name = ?", (name,))
        return None if value is None else str(value)

    def put_credential(self, name: str, ciphertext: str) -> None:
        self.db.execute(
            "INSERT INTO credentials(name, ciphertext, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET ciphertext = excluded.ciphertext, "
            "updated_at = excluded.updated_at",
            (name, ciphertext, self.now()),
        )

    def delete_credential(self, name: str) -> None:
        self.db.execute("DELETE FROM credentials WHERE name = ?", (name,))

    def credential_names(self) -> list[str]:
        return [r[0] for r in self.db.query("SELECT name FROM credentials ORDER BY name")]

    # ------------------------------------------------------------ accounts

    def get_account(self, provider: str) -> sqlite3.Row | None:
        return self.db.query_one("SELECT * FROM accounts WHERE provider = ?", (provider,))

    def set_account(self, provider: str, identity: str, display: str | None) -> None:
        now = self.now()
        self.db.execute(
            "INSERT INTO accounts(provider, identity, display, auth_state, auth_state_since, "
            "last_verified_at, created_at) VALUES (?, ?, ?, 'ok', ?, ?, ?) "
            "ON CONFLICT(provider) DO UPDATE SET identity = excluded.identity, "
            "display = excluded.display, auth_state = 'ok', auth_error = NULL, "
            "auth_state_since = excluded.auth_state_since, last_verified_at = excluded.last_verified_at",
            (provider, identity, display, now, now, now),
        )

    def set_auth_state(self, provider: str, state: str, error: str | None = None) -> bool:
        """Returns True if the state changed."""
        row = self.get_account(provider)
        if row is None:
            return False
        if row["auth_state"] == state and (state == "ok" or row["auth_error"] == error):
            if state == "ok":
                self.db.execute("UPDATE accounts SET last_verified_at = ? WHERE provider = ?", (self.now(), provider))
            return False
        self.db.execute(
            "UPDATE accounts SET auth_state = ?, auth_error = ?, auth_state_since = ?, "
            "last_verified_at = CASE WHEN ? = 'ok' THEN ? ELSE last_verified_at END "
            "WHERE provider = ?",
            (state, error, self.now(), state, self.now(), provider),
        )
        return True

    # ------------------------------------------------------------ calendars

    def upsert_calendar(
        self,
        *,
        provider: str,
        remote_id: str,
        display_name: str | None,
        access: str,
        writable: bool,
        supports_events: bool,
        is_primary: bool,
        timezone: str | None,
        default_reminders: list[dict[str, Any]] | None = None,
    ) -> int:
        now = self.now()
        self.db.execute(
            "INSERT INTO calendars(provider, remote_id, display_name, access, writable, "
            "supports_events, is_primary, timezone, default_reminders_json, last_discovered_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(provider, remote_id) DO UPDATE SET display_name = excluded.display_name, "
            "access = excluded.access, writable = excluded.writable, "
            "supports_events = excluded.supports_events, is_primary = excluded.is_primary, "
            "timezone = excluded.timezone, default_reminders_json = excluded.default_reminders_json, "
            "last_discovered_at = excluded.last_discovered_at, missing_since = NULL",
            (
                provider,
                remote_id,
                display_name,
                access,
                int(writable),
                int(supports_events),
                int(is_primary),
                timezone,
                _dumps(default_reminders or []),
                now,
            ),
        )
        return int(
            self.db.scalar("SELECT id FROM calendars WHERE provider = ? AND remote_id = ?", (provider, remote_id))
        )

    def mark_calendars_missing(self, provider: str, seen_remote_ids: Iterable[str]) -> None:
        seen = list(seen_remote_ids)
        placeholders = ",".join("?" for _ in seen) or "''"
        self.db.execute(
            f"UPDATE calendars SET missing_since = COALESCE(missing_since, ?) "  # noqa: S608
            f"WHERE provider = ? AND remote_id NOT IN ({placeholders})",
            (self.now(), provider, *seen),
        )

    def set_calendar_role(self, calendar_id: int, role: str | None) -> None:
        self.db.execute("UPDATE calendars SET role = ? WHERE id = ?", (role, calendar_id))

    def calendars(self, provider: str | None = None) -> list[CalendarRow]:
        if provider:
            rows = self.db.query(
                "SELECT * FROM calendars WHERE provider = ? ORDER BY is_primary DESC, display_name",
                (provider,),
            )
        else:
            rows = self.db.query("SELECT * FROM calendars ORDER BY provider, is_primary DESC, display_name")
        return [CalendarRow.from_row(r) for r in rows]

    def calendar(self, calendar_id: int) -> CalendarRow:
        row = self.db.query_one("SELECT * FROM calendars WHERE id = ?", (calendar_id,))
        if row is None:
            raise KeyError(f"calendar {calendar_id} not found")
        return CalendarRow.from_row(row)

    def calendar_by_remote(self, provider: str, remote_id: str) -> CalendarRow | None:
        row = self.db.query_one("SELECT * FROM calendars WHERE provider = ? AND remote_id = ?", (provider, remote_id))
        return CalendarRow.from_row(row) if row else None

    def status_calendar(self) -> CalendarRow | None:
        row = self.db.query_one(
            "SELECT * FROM calendars WHERE role = 'status' AND provider = 'google' "
            "ORDER BY missing_since IS NOT NULL, id DESC"
        )
        return CalendarRow.from_row(row) if row else None

    # ------------------------------------------------------------ mappings

    def create_mapping(
        self, *, name: str, google_calendar_id: int, icloud_calendar_id: int, history_days: int | None
    ) -> int:
        cursor = self.db.execute(
            "INSERT INTO mappings(name, google_calendar_id, icloud_calendar_id, state, history_days, "
            "created_at) VALUES (?, ?, ?, 'pending_initial', ?, ?)",
            (name, google_calendar_id, icloud_calendar_id, history_days, self.now()),
        )
        mapping_id = int(cursor.lastrowid or 0)
        for provider in ("google", "icloud"):
            self.db.execute(
                "INSERT INTO checkpoints(mapping_id, provider, needs_full) VALUES (?, ?, 1)",
                (mapping_id, provider),
            )
        return mapping_id

    def mappings(self, states: Iterable[str] | None = None) -> list[MappingRow]:
        if states is None:
            rows = self.db.query("SELECT * FROM mappings ORDER BY id")
        else:
            wanted = list(states)
            placeholders = ",".join("?" for _ in wanted)
            rows = self.db.query(
                f"SELECT * FROM mappings WHERE state IN ({placeholders}) ORDER BY id",  # noqa: S608
                wanted,
            )
        return [MappingRow.from_row(r) for r in rows]

    def mapping(self, mapping_id: int) -> MappingRow:
        row = self.db.query_one("SELECT * FROM mappings WHERE id = ?", (mapping_id,))
        if row is None:
            raise KeyError(f"mapping {mapping_id} not found")
        return MappingRow.from_row(row)

    def mapping_by_name(self, name: str) -> MappingRow | None:
        row = self.db.query_one(
            "SELECT * FROM mappings WHERE name = ? AND state != 'removed' ORDER BY id DESC", (name,)
        )
        return MappingRow.from_row(row) if row else None

    def update_mapping(self, mapping_id: int, **fields: Any) -> None:
        self._update("mappings", "id", mapping_id, fields)

    def set_hold(self, mapping_id: int, reason: str) -> bool:
        mapping = self.mapping(mapping_id)
        if mapping.destructive_hold:
            return False
        self.update_mapping(
            mapping_id,
            destructive_hold=1,
            destructive_hold_reason=reason,
            destructive_hold_since=self.now(),
        )
        return True

    def clear_hold(self, mapping_id: int) -> None:
        self.update_mapping(mapping_id, destructive_hold=0, destructive_hold_reason=None, destructive_hold_since=None)

    # ------------------------------------------------------------ checkpoints

    def checkpoint(self, mapping_id: int, provider: str) -> CheckpointRow:
        row = self.db.query_one(
            "SELECT * FROM checkpoints WHERE mapping_id = ? AND provider = ?", (mapping_id, provider)
        )
        if row is None:
            raise KeyError(f"checkpoint {mapping_id}/{provider} missing")
        return CheckpointRow.from_row(row)

    def update_checkpoint(self, mapping_id: int, provider: str, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(
            f"UPDATE checkpoints SET {assignments} WHERE mapping_id = ? AND provider = ?",  # noqa: S608
            (*fields.values(), mapping_id, provider),
        )

    def checkpoint_ok(self, mapping_id: int, provider: str, **fields: Any) -> None:
        self.update_checkpoint(
            mapping_id,
            provider,
            last_read_ok_at=self.now(),
            consecutive_failures=0,
            last_error=None,
            **fields,
        )

    def checkpoint_failed(self, mapping_id: int, provider: str, error: str) -> None:
        self.db.execute(
            "UPDATE checkpoints SET last_error = ?, last_error_at = ?, "
            "consecutive_failures = consecutive_failures + 1 WHERE mapping_id = ? AND provider = ?",
            (error, self.now(), mapping_id, provider),
        )

    def request_full_resync(self, mapping_id: int | None = None) -> None:
        if mapping_id is None:
            self.db.execute("UPDATE checkpoints SET needs_full = 1")
        else:
            self.db.execute("UPDATE checkpoints SET needs_full = 1 WHERE mapping_id = ?", (mapping_id,))

    # ------------------------------------------------------------ google observations

    def google_event(self, mapping_id: int, event_id: str) -> GoogleEventRow | None:
        row = self.db.query_one(
            "SELECT * FROM google_events WHERE mapping_id = ? AND event_id = ?", (mapping_id, event_id)
        )
        return GoogleEventRow.from_row(row) if row else None

    def upsert_google_event(
        self,
        mapping_id: int,
        raw: dict[str, Any],
        *,
        master_id: str | None,
        recurrence_key: str | None,
    ) -> bool:
        """Store an observed Google event. Returns True if it differs from what was stored."""
        event_id = raw["id"]
        etag = raw.get("etag")
        status = raw.get("status", "confirmed")
        existing = self.db.query_one(
            "SELECT etag, status, raw_json FROM google_events WHERE mapping_id = ? AND event_id = ?",
            (mapping_id, event_id),
        )
        raw_json = json.dumps(raw, sort_keys=True, ensure_ascii=False)
        if (
            existing is not None
            and existing["etag"] == etag
            and existing["status"] == status
            and existing["raw_json"] == raw_json
        ):
            return False
        self.db.execute(
            "INSERT INTO google_events(mapping_id, event_id, master_id, ical_uid, recurrence_key, "
            "status, etag, updated, raw_json, observed_at, skip_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL) "
            "ON CONFLICT(mapping_id, event_id) DO UPDATE SET "
            "master_id = COALESCE(excluded.master_id, google_events.master_id), "
            "ical_uid = COALESCE(excluded.ical_uid, google_events.ical_uid), "
            "recurrence_key = COALESCE(excluded.recurrence_key, google_events.recurrence_key), "
            "status = excluded.status, etag = excluded.etag, updated = excluded.updated, "
            "raw_json = excluded.raw_json, observed_at = excluded.observed_at, "
            "skip_reason = CASE WHEN google_events.skip_reason = 'ambiguous_match' "
            "THEN 'ambiguous_match' ELSE NULL END",
            (
                mapping_id,
                event_id,
                master_id,
                raw.get("iCalUID"),
                recurrence_key,
                status,
                etag,
                raw.get("updated"),
                raw_json,
                self.now(),
            ),
        )
        return True

    def google_family(
        self, mapping_id: int, master_event_id: str
    ) -> tuple[GoogleEventRow | None, list[GoogleEventRow]]:
        master = self.google_event(mapping_id, master_event_id)
        rows = self.db.query(
            "SELECT * FROM google_events WHERE mapping_id = ? AND master_id = ? ORDER BY recurrence_key",
            (mapping_id, master_event_id),
        )
        return master, [GoogleEventRow.from_row(r) for r in rows]

    def google_event_ids(self, mapping_id: int, *, include_cancelled: bool = False) -> set[str]:
        sql = "SELECT event_id FROM google_events WHERE mapping_id = ?"
        if not include_cancelled:
            sql += " AND status != 'cancelled'"
        return {r[0] for r in self.db.query(sql, (mapping_id,))}

    def unlinked_google_roots(self, mapping_id: int) -> list[GoogleEventRow]:
        """Live Google events not attached to any item: masters/singles, plus exception instances
        whose master is not visible (detached occurrences)."""
        rows = self.db.query(
            "SELECT g.* FROM google_events g WHERE g.mapping_id = ? AND g.status != 'cancelled' "
            "AND g.skip_reason IS NULL "
            "AND (g.master_id IS NULL OR NOT EXISTS (SELECT 1 FROM google_events m WHERE "
            "     m.mapping_id = g.mapping_id AND m.event_id = g.master_id)) "
            "AND NOT EXISTS (SELECT 1 FROM items i WHERE i.mapping_id = g.mapping_id "
            "     AND i.google_event_id = g.event_id AND i.state != 'deleted') "
            "ORDER BY g.event_id",
            (mapping_id,),
        )
        return [GoogleEventRow.from_row(r) for r in rows]

    def google_events_by_uid(self, mapping_id: int, ical_uid: str) -> list[GoogleEventRow]:
        rows = self.db.query(
            "SELECT * FROM google_events WHERE mapping_id = ? AND ical_uid = ? AND master_id IS NULL",
            (mapping_id, ical_uid),
        )
        return [GoogleEventRow.from_row(r) for r in rows]

    def set_google_skip(self, mapping_id: int, event_id: str, reason: str | None) -> None:
        self.db.execute(
            "UPDATE google_events SET skip_reason = ? WHERE mapping_id = ? AND event_id = ?",
            (reason, mapping_id, event_id),
        )

    def mark_google_cancelled(self, mapping_id: int, event_id: str) -> bool:
        cursor = self.db.execute(
            "UPDATE google_events SET status = 'cancelled', observed_at = ? "
            "WHERE mapping_id = ? AND event_id = ? AND status != 'cancelled'",
            (self.now(), mapping_id, event_id),
        )
        return cursor.rowcount > 0

    # ------------------------------------------------------------ icloud observations

    def icloud_resource(self, mapping_id: int, href: str) -> ICloudResourceRow | None:
        row = self.db.query_one("SELECT * FROM icloud_resources WHERE mapping_id = ? AND href = ?", (mapping_id, href))
        return ICloudResourceRow.from_row(row) if row else None

    def icloud_etags(self, mapping_id: int) -> dict[str, str | None]:
        return {
            r["href"]: r["etag"]
            for r in self.db.query(
                "SELECT href, etag FROM icloud_resources WHERE mapping_id = ? AND status = 'present'",
                (mapping_id,),
            )
        }

    def upsert_icloud_resource(
        self, mapping_id: int, href: str, *, uid: str | None, etag: str | None, ics: str
    ) -> bool:
        existing = self.db.query_one(
            "SELECT etag, status, ics FROM icloud_resources WHERE mapping_id = ? AND href = ?",
            (mapping_id, href),
        )
        if (
            existing is not None
            and existing["status"] == "present"
            and existing["etag"] == etag
            and existing["ics"] == ics
        ):
            return False
        self.db.execute(
            "INSERT INTO icloud_resources(mapping_id, href, uid, etag, ics, status, observed_at) "
            "VALUES (?, ?, ?, ?, ?, 'present', ?) "
            "ON CONFLICT(mapping_id, href) DO UPDATE SET uid = excluded.uid, etag = excluded.etag, "
            "ics = excluded.ics, status = 'present', observed_at = excluded.observed_at, "
            "deleted_verified_at = NULL, "
            "skip_reason = CASE WHEN icloud_resources.skip_reason = 'ambiguous_match' "
            "THEN 'ambiguous_match' ELSE NULL END",
            (mapping_id, href, uid, etag, ics, self.now()),
        )
        return True

    def mark_icloud_missing(self, mapping_id: int, href: str) -> None:
        self.db.execute(
            "UPDATE icloud_resources SET status = 'unverified_missing', observed_at = ? "
            "WHERE mapping_id = ? AND href = ? AND status = 'present'",
            (self.now(), mapping_id, href),
        )

    def mark_icloud_deleted(self, mapping_id: int, href: str) -> bool:
        now = self.now()
        cursor = self.db.execute(
            "UPDATE icloud_resources SET status = 'deleted', deleted_verified_at = ?, observed_at = ? "
            "WHERE mapping_id = ? AND href = ? AND status != 'deleted'",
            (now, now, mapping_id, href),
        )
        return cursor.rowcount > 0

    def icloud_unverified(self, mapping_id: int) -> list[str]:
        return [
            r[0]
            for r in self.db.query(
                "SELECT href FROM icloud_resources WHERE mapping_id = ? AND status = 'unverified_missing'",
                (mapping_id,),
            )
        ]

    def icloud_present_hrefs(self, mapping_id: int) -> set[str]:
        return {
            r[0]
            for r in self.db.query(
                "SELECT href FROM icloud_resources WHERE mapping_id = ? AND status = 'present'",
                (mapping_id,),
            )
        }

    def unlinked_icloud(self, mapping_id: int) -> list[ICloudResourceRow]:
        rows = self.db.query(
            "SELECT r.* FROM icloud_resources r WHERE r.mapping_id = ? AND r.status = 'present' "
            "AND r.skip_reason IS NULL AND NOT EXISTS (SELECT 1 FROM items i WHERE "
            "i.mapping_id = r.mapping_id AND i.icloud_href = r.href AND i.state != 'deleted') "
            "ORDER BY r.href",
            (mapping_id,),
        )
        return [ICloudResourceRow.from_row(r) for r in rows]

    def icloud_by_uid(self, mapping_id: int, uid: str) -> list[ICloudResourceRow]:
        rows = self.db.query(
            "SELECT * FROM icloud_resources WHERE mapping_id = ? AND uid = ? AND status = 'present'",
            (mapping_id, uid),
        )
        return [ICloudResourceRow.from_row(r) for r in rows]

    def set_icloud_skip(self, mapping_id: int, href: str, reason: str | None) -> None:
        self.db.execute(
            "UPDATE icloud_resources SET skip_reason = ? WHERE mapping_id = ? AND href = ?",
            (reason, mapping_id, href),
        )

    def skipped_counts(self, mapping_id: int) -> dict[str, int]:
        counts: dict[str, int] = {}
        for table, provider in (("google_events", "google"), ("icloud_resources", "icloud")):
            for row in self.db.query(
                f"SELECT skip_reason, COUNT(*) FROM {table} WHERE mapping_id = ? "  # noqa: S608
                "AND skip_reason IS NOT NULL GROUP BY skip_reason",
                (mapping_id,),
            ):
                counts[f"{provider}:{row[0]}"] = row[1]
        return counts

    # ------------------------------------------------------------ items

    def create_item(self, *, mapping_id: int, origin: str, authority: str, **fields: Any) -> int:
        now = self.now()
        columns = {
            "mapping_id": mapping_id,
            "origin": origin,
            "authority": authority,
            "state": fields.pop("state", "pending"),
            "dirty": 1,
            "created_at": now,
            "updated_at": now,
            **fields,
        }
        names = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        cursor = self.db.execute(
            f"INSERT INTO items({names}) VALUES ({placeholders})",  # noqa: S608
            tuple(columns.values()),
        )
        return int(cursor.lastrowid or 0)

    def item(self, item_id: int) -> ItemRow:
        row = self.db.query_one("SELECT * FROM items WHERE id = ?", (item_id,))
        if row is None:
            raise KeyError(f"item {item_id} not found")
        return ItemRow.from_row(row)

    def update_item(self, item_id: int, **fields: Any) -> None:
        fields.setdefault("updated_at", self.now())
        self._update("items", "id", item_id, fields)

    def item_by_google(self, mapping_id: int, event_id: str) -> ItemRow | None:
        row = self.db.query_one(
            "SELECT * FROM items WHERE mapping_id = ? AND google_event_id = ? AND state != 'deleted'",
            (mapping_id, event_id),
        )
        return ItemRow.from_row(row) if row else None

    def item_by_icloud(self, mapping_id: int, href: str) -> ItemRow | None:
        row = self.db.query_one(
            "SELECT * FROM items WHERE mapping_id = ? AND icloud_href = ? AND state != 'deleted'",
            (mapping_id, href),
        )
        return ItemRow.from_row(row) if row else None

    def items_by_uid(self, mapping_id: int, uid: str) -> list[ItemRow]:
        rows = self.db.query(
            "SELECT * FROM items WHERE mapping_id = ? AND state != 'deleted' "
            "AND (icloud_uid = ? OR google_ical_uid = ?)",
            (mapping_id, uid, uid),
        )
        return [ItemRow.from_row(r) for r in rows]

    def mark_dirty_google(self, mapping_id: int, event_id: str, master_id: str | None) -> None:
        root = master_id or event_id
        self.db.execute(
            "UPDATE items SET dirty = 1 WHERE mapping_id = ? AND state != 'deleted' AND google_event_id IN (?, ?)",
            (mapping_id, root, event_id),
        )

    def mark_dirty_icloud(self, mapping_id: int, href: str) -> None:
        self.db.execute(
            "UPDATE items SET dirty = 1 WHERE mapping_id = ? AND icloud_href = ? AND state != 'deleted'",
            (mapping_id, href),
        )

    def mark_all_dirty(self, mapping_id: int) -> None:
        self.db.execute("UPDATE items SET dirty = 1 WHERE mapping_id = ? AND state != 'deleted'", (mapping_id,))

    def items_to_process(self, mapping_id: int, now: str) -> list[ItemRow]:
        rows = self.db.query(
            "SELECT * FROM items WHERE mapping_id = ? AND state != 'deleted' AND ("
            " dirty = 1"
            " OR (state = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?))"
            " OR id IN (SELECT item_id FROM conflicts WHERE state = 'resolving')"
            ") ORDER BY id",
            (mapping_id, now),
        )
        return [ItemRow.from_row(r) for r in rows]

    def items(self, mapping_id: int | None = None, state: str | None = None) -> list[ItemRow]:
        sql = "SELECT * FROM items WHERE 1 = 1"
        params: list[Any] = []
        if mapping_id is not None:
            sql += " AND mapping_id = ?"
            params.append(mapping_id)
        if state is not None:
            sql += " AND state = ?"
            params.append(state)
        return [ItemRow.from_row(r) for r in self.db.query(sql + " ORDER BY id", params)]

    def item_counts(self, mapping_id: int) -> dict[str, int]:
        counts = dict.fromkeys(ITEM_STATES, 0)
        for row in self.db.query(
            "SELECT state, COUNT(*) FROM items WHERE mapping_id = ? GROUP BY state", (mapping_id,)
        ):
            counts[row[0]] = row[1]
        return counts

    def live_item_count(self, mapping_id: int) -> int:
        return int(
            self.db.scalar("SELECT COUNT(*) FROM items WHERE mapping_id = ? AND state != 'deleted'", (mapping_id,))
        )

    def prune_history(self, before: str, *, audit_before: str) -> int:
        """Remove what is no longer needed once tombstones expire: observations of deleted events
        that no live item refers to (cancelled occurrences of a live series are kept), finished
        journal entries, settled conflicts and cleared alerts older than ``before``, and audit
        entries older than ``audit_before``. Returns the number of rows removed."""
        statements = (
            (
                "DELETE FROM google_events WHERE status = 'cancelled' AND observed_at < ? AND NOT EXISTS ("
                "SELECT 1 FROM items i WHERE i.mapping_id = google_events.mapping_id AND i.state != 'deleted' "
                "AND i.google_event_id IN (google_events.event_id, google_events.master_id))",
                before,
            ),
            (
                "DELETE FROM icloud_resources WHERE status = 'deleted' AND observed_at < ? AND NOT EXISTS ("
                "SELECT 1 FROM items i WHERE i.mapping_id = icloud_resources.mapping_id AND i.state != 'deleted' "
                "AND i.icloud_href = icloud_resources.href)",
                before,
            ),
            ("DELETE FROM write_intents WHERE state IN ('verified', 'failed', 'abandoned') AND updated_at < ?", before),
            (
                "DELETE FROM conflicts WHERE state IN ('resolved', 'superseded') "
                "AND COALESCE(resolved_at, created_at) < ?",
                before,
            ),
            ("DELETE FROM alerts WHERE state = 'cleared' AND cleared_at < ?", before),
            ("DELETE FROM audit_log WHERE at < ?", audit_before),
        )
        return sum(self.db.execute(sql, (cutoff,)).rowcount for sql, cutoff in statements)

    def oldest_open_intent_at(self, mapping_id: int) -> str | None:
        value = self.db.scalar(
            "SELECT MIN(created_at) FROM write_intents WHERE mapping_id = ? AND state IN ('pending', 'applied')",
            (mapping_id,),
        )
        return None if value is None else str(value)

    def requeue_failed(self, max_attempts: int) -> int:
        """Give failed items one more attempt: the cause (an outage, a provider bug, a bridge bug
        fixed by an upgrade) may have cleared since. One attempt only, so that a failure that
        always recurs costs one try a day, not a new series of retries."""
        cursor = self.db.execute(
            "UPDATE items SET state = 'pending', dirty = 1, retry_count = ?, next_attempt_at = NULL, "
            "updated_at = ? WHERE state = 'failed'",
            (max(0, max_attempts - 1), self.now()),
        )
        return cursor.rowcount

    def purge_tombstones(self, older_than: str) -> int:
        stale = [
            r[0]
            for r in self.db.query("SELECT id FROM items WHERE state = 'deleted' AND deleted_at < ?", (older_than,))
        ]
        for item_id in stale:
            self.db.execute("UPDATE write_intents SET item_id = NULL WHERE item_id = ?", (item_id,))
            self.db.execute("UPDATE conflicts SET item_id = NULL WHERE item_id = ?", (item_id,))
            self.db.execute("DELETE FROM items WHERE id = ?", (item_id,))
        return len(stale)

    # ------------------------------------------------------------ write intents

    def create_intent(
        self,
        *,
        mapping_id: int,
        item_id: int | None,
        provider: str,
        op: str,
        target: str,
        precondition: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        now = self.now()
        cursor = self.db.execute(
            "INSERT INTO write_intents(mapping_id, item_id, provider, op, target, precondition, "
            "payload_json, state, attempts, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)",
            (mapping_id, item_id, provider, op, target, precondition, _dumps(payload), now, now),
        )
        return int(cursor.lastrowid or 0)

    def update_intent(self, intent_id: int, **fields: Any) -> None:
        fields.setdefault("updated_at", self.now())
        self._update("write_intents", "id", intent_id, fields)

    def record_attempt(
        self, intent_id: int, *, state: str | None = None, error: str | None = None, etag: str | None = None
    ) -> None:
        self.db.execute(
            "UPDATE write_intents SET attempts = attempts + 1, updated_at = ?, "
            "state = COALESCE(?, state), last_error = ?, result_etag = COALESCE(?, result_etag) "
            "WHERE id = ?",
            (self.now(), state, error, etag, intent_id),
        )

    def close_intents(self, intent_ids: Iterable[int], state: str) -> None:
        for intent_id in intent_ids:
            self.db.execute(
                "UPDATE write_intents SET state = ?, updated_at = ? WHERE id = ? AND state IN ('pending', 'applied')",
                (state, self.now(), intent_id),
            )

    def open_intents(self, mapping_id: int) -> list[IntentRow]:
        rows = self.db.query(
            "SELECT * FROM write_intents WHERE mapping_id = ? AND state IN ('pending', 'applied') ORDER BY id",
            (mapping_id,),
        )
        return [IntentRow.from_row(r) for r in rows]

    def open_intent_targets(self, mapping_id: int, provider: str) -> set[str]:
        return {
            r[0]
            for r in self.db.query(
                "SELECT target FROM write_intents WHERE mapping_id = ? AND provider = ? "
                "AND op = 'create' AND state IN ('pending', 'applied')",
                (mapping_id, provider),
            )
        }

    def intents_for_item(self, item_id: int, limit: int = 20) -> list[IntentRow]:
        rows = self.db.query("SELECT * FROM write_intents WHERE item_id = ? ORDER BY id DESC LIMIT ?", (item_id, limit))
        return [IntentRow.from_row(r) for r in rows]

    # ------------------------------------------------------------ conflicts

    def create_conflict(
        self,
        *,
        mapping_id: int,
        item_id: int | None,
        kind: str,
        groups: Iterable[str],
        google: CanonicalEvent | None,
        icloud: CanonicalEvent | None,
        google_base: CanonicalEvent | None,
        icloud_base: CanonicalEvent | None,
        reason: str,
        candidates: list[dict[str, Any]] | None = None,
    ) -> int:
        cursor = self.db.execute(
            "INSERT INTO conflicts(mapping_id, item_id, kind, groups_json, google_json, icloud_json, "
            "google_base_json, icloud_base_json, candidates_json, reason, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
            (
                mapping_id,
                item_id,
                kind,
                _dumps(sorted(groups)),
                google.dumps() if google else None,
                icloud.dumps() if icloud else None,
                google_base.dumps() if google_base else None,
                icloud_base.dumps() if icloud_base else None,
                _dumps(candidates or []),
                reason,
                self.now(),
            ),
        )
        return int(cursor.lastrowid or 0)

    def conflict(self, conflict_id: int) -> ConflictRow:
        row = self.db.query_one("SELECT * FROM conflicts WHERE id = ?", (conflict_id,))
        if row is None:
            raise KeyError(f"conflict {conflict_id} not found")
        return ConflictRow.from_row(row)

    def conflicts(self, states: Iterable[str] = ("open", "resolving")) -> list[ConflictRow]:
        wanted = list(states)
        placeholders = ",".join("?" for _ in wanted)
        rows = self.db.query(
            f"SELECT * FROM conflicts WHERE state IN ({placeholders}) ORDER BY id",  # noqa: S608
            wanted,
        )
        return [ConflictRow.from_row(r) for r in rows]

    def active_conflict_for_item(self, item_id: int) -> ConflictRow | None:
        row = self.db.query_one(
            "SELECT * FROM conflicts WHERE item_id = ? AND state IN ('open', 'resolving') ORDER BY id DESC",
            (item_id,),
        )
        return ConflictRow.from_row(row) if row else None

    def update_conflict(self, conflict_id: int, **fields: Any) -> None:
        if "resolution_data" in fields:
            fields["resolution_json"] = _dumps(fields.pop("resolution_data"))
        if "groups" in fields:
            fields["groups_json"] = _dumps(sorted(fields.pop("groups")))
        self._update("conflicts", "id", conflict_id, fields)

    def open_conflict_count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM conflicts WHERE state IN ('open', 'resolving')"))

    # ------------------------------------------------------------ runs, audit, alerts

    def start_run(self, kind: str) -> int:
        cursor = self.db.execute("INSERT INTO sync_runs(kind, started_at) VALUES (?, ?)", (kind, self.now()))
        return int(cursor.lastrowid or 0)

    def finish_run(self, run_id: int, *, ok: bool, stats: dict[str, Any], error: str | None) -> None:
        self.db.execute(
            "UPDATE sync_runs SET finished_at = ?, ok = ?, stats_json = ?, error = ? WHERE id = ?",
            (self.now(), int(ok), _dumps(stats), error, run_id),
        )

    def last_runs(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.db.query("SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (limit,))

    def last_ok_run(self) -> sqlite3.Row | None:
        return self.db.query_one("SELECT * FROM sync_runs WHERE ok = 1 ORDER BY id DESC LIMIT 1")

    def prune_runs(self, keep: int = 5000) -> None:
        self.db.execute("DELETE FROM sync_runs WHERE id <= (SELECT MAX(id) FROM sync_runs) - ?", (keep,))

    def audit(
        self,
        action: str,
        *,
        mapping_id: int | None = None,
        item_id: int | None = None,
        provider: str | None = None,
        detail: str | None = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO audit_log(at, mapping_id, item_id, provider, action, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (self.now(), mapping_id, item_id, provider, action, detail),
        )

    def audit_entries(
        self,
        limit: int = 50,
        item_id: int | None = None,
        *,
        mapping_id: int | None = None,
        exclude: Iterable[str] = (),
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM audit_log WHERE 1 = 1"
        params: list[Any] = []
        if item_id is not None:
            sql += " AND item_id = ?"
            params.append(item_id)
        if mapping_id is not None:
            sql += " AND mapping_id = ?"
            params.append(mapping_id)
        skipped = list(exclude)
        if skipped:
            sql += f" AND action NOT IN ({','.join('?' for _ in skipped)})"
            params += skipped
        return self.db.query(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))

    def restored_copies(self, item_id: int) -> list[int]:
        """Live items re-created from the tombstone ``item_id`` (``tombstones restore``)."""
        rows = self.db.query(
            "SELECT a.item_id FROM audit_log a JOIN items i ON i.id = a.item_id "
            "WHERE a.action = 'tombstone_restore' AND a.detail = ? AND i.state != 'deleted' ORDER BY a.id",
            (f"from item {item_id}",),
        )
        return [r[0] for r in rows]

    def recent_verified(self, since: str, provider: str, limit: int = 20) -> list[sqlite3.Row]:
        column = "last_icloud_verified_at" if provider == "icloud" else "last_google_verified_at"
        return self.db.query(
            f"SELECT * FROM items WHERE {column} >= ? AND state = 'synced' "  # noqa: S608
            f"ORDER BY {column} DESC LIMIT ?",
            (since, limit),
        )

    def alert(self, key: str) -> sqlite3.Row | None:
        return self.db.query_one("SELECT * FROM alerts WHERE key = ?", (key,))

    def alerts(self, state: str | None = "active") -> list[sqlite3.Row]:
        if state is None:
            return self.db.query("SELECT * FROM alerts ORDER BY key")
        return self.db.query("SELECT * FROM alerts WHERE state = ? ORDER BY key", (state,))

    def raise_alert(self, key: str, severity: str, message: str) -> bool:
        """Returns True when the alert is newly active (or re-raised after clearing)."""
        existing = self.alert(key)
        now = self.now()
        if existing is not None and existing["state"] == "active":
            if existing["message"] != message:
                self.db.execute("UPDATE alerts SET message = ?, severity = ? WHERE key = ?", (message, severity, key))
            return False
        self.db.execute(
            "INSERT INTO alerts(key, state, severity, message, first_raised_at, send_count) "
            "VALUES (?, 'active', ?, ?, ?, 0) ON CONFLICT(key) DO UPDATE SET state = 'active', "
            "severity = excluded.severity, message = excluded.message, "
            "first_raised_at = excluded.first_raised_at, cleared_at = NULL, last_sent_at = NULL, send_count = 0",
            (key, severity, message, now),
        )
        return True

    def clear_alert(self, key: str) -> bool:
        cursor = self.db.execute(
            "UPDATE alerts SET state = 'cleared', cleared_at = ? WHERE key = ? AND state = 'active'",
            (self.now(), key),
        )
        return cursor.rowcount > 0

    def mark_alert_sent(self, key: str) -> None:
        self.db.execute(
            "UPDATE alerts SET last_sent_at = ?, send_count = send_count + 1 WHERE key = ?",
            (self.now(), key),
        )

    # ------------------------------------------------------------ helpers

    def _update(self, table: str, key: str, key_value: Any, fields: dict[str, Any]) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = [int(v) if isinstance(v, bool) else v for v in fields.values()]
        self.db.execute(
            f"UPDATE {table} SET {assignments} WHERE {key} = ?",  # noqa: S608
            (*values, key_value),
        )
