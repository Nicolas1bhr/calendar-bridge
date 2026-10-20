# Acceptance

Two layers: an **automated live test** against the real APIs on throw-away calendars, then the
**end-to-end checks** with ChatGPT and an Apple device on your real setup. Success is declared
only when the persisted event has been seen on both providers — not because a test passed or an
API returned 200.

## 1. Automated live test (real Google + real iCloud, isolated calendars)

Prerequisites: both providers authorized (`calendar-bridge setup`) in the data directory
below. The test creates a Google calendar `Calendar Bridge test cb-live-...` and an iCloud
calendar with the same name, maps only those two in a temporary database, runs the scenarios and
deletes **only** what it created.

```bash
# on the host, from a checkout with the dev environment installed (see CONTRIBUTING.md)
docker compose stop calendar-bridge          # the test reads the credentials from the database
mkdir -p live-data
docker run --rm -v calendar-bridge-data:/data -v "$PWD/live-data":/out alpine \
  sh -c "cp /data/calendar-bridge.db /out/ && chown $(id -u) /out/calendar-bridge.db"
sudo install -m 600 -o "$(id -u)" secrets/calendar_bridge_key live-data/key
CB_LIVE=1 CB_LIVE_DATA_DIR="$PWD/live-data" CB_ENCRYPTION_KEY_FILE="$PWD/live-data/key" \
  pytest -m live tests/live -v
docker compose start calendar-bridge
rm -rf live-data
```

If creating calendars over CalDAV is refused for your account, create an empty iCloud calendar
named `Calendar Bridge test` yourself and pass its URL (`calendar-bridge calendars --json`) as
`CB_LIVE_ICLOUD_CALENDAR_URL`.

Covered: Google create → iCloud; Google update → iCloud; iCloud update → Google; deletions both
ways; iCloud-only all-day event with alert → Google; recurring series with a moved and a
cancelled occurrence; restart without duplicates; iCloud outage (wrong credentials) and
recovery; invitation safety (mirror has no attendees); verified-write timestamps in the status.

## 2. End-to-end checks on your real calendars

Run these after `docker compose up -d`. "Within a minute" means one sync interval.

| # | Do this | Expect |
| --- | --- | --- |
| 1 | Ask ChatGPT: *"Add 'Acceptance test 1' tomorrow 10:00–10:30."* | Within a minute it is on your iPhone (iCloud calendar), at 10:00 local time. `calendar-bridge status` lists it under *recently verified in iCloud*. |
| 2 | Ask ChatGPT to move it to 11:00 and rename it *'Acceptance test 1b'*. | The iPhone shows the new time and title. |
| 3 | On the iPhone, change the location to *'Room 2'* and add a 15-minute alert. Ask ChatGPT *"Where is Acceptance test 1b?"* | ChatGPT answers *Room 2*; Google shows a 15-minute reminder. |
| 4 | Delete it in ChatGPT. Create *'Acceptance test 4'* on the iPhone, then delete it there. | Both disappear from the other side within a minute. |
| 5 | Create *'Acceptance test 5'* on the Mac/iPhone only (all-day, next Friday). Ask ChatGPT about next Friday, then ask it to change the title. | ChatGPT sees it; the rename appears on the iPhone. |
| 6 | Create a weekly series on the iPhone (4 weeks). Move the 2nd occurrence, delete the 3rd. Ask ChatGPT to change the time of the 4th only. | Google shows the series with the moved and cancelled occurrences; the iPhone shows ChatGPT's change on the 4th only. |
| 7 | `docker compose restart calendar-bridge`, then `calendar-bridge sync` twice. | No new or duplicated events anywhere; `status` shows everything `synced`. |
| 8 | Disconnect the host from the network for 5 minutes while creating one event on each side; reconnect. | Both events arrive on the other side; nothing is deleted; an alert fires if the outage exceeds `CB_LAG_ALERT_SECONDS`. |
| 9 | Ask ChatGPT to create *'Acceptance meeting'* with a guest **address you control**. Then move it on the iPhone. | One invitation e-mail only (from Google, caused by ChatGPT). The iPhone copy shows the guest in the notes block, not as an invitee. Moving it on the iPhone creates a `guest_managed` conflict and **no** e-mail; resolve with `conflicts resolve <id> --keep google`. |
| 10 | Read today's event in the *Calendar Bridge Status* calendar (or ask ChatGPT to). Stop the container for 20 minutes. | The status lists what was confirmed in iCloud. After the stop, the heartbeat/lag alert (or your healthchecks.io check) fires, and the status event shows the last successful sync time. |

Clean up the acceptance events afterwards (delete them on either side and let the bridge
propagate).

## 3. Automated suite (no accounts needed)

```bash
pytest                       # unit, property, integration (real CalDAV server), recovery, CLI
HYPOTHESIS_PROFILE=deep pytest tests/property
```

The integration tests run a real CalDAV server (Radicale) and a stateful Google Calendar API fake
with sync tokens, 410s, ETags, pagination and recurring instances. They exercise crashes between
a remote write and the local commit (for creates, updates and deletes on both providers),
restarts, provider outages, expired and rejected sync tokens, unverifiable deletions, the
mass-deletion hold, concurrent edits, conflicts and every resolution, initial reconciliation with
identical / differing / ambiguous events, database restore without duplicates, and that repeated
cycles make zero writes.
