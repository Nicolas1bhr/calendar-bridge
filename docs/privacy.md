# Privacy policy

*Effective 21 October 2026*

Calendar Bridge is open-source software that people install and run on their own computer. Each
installation is operated by the person who installed it and authorized it for their own Google
and Apple accounts. Nobody else, including the authors of the software, receives any data from
it. This policy describes what the software does with the data it can access.

## What data it accesses

- **Google Calendar**, through the Google Calendar API, with these permissions:
  - see the list of your calendars (`calendar.calendarlist.readonly`);
  - see, create, change and delete events on calendars you own (`calendar.events.owned`);
  - create its own calendars and manage their events (`calendar.app.created`): the
    "Calendar Bridge Status" calendar, and calendars you ask it to create for additional
    calendar pairs.
- **Apple iCloud Calendar**, through CalDAV with an app-specific password: the events of the
  iCloud calendars you choose to synchronize.

The events it reads include their title, time, location, notes, reminders, recurrence and the
guest list shown on an event.

## How the data is used

Only to keep the calendars you selected synchronized, and to show you the synchronization status
(in the command line, in a status file and in the "Calendar Bridge Status" calendar in your own
Google account). It is not used for advertising, profiling, analytics or training
machine-learning models, and it is never sold or shared.

## Where it is stored

- On your own machine, in a local database: copies of the synchronized events (needed to detect
  changes and to recover deleted events) and the synchronization state.
- The Google authorization and the Apple app-specific password are encrypted with a key that is
  kept outside that database. Local backups of the database are encrypted with the same key.
- Logs record what the service does, never the content of events.

## Sharing

Calendar data only travels, over HTTPS, between your machine, Google and Apple. If you configure
alerts (ntfy, a webhook or e-mail) or a monitoring service, they receive status messages only,
never event content.

## Retention and deletion

Data is kept while you use Calendar Bridge. Records of deleted events are purged after 90 days,
and only the most recent backups are kept. `calendar-bridge uninstall` revokes its access and
deletes its stored credentials and status calendar; removing its data volume deletes everything
else it stored. Your events stay in Google Calendar and iCloud.

## Revoking access

- Google: run `calendar-bridge uninstall`, or remove the app at
  <https://myaccount.google.com/permissions>.
- Apple: revoke its app-specific password at <https://account.apple.com> (Sign-In and Security →
  App-Specific Passwords).

## Google API Services User Data Policy

Calendar Bridge's use and transfer of information received from Google APIs adheres to the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements.

## Changes and questions

Changes to this policy are published here with a new effective date. Questions about the software:
open an issue at <https://github.com/Nicolas1bhr/calendar-bridge/issues>. Questions about a
particular installation: ask the person who operates it.
