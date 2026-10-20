# Recovery

Your calendar data lives in Google and iCloud; the bridge's database holds synchronization state
(identities, baselines, journal, conflicts). Losing the database never loses events, and the
bridge is designed so that rebuilding state never duplicates or deletes them.

## Backups

- Automatic: daily (`CB_BACKUP_INTERVAL_HOURS`), the last 14 kept (`CB_BACKUP_KEEP`), in
  `/data/backups` on the data volume. Encrypted with the credential key when one is configured.
- Before every schema migration: a `pre-migration` backup.
- Manual: `calendar-bridge db backup` / `db backups`.

Copy backups off the host with the volume, e.g.:

```bash
docker run --rm -v calendar-bridge-data:/data -v "$PWD":/out alpine \
  sh -c 'cp /data/backups/*.enc /out/'
```

Backups are useless without `secrets/calendar_bridge_key`; keep the key in your password manager.

## Restore

```bash
docker compose stop calendar-bridge
docker compose run --rm calendar-bridge db restore /data/backups/calendar-bridge-<stamp>-auto.db.enc
docker compose start calendar-bridge
```

The restore verifies the backup's integrity, keeps the current database aside
(`calendar-bridge.db.pre-restore-<stamp>`), forces a complete re-read of both calendars, and
re-links events created after the backup through the markers the bridge writes on every mirror
(`X-CALENDAR-BRIDGE-*` in iCloud, private extended properties in Google) — instead of copying
them again.

## Playbooks

### "Google access needs attention" (refresh token revoked or expired)

Typical causes: the OAuth app was left in *Testing* status (tokens expire after 7 days), access
was removed at <https://myaccount.google.com/permissions>, the password changed with
"sign out everywhere", or the OAuth client was deleted. Fix the cause (publish the app *In
production*, see [setup](setup.md#1-google-create-your-oauth-client)), then run `setup`. Nothing
is lost: changes made meanwhile on either side are picked up on the next cycle.

### "iCloud access needs attention"

The app-specific password was revoked or the Apple Account password changed (which revokes all
app-specific passwords). Create a new one and run `setup`.

### One provider is down

Expected and harmless. The bridge keeps reading the reachable side, records its changes durably
and propagates them when the other side is back. A mapping is never reconciled from a partial
view. After `CB_LAG_ALERT_SECONDS` without a fully successful cycle you get an alert.

### Expired sync tokens

Handled automatically: Google's HTTP 410 and CalDAV's `valid-sync-token` error trigger a complete
re-read, which is compared against stored identities. Events missing from a complete listing are
only treated as deleted after a direct read of that specific event confirms it.

### Deletions are suspended

Someone (or something) deleted many events at once. Review with `items list --state pending`
and `safety status`. If intended: `safety release --mapping <id>`. If not: restore the events on
the side where they were deleted (Google: Trash / iCloud: <https://www.icloud.com> →
Data Recovery → Restore Calendars). The bridge then sees them as unchanged, nothing is lost on the
other side because the held deletions were never executed, and the hold lifts by itself.

### An event was deleted by mistake

`calendar-bridge tombstones list`, then `tombstones restore <id>`: it is re-created in iCloud from
its last synchronized version and copied to Google.

### The daemon crashed or the host lost power

Just start it again (Docker does so automatically). Open write intents are inspected first:
a creation that reached the provider is adopted, one that did not is re-done with the same
idempotent identifier; updates and deletions are re-planned from freshly observed state.

### Database corruption

The periodic integrity check suspends deletions and alerts. `db check` shows the problem.
Restore the latest good backup (above); if none, move the database aside and start fresh with
`setup`: the initial reconciliation pairs the existing copies (by UID and bridge markers) instead of
duplicating them.

### Lost encryption key

Move the old key file aside, create a new one with `docker compose run --rm keygen`, then run
`setup` and connect both accounts again; it asks for the Google client file, since the stored copy
cannot be read either. The new credentials overwrite the unreadable ones,
and all synchronization state is kept. Encrypted backups made with the old key cannot be restored.
Also remove the bridge's old grant at <https://myaccount.google.com/permissions> and revoke the old
app-specific password at Apple.

### Moving to another host

Stop the service, copy the `calendar-bridge-data` volume and `secrets/`, start it on the new host.
Never run two bridges against the same calendars at the same time.
