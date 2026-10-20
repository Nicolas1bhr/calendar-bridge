"""HTTP-level behaviour of both provider clients: error classification, retries, preconditions."""

from __future__ import annotations

import datetime as dt
import email.utils
from typing import Any

import pytest
import requests

from calendar_bridge import errors
from calendar_bridge.providers.google.client import GoogleCalendarClient, new_event_id
from calendar_bridge.providers.icloud.client import CalDAVClient, normalize_path, parse_multistatus
from calendar_bridge.providers.retry import RetryPolicy, call_with_retries, parse_retry_after
from tests.fakes.google_api import FakeResponse


class ScriptedSession:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def google_error(status: int, reason: str) -> FakeResponse:
    return FakeResponse(status, {"error": {"code": status, "errors": [{"reason": reason}]}})


@pytest.mark.parametrize(
    ("status", "reason", "context_path", "expected"),
    [
        (401, "authError", "/calendars/c/events/e", errors.AuthError),
        (403, "rateLimitExceeded", "/calendars/c/events/e", errors.RateLimited),
        (403, "forbiddenForNonOrganizer", "/calendars/c/events/e", errors.PermissionDenied),
        (404, "notFound", "/calendars/c/events/e", errors.NotFound),
        (409, "duplicate", "/calendars/c/events/e", errors.AlreadyExists),
        (412, "conditionNotMet", "/calendars/c/events/e", errors.PreconditionFailed),
        (429, "rateLimitExceeded", "/calendars/c/events/e", errors.RateLimited),
        (500, "backendError", "/calendars/c/events/e", errors.TransientError),
        (400, "invalid", "/calendars/c/events/e", errors.InvalidRequest),
    ],
)
def test_google_error_classification(status: int, reason: str, context_path: str, expected: type) -> None:
    client = GoogleCalendarClient(ScriptedSession([google_error(status, reason)]), retry=RetryPolicy(attempts=1))
    with pytest.raises(expected):
        client.get_event("c", "e")


def test_google_410_on_list_means_full_sync_required() -> None:
    client = GoogleCalendarClient(
        ScriptedSession([google_error(410, "fullSyncRequired")]), retry=RetryPolicy(attempts=1)
    )
    with pytest.raises(errors.SyncTokenInvalid):
        client.list_events_page("c", sync_token="old")


def test_google_retries_transient_errors_and_network_failures() -> None:
    session = ScriptedSession(
        [
            google_error(503, "backendError"),
            requests.exceptions.ConnectionError("reset"),
            FakeResponse(200, {"id": "e"}),
        ]
    )
    client = GoogleCalendarClient(session, retry=RetryPolicy(attempts=3, base_delay=0, max_delay=0))
    assert client.get_event("c", "e") == {"id": "e"}
    assert len(session.calls) == 3


def test_google_writes_never_notify_guests_and_send_preconditions() -> None:
    session = ScriptedSession([FakeResponse(200, {"id": "e"}), FakeResponse(200, {"id": "e"}), FakeResponse(204)])
    client = GoogleCalendarClient(session, retry=RetryPolicy(attempts=1))
    client.insert_event("cal#1@x", {"id": "e"})
    client.patch_event("cal#1@x", "e", {"summary": "s"}, etag='"7"')
    client.delete_event("cal#1@x", "e", etag='"8"')
    for call in session.calls:
        assert call["params"]["sendUpdates"] == "none"
        assert "cal%231%40x" in call["url"]
    assert session.calls[1]["headers"] == {"If-Match": '"7"'}
    assert session.calls[2]["headers"] == {"If-Match": '"8"'}
    assert session.calls[0]["params"]["conferenceDataVersion"] == "0"


def test_new_event_ids_use_base32hex() -> None:
    for _ in range(50):
        event_id = new_event_id()
        assert set(event_id) <= set("0123456789abcdefghijklmnopqrstuv") and 5 <= len(event_id) <= 1024


def test_retry_policy_respects_retry_after() -> None:
    slept: list[float] = []
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise errors.RateLimited("slow down", retry_after=7, provider="x")
        return "ok"

    assert call_with_retries(flaky, RetryPolicy(attempts=2, max_delay=60), what="t", sleep=slept.append) == "ok"
    assert slept == [7]


# ---------------------------------------------------------------------- CalDAV XML


ICLOUD_SYNC_RESPONSE = b"""<?xml version="1.0" encoding="UTF-8"?>
<multistatus xmlns="DAV:">
  <response><href>/123456/calendars/home/</href><propstat><prop><getetag>"c"</getetag></prop><status>HTTP/1.1 200 OK</status></propstat></response>
  <response><href>/123456/calendars/home/A%20B.ics</href><propstat><prop><getetag>"1"</getetag></prop><status>HTTP/1.1 200 OK</status></propstat></response>
  <response><href>https://p12-caldav.icloud.com/123456/calendars/home/gone.ics</href><status>HTTP/1.1 404 Not Found</status></response>
  <sync-token>https://caldav.icloud.com/sync/42</sync-token>
</multistatus>"""


