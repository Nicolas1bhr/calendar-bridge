# Architecture

```
            ┌──────────────── Calendar Bridge (one container, outbound HTTPS only) ────────────────┐
 Google     │  GoogleObserver ──┐                                       ┌── Executor ──► Google API │
 Calendar ◄─┼─ (events.list +   │    observed state     ┌─ Reconciler ─┤   (intent journal,        │
 API        │   nextSyncToken)  ├──► google_events ──────┤  link → plan  │    conditional writes,   │
            │                   │    icloud_resources    │  → guard →    │    read-back verify)     │
 iCloud     │  ICloudObserver ──┘    (SQLite, WAL)       └─ execute ────┴── Executor ──► CalDAV    │
 CalDAV  ◄──┼─ (sync-collection,       items / baselines / write_intents / conflicts / audit_log   │
            │   multiget, verify)      status.json · status calendar · alerts · backups            │
            └────────────────────────────────────────────────────────────────────────────────────┘
```

Source layout (`src/calendar_bridge`):

| Module | Role |
| --- | --- |
| `model/canonical.py` | Provider-neutral event model and *field groups* |
| `model/merge.py` | Three-way change detection and merge/conflict decisions |
| `model/recurrence.py`, `timeutil.py`, `text.py` | Occurrence identity, RRULE normalization, zones, mirror info block |
| `providers/google/` | OAuth, REST client, Google ⇄ canonical conversion |
| `providers/icloud/` | CalDAV client (discovery via `caldav`, strict data plane), iCalendar ⇄ canonical |
| `sync/observe.py` | Change detection with deletion-evidence rules |
| `sync/reconcile.py`, `executor.py`, `recovery.py`, `resolve.py`, `initial.py` | The engine |
| `db/` | Schema, migrations, repository, backups |
| `status/` | Status contract, Google status calendar, alerts |
| `daemon.py`, `cli.py` | Service loop and operator commands |

## The canonical model

One **item** is one synchronization unit: a single event, a recurring series (master + modified
occurrences + cancelled occurrences), or a detached occurrence. Both providers are converted into
`CanonicalEvent`; changes are compared and applied per **field group**:

| Group | Contents |
| --- | --- |
| `summary`, `description`, `location`, `status`, `transparency`, `visibility`, `alarms`, `url` | one field each |
| `timing` | start, end, RRULE, RDATE together (any of them shifts occurrences) |
| `exdates` | cancelled occurrences — EXDATE in iCalendar, cancelled instances in Google |
| `override:<key>` | one modified occurrence, keyed by its **original** start (`YYYYMMDD` or UTC `YYYYMMDDTHHMMSSZ`), never by its current start |
| `people`, `conference` | informational: guest list and call links, only ever rendered as text on the mirror |

Occurrences are identified by their original start, which is also the suffix Google uses for
instance ids (`<masterId>_<key>`), so occurrence identity survives moves.

## Per-side baselines and three-way merge

For every item the database stores, separately for Google and for iCloud, the canonical form of
that provider's copy as it was when both sides were last verified in agreement. A side *changed*
when its current form differs from **its own** baseline. Comparing each provider only with itself
makes provider normalizations (re-ordered RRULE parts, rewritten zones, extra Apple properties)
invisible, and makes the bridge's own writes come back as "no change" — no echo loops, no
reliance on timestamps.

Decision rules (`model/merge.py`), all-or-nothing per item:

1. changed on one side only → copy those groups to the other side;
2. changed on both sides to the same value → nothing to do;
3. changed on both sides differently → **conflict**, both versions preserved, nothing written;
4. `exdates` changed on both sides (timing untouched) → union of both sides' additions/removals;
5. `timing` on one side + occurrence edits on the other → conflict (occurrence identity depends on timing);
6. `people`/`conference` only flow from the owning provider to the mirror;
7. meetings under the default `authoritative` policy: edits of shared fields made on the mirror
   become `guest_managed` conflicts; personal fields (alarms, free/busy, visibility) still sync;
8. deletion on one side with no change on the other → propagate the deletion; deletion vs
   modification → conflict.

"Converged" is judged through **projections**: the canonical form a value would have after being
written to the other provider (e.g. HTML descriptions become text in iCloud; floating times are
pinned to the default zone in Google; Google keeps at most 5 reminders ≤ 4 weeks). A difference
that is only due to such a known limitation is never reported as a change.

## Change detection

**Google** — `events.list` with identical parameters for full and incremental reads
(`singleEvents=false`, `showDeleted=true`), all pages, then `nextSyncToken`. HTTP 410 → full
re-read. Recurring masters and their exception instances are stored separately and grouped by
`recurringEventId`.

**iCloud** — cheap `getctag`/`sync-token` check; `sync-collection` (RFC 6578) with truncation
handling; changed resources fetched with `calendar-multiget`; `valid-sync-token` errors or missing
support fall back to a complete listing (sync-collection with an empty token, else
`PROPFIND Depth: 1`) compared by ETag.

