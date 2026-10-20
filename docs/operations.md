# Operations

Everything below runs as `docker compose exec calendar-bridge calendar-bridge <command>` while the
service is up (or `docker compose run --rm calendar-bridge <command>` when it is stopped). A shell
alias saves typing:

```bash
alias cb='docker compose -f ~/calendar-bridge/docker-compose.yml exec calendar-bridge calendar-bridge'
cb status
```

Read-only commands never take the synchronization lock and never write to a calendar. Every
command has `--help`.

## Day to day: nothing

The daemon synchronizes every `CB_SYNC_INTERVAL` seconds (default 60), re-reads both calendars
completely every `CB_FULL_RECONCILE_INTERVAL` (default 6 h), backs the database up daily and
publishes its status. You only act when an alert tells you to.

Each cycle handles at most `CB_MAX_ITEMS_PER_CYCLE` events (default 500), so a large first-time
copy simply spreads over a few cycles instead of producing one very long cycle.

## Status and health

| Command | What it tells you |
| --- | --- |
| `status` | Overall state, authorization, per-mapping last reads, last *verified* writes in each direction, item counts, pending events, conflicts, alerts. `--json` gives the [status contract](architecture.md#status-contract). |
| `activity` | What the bridge changed recently, in plain words, newest first: copies, updates, deletions, conflicts, holds. `--no-titles`, `--json`, `--mapping <id>`, `--limit N`. |
| `health` | Exit 0 if the daemon's heartbeat is fresh (used by the Docker healthcheck). `--strict` also requires a recent fully successful cycle. |
| `doctor` | Read-only end-to-end checks: key, database integrity, Google and iCloud access (and that the account did not change), mapped calendars, heartbeat. |
| `items list [--state failed]`, `items show <id>` | Individual events: state, reason, notes about what could not be represented, recent writes and audit trail. |
| `calendars` | Both providers' calendars, which are mapped, which are read-only. |

`<data volume>/status.json` holds the same document as `status --json`, refreshed every cycle,
for other systems (home automation, dashboards, your own scripts) to read without depending on the
bridge's internals.

### Item states

| State | Meaning | What to do |
| --- | --- | --- |
| `synced` | Both copies verified equal (as far as both providers can represent). | Nothing. |
| `pending` | A change is waiting: retry with backoff after a temporary error, or held by a safety hold. | Nothing, unless it persists; see the reason. |
| `conflicted` | Both sides changed the same thing, or a guest-managed meeting was edited on the mirror. | Resolve it (below). |
| `rejected` | A provider refused the change (e.g. permission denied). | Fix the cause, then `items retry <id>`. |
| `unsupported` | The event uses something the other provider cannot hold faithfully; it is kept as is on its side. | See [semantics](semantics.md). |
| `failed` | Gave up after repeated errors or an internal error. | Retried automatically once a day; `items show` explains, `items retry` retries now. |
| `deleted` | Tombstone kept for recovery (90 days by default). | `tombstones restore <id>` if needed. |

## Conflicts

```bash
calendar-bridge conflicts list
calendar-bridge conflicts show 12          # both versions side by side
calendar-bridge conflicts resolve 12 --keep google       # or --keep icloud
calendar-bridge conflicts resolve 12 --merge --field summary=icloud --field timing=google
calendar-bridge conflicts resolve 12 --accept-delete     # deletion vs modification: delete
calendar-bridge conflicts resolve 12 --restore           # ...or bring the deleted copy back
calendar-bridge conflicts resolve 12 --link <googleEventId> <icloudHref>   # ambiguous initial match
calendar-bridge conflicts resolve 12 --keep-separate     # ambiguous / duplicate: treat as different events
calendar-bridge conflicts resolve 12 --delete-duplicate  # remove a duplicate copy
```

The resolution is recorded immediately and applied on the next cycle (`--now` to apply at once),
through the same journaled and verified write path as normal synchronization. Field names for
`--merge`: `summary`, `description`, `location`, `status`, `transparency`, `visibility`, `alarms`,
`url`, `timing`, `exdates`, `override:<key>`.

**Guest-managed conflicts.** A meeting with other guests belongs to the provider where its guest
list lives. If it is changed on the other side (for example moved on the iPhone while the
invitation lives in Google), the bridge does not silently push the change to the owner, because
that is where guests would be notified. `--keep <owner>` reverts the mirror; `--keep <mirror>`
applies the change to the owner (you are asked to confirm, as the owner may notify guests).
Personal fields (reminders, free/busy, visibility) always sync.

## Safety holds

If more than `max(CB_MASS_DELETE_COUNT, CB_MASS_DELETE_RATIO × events)` deletions are requested
in one cycle, deletions for that mapping are suspended and you get an alert. Everything else keeps
synchronizing.

```bash
calendar-bridge safety status
calendar-bridge items list --state pending          # what is being held
calendar-bridge safety release --mapping 1          # intended? let them run (within the next hour)
```

If the deletions were a mistake, restore the events where they were deleted (Google: Trash; iCloud:
<https://www.icloud.com> → Data Recovery). Once nothing is waiting to be deleted any more, the hold
lifts by itself. A release only covers the deletions held when you gave it.

A failed database integrity check suspends deletions globally (`safety clear-global` once
`db check` passes again).

## Mappings

```bash
calendar-bridge setup                 # add calendar pairs interactively (also resumes paused ones)
calendar-bridge mappings list
calendar-bridge mappings pause 1      # stop syncing; changes are caught up on resume
calendar-bridge mappings resume 1
calendar-bridge mappings remove 1     # stops and forgets the mapping; deletes NO events
```

## Credentials

```bash
calendar-bridge auth status
calendar-bridge setup                 # reconnect an account (keeps all sync state)
calendar-bridge auth google           # the same for Google only
calendar-bridge auth icloud           # the same for iCloud only, e.g. after rotating the app-specific password
calendar-bridge auth revoke google    # revoke at Google and delete the stored token
calendar-bridge auth revoke icloud    # delete the stored password (then revoke it at account.apple.com)
calendar-bridge keys rotate           # after adding a new key as the FIRST line of the key file
```

Re-authorization refuses a *different* account (or iCloud principal) than before unless you pass
`--allow-account-change`, so a mistake cannot point existing mappings at another person's data.
The daemon picks up new credentials on its next cycle; no restart needed.

## Upgrades

```bash
git pull
docker compose pull          # or `docker compose build` if you build the image yourself
docker compose up -d
```

Database migrations run automatically at start-up, after an automatic `pre-migration` backup.
Read the [changelog](../CHANGELOG.md) first: it says when an upgrade needs anything from you.

## Stopping for good

```bash
calendar-bridge uninstall        # removes its access to both accounts and its status calendar
docker compose down -v           # removes the service and its database
```

Your events stay in both calendars. Also revoke the app-specific password at
<https://account.apple.com>; `uninstall` prints the remaining steps.

## Logs

`docker compose logs -f calendar-bridge` — JSON lines with identifiers and outcomes only; event
titles, descriptions, attendees, tokens and passwords are never logged. `CB_LOG_FORMAT=text` for
human-friendly output, `CB_LOG_LEVEL=DEBUG` for more detail (still without content).