def test_parse_multistatus_handles_icloud_shapes() -> None:
    responses, token = parse_multistatus(ICLOUD_SYNC_RESPONSE, "https://p12-caldav.icloud.com/123456/calendars/home/")
    assert token == "https://caldav.icloud.com/sync/42"
    by_path = {r.path: r for r in responses}
    assert by_path["/123456/calendars/home/A%20B.ics"].text("DAV:", "getetag") == '"1"'
    assert by_path["/123456/calendars/home/gone.ics"].status == 404
    assert normalize_path("https://h/x/", "/x/A B.ics") == normalize_path("https://h/x/", "/x/A%20B.ics")


def test_entity_expansion_is_not_resolved() -> None:
    evil = b"""<?xml version="1.0"?><!DOCTYPE m [<!ENTITY x SYSTEM "file:///etc/passwd">]>
<multistatus xmlns="DAV:"><response><href>&x;</href><status>HTTP/1.1 404 Not Found</status></response></multistatus>"""
    responses, _ = parse_multistatus(evil, "https://h/")
    assert all("root" not in r.path for r in responses)


class DavSession(requests.Session):
    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        super().__init__()
        self._status, self._body, self._headers = status, body, headers or {}

    def request(self, method: str, url: str, *args: Any, **kwargs: Any) -> requests.Response:  # type: ignore[override]
        response = requests.Response()
        response.status_code = self._status
        response._content = self._body
        response.headers.update(self._headers)
        response.url = url
        return response


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (403, b'<D:error xmlns:D="DAV:"><D:valid-sync-token/></D:error>', errors.SyncTokenInvalid),
        (409, b'<D:error xmlns:D="DAV:"><D:valid-sync-token/></D:error>', errors.SyncTokenInvalid),
        (
            403,
            b"<D:error xmlns:D='DAV:'><C:no-uid-conflict xmlns:C='urn:ietf:params:xml:ns:caldav'>"
            b"<D:href>/1/calendars/home/x.ics</D:href></C:no-uid-conflict></D:error>",
            errors.UidConflict,
        ),
        (401, b"", errors.AuthError),
        (403, b"", errors.PermissionDenied),
        (412, b"", errors.PreconditionFailed),
        (503, b"", errors.RateLimited),
        (502, b"", errors.TransientError),
        (501, b"", errors.UnsupportedFeature),
    ],
)
def test_caldav_error_classification(status: int, body: bytes, expected: type) -> None:
    client = CalDAVClient(
        base_url="https://caldav.example/",
        username="u",
        password="p",
        retry=RetryPolicy(attempts=1),
        session=DavSession(status, body),
    )
    with pytest.raises(expected):
        client.sync_collection("https://caldav.example/1/calendars/home/", "tok")


def test_caldav_refuses_unconditional_writes() -> None:
    client = CalDAVClient(base_url="https://caldav.example/", username="u", password="p", session=DavSession(204, b""))
    with pytest.raises(errors.InvalidRequest):
        client.put("https://caldav.example/x.ics", "BEGIN:VCALENDAR")
    with pytest.raises(errors.InvalidRequest):
        client.delete("https://caldav.example/x.ics", if_match=None)


class PagedDavSession(requests.Session):
    """Two sync-collection pages: the first truncated (507 on the collection itself)."""

    def __init__(self) -> None:
        super().__init__()
        self.bodies: list[bytes] = []

    def request(self, method: str, url: str, *args: Any, **kwargs: Any) -> requests.Response:  # type: ignore[override]
        self.bodies.append(kwargs.get("data") or b"")
        page = len(self.bodies)
        response = requests.Response()
        response.status_code = 207
        response.url = url
        if page == 1:
            response._content = (
                b'<multistatus xmlns="DAV:"><response><href>/c/a.ics</href><propstat><prop><getetag>"1"</getetag></prop>'
                b"<status>HTTP/1.1 200 OK</status></propstat></response><response><href>/c/</href>"
                b"<status>HTTP/1.1 507 Insufficient Storage</status></response><sync-token>t2</sync-token></multistatus>"
            )
        else:
            response._content = (
                b'<multistatus xmlns="DAV:"><response><href>/c/b.ics</href><status>HTTP/1.1 404 Not Found</status>'
                b"</response><sync-token>t3</sync-token></multistatus>"
            )
        return response


def test_truncated_sync_collection_is_followed() -> None:
    session = PagedDavSession()
    client = CalDAVClient(
        base_url="https://h/", username="u", password="p", session=session, retry=RetryPolicy(attempts=1)
    )
    result = client.sync_collection("https://h/c/", "t1")
    assert result.changed == {"/c/a.ics": '"1"'} and result.deleted == {"/c/b.ics"} and result.token == "t3"
    assert b"<d:sync-token>t2</d:sync-token>" in session.bodies[1]


def caldav(session: requests.Session) -> CalDAVClient:
    return CalDAVClient(
        base_url="https://caldav.example/", username="u", password="p", retry=RetryPolicy(attempts=1), session=session
    )


