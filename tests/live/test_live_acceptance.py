"""Live acceptance tests against the real Google Calendar API and iCloud CalDAV.

Opt-in only. They never touch your existing calendars: they create a dedicated Google test
calendar and a dedicated iCloud test calendar, map those two to each other in a temporary
database, run the scenarios, and delete only what they created.

Run (with credentials already authorized through ``calendar-bridge auth``):

    CB_LIVE=1 CB_LIVE_DATA_DIR=/path/to/data CB_ENCRYPTION_KEY_FILE=/path/to/key \\
        pytest -m live tests/live -v

Optional: CB_LIVE_ICLOUD_CALENDAR_URL=<url of an existing *empty test* calendar> if your
account does not allow creating calendars over CalDAV.
"""

from __future__ import annotations

import datetime as dt
import os
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from calendar_bridge.app import open_app
from calendar_bridge.config import Settings
from calendar_bridge.credentials import CredentialStore, StoredCredentialProviders
from calendar_bridge.crypto import SecretBox, load_keys
from calendar_bridge.db.database import Database
from calendar_bridge.db.repo import Repo
from calendar_bridge.providers.icloud.client import resource_url
from calendar_bridge.providers.icloud.convert import ICloudContext, icloud_to_canonical
from calendar_bridge.status.report import build_status
from calendar_bridge.sync.context import MappingContext
from calendar_bridge.sync.discovery import refresh_google_calendars, refresh_icloud_calendars
from calendar_bridge.sync.initial import apply_plan, build_plan

pytestmark = [pytest.mark.live, pytest.mark.skipif(os.environ.get("CB_LIVE") != "1", reason="live tests are opt-in")]
TZ = "Europe/Brussels"
TAG = f"cb-live-{dt.datetime.now(dt.UTC):%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}"


