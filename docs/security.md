# Security

Calendar data is private: who you meet, where and when. The bridge is built to need as little
access as possible and to keep what it holds protected.

## Credentials

| Secret | Where it lives | Protection |
| --- | --- | --- |
| Google OAuth refresh token | database `credentials` table | Fernet (AES-128-CBC + HMAC-SHA256), key outside the database |
| Google OAuth client id/secret | database `credentials` table | same (a *Desktop app* client secret is not confidential by Google's design, but is kept encrypted anyway) |
| iCloud app-specific password | database `credentials` table | same |
| Encryption key | Docker secret `secrets/calendar_bridge_key` → `/run/secrets/...` | never in the database, the image, `.env` or git; created by `keygen` owned by the service's user with mode 400, never overwritten |

- The bridge uses its **own** OAuth client. It never reads, reuses or intercepts ChatGPT's
  connector credentials.
- Only an Apple **app-specific** password is accepted (never the Apple Account password); it can
  be revoked independently at <https://account.apple.com>.
- `keys rotate` re-encrypts all credentials with a new key; several keys can be configured during
  a rotation.
- `auth revoke google` revokes the token at Google *and* deletes it; `auth revoke icloud` deletes
  the stored password (revoke the password itself at Apple). `uninstall` does both and also
  deletes the status calendar the bridge created.
- Re-authorization refuses a different Google account or iCloud principal unless explicitly
  allowed.

## Least privilege

Google scopes (default profile):

| Scope | Why |
| --- | --- |
| `calendar.calendarlist.readonly` | discover calendars, the primary calendar's time zone and default reminders |
| `calendar.events.owned` | read and write events on calendars **you own** (primary and your secondaries) |
| `calendar.app.created` | create the status calendar (and new calendars you ask for) and manage their events |

The broad `calendar` scope (which also allows changing sharing and deleting calendars) is never
requested. The `shared` profile swaps `events.owned` for `calendar.events` only when you map a
calendar someone else owns.

iCloud: CalDAV access through the app-specific password; Apple offers no narrower scope. The
bridge only writes to collections that are explicitly mapped and reported writable, and refuses
unconditional writes (`If-Match`/`If-None-Match` are mandatory).

## Network

- No inbound ports, no web interface, no webhooks (Google push notifications would require a
  public HTTPS endpoint, so the bridge polls instead).
- Outbound HTTPS only: `www.googleapis.com`, `oauth2.googleapis.com`, `caldav.icloud.com` and the
  `pNN-caldav.icloud.com` partition it discovers, plus the alert/heartbeat endpoints you configure.
- TLS certificate verification is always on. The iCloud password is only ever sent over HTTPS:
  plain-HTTP CalDAV addresses are refused unless explicitly allowed for local testing
  (`CB_ALLOW_INSECURE_CALDAV`), and redirects are never followed (a redirect could hand the
  credentials to another host).
- XML from servers is parsed with entity resolution and network access disabled (no XXE).

## Data at rest

The database (`/data/calendar-bridge.db`) contains event content (titles, times, descriptions)
needed to compare versions and to recover tombstones. It is created with mode 600 inside a
container running as an unprivileged user; backups are encrypted when a key is configured. Put
the Docker volume on an encrypted disk if the host is shared or portable.

## Logs and alerts

Logs carry identifiers and outcomes only. A redaction filter blanks fields such as `summary`,
`description`, `attendees`, `token`, `password` if they are ever passed by mistake. Alerts sent to
external channels describe conditions only ("3 unresolved conflicts"), never event content.

The status calendar in your own Google account and `status.json` on your own volume do contain
event titles (so that ChatGPT can confirm what reached iCloud). Disable titles with
`status --no-titles` / leave `CB_STATUS_CALENDAR=false` if you prefer.

## Container hardening

Non-root user (uid 10001), read-only root filesystem, all Linux capabilities dropped,
`no-new-privileges`, memory and PID limits, no ports, base image pinned by digest, every Python
dependency pinned by version **and hash** and installed from wheels only. Dependabot proposes
updates with a two-week cooldown; CI runs `pip-audit` on the lock file.

## Reporting what changed

Every write the bridge makes is in the `audit_log` table (identifiers, action, provider, time) and
visible per event with `items show <id>`.
