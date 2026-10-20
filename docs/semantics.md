# Semantics and limitations

What is synchronized, how, and — explicitly — what is not. When something cannot be represented
faithfully on the other side, the bridge keeps the original untouched, marks the event
`unsupported` (or adds a note to it) and tells you why. It never silently produces an approximate
copy and never "repairs" the original to match a limitation.

## Field mapping

| Field | Google Calendar | iCloud (iCalendar) | Notes |
| --- | --- | --- | --- |
| Title | `summary` | `SUMMARY` | surrounding whitespace ignored |
| Notes | `description` (may be HTML) | `DESCRIPTION` (plain text) | Google rich text becomes plain text (links kept) in iCloud |
| Location | `location` | `LOCATION` | Apple's map pin (`X-APPLE-STRUCTURED-LOCATION`) is preserved, not synced |
| Start / end | `start`/`end` (`date` or `dateTime` + `timeZone`) | `DTSTART`/`DTEND` or `DURATION` | see time zones below |
| All-day | `date` | `VALUE=DATE` | end date exclusive on both sides |
| Status | `confirmed`, `tentative` | `STATUS` | see cancelled events below |
| Free/busy | `transparency` | `TRANSP` | |
| Privacy | `visibility` (`default` / `public` / `private` / `confidential`) | `CLASS` (absent = default) | |
| Alerts | `reminders` (popup / email, minutes before) | `VALARM` (`DISPLAY`/`AUDIO`/`EMAIL`) | see reminders below |
| URL | `source.url` | `URL` | Google accepts only http(s) URLs |
| Recurrence | `recurrence` (RRULE/RDATE/EXDATE) | `RRULE`/`RDATE`/`EXDATE` | RRULE parts normalized, never expanded |
| Modified occurrence | instance event (`recurringEventId`, `originalStartTime`) | `VEVENT` with `RECURRENCE-ID` | identity = original start |
| Cancelled occurrence | cancelled instance or `EXDATE` | `EXDATE` or `STATUS:CANCELLED` override | treated as the same thing |
| Guests / organizer | `attendees` / `organizer` | `ATTENDEE` / `ORGANIZER` | **shown as text on the mirror only** — see meetings |
| Video call | `conferenceData`, `hangoutLink` | `X-GOOGLE-CONFERENCE`, `CONFERENCE` | shown as text; never created or regenerated |

Everything else is preserved but not synchronized: Google event colors, attachments, guest
permissions, other apps' extended properties; Apple's `X-APPLE-*` properties, travel time,
structured locations, alerts Google cannot hold. Updates rewrite only the changed fields (Google
`PATCH`, in-place iCalendar edits), so unrelated edits never discard this data.

## Time zones

- Times keep their original zone. Windows zone names ("Romance Standard Time") and vendor-prefixed
  names (`/mozilla.org/.../Europe/Brussels`) are mapped to IANA names, which Google requires.
- UTC times stay UTC. Google times without a `timeZone` are kept as exact instants.
- **Floating** times (no zone, iCalendar only) have no Google equivalent; the Google copy is
  pinned to a home zone and the event carries a note. The iCloud original stays floating. The home
  zone is `CB_DEFAULT_TIMEZONE` if you set one, otherwise your Google account's zone, recorded when
  the first calendar pair starts and kept from then on (occurrences of a floating recurring event
  are identified by their local time, so the zone must not drift under them).
- A single event in a custom zone that has no IANA equivalent is copied as its exact UTC instant
  (noted). A **recurring** event in such a zone is `unsupported`, because its daylight-saving
  behaviour could not be reproduced. A zone name with no definition at all is `unsupported`.
- Recurring events keep their wall-clock time across daylight-saving changes on both sides.

## Recurring events

Supported: whole-series changes, single-occurrence changes, cancelled and restored occurrences,
moved occurrences, `RDATE`, `EXDATE`, series ending (`UNTIL`/`COUNT`), splitting a series
("this and following" — both Google and Apple implement it as ending the old series and creating a
new one, which the bridge mirrors as exactly that).

Not supported (reported, original kept): `RANGE=THISANDFUTURE` overrides, `EXRULE` (deprecated
in RFC 5545), `RDATE` with periods, a resource containing several unrelated events. A modified
occurrence on a date the Google series does not generate cannot be stored by Google and is noted.