class Live:
    def __init__(self, tmp: Path) -> None:
        source = Path(os.environ["CB_LIVE_DATA_DIR"])
        key_file = Path(os.environ["CB_ENCRYPTION_KEY_FILE"]) if os.environ.get("CB_ENCRYPTION_KEY_FILE") else None
        box = SecretBox(load_keys(key_file))
        prod_db = Database(
            source / "calendar-bridge.db", __import__("calendar_bridge.clock", fromlist=["x"]).SystemClock()
        )
        prod_store = CredentialStore(Repo(prod_db), box)
        secrets, token, icloud = prod_store.google_client_secrets(), prod_store.google_token(), prod_store.icloud()
        prod_db.close()
        assert secrets and token and icloud, "authorize both providers first"
        self.settings = Settings(
            data_dir=tmp,
            key_file=key_file,
            default_history_days=None,
            status_calendar=False,
            icloud_url=icloud["url"],
            sync_interval=60,
        )
        app = open_app(self.settings)
        store = CredentialStore(app.repo, box)
        with app.db.tx():
            store.save_google_client_secrets(secrets)
            store.save_google_token(token)
            store.save_icloud(username=icloud["username"], password=icloud["password"], url=icloud["url"])
        self.app = app
        self.providers = StoredCredentialProviders(self.settings, store)
        app._providers = self.providers
        self.google = self.providers.google()
        self.icloud = self.providers.icloud()
        self.google_cal = ""
        self.icloud_url = ""
        self.created_icloud_calendar = False

    def sync(self) -> None:
        report = self.app.engine().run_cycle()
        errors = [e for m in report.mappings for e in m.errors] + report.errors
        assert not errors, errors

    def icloud_events(self) -> dict[str, Any]:
        listing = self.icloud.list_etags(self.icloud_url)
        fetched = self.icloud.multiget(self.icloud_url, sorted(listing))
        out = {}
        for path, value in fetched.items():
            if value is not None:
                event = icloud_to_canonical(value[1], ICloudContext(TZ)).event
                if event is not None:
                    out[event.master.summary] = (path, value[0], value[1], event)
        return out

    def google_events(self) -> dict[str, dict[str, Any]]:
        out = {}
        for page in self.google.iter_event_pages(self.google_cal):
            for event in page.get("items", []):
                if event.get("status") != "cancelled" and not event.get("recurringEventId"):
                    out[event.get("summary", "")] = event
        return out


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Live]:
    env = Live(tmp_path_factory.mktemp("live"))
    created = env.google.insert_calendar(f"Calendar Bridge test {TAG}", TZ, "Temporary calendar for live tests")
    env.google_cal = created["id"]
    discovery = env.icloud.discover()
    if os.environ.get("CB_LIVE_ICLOUD_CALENDAR_URL"):
        env.icloud_url = os.environ["CB_LIVE_ICLOUD_CALENDAR_URL"]
        assert not env.icloud.list_etags(env.icloud_url), "the given iCloud test calendar must be empty"
    else:
        env.icloud_url = discovery.home_url.rstrip("/") + f"/{TAG}/"
        body = (
            '<?xml version="1.0" encoding="utf-8"?><C:mkcalendar xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
            f"<D:set><D:prop><D:displayname>Calendar Bridge test {TAG}</D:displayname></D:prop></D:set></C:mkcalendar>"
        )
        env.icloud._send(
            "MKCALENDAR", env.icloud_url, body=body, headers={"Content-Type": "application/xml"}, context="mkcalendar"
        )
        env.created_icloud_calendar = True
    ctx = env.app.ctx
    refresh_google_calendars(ctx, env.google)
    refresh_icloud_calendars(ctx, env.icloud)
    repo = env.app.repo
    google_row = repo.calendar_by_remote("google", env.google_cal)
    icloud_row = next(
        c
        for c in repo.calendars("icloud")
        if c.remote_id.rstrip("/").endswith(env.icloud_url.rstrip("/").rsplit("/", 1)[-1])
    )
    with env.app.db.tx():
        repo.set_account("google", next(e["id"] for e in env.google.calendar_list() if e.get("primary")), "live")
        repo.set_account("icloud", discovery.principal_url, "live")
        mapping_id = repo.create_mapping(
            name="live", google_calendar_id=google_row.id, icloud_calendar_id=icloud_row.id, history_days=None
        )
    env.icloud_url = icloud_row.remote_id
    mc = MappingContext.build(ctx, repo.mapping(mapping_id))
    apply_plan(ctx, mc, build_plan(ctx, mc, env.google, env.icloud), accept_ambiguous=False)
    yield env
    # Cleanup: only what this module created.
    env.google.delete_calendar(env.google_cal)
    if env.created_icloud_calendar:
        etag_listing = env.icloud.list_etags(env.icloud_url)
        for path, etag in etag_listing.items():
            env.icloud.delete(resource_url(env.icloud_url, path), if_match=etag or "*")
        env.icloud._send("DELETE", env.icloud_url, context="delete test calendar")
    env.app.close()


def g_body(summary: str, start: str, end: str, **extra: Any) -> dict[str, Any]:
    return {
        "summary": summary,
        "start": {"dateTime": start, "timeZone": TZ},
        "end": {"dateTime": end, "timeZone": TZ},
        **extra,
    }


def test_1_google_create_reaches_icloud(live: Live) -> None:
    live.google.insert_event(live.google_cal, g_body("LT create", "2031-02-03T10:00:00", "2031-02-03T11:00:00"))
    live.sync()
    _path, _, ics, event = live.icloud_events()["LT create"]
    assert event.master.start.local == dt.datetime(2031, 2, 3, 10) and "ATTENDEE" not in ics


def test_2_google_update_reaches_icloud(live: Live) -> None:
    event = live.google_events()["LT create"]
    live.google.patch_event(live.google_cal, event["id"], {"summary": "LT updated", "location": "Brussels"})
    live.sync()
    assert live.icloud_events()["LT updated"][3].master.location == "Brussels"


def test_3_icloud_update_reaches_google(live: Live) -> None:
    path, etag, ics, _ = live.icloud_events()["LT updated"]
    live.icloud.put(
        resource_url(live.icloud_url, path), ics.replace("SUMMARY:LT updated", "SUMMARY:LT from iPhone"), if_match=etag
    )
    live.sync()
    assert "LT from iPhone" in live.google_events()


