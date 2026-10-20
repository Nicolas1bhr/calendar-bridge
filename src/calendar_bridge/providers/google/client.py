"""Minimal Google Calendar API v3 client over an authorized HTTP session.

Uses the REST API directly (through ``google-auth``'s ``AuthorizedSession``) so that conditional
requests (``If-Match``), timeouts and error classification are explicit.

Every write passes ``sendUpdates=none``: the bridge never causes Google to email anyone.
"""

from __future__ import annotations

import base64
import re
import secrets
from collections.abc import Iterator
from typing import Any, Protocol
from urllib.parse import quote

import requests
from google.auth.exceptions import GoogleAuthError, RefreshError

from calendar_bridge.errors import (
    AlreadyExists,
    AuthError,
    InvalidRequest,
    NotFound,
    PermissionDenied,
    PreconditionFailed,
    ProviderError,
    RateLimited,
    SyncTokenInvalid,
    TransientError,
)
from calendar_bridge.providers.retry import RetryPolicy, call_with_retries, parse_retry_after

API = "https://www.googleapis.com/calendar/v3"
API_LIBRARY = "https://console.cloud.google.com/apis/library/calendar-json.googleapis.com"
RATE_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "dailyLimitExceeded"})
OAUTH_REFUSAL = re.compile(r"\b(invalid_grant|deleted_client|invalid_client|unauthorized_client|invalid_scope)\b")
# Query parameters must be identical for the initial full sync and every incremental sync.
SYNC_PARAMS = {"singleEvents": "false", "showDeleted": "true", "maxResults": "2500"}


class ResponseLike(Protocol):
    status_code: int
    headers: Any
    reason: str

    def json(self) -> Any: ...


