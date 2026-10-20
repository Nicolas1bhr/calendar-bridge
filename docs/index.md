# Calendar Bridge

Two-way sync between your **Apple iCloud calendar** and **Google Calendar**, running on your own
machine.

Keep using the Calendar app on your iPhone, iPad and Mac as you always have, and let everything
that works with Google Calendar see and change the same events: ChatGPT and Gemini, Android,
Google Assistant, booking tools. Calendar Bridge copies every change in both directions within
about a minute and checks every copy it writes.

- **Two-way and real:** create, change or delete events on either side; your actual iCloud
  calendar is updated, not a read-only subscription.
- **Faithful:** recurring events and their exceptions, all-day events, time zones, reminders.
- **Safe:** nothing is deleted without proof, concurrent edits are never overwritten, no
  invitation is ever sent, and crashes or outages never lose or duplicate an event.
- **Private:** it runs on your machine, talks only to Google and Apple, keeps its credentials
  encrypted and never logs event content.

It is free, open-source software that you install yourself; there is no account to create and no
service in between.

- [Source code and quick start](https://github.com/Nicolas1bhr/calendar-bridge)
- [Setup guide](setup.md)
- [Privacy policy](privacy.md)

## Google permissions it asks for

| Permission | Why |
| --- | --- |
| See the list of your calendars (`calendar.calendarlist.readonly`) | find the calendars to synchronize |
| See, create, change and delete events on calendars you own (`calendar.events.owned`) | keep events in sync |
| Create its own calendars and manage their events (`calendar.app.created`) | the "Calendar Bridge Status" calendar, and calendars you ask it to create |

It never sends invitations or updates to other people, and it cannot change your calendars'
sharing settings or delete your calendars.