def test_caldav_never_follows_redirects_and_a_redirected_write_is_not_a_success() -> None:
    client = caldav(DavSession(307, b"", {"Location": "https://elsewhere.example/x.ics"}))
    with pytest.raises(errors.TransientError, match="redirect"):
        client.put("https://caldav.example/c/x.ics", "BEGIN:VCALENDAR", if_match='"1"')
    with pytest.raises(errors.TransientError, match="redirect"):
        client.sync_collection("https://caldav.example/c/", "tok")


def test_caldav_refuses_plain_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CB_ALLOW_INSECURE_CALDAV", raising=False)
    with pytest.raises(errors.ConfigError, match="https://"):
        CalDAVClient(base_url="http://caldav.icloud.com/", username="u", password="p")


def test_caldav_never_reads_a_page_that_is_not_a_multistatus_as_empty() -> None:
    client = caldav(DavSession(200, b"<html><body>Sign in to the Wi-Fi</body></html>"))
    with pytest.raises(errors.TransientError, match="multistatus"):
        client.sync_collection("https://caldav.example/c/", "tok")


def test_caldav_calendar_data_that_is_not_text_is_left_alone() -> None:
    latin = "BEGIN:VCALENDAR\r\nSUMMARY:Café\r\nEND:VCALENDAR\r\n".encode("latin-1")
    assert caldav(DavSession(200, latin, {"ETag": '"1"'})).get("https://caldav.example/c/x.ics") == ('"1"', "")
    declared = caldav(DavSession(200, latin, {"Content-Type": "text/calendar; charset=iso-8859-1"}))
    assert "Café" in (declared.get("https://caldav.example/c/x.ics") or ("", ""))[1]
    bom = caldav(DavSession(200, "﻿BEGIN:VCALENDAR".encode()))
    assert (bom.get("https://caldav.example/c/x.ics") or ("", ""))[1] == "BEGIN:VCALENDAR"


def test_google_explains_a_disabled_calendar_api() -> None:
    message = (
        "Google Calendar API has not been used in project 123 before or it is disabled. Enable it by visiting "
        "https://console.developers.google.com/apis/api/calendar-json.googleapis.com/overview?project=123 then retry."
    )
    body = {"error": {"code": 403, "message": message, "errors": [{"reason": "accessNotConfigured"}]}}
    client = GoogleCalendarClient(ScriptedSession([FakeResponse(403, body)]), retry=RetryPolicy(attempts=1))
    with pytest.raises(errors.PermissionDenied, match=r"not enabled .*overview\?project=123 and try again"):
        client.calendar_list()


def test_google_daily_quota_is_rate_limiting_and_odd_answers_are_transient() -> None:
    session = ScriptedSession([google_error(403, "dailyLimitExceeded"), FakeResponse(200, None)])
    client = GoogleCalendarClient(session, retry=RetryPolicy(attempts=1))
    with pytest.raises(errors.RateLimited):
        client.get_event("c", "e")
    with pytest.raises(errors.TransientError, match="not JSON"):
        client.get_event("c", "e")


def test_google_refresh_failures_follow_google_auths_verdict() -> None:
    from google.auth.exceptions import RefreshError, TransportError

    session = ScriptedSession(
        [
            RefreshError("deleted_client: The OAuth client was deleted.", retryable=False),
            RefreshError("('invalid_grant: Token has been expired or revoked.', {...})", retryable=False),
            RefreshError("internal_failure", retryable=True),
            RefreshError("Unable to parse the token response: <html>502 Bad Gateway</html>", retryable=False),
            TransportError("dns failure"),
        ]
    )
    client = GoogleCalendarClient(session, retry=RetryPolicy(attempts=1))
    for code in ("deleted_client", "invalid_grant"):
        with pytest.raises(errors.AuthError, match=code):
            client.get_event("c", "e")
    for _ in range(3):  # temporary trouble never asks the user to reconnect
        with pytest.raises(errors.TransientError):
            client.get_event("c", "e")


def test_programming_errors_are_not_retried_as_if_transient() -> None:
    session = ScriptedSession([TypeError("not serializable")])
    client = GoogleCalendarClient(session, retry=RetryPolicy(attempts=3, base_delay=0, max_delay=0))
    with pytest.raises(TypeError):
        client.get_event("c", "e")
    assert len(session.calls) == 1


def test_creating_a_calendar_is_never_retried() -> None:
    session = ScriptedSession([google_error(503, "backendError"), FakeResponse(200, {"id": "x"})])
    client = GoogleCalendarClient(session, retry=RetryPolicy(attempts=3, base_delay=0, max_delay=0))
    with pytest.raises(errors.TransientError):
        client.insert_calendar("Work (iCloud)", "UTC")
    assert len(session.calls) == 1, "a lost answer must not create a second calendar"


def test_retry_after_accepts_seconds_and_http_dates() -> None:
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after("nan") is None and parse_retry_after("soon") is None and parse_retry_after(None) is None
    soon = email.utils.format_datetime(dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30), usegmt=True)
    assert 20 < (parse_retry_after(soon) or 0) <= 30
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0
