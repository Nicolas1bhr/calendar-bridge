# Setup

This guide takes you from nothing to a running bridge that keeps your iCloud calendar and your
Google calendar in sync. Budget about 30 minutes; most of it is step 1. Steps 1 and 2 happen in
your own Google and Apple accounts, so only you can do them.

## What you need

- An always-on machine with Docker Engine and Docker Compose v2: a home server, a NAS, a
  Raspberry Pi 4 or 5, a small VPS (`amd64` and `arm64` both work). It only needs outbound HTTPS.
- A Google account (for ChatGPT: the account its Google Calendar connector uses).
- An Apple Account with two-factor authentication (required for app-specific passwords).

## Why your own Google Cloud project

Google only lets a registered app keep lasting access to a calendar. Instead of trusting a shared
app run by someone else, you register your own: it is free, private to you, and nobody else,
including the authors of Calendar Bridge, ever holds access to your calendar. The bridge also never
touches the access you gave ChatGPT or any other app.

## 1. Google: create your OAuth client

1. Open <https://console.cloud.google.com/>, signed in to the Google account you want to sync, and
   create a project (for example `calendar-bridge`).
2. Enable the **Google Calendar API**:
   <https://console.cloud.google.com/apis/library/calendar-json.googleapis.com> → **Enable**.
3. Open **Google Auth Platform** (formerly *OAuth consent screen*) and click **Get started**:
   - app name `Calendar Bridge`, your address as support e-mail;
   - audience **External** (or **Internal** if this is a Google Workspace account: then skip the
     publishing step 6 below);
   - your address as contact e-mail, accept the policy, **Create**.
4. **Data Access → Add or remove scopes**. Add exactly these three (paste them under *Manually add
   scopes* if the list is long), then **Save**:
   - `https://www.googleapis.com/auth/calendar.calendarlist.readonly`: see your list of calendars
   - `https://www.googleapis.com/auth/calendar.events.owned`: manage events on calendars you own
   - `https://www.googleapis.com/auth/calendar.app.created`: create its own calendars (the status
     calendar, and calendars you ask it to create)
5. **Branding**: add an *Application home page*, an *Application privacy policy link* and their
   domain under *Authorized domains*. Google requires these before an app can be published. The
   simplest way is to publish this repository's documentation from your own GitHub account:
   - fork <https://github.com/Nicolas1bhr/calendar-bridge> on GitHub;
   - in your fork: **Settings → Pages → Build and deployment**: *Deploy from a branch*, branch
     `main`, folder `/docs`, **Save**, and wait a minute;
   - home page `https://<you>.github.io/calendar-bridge/`, privacy policy
     `https://<you>.github.io/calendar-bridge/privacy.html`, authorized domain `<you>.github.io`.

   Any page you control works just as well.
6. **Audience → Publish app → In production.** This matters: while an *External* app is in
   *Testing*, Google expires its authorizations after **7 days** and synchronization would stop
   every week. Your own app does not need Google's verification; during consent you will see
   "Google hasn't verified this app", which is expected for an app you created yourself
   (**Advanced → Go to Calendar Bridge**).
7. **Clients → Create client → Application type: Desktop app**, name it `calendar-bridge`, then
   **Download JSON** in the dialog that follows. Google shows the client secret only once, so do it
   now, and keep the file somewhere safe (a password manager): you need it in step 3, and again if
   you ever connect from scratch.

> To sync a Google calendar that *someone else* owns and shared with you, add the scope
> `https://www.googleapis.com/auth/calendar.events` as well, and later connect with
> `calendar-bridge auth google --scope-profile shared`.

## 2. Apple: create an app-specific password

1. Go to <https://account.apple.com> → **Sign-In and Security → App-Specific Passwords**.
2. Create one named `Calendar Bridge`. It looks like `abcd-efgh-ijkl-mnop`; keep it for step 3.
   Never give the bridge your real Apple Account password (it refuses anything else anyway).

Changing your Apple Account password later revokes all app-specific passwords; you would then
create a new one and run `setup` again.

## 3. Install and run `setup`

On the always-on machine:

```bash
git clone https://github.com/Nicolas1bhr/calendar-bridge.git && cd calendar-bridge
docker compose pull                    # prebuilt image; or `docker compose build` to build it yourself
docker compose run --rm keygen         # creates secrets/calendar_bridge_key, once
mkdir -p config && cp ~/Downloads/client_secret_*.json config/   # the file from step 1.7, if it is on this machine
docker compose run --rm calendar-bridge setup
```