**Durability** — observed changes and the new checkpoint are committed in one transaction, after
all pages were read. A crash before that commit simply re-reads the same changes (recording is
idempotent).

**Deletion evidence** — Google: an explicit `cancelled` record, or `404/410` on a direct read of
an event missing from a complete listing. iCloud: a direct request for that exact resource
returning `404/410`. Anything else (errors, timeouts, truncated listings) parks the resource as
`unverified_missing`, re-checked next cycle, never acted upon.

## Write path and the intent journal

For each remote mutation (`sync/executor.py`):

1. record a write intent (operation, target, precondition, and for creations the *source version*
   and the pre-chosen remote identity) and commit;
2. send the request — creations use a client-chosen Google event id (409 on retry → adopt) or a
   UID-derived CalDAV URL with `If-None-Match: *` (412 on retry → adopt); updates and deletions
   carry `If-Match: <observed ETag>`;
3. mark the intent applied;
4. read the result back, store it as the latest observation, verify each written group against
   the projection of the target, then commit the new baselines and close the intents. The new
   baseline takes from the read-back **only the groups that were written** (plus occurrences a
   timing change can reshape): anything else that changed meanwhile was someone else's edit, so
   it keeps its old baseline value and propagates on the next cycle instead of being absorbed.

**Recovery** (`sync/recovery.py`) runs before every reconciliation: an open *create* intent is
looked up by its exact identity — found: adopted, with the recorded source version as the other
side's baseline, minus anything the interrupted create had not yet applied (so the next cycle
completes it); not found: released and re-done (respecting any retry backoff). Open update/delete intents are abandoned and the
item is re-planned from freshly observed state (an applied write is recognized as converged).

Every write also lands in `audit_log`. Google writes use `sendUpdates=none` and never include
attendees, organizer or conference data; CalDAV writes send `Schedule-Reply: F` and mirrors never
contain `ORGANIZER`/`ATTENDEE`.

## Safety guard

Plans are computed for all dirty items before anything runs. If the number of deletions exceeds
`max(mass_delete_count, mass_delete_ratio × live items)`, deletions for that mapping are held
(`items` stay `pending`), an alert is raised, and everything non-destructive proceeds (held items
do not count against the per-cycle budget). A release (`safety release`) records the ids of the
held deletions the user reviewed and lets exactly those run, within one hour; any other deletion is
checked again. A hold lifts by itself once no deletion is waiting. A global hold follows a failed
database integrity check.

## Initial reconciliation

`reconcile` reads both calendars completely and pairs existing events — by identity (UID,
`iCalUID`, bridge markers), then by exact content when exactly one candidate exists on each side —
before anything is copied. Ambiguous candidates are never merged. Pairs whose content differs
become conflicts; differences only in personal fields are tolerated (each side keeps its own).
The plan is printed with a verdict; `--apply` records the pairs and activates the mapping; copying
then happens through the normal write path.

## Database

SQLite in WAL mode with `synchronous=FULL` and foreign keys. Main tables: `calendars`,
`mappings` (partial unique indexes enforce one-to-one), `checkpoints`, `google_events` and
`icloud_resources` (last observed state), `items` (identities, per-side baselines and
fingerprints, state), `write_intents`, `conflicts`, `audit_log`, `alerts`, `sync_runs`,
`credentials` (encrypted), `meta`. Migrations are numbered SQL files applied at start-up after an
automatic backup.

## Status contract

`calendar-bridge status --json` and `<data>/status.json` expose `calendar-bridge.status/v1`
(fields are only added within v1):

```json
{
  "schema": "calendar-bridge.status/v1",
  "overall": "ok | degraded | error | not_configured",
  "daemon": {"heartbeat_at": "...", "last_ok_cycle_at": "...", "sync_interval_seconds": 60},
  "google": {"state": "ok | invalid | revoked | missing", "account": "..."},
  "icloud": {"state": "...", "account": "..."},
  "mappings": [{
    "id": 1, "name": "main", "state": "active",
    "last_google_read_at": "...", "last_icloud_read_at": "...",
    "last_google_write_verified_at": "...", "last_icloud_write_verified_at": "...",
    "lag_seconds": 12.3, "items": {"synced": 412, "pending": 0, "conflicted": 0, "...": 0},
    "open_write_intents": 0, "oldest_open_write_intent_at": null, "destructive_writes": "allowed | suspended",
    "pending": [{"item_id": 7, "google_event_id": "...", "waiting_for": "icloud", "title": "...", "start": "..."}],
    "recently_verified_in_icloud": [{"item_id": 3, "google_event_id": "...", "verified_in_icloud_at": "...", "title": "..."}]
  }],
  "conflicts": {"open": 0, "items": []},
  "failures": {"retryable": 0, "permanent": 0},
  "alerts": []
}
```

Other systems should rely on this contract (or on the CLI) rather than on database tables.
