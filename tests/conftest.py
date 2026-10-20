from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from hypothesis import settings as settings_profiles

from calendar_bridge.app import App, open_app
from calendar_bridge.config import Settings
from calendar_bridge.credentials import CredentialStore
from calendar_bridge.crypto import generate_key
from calendar_bridge.model.canonical import CanonicalEvent
from calendar_bridge.providers.google.auth import ClientSecrets
from calendar_bridge.providers.google.client import GoogleCalendarClient
from calendar_bridge.providers.icloud.client import CalDAVClient, resource_url
from calendar_bridge.providers.icloud.convert import ICloudContext, icloud_to_canonical
from calendar_bridge.providers.retry import RetryPolicy
from calendar_bridge.sync.context import MappingContext
from calendar_bridge.sync.discovery import refresh_google_calendars, refresh_icloud_calendars
from calendar_bridge.sync.engine import CycleReport
from calendar_bridge.sync.initial import apply_plan, build_plan
from tests.fakes.caldav_server import CalDAVServer
from tests.fakes.google_api import FakeGoogle

GOOGLE_ACCOUNT = "me@gmail.com"
TZ = "Europe/Brussels"

logging.getLogger("radicale").setLevel(logging.ERROR)

settings_profiles.register_profile("default", max_examples=120)
settings_profiles.register_profile("deep", max_examples=1500)
settings_profiles.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))


class TestProviders:
    __test__ = False

    def __init__(self, google: GoogleCalendarClient, icloud: CalDAVClient) -> None:
        self._google = google
        self._icloud = icloud

    def google(self) -> GoogleCalendarClient:
        return self._google

    def icloud(self) -> CalDAVClient:
        return self._icloud

    def after_cycle(self) -> None:
        return None


@pytest.fixture
def caldav_server(tmp_path: Path) -> Iterator[CalDAVServer]:
    server = CalDAVServer(tmp_path / "radicale")
    yield server
    server.stop()


@pytest.fixture
def fake_google() -> FakeGoogle:
    google = FakeGoogle()
    google.add_calendar(GOOGLE_ACCOUNT, GOOGLE_ACCOUNT, primary=True)
    google.add_calendar("work@group.calendar.google.com", "Work")
    google.add_calendar("en.be#holiday@group.v.calendar.google.com", "Holidays", access="reader")
    return google


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caldav_server: CalDAVServer) -> Settings:
    monkeypatch.setenv("CB_ENCRYPTION_KEY", generate_key())
    monkeypatch.setenv("CB_ALLOW_INSECURE_CALDAV", "1")
    return Settings(
        data_dir=tmp_path / "data",
        key_file=None,
        sync_interval=15,
        retry_attempts=1,
        retry_base_delay=0.0,
        default_history_days=None,
        status_calendar=False,
        icloud_url=caldav_server.base_url,
        item_retry_base=0,
        item_retry_max=0,
    )