`keygen` creates the key that encrypts the stored credentials, readable only by the service, and
never replaces an existing key. Keep a copy somewhere safe, such as your password manager
(`sudo cat secrets/calendar_bridge_key`): without it, stored credentials and encrypted backups
cannot be read (you would simply connect both accounts again; your events are never at risk).

`setup` then walks you through five steps:

1. **Encryption key**: checked.
2. **Google**: give the client file as `/config/<file name>`, or paste its content. Open the address
   it prints in any browser, sign in, allow access, and paste back the address the browser ends up on
   (starting with `http://127.0.0.1:8765/`, it fails to load: that is expected). When the bridge
   runs directly on the computer you browse with (not in Docker), `setup --listen --open-browser`
   opens the browser and receives the answer itself.
3. **iCloud**: your Apple Account e-mail and the app-specific password (nothing is shown while you
   type or paste). The bridge discovers your calendars itself.
4. **Calendars**: it proposes your main iCloud calendar with your main Google calendar, which is the
   one ChatGPT and most Google tools use, then offers to sync your other iCloud calendars, each into
   its own new Google calendar (for example "Work (iCloud)").
5. **Review and start**: it reads both calendars completely and shows, **without writing
   anything**, what is already in both calendars, what would be copied each way, what differs (you
   will choose which version to keep), what is ambiguous (look-alike events it will not pair on its
   own) and what cannot be synchronized. Nothing happens until you confirm.

By default events that ended more than 365 days ago are left alone (`CB_DEFAULT_HISTORY_DAYS`,
or `all`). Run `setup` again whenever you want to add calendars or reconnect an account; finished
steps are skipped. Once it has run, delete the client file from `config/`, but keep a copy next to
the key in your password manager: Google does not show the client secret again, and you need it to
connect from scratch (for example after losing the key).

## 4. Start the service

```bash
docker compose up -d
docker compose logs -f calendar-bridge                         # Ctrl+C to stop following
docker compose exec calendar-bridge calendar-bridge status
```

Within a minute, the events from the plan appear on the other side. From then on, every change is
copied within about a minute, in both directions. `calendar-bridge activity` lists what it did.

## 5. Get alerted when something needs you (recommended)

Calendar Bridge alerts through **ntfy** (phone notifications), a **webhook** (Slack, Discord,
Mattermost...) or **e-mail**, when an authorization expires, a conflict waits for a decision,
deletions are held for review or synchronization stops. Alerts never contain event details.

For phone notifications: install the ntfy app, subscribe to a topic name nobody can guess (`setup`
prints one), and put it in a `.env` file next to `docker-compose.yml`:

```bash
echo 'CB_ALERT_NTFY_URL=https://ntfy.sh/calendar-bridge-<your random part>' >> .env
docker compose up -d                                          # apply
docker compose exec calendar-bridge calendar-bridge test-alert
```

Also recommended: a dead-man's switch such as <https://healthchecks.io>, which tells you when the
bridge stops entirely (machine down, container removed): `CB_HEARTBEAT_URL=https://hc-ping.com/<uuid>`.
All settings are listed in [`.env.example`](../.env.example).

## 6. Using it with ChatGPT

See [the README](../README.md#using-it-with-chatgpt) for the custom instruction that makes ChatGPT
check the "Calendar Bridge Status" calendar before claiming an event reached your iCloud calendar.

## 7. Optional: acceptance checks

[acceptance.md](acceptance.md) lists ten end-to-end checks with ChatGPT and an Apple device, and an
automated live test that runs against throw-away calendars.

## Without the wizard

Every step of `setup` is also a command, for scripting or for unusual layouts:

```bash
calendar-bridge auth google --client-secrets /config/client_secret.json
calendar-bridge auth icloud --apple-id you@example.com
calendar-bridge calendars                         # both providers' calendars with their ids
calendar-bridge mappings add --name main --icloud <id or name> --google primary
calendar-bridge reconcile --mapping 1 --details   # dry run: what the first sync would do
calendar-bridge reconcile --mapping 1 --apply     # record the plan and start
```

`mappings add --create-google-calendar "Work (iCloud)"` creates a new Google calendar for the pair;
`reconcile --apply --accept-ambiguous` starts even with look-alike events, which then stay
unsynchronized until you resolve them (see [operations](operations.md#conflicts)).