class SessionLike(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> ResponseLike: ...


def new_event_id() -> str:
    """Client-chosen event id (base32hex alphabet, as the API requires)."""
    raw = base64.b32hexencode(secrets.token_bytes(15)).decode("ascii").lower().rstrip("=")
    return "cb" + raw


def _q(value: str) -> str:
    return quote(value, safe="")


class GoogleCalendarClient:
    provider = "google"

    def __init__(self, session: SessionLike, *, timeout: float = 30.0, retry: RetryPolicy | None = None) -> None:
        self.session = session
        self.timeout = timeout
        self.retry = retry or RetryPolicy()

    # ------------------------------------------------------------ plumbing

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        context: str = "",
        retry: bool = True,
    ) -> Any:
        def once() -> Any:
            try:
                response = self.session.request(
                    method,
                    API + path,
                    params=params,
                    json=body,
                    headers=headers or {},
                    timeout=self.timeout,
                )
            except requests.exceptions.RequestException as exc:
                name = type(exc).__name__
                raise TransientError(f"network error: {name}", provider="google", reason=name) from exc
            except GoogleAuthError as exc:  # refreshing the access token failed
                raise _classify_auth_exception(exc) from exc
            if response.status_code >= 400:
                raise _error_for(response, context)
            if response.status_code == 204:
                return None
            try:
                return response.json()
            except ValueError:
                raise TransientError(
                    f"{context or path}: the answer is not JSON",
                    provider="google",
                    status=response.status_code,
                    reason="not_json",
                ) from None

        policy = self.retry if retry else RetryPolicy(attempts=1)
        return call_with_retries(once, policy, what=f"google {method} {context or path}")

    # ------------------------------------------------------------ calendars

    def calendar_list(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page_token = None
        while True:
            params: dict[str, Any] = {"maxResults": 250, "showHidden": "true"}
            if page_token:
                params["pageToken"] = page_token
            page = self._request("GET", "/users/me/calendarList", params=params, context="calendarList")
            items.extend(page.get("items", []))
            page_token = page.get("nextPageToken")
            if not page_token:
                return items

    def insert_calendar(self, summary: str, timezone: str, description: str = "") -> dict[str, Any]:
        # Not retried: if an answer is lost after Google created the calendar, a retry would
        # create a second one. Callers look for an existing calendar first instead.
        return dict(
            self._request(
                "POST",
                "/calendars",
                body={"summary": summary, "timeZone": timezone, "description": description},
                context="calendars.insert",
                retry=False,
            )
        )

    def delete_calendar(self, calendar_id: str) -> None:
        """Only ever used on calendars the bridge created itself (its status calendar, live-test
        calendars)."""
        self._request("DELETE", f"/calendars/{_q(calendar_id)}", context="calendars.delete")

    # ------------------------------------------------------------ events

    def list_events_page(
        self,
        calendar_id: str,
        *,
        sync_token: str | None = None,
        page_token: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = dict(SYNC_PARAMS)
        if sync_token:
            params["syncToken"] = sync_token
        if page_token:
            params["pageToken"] = page_token
        if extra:
            params.update(extra)
        return dict(self._request("GET", f"/calendars/{_q(calendar_id)}/events", params=params, context="events.list"))

    def iter_event_pages(self, calendar_id: str, *, sync_token: str | None = None) -> Iterator[dict[str, Any]]:
        page_token = None
        while True:
            page = self.list_events_page(calendar_id, sync_token=sync_token, page_token=page_token)
            yield page
            page_token = page.get("nextPageToken")
            if not page_token:
                return

    def search_events(self, calendar_id: str, **filters: str) -> list[dict[str, Any]]:
        """Non-incremental lookup (e.g. by iCalUID or private extended property)."""
        results: list[dict[str, Any]] = []
        page_token = None
        while True:
            params: dict[str, Any] = {"showDeleted": "true", "singleEvents": "false", "maxResults": "250"}
            params.update(filters)
            if page_token:
                params["pageToken"] = page_token
            page = self._request("GET", f"/calendars/{_q(calendar_id)}/events", params=params, context="events.search")
            results.extend(page.get("items", []))
            page_token = page.get("nextPageToken")
            if not page_token:
                return results

    def events_by_ical_uid(self, calendar_id: str, ical_uid: str) -> list[dict[str, Any]]:
        return self.search_events(calendar_id, iCalUID=ical_uid)

    def events_by_private_property(self, calendar_id: str, key: str, value: str) -> list[dict[str, Any]]:
        return self.search_events(calendar_id, privateExtendedProperty=f"{key}={value}")

    def get_event(self, calendar_id: str, event_id: str) -> dict[str, Any]:
        return dict(self._request("GET", f"/calendars/{_q(calendar_id)}/events/{_q(event_id)}", context="events.get"))

    def insert_event(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return dict(
            self._request(
                "POST",
                f"/calendars/{_q(calendar_id)}/events",
                params={"sendUpdates": "none", "conferenceDataVersion": "0"},
                body=body,
                context="events.insert",
            )
        )

    def patch_event(
        self, calendar_id: str, event_id: str, body: dict[str, Any], *, etag: str | None = None
    ) -> dict[str, Any]:
        headers = {"If-Match": etag} if etag else None
        return dict(
            self._request(
                "PATCH",
                f"/calendars/{_q(calendar_id)}/events/{_q(event_id)}",
                params={"sendUpdates": "none", "conferenceDataVersion": "0"},
                body=body,
                headers=headers,
                context="events.patch",
            )
        )

    def delete_event(self, calendar_id: str, event_id: str, *, etag: str | None = None) -> None:
        headers = {"If-Match": etag} if etag else None
        self._request(
            "DELETE",
            f"/calendars/{_q(calendar_id)}/events/{_q(event_id)}",
            params={"sendUpdates": "none"},
            headers=headers,
            context="events.delete",
        )

    def instances(self, calendar_id: str, event_id: str, *, original_start: str) -> list[dict[str, Any]]:
        page = self._request(
            "GET",
            f"/calendars/{_q(calendar_id)}/events/{_q(event_id)}/instances",
            params={"originalStart": original_start, "showDeleted": "true"},
            context="events.instances",
        )
        return list(page.get("items", []))


def _classify_auth_exception(exc: GoogleAuthError) -> ProviderError:
    """Only an OAuth error from Google (revoked or expired token, deleted or changed OAuth client,
    changed scopes) needs the user; anything else (network, a 5xx or an HTML page from the token
    endpoint) is temporary."""
    name = type(exc).__name__
    code = OAUTH_REFUSAL.search(str(exc))
    if isinstance(exc, RefreshError) and code:
        return AuthError(
            "Google refused the stored authorization (revoked or expired, or the OAuth client was deleted "
            f"or changed): {code.group(1)}",
            provider="google",
            reason=code.group(1),
        )
    return TransientError(f"refreshing the Google authorization failed: {name}", provider="google", reason=name)


def _error_for(response: ResponseLike, context: str) -> ProviderError:
    status = response.status_code
    try:
        error = (response.json() or {}).get("error") or {}
    except ValueError:
        error = {}
    if not isinstance(error, dict):
        error = {"message": str(error)}
    reasons = [e.get("reason") for e in error.get("errors", []) if isinstance(e, dict)]
    reasons += [d.get("reason") for d in error.get("details", []) if isinstance(d, dict) and d.get("reason")]
    reason = reasons[0] if reasons else None
    message = f"{context or 'request'}: HTTP {status} {reason or ''}".strip()
    kw: dict[str, Any] = {"provider": "google", "status": status, "reason": reason}
    if status == 401:
        return AuthError(message, **kw)
    if status == 429 or (status == 403 and set(reasons) & RATE_REASONS):
        return RateLimited(message, retry_after=parse_retry_after(response.headers.get("Retry-After")), **kw)
    if status == 403 and set(reasons) & {"accessNotConfigured", "SERVICE_DISABLED"}:
        link = re.search(r"https://console\.(?:developers|cloud)\.google\.com/\S+", str(error.get("message", "")))
        target = link.group(0).rstrip(".,)") if link else API_LIBRARY
        return PermissionDenied(
            f"the Google Calendar API is not enabled in your Google Cloud project: enable it at {target} and try again",
            **kw,
        )
    if status == 403 and "insufficientPermissions" in reasons:
        return PermissionDenied(
            f"{message}: the authorization does not cover this; to sync a calendar someone else owns, run "
            "`calendar-bridge auth google --scope-profile shared`",
            **kw,
        )
    if status == 403:
        return PermissionDenied(message, **kw)
    if status == 404:
        return NotFound(message, **kw)
    if status == 410:
        if context == "events.list" or "fullSyncRequired" in reasons:
            return SyncTokenInvalid(message, **kw)
        return NotFound(message, **kw)
    if status == 409:
        return AlreadyExists(message, **kw)
    if status == 412:
        return PreconditionFailed(message, **kw)
    if status >= 500:
        return TransientError(message, **kw)
    return InvalidRequest(message, **kw)
