-- Calendar Bridge schema, version 1.
-- All timestamps are UTC ISO-8601 strings ending in 'Z'.

CREATE TABLE meta (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT NOT NULL
);

-- Encrypted credential blobs (Fernet tokens). The key never lives in this database.
CREATE TABLE credentials (
    name        TEXT PRIMARY KEY CHECK (name IN ('google_client', 'google_token', 'icloud')),
    ciphertext  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Identity anchors: detect a switch to a different account before it can cause damage.
CREATE TABLE accounts (
    provider             TEXT PRIMARY KEY CHECK (provider IN ('google', 'icloud')),
    identity             TEXT NOT NULL,   -- google: primary calendar id; icloud: principal URL
    display              TEXT,
    auth_state           TEXT NOT NULL DEFAULT 'ok' CHECK (auth_state IN ('ok', 'invalid', 'revoked')),
    auth_error           TEXT,
    auth_state_since     TEXT,
    last_verified_at     TEXT,
    created_at           TEXT NOT NULL
);

CREATE TABLE calendars (
    id                      INTEGER PRIMARY KEY,
    provider                TEXT NOT NULL CHECK (provider IN ('google', 'icloud')),
    remote_id               TEXT NOT NULL,     -- google calendar id / CalDAV collection URL
    display_name            TEXT,
    access                  TEXT NOT NULL,
    writable                INTEGER NOT NULL,
    supports_events         INTEGER NOT NULL DEFAULT 1,
    is_primary              INTEGER NOT NULL DEFAULT 0,
    timezone                TEXT,
    role                    TEXT CHECK (role IS NULL OR role IN ('status')),
    default_reminders_json  TEXT,
    last_discovered_at      TEXT NOT NULL,
    missing_since           TEXT,
    UNIQUE (provider, remote_id)
);

CREATE TABLE mappings (
    id                      INTEGER PRIMARY KEY,
    name                    TEXT NOT NULL,
    google_calendar_id      INTEGER NOT NULL REFERENCES calendars(id),
    icloud_calendar_id      INTEGER NOT NULL REFERENCES calendars(id),
    state                   TEXT NOT NULL CHECK (state IN ('pending_initial', 'active', 'paused', 'removed')),
    history_days            INTEGER,           -- NULL: no history limit
    created_at              TEXT NOT NULL,
    activated_at            TEXT,
    destructive_hold        INTEGER NOT NULL DEFAULT 0,
    destructive_hold_reason TEXT,
    destructive_hold_since  TEXT,
    last_cycle_ok_at        TEXT,
    last_full_reconcile_at  TEXT
);
CREATE UNIQUE INDEX mappings_google_live ON mappings(google_calendar_id) WHERE state != 'removed';
CREATE UNIQUE INDEX mappings_icloud_live ON mappings(icloud_calendar_id) WHERE state != 'removed';
CREATE UNIQUE INDEX mappings_name_live ON mappings(name) WHERE state != 'removed';

CREATE TABLE checkpoints (
    mapping_id              INTEGER NOT NULL REFERENCES mappings(id),
    provider                TEXT NOT NULL CHECK (provider IN ('google', 'icloud')),
    sync_token              TEXT,
    ctag                    TEXT,
    needs_full              INTEGER NOT NULL DEFAULT 1,
    last_full_at            TEXT,
    last_read_ok_at         TEXT,
    last_write_verified_at  TEXT,
    last_error              TEXT,
    last_error_at           TEXT,
    consecutive_failures    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (mapping_id, provider)
);

-- Synchronization units. One row per single event, recurring series or detached occurrence.
CREATE TABLE items (
    id                      INTEGER PRIMARY KEY,
    mapping_id              INTEGER NOT NULL REFERENCES mappings(id),
    origin                  TEXT NOT NULL CHECK (origin IN ('google', 'icloud', 'both')),
    authority               TEXT NOT NULL CHECK (authority IN ('google', 'icloud')),
    google_event_id         TEXT,
    google_ical_uid         TEXT,
    icloud_href             TEXT,
    icloud_uid              TEXT,
    icloud_detached_key     TEXT,
    state                   TEXT NOT NULL CHECK (state IN
                              ('synced', 'pending', 'conflicted', 'rejected', 'unsupported', 'failed', 'deleted')),
    state_reason            TEXT,
    dirty                   INTEGER NOT NULL DEFAULT 1,
    suppressed              TEXT CHECK (suppressed IS NULL OR suppressed IN ('google')),
    google_synced_json      TEXT,
    icloud_synced_json      TEXT,
    google_synced_fp        TEXT,
    icloud_synced_fp        TEXT,
    meeting                 INTEGER NOT NULL DEFAULT 0,
    notes_json              TEXT,
    retry_count             INTEGER NOT NULL DEFAULT 0,
    next_attempt_at         TEXT,
    last_error              TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    last_synced_at          TEXT,
    last_google_verified_at TEXT,
    last_icloud_verified_at TEXT,
    deleted_at              TEXT,
    deleted_by              TEXT
);
CREATE UNIQUE INDEX items_google_live ON items(mapping_id, google_event_id)
    WHERE google_event_id IS NOT NULL AND state != 'deleted';
CREATE UNIQUE INDEX items_icloud_live ON items(mapping_id, icloud_href)
    WHERE icloud_href IS NOT NULL AND state != 'deleted';
CREATE INDEX items_dirty ON items(mapping_id, dirty);
CREATE INDEX items_state ON items(mapping_id, state);
CREATE INDEX items_icloud_uid ON items(mapping_id, icloud_uid);
CREATE INDEX items_google_uid ON items(mapping_id, google_ical_uid);

-- Last observed Google state (one row per event, including exception instances).
-- Rows are linked to items through items.google_event_id (masters) and master_id (instances).
CREATE TABLE google_events (
    mapping_id      INTEGER NOT NULL REFERENCES mappings(id),
    event_id        TEXT NOT NULL,
    master_id       TEXT,                 -- recurringEventId of exception instances
    ical_uid        TEXT,
    recurrence_key  TEXT,
    status          TEXT NOT NULL,
    etag            TEXT,
    updated         TEXT,
    raw_json        TEXT NOT NULL,
    observed_at     TEXT NOT NULL,
    skip_reason     TEXT,                 -- why an unlinked event is deliberately not mirrored
    PRIMARY KEY (mapping_id, event_id)
);
CREATE INDEX google_events_master ON google_events(mapping_id, master_id);
CREATE INDEX google_events_uid ON google_events(mapping_id, ical_uid);

-- Last observed iCloud state (one row per CalDAV resource).
CREATE TABLE icloud_resources (
    mapping_id          INTEGER NOT NULL REFERENCES mappings(id),
    href                TEXT NOT NULL,
    uid                 TEXT,
    etag                TEXT,
    ics                 TEXT,
    status              TEXT NOT NULL CHECK (status IN ('present', 'deleted', 'unverified_missing')),
    observed_at         TEXT NOT NULL,
    deleted_verified_at TEXT,
    skip_reason         TEXT,
    PRIMARY KEY (mapping_id, href)
);
CREATE INDEX icloud_resources_uid ON icloud_resources(mapping_id, uid);

-- Durable write-intent journal: recorded *before* every remote mutation.
CREATE TABLE write_intents (
    id              INTEGER PRIMARY KEY,
    mapping_id      INTEGER NOT NULL REFERENCES mappings(id),
    item_id         INTEGER REFERENCES items(id),
    provider        TEXT NOT NULL CHECK (provider IN ('google', 'icloud')),
    op              TEXT NOT NULL,
    target          TEXT NOT NULL,
    precondition    TEXT,
    payload_json    TEXT,
    state           TEXT NOT NULL CHECK (state IN ('pending', 'applied', 'verified', 'failed', 'abandoned')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    result_etag     TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX write_intents_open ON write_intents(mapping_id, state) WHERE state IN ('pending', 'applied');
CREATE INDEX write_intents_item ON write_intents(item_id);

CREATE TABLE conflicts (
    id              INTEGER PRIMARY KEY,
    mapping_id      INTEGER NOT NULL REFERENCES mappings(id),
    item_id         INTEGER REFERENCES items(id),
    kind            TEXT NOT NULL,
    groups_json     TEXT,
    google_json     TEXT,
    icloud_json     TEXT,
    google_base_json TEXT,
    icloud_base_json TEXT,
    candidates_json TEXT,
    reason          TEXT,
    state           TEXT NOT NULL CHECK (state IN ('open', 'resolving', 'resolved', 'superseded')),
    resolution      TEXT,
    resolution_json TEXT,
    created_at      TEXT NOT NULL,
    resolved_at     TEXT
);
CREATE INDEX conflicts_state ON conflicts(state);
CREATE INDEX conflicts_item ON conflicts(item_id);

CREATE TABLE sync_runs (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok          INTEGER,
    stats_json  TEXT,
    error       TEXT
);

-- What the bridge changed, when and why. Identifiers only; never event content.
CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY,
    at          TEXT NOT NULL,
    mapping_id  INTEGER,
    item_id     INTEGER,
    provider    TEXT,
    action      TEXT NOT NULL,
    detail      TEXT
);
CREATE INDEX audit_log_at ON audit_log(at);

CREATE TABLE alerts (
    key             TEXT PRIMARY KEY,
    state           TEXT NOT NULL CHECK (state IN ('active', 'cleared')),
    severity        TEXT NOT NULL,
    message         TEXT NOT NULL,
    first_raised_at TEXT NOT NULL,
    last_sent_at    TEXT,
    send_count      INTEGER NOT NULL DEFAULT 0,
    cleared_at      TEXT
);