def test_4_deletions_both_ways(live: Live) -> None:
    google_event = live.google_events()["LT from iPhone"]
    live.google.delete_event(live.google_cal, google_event["id"])
    live.sync()
    assert "LT from iPhone" not in live.icloud_events()
    uid = f"{uuid.uuid4()}"
    ics = (
        f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//live//EN\r\nBEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:20310101T000000Z\r\n"
        f"DTSTART;TZID={TZ}:20310204T090000\r\nDTEND;TZID={TZ}:20310204T100000\r\nSUMMARY:LT icloud delete\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    url = live.icloud_url + f"{uid}.ics"
    live.icloud.put(url, ics, create=True)
    live.sync()
    assert "LT icloud delete" in live.google_events()
    etag = live.icloud.etag(url)
    live.icloud.delete(url, if_match=etag)
    live.sync()
    assert "LT icloud delete" not in live.google_events()


def test_5_icloud_only_event_becomes_manageable_in_google(live: Live) -> None:
    uid = f"{uuid.uuid4()}"
    ics = (
        f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//live//EN\r\nBEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:20310101T000000Z\r\n"
        f"DTSTART;VALUE=DATE:20310210\r\nDTEND;VALUE=DATE:20310211\r\nSUMMARY:LT all day\r\n"
        "BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT30M\r\nDESCRIPTION:x\r\nEND:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    live.icloud.put(live.icloud_url + f"{uid}.ics", ics, create=True)
    live.sync()
    event = live.google_events()["LT all day"]
    assert event["start"] == {"date": "2031-02-10"}
    assert event["reminders"]["overrides"] == [{"method": "popup", "minutes": 30}]


def test_6_recurrence_and_exceptions(live: Live) -> None:
    master = live.google.insert_event(
        live.google_cal,
        g_body("LT series", "2031-03-24T09:00:00", "2031-03-24T10:00:00", recurrence=["RRULE:FREQ=WEEKLY;COUNT=4"]),
    )
    live.sync()
    instances = live.google.search_events(live.google_cal, iCalUID=master["iCalUID"], singleEvents="true")
    second, third = sorted(i["id"] for i in instances if i.get("recurringEventId"))[1:3]
    live.google.patch_event(live.google_cal, second, {"summary": "LT series (moved)"})
    live.google.delete_event(live.google_cal, third)
    live.sync()
    _, _, ics, event = live.icloud_events()["LT series"]
    assert len(event.recurrence.exdates) == 1 and len(event.overrides) == 1, ics


def test_7_restart_does_not_duplicate(live: Live) -> None:
    before = (sorted(live.google_events()), sorted(live.icloud_events()))
    live.app.close()
    live.app = open_app(live.settings)
    live.app._providers = live.providers
    live.sync()
    live.sync()
    assert (sorted(live.google_events()), sorted(live.icloud_events())) == before


def test_8_provider_interruption_is_recovered(live: Live) -> None:
    real_password = live.icloud._password
    live.icloud.session.auth = ("wrong", "wrong")
    live.google.insert_event(live.google_cal, g_body("LT during outage", "2031-02-05T09:00:00", "2031-02-05T10:00:00"))
    report = live.app.engine().run_cycle()
    assert not report.ok
    live.icloud.session.auth = (live.icloud.username, real_password)
    live.sync()
    assert "LT during outage" in live.icloud_events()


def test_9_invitations_are_never_sent_or_impersonated(live: Live) -> None:
    live.google.insert_event(
        live.google_cal,
        g_body(
            "LT meeting",
            "2031-02-06T09:00:00",
            "2031-02-06T10:00:00",
            attendees=[{"email": "calendar-bridge-test@example.com"}],
        ),
    )  # reserved domain: undeliverable by design
    live.sync()
    _, _, ics, event = live.icloud_events()["LT meeting"]
    assert (
        "ATTENDEE" not in ics
        and "ORGANIZER" not in ics
        and "calendar-bridge-test@example.com" in event.master.description
    )


def test_10_status_reflects_verified_state(live: Live) -> None:
    time.sleep(1)
    status = build_status(live.app.ctx)
    (mapping,) = status["mappings"]
    assert mapping["last_icloud_write_verified_at"] and mapping["last_google_write_verified_at"]
    assert mapping["items"]["failed"] == 0 and mapping["items"]["conflicted"] == 0