If one side changes the series timing while the other edits individual occurrences, the item
becomes a conflict: occurrence identities depend on the series timing, so merging would be
unsafe. When Google itself discards occurrence changes after a series edit (its documented
behaviour), the bridge reports the difference instead of deleting them in iCloud.

## Reminders

- Google's popup reminders ↔ iCloud display/sound alerts; Google e-mail reminders ↔ iCloud e-mail
  alerts.
- Google events that use the calendar's **default reminders** (timed events) are copied with
  those defaults written explicitly. Google does not expose all-day default reminders through
  its API; such all-day events get no alert in iCloud (noted).
- Alerts Google cannot hold — after the start (Apple's "on day of event, 9:00" for all-day
  events), absolute dates on recurring events, more than 5, more than 4 weeks — stay in iCloud
  untouched and are not copied. Apple's explicit "None" alert is preserved.
- Reminder changes are detected by comparing content (Google does not always update the event's
  modification time for them).

## Meetings and invitations

The bridge must never send invitations, cancellations or RSVPs, and must never impersonate an
organizer. Therefore:

- **Mirrors never contain real guests.** The copy on the other provider has no
  attendees/organizer; the guest list, RSVP states and call links appear as a read-only block in
  its notes ("— Calendar Bridge: synced details (not editable) —"). That block is stripped when the
  mirror is read, so it never syncs back.
- Google writes always use `sendUpdates=none` and never create conference links; CalDAV writes
  carry `Schedule-Reply: F`. iCloud copies created by the bridge contain no `ORGANIZER`/`ATTENDEE`,
  so iCloud's server-side scheduling has nothing to send.
- A meeting belongs to the provider where **you are the organizer** (a meeting you organize on
  your iPhone that also reached Gmail as an invitation belongs to iCloud); otherwise to the provider
  holding the guest list. Changes made there flow to the mirror normally.
- **Hard guard:** an iCloud meeting you organize (one iCloud would send invitations, updates or
  cancellations for) is never changed or deleted automatically, except for personal fields
  (alerts, free/busy, visibility). Anything else becomes a `guest_managed` conflict, even if the
  ownership were ever misjudged. A new modified occurrence inside such a meeting keeps the
  meeting's organizer and guests, so it cannot read as "cancelled for everyone". Changes made **on the mirror** to shared fields (title, time, place, notes,
  occurrences) or deleting the mirror become a `guest_managed` conflict for you to confirm,
  because applying them to the original is where guests could be notified (iCloud's own
  scheduling may e-mail guests about a time change you confirm; Google is always told not to).
  Reminders, free/busy and visibility on the mirror sync without asking.
  `CB_MEETING_POLICY=propagate` applies mirror edits directly instead.
- Adding guests on the mirror is not copied to the owner (noted).

## Cancelled and deleted events

- Google represents cancellation as deletion. A deleted Google event deletes its iCloud copy.
- iCloud can show an event as *cancelled* (`STATUS:CANCELLED`, e.g. an organizer cancelled an
  invitation). For events owned by iCloud, the Google copy is removed while the event is
  cancelled and re-created if it is reinstated; the iCloud event itself is never deleted for this.
- Deletions require explicit evidence and are held for review when many happen at once
  ([operations](operations.md#safety-holds)). Deleted events are kept as tombstones for 90 days.

## Special Google event types

| Type | Default |
| --- | --- |
| `default` | synchronized |
| `fromGmail` (flights, reservations from Gmail) | copied to iCloud (`CB_MIRROR_GOOGLE_EVENT_TYPES`); Google restricts editing them, so edits from iCloud may come back as `rejected` |
| `outOfOffice`, `focusTime`, `workingLocation`, `birthday` | `unsupported` — they have behaviours (auto-decline, working location, contacts' birthdays) iCloud cannot represent |

## Calendars

- Only explicit one-to-one mappings are synchronized; a calendar can be in one mapping only.
- Read-only calendars (holidays, other people's calendars without write access, subscribed
  calendars) cannot be mapped. Tasks/reminders (`VTODO`) are not events and are ignored.
- Moving an event between two *mapped* calendars is synchronized as a deletion in one and a
  creation in the other (the event identity is not carried across mappings).
- Removing a mapping deletes nothing.

## What "synced" means

An item is `synced` only after the written copy was read back from the provider and every field
the bridge wrote matched what was intended (modulo the documented limitations above). A Google
event that the bridge has not processed yet is **not** reported as being in iCloud; the status
calendar lists events as "Confirmed in iCloud" only after that verification.
