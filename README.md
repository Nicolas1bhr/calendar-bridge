# Calendar Bridge

Two-way sync between your **Apple iCloud calendar** and **Google Calendar**, running on your own
machine.

[![CI](https://github.com/Nicolas1bhr/calendar-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/Nicolas1bhr/calendar-bridge/actions/workflows/ci.yml)
[![License: GPL v3+](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)](LICENSE)

Keep using the Calendar app on your iPhone, iPad and Mac exactly as before, and let everything that
works with Google Calendar see and change the same events: ChatGPT and Gemini, Android phones,
Google Assistant, booking tools. Calendar Bridge copies each change to the other side within about
a minute, reads it back to make sure it landed, and tells you when something needs your attention.

```
iPhone · iPad · Mac  ⇄  iCloud  ⇄  Calendar Bridge (your machine)  ⇄  Google Calendar  ⇄  ChatGPT · Gemini · Android
```

## Why you can let it run unattended

| You might worry about | What Calendar Bridge does |
| --- | --- |
| A change lost, or copied twice, after a crash or an outage | Every write is recorded in a journal before it is sent and uses an identity chosen in advance, so a retry can never create a second copy. Nothing counts as done until it has been read back. |
| Losing events | An event is only deleted when the other side really deleted it, never because of an error, a timeout or an incomplete list. Many deletions at once are held until you confirm them. Deleted events can be restored for 90 days. |
| Overwriting an edit made at the same time | Every write is conditional on the version it last saw. If both sides changed the same detail differently, both versions are kept and you choose. |
| Invitations sent to your guests | Copies never carry the guest list (it is shown as text instead), and every write tells Google and iCloud not to notify anyone. |
| Your calendar leaving your home | It runs on your machine, only talks to Google and Apple over HTTPS, opens no ports, keeps its credentials encrypted and never logs event content. |
| Syncing more than you meant to | Only the calendar pairs you choose, after showing you exactly what the first sync will do. |

## What you need

- An always-on machine with Docker: a home server, a NAS, a Raspberry Pi 4 or 5, a small VPS
  (amd64 or arm64).
- A Google account, and an Apple Account with two-factor authentication.
- About 30 minutes. Most of it is spent creating your own free Google sign-in credentials
  ([why](docs/setup.md#why-your-own-google-cloud-project)).

## Quick start

1. Create a Google OAuth client and an Apple app-specific password: steps 1 and 2 of the
   [setup guide](docs/setup.md).
2. On the always-on machine:

   ```bash
   git clone https://github.com/Nicolas1bhr/calendar-bridge.git && cd calendar-bridge
   docker compose pull                              # or: docker compose build
   docker compose run --rm keygen                   # creates the encryption key, once
   docker compose run --rm calendar-bridge setup    # connect both accounts, choose calendars
   docker compose up -d
   ```

`setup` asks for the Google client file, sends you to Google to allow access, asks for the
app-specific password, proposes your main iCloud calendar with your main Google calendar (and,
if you like, your other iCloud calendars), shows what the first sync will do, and starts only when
you say so. You can run it again at any time.

## Day to day: nothing to do

```bash
docker compose exec calendar-bridge calendar-bridge status     # is everything in sync?
docker compose exec calendar-bridge calendar-bridge activity   # what did it change lately?
```

- **Alerts** on your phone (ntfy), by webhook or by e-mail when something needs you: an expired
  authorization, a conflict to decide, a sync that stopped. `setup` suggests a private ntfy topic.
- A **"Calendar Bridge Status"** calendar in your Google account summarizes its state, readable
  from any device, and by ChatGPT.
- **Conflicts** (the same detail changed differently on both sides) wait for your choice:
  `calendar-bridge conflicts list`, then `conflicts resolve <id> --keep google` or `--keep icloud`.

The [operations guide](docs/operations.md) covers everything else.

## Using it with ChatGPT

ChatGPT's Google Calendar connector works on your main Google calendar, so pairing your main iCloud
calendar with it lets ChatGPT manage your real iCloud calendar. To make sure ChatGPT never claims an
event is on your iPhone before it actually is, add this to its custom instructions:

> My Google primary calendar is synchronized with my iCloud calendar by a bridge that runs every
> minute. Before telling me an event is in my iCloud/Apple calendar, read today's event in the
> "Calendar Bridge Status" calendar: only events listed under "Confirmed in iCloud" are there.

The same works for any assistant that reads Google Calendar.

## What is synchronized

Titles, notes, locations, start and end times with their time zones, all-day events, recurring
events with moved and cancelled occurrences, reminders, free/busy and privacy. Guest lists and
video-call links appear as text on the copy. A few things cannot be represented on the other side
(for example Google's birthday and focus-time events); those are left untouched and listed, never
approximated. Details: [semantics and limitations](docs/semantics.md).

## Questions

**Why do I need my own Google Cloud project?** Google only lets a registered app keep lasting access
to a calendar. Registering your own private app (free) means nobody else, including the authors of
this project, ever holds access to your calendar.

**What if my server is off for a day?** Nothing is lost. Changes made meanwhile on either side are
picked up when it is back.

**Can it sync several calendars?** Yes. Each iCloud calendar is paired with its own Google calendar;
`setup` can create those Google calendars for you.

**Does it work without Docker?** Yes: install it with `pip install
git+https://github.com/Nicolas1bhr/calendar-bridge`, create a key with `calendar-bridge keys init
<key file>`, and set `CB_DATA_DIR` (where it keeps its database) and `CB_ENCRYPTION_KEY_FILE` (that
key file) both in your shell and in the service that runs `calendar-bridge daemon`, so that `setup`
and the service use the same data. Docker is the documented and tested way.

**How do I stop using it?** `calendar-bridge uninstall` removes its access to both accounts, then
`docker compose down -v` removes it. Your events stay where they are.

**Something is wrong. Where do I look?** `calendar-bridge doctor`, then
[troubleshooting](docs/troubleshooting.md).

## Documentation

- [Setup](docs/setup.md): Google and Apple credentials, installation, first sync
- [Operations](docs/operations.md): status, activity, alerts, conflicts, safety holds, upgrades
- [Troubleshooting](docs/troubleshooting.md): error messages and their fixes
- [Recovery](docs/recovery.md): backups, restore, re-authorization, incidents
- [Semantics and limitations](docs/semantics.md): exactly what is synchronized, and what is not
- [Security](docs/security.md): credentials, permissions, encryption, network, container
- [Architecture](docs/architecture.md): data model, sync algorithm, write journal, status contract
- [Acceptance](docs/acceptance.md): live tests and end-to-end checks

## Contributing and security

Bug reports and pull requests are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md). Report
vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## License

Calendar Bridge is free software: you can redistribute it and/or modify it under the terms of the
[GNU General Public License](LICENSE) as published by the Free Software Foundation, either version 3
of the License, or (at your option) any later version. It is distributed in the hope that it will
be useful, but without any warranty. Copyright © 2026 Nicolas Beeckman.
