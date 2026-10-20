# Changelog

## 0.2.0 — 2026-10-21

The first release meant for anyone to set up. Upgrading from 0.1.0 needs nothing from you: pull or
build, then `docker compose up -d`. Existing calendar pairs keep the time zone 0.1.0 used for times
without a zone (`CB_DEFAULT_TIMEZONE`, or Europe/Brussels if you never set it).

**New**

- `calendar-bridge setup`: a guided setup (encryption key, Google, iCloud, choice of calendars,
  review of the first sync). It can be run again at any time to add calendars or reconnect an
  account, and accepts the Google client file as a path or as pasted content.
- `docker compose run --rm keygen` creates the encryption key with the right owner and mode (no
  `sudo`), never replaces an existing key, and repairs the empty directory Docker leaves when the
  key file is missing.
- `calendar-bridge activity`: what the bridge changed recently, in plain words.
- `calendar-bridge uninstall`: removes the bridge's access to both accounts and its status
  calendar; events stay where they are.
- Times without a zone use your Google account's time zone, recorded when the first calendar pair
  starts (`CB_DEFAULT_TIMEZONE=auto`), instead of a fixed default.
- Failed events are retried automatically once a day.
- Prebuilt multi-architecture images (amd64, arm64) on GitHub Container Registry, with provenance
  and SBOM; a public home page and privacy policy for your own Google OAuth app (GitHub Pages).
- Published as free software under the GNU GPL, version 3 or later.

**Safety and robustness** (from an independent review of every component)

- The mass-deletion guard: a release covers exactly the deletions you reviewed (it used to let
  through anything deleted before the next cycle, and could stay armed for weeks); holds lift
  themselves once nothing is waiting; held deletions no longer hold up other changes.
- CalDAV never follows redirects (a redirected write used to count as done) and only ever sends
  the password over HTTPS; answers that are not WebDAV are never read as empty listings; calendar
  data that is not valid text is reported instead of stopping the whole calendar pair.
- Encrypted backups use bounded memory and are written atomically; an interrupted backup can no
  longer leave a broken file or cause good backups to be pruned.
- Failures are never silent: status, alerts and the heartbeat run even when a cycle fails, a
  database that cannot be read alerts directly, credentials that cannot be decrypted alert as an
  authorization problem, and the dead-man's switch only reports real outages.
- Housekeeping removes what deleted events leave behind; a deleted status calendar is replaced
  once; Google calendars are never created twice after a lost answer.
- Clear messages for every common mistake: a disabled Calendar API, unticked consent boxes, a
  cancelled consent, wrong or misspelled settings, key file problems, bad conflict choices.
  Unexpected errors print a short message instead of a traceback.
- `tombstones restore` cannot restore the same event twice; removed mappings cannot be revived by
  accident; `test-alert` fails when a channel fails.

**Fixed since 0.1.0**

- Security: certifi 2026.10.19 (GHSA-6q48-6c7w-9gw2). The previous CA bundle still trusted the
  emSign ECC Root CA - G3, whose revoked intermediates had compromised keys.
- `auth google --listen --open-browser`: the browser now opens the same consent address that is
  printed (it used to open a second one, so using the printed one failed with a state mismatch),
  and the loopback server no longer echoes the authorization code to the terminal.
- Google consent always asks which account to use, so the bridge cannot silently attach to
  whichever Google account happens to be signed in.
- `auth google --listen` takes its loopback port exclusively and switches to a free port when the
  configured one (`google_oauth_port`, 8765) is taken. On Windows it used to share the port with
  the other program, which then received the redirect.
- `auth icloud` refuses anything that is not an app-specific password before contacting Apple,
  so a mistyped main Apple Account password never leaves the machine (and cannot trigger
  Apple's lockout). Pasted passwords without dashes or in capitals are normalized.

## 0.1.0 — initial release

- Bidirectional synchronization between iCloud CalDAV calendars and Google calendars through
  explicit one-to-one mappings (main iCloud calendar ↔ Google primary by default).
- Canonical event model with per-side baselines and three-way, field-group merge; conflicts keep
  both versions and are resolved explicitly (`conflicts show/resolve`).
- Google incremental sync (`nextSyncToken`, 410 recovery) and CalDAV `sync-collection` with
  full-listing fallback; deletions only on explicit, verified evidence.
- Durable write-intent journal, conditional writes (`If-Match` / `If-None-Match`), read-back
  verification, crash recovery without duplicates, bounded work per cycle.
- Recurring series with modified/cancelled occurrences, reminders, all-day events, time zones
  (IANA, Windows and vendor names), DST.
- Invitation safety: mirrors carry no attendees/organizer, `sendUpdates=none`,
  `Schedule-Reply: F`, guest-managed meetings.
- Initial reconciliation with a dry-run plan, ambiguity detection and safety verdict.
- Mass-deletion hold, integrity checks, tombstones and restore, encrypted backups and restore with
  marker-based re-linking.
- Status contract (`status --json`, `status.json`), Google status calendar for ChatGPT, alerts
  (ntfy, webhook, e-mail), dead-man's-switch heartbeat, health/doctor commands.
- Hardened Docker image and Compose deployment; CI with lint, types, tests, deep property run,
  dependency audit and image smoke test.