@dataclasses.dataclass
class Bridge:
    """Test harness: the bridge plus 'user' access to both calendars."""

    __test__ = False

    app: App
    google: FakeGoogle
    server: CalDAVServer
    google_client: GoogleCalendarClient
    icloud_client: CalDAVClient
    google_cal: str = GOOGLE_ACCOUNT
    icloud_url: str = ""
    mapping_id: int = 0

    # ---------------------------------------------------------------- bridge control
    def sync(self, **kw: Any) -> CycleReport:
        report = self.app.engine().run_cycle(**kw)
        return report

    def sync_ok(self, **kw: Any) -> CycleReport:
        report = self.sync(**kw)
        errors = [e for m in report.mappings for e in m.errors] + report.errors
        assert not errors, errors
        return report

    def map(
        self,
        *,
        history_days: int | None = None,
        apply: bool = True,
        accept_ambiguous: bool = False,
        icloud_name: str = "home",
    ) -> Any:
        ctx = self.app.ctx
        refresh_google_calendars(ctx, self.google_client)
        discovery = refresh_icloud_calendars(ctx, self.icloud_client)
        repo = self.app.repo
        store = CredentialStore(repo, self.app.box())
        with self.app.db.tx():
            repo.set_account("google", GOOGLE_ACCOUNT, GOOGLE_ACCOUNT)
            repo.set_account("icloud", discovery.principal_url, self.server.user)
            store.save_google_client_secrets(ClientSecrets("client-id", "client-secret"))
            store.save_google_token({"refresh_token": "test-refresh", "token": None, "scopes": []})
            store.save_icloud(username=self.server.user, password=self.server.password, url=self.server.base_url)
        google_row = repo.calendar_by_remote("google", self.google_cal)
        icloud_row = next(c for c in repo.calendars("icloud") if c.remote_id.rstrip("/").endswith(icloud_name))
        assert google_row and icloud_row
        with self.app.db.tx():
            self.mapping_id = repo.create_mapping(
                name="main",
                google_calendar_id=google_row.id,
                icloud_calendar_id=icloud_row.id,
                history_days=history_days,
            )
        self.icloud_url = icloud_row.remote_id
        mc = MappingContext.build(ctx, repo.mapping(self.mapping_id))
        plan = build_plan(ctx, mc, self.google_client, self.icloud_client)
        if apply:
            apply_plan(ctx, mc, plan, accept_ambiguous=accept_ambiguous)
        return plan

    def items(self, state: str | None = None) -> list[Any]:
        return self.app.repo.items(self.mapping_id, state)

    def conflicts(self) -> list[Any]:
        return self.app.repo.conflicts()

    # ---------------------------------------------------------------- google "user" side
    def g_create(self, summary: str, start: str, end: str, tz: str | None = TZ, **extra: Any) -> str:
        body: dict[str, Any] = {"summary": summary, **extra}
        if len(start) == 10:
            body["start"], body["end"] = {"date": start}, {"date": end}
        else:
            body["start"] = {"dateTime": start, **({"timeZone": tz} if tz else {})}
            body["end"] = {"dateTime": end, **({"timeZone": tz} if tz else {})}
        return str(self.google.user_insert(self.google_cal, body)["id"])

    def g_patch(self, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.google.user_patch(self.google_cal, event_id, body)

    def g_delete(self, event_id: str) -> None:
        self.google.user_delete(self.google_cal, event_id)

    def g_live(self) -> list[dict[str, Any]]:
        return self.google.live_events(self.google_cal)

    def g_by_summary(self, summary: str) -> dict[str, Any]:
        found = [e for e in self.g_live() if e.get("summary") == summary]
        assert len(found) == 1, (summary, [e.get("summary") for e in self.g_live()])
        return found[0]

    # ---------------------------------------------------------------- icloud "user" side
    def i_put(self, ics: str, name: str | None = None, etag: str | None = None) -> str:
        href = name or f"{uuid.uuid4().hex.upper()}.ics"
        url = self.icloud_url + href if not href.startswith("http") else href
        self.server.put(url, ics, etag)
        return url

    def i_create(
        self,
        summary: str,
        start: str,
        end: str,
        *,
        uid: str | None = None,
        extra: str = "",
        tz: str | None = TZ,
        components: str = "",
    ) -> tuple[str, str]:
        uid = uid or f"{uuid.uuid4()}"
        if len(start) == 8:
            times = f"DTSTART;VALUE=DATE:{start}\r\nDTEND;VALUE=DATE:{end}\r\n"
        elif tz:
            times = f"DTSTART;TZID={tz}:{start}\r\nDTEND;TZID={tz}:{end}\r\n"
        else:
            times = f"DTSTART:{start}\r\nDTEND:{end}\r\n"
        ics = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Apple Inc.//iOS 18//EN\r\nCALSCALE:GREGORIAN\r\n"
            f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:20300101T000000Z\r\n{times}SUMMARY:{summary}\r\n{extra}"
            "END:VEVENT\r\n" + components + "END:VCALENDAR\r\n"
        )
        return self.i_put(ics), uid

    def i_resources(self) -> dict[str, str]:
        return self.server.list(self.icloud_url)

    def i_events(self) -> dict[str, CanonicalEvent]:
        ctx = ICloudContext(default_tz=TZ)
        out = {}
        for ics in self.i_resources().values():
            conv = icloud_to_canonical(ics, ctx)
            assert conv.event is not None and conv.uid
            out[conv.uid] = conv.event
        return out

    def i_by_summary(self, summary: str) -> tuple[str, str]:
        """(url, ics) of the single iCloud resource with this title."""
        found = []
        for href, ics in self.i_resources().items():
            conv = icloud_to_canonical(ics, ICloudContext(default_tz=TZ))
            if conv.event is not None and conv.event.master.summary == summary:
                found.append((resource_url(self.icloud_url, href), ics))
        assert len(found) == 1, (summary, len(found))
        return found[0]

    def i_edit(self, summary: str, transform: Any) -> str:
        url, _ics = self.i_by_summary(summary)
        etag, current = self.server.get(url) or (None, "")
        new = transform(current)
        self.server.put(url, new, etag)
        return url


def make_bridge(settings: Settings, google: FakeGoogle, server: CalDAVServer, *, clock: Any = None) -> Bridge:
    google_client = GoogleCalendarClient(google.session(), retry=RetryPolicy(attempts=1, base_delay=0))
    icloud_client = CalDAVClient(
        base_url=server.base_url,
        username=server.user,
        password=server.password,
        retry=RetryPolicy(attempts=1, base_delay=0),
    )
    server.make_calendar("home", "Home")
    app = open_app(settings, providers=TestProviders(google_client, icloud_client), clock=clock)
    return Bridge(app, google, server, google_client, icloud_client)


@pytest.fixture
def bridge(settings: Settings, fake_google: FakeGoogle, caldav_server: CalDAVServer) -> Iterator[Bridge]:
    harness = make_bridge(settings, fake_google, caldav_server)
    yield harness
    harness.app.close()


def reopen(b: Bridge) -> Bridge:
    """Simulate a process restart on the same database and providers."""
    b.app.close()
    app = open_app(b.app.settings, providers=TestProviders(b.google_client, b.icloud_client))
    return dataclasses.replace(b, app=app)


def future(days: int = 30, hour: int = 10) -> dt.datetime:
    return dt.datetime(2030, 3, 1, hour, 0) + dt.timedelta(days=days)
