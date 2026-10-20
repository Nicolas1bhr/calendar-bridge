"""CalDAV client for iCloud.

Discovery (current-user-principal -> calendar-home-set -> calendar-user-address-set) uses the
maintained ``caldav`` library. Synchronization traffic (sync-collection, calendar-multiget,
conditional PUT/DELETE) is issued directly so that every per-resource status is inspected
explicitly: a resource only counts as deleted when the server says so for that exact URL
(``404``/``410``), never because it is absent from a listing or because a request failed.
``caldav`` turns every 403 into a generic authorization error, which would hide the
``valid-sync-token`` and ``no-uid-conflict`` preconditions this module depends on.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import re
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from xml.sax.saxutils import escape

import requests
from lxml import etree

from calendar_bridge import __version__
from calendar_bridge.errors import (
    AuthError,
    ConfigError,
    InvalidRequest,
    NotFound,
    PermissionDenied,
    PreconditionFailed,
    ProviderError,
    RateLimited,
    SyncTokenInvalid,
    TransientError,
    UidConflict,
    UnsupportedFeature,
)
from calendar_bridge.logs import get_logger
from calendar_bridge.providers.retry import RetryPolicy, call_with_retries, parse_retry_after

log = get_logger("caldav")
DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
CS = "http://calendarserver.org/ns/"
APPLE = "http://apple.com/ns/ical/"
MULTIGET_BATCH = 50
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
_SAFE_PATH = "/:@!$&'()*+,;=-._~"


def _tag(ns: str, name: str) -> str:
    return f"{{{ns}}}{name}"


def normalize_path(base_url: str, href: str) -> str:
    path = urlsplit(urljoin(base_url, href.strip())).path
    return quote(unquote(path), safe=_SAFE_PATH)


def resource_url(collection_url: str, path: str) -> str:
    parts = urlsplit(collection_url)
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _status_code(text: str | None) -> int | None:
    if not text:
        return None
    pieces = text.split()
    for piece in pieces[1:2]:
        if piece.isdigit():
            return int(piece)
    return None


@dataclasses.dataclass
class DavResponse:
    path: str
    status: int | None
    props: dict[str, Any]
    missing: set[str]

    def text(self, ns: str, name: str) -> str | None:
        element = self.props.get(_tag(ns, name))
        if element is None:
            return None
        return element.text if element.text is not None else ""


def parse_multistatus(content: bytes, base_url: str) -> tuple[list[DavResponse], str | None]:
    try:
        root = etree.fromstring(content, _PARSER)
    except etree.XMLSyntaxError as exc:
        # The parser's message can quote bytes of the document (event content): keep it out.
        raise TransientError("malformed multistatus response", provider="icloud", reason="xml") from exc
    if root.tag != _tag(DAV, "multistatus"):
        # E.g. a captive portal or proxy page: never read it as an empty listing.
        raise TransientError("unexpected answer instead of a WebDAV multistatus", provider="icloud", reason="xml")
    responses: list[DavResponse] = []
    for response in root.findall(_tag(DAV, "response")):
        status = _status_code(response.findtext(_tag(DAV, "status")))
        props: dict[str, Any] = {}
        missing: set[str] = set()
        for propstat in response.findall(_tag(DAV, "propstat")):
            prop_status = _status_code(propstat.findtext(_tag(DAV, "status")))
            prop = propstat.find(_tag(DAV, "prop"))
            if prop is None:
                continue
            for child in prop:
                if prop_status == 200:
                    props[child.tag] = child
                else:
                    missing.add(child.tag)
        for href in response.findall(_tag(DAV, "href")):
            if href.text:
                responses.append(DavResponse(normalize_path(base_url, href.text), status, props, missing))
    return responses, root.findtext(_tag(DAV, "sync-token"))


def _precondition(body: bytes) -> tuple[str | None, str | None]:
    """Name of a DAV error precondition in an error body, plus an href it carries."""
    try:
        root = etree.fromstring(body, _PARSER)
    except etree.XMLSyntaxError:
        return None, None
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        local = element.tag.split("}")[-1]
        if local in ("valid-sync-token", "no-uid-conflict", "need-privileges", "number-of-matches-within-limits"):
            href = element.findtext(_tag(DAV, "href"))
            return local, href
    return None, None


@dataclasses.dataclass
class SyncResult:
    changed: dict[str, str | None]
    deleted: set[str]
    token: str | None


@dataclasses.dataclass
class CalendarInfo:
    url: str
    display_name: str
    writable: bool
    supports_events: bool
    sync_token: str | None
    ctag: str | None
    timezone: str | None
    is_default: bool = False


@dataclasses.dataclass
class Discovery:
    principal_url: str
    home_url: str
    addresses: list[str]
    calendars: list[CalendarInfo]


class CalDAVClient:
    provider = "icloud"

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        timeout: float = 30.0,
        retry: RetryPolicy | None = None,
        session: requests.Session | None = None,
    ) -> None:
        if urlsplit(base_url).scheme != "https" and not os.environ.get("CB_ALLOW_INSECURE_CALDAV"):
            # The password travels in every request: never over plain HTTP.
            raise ConfigError(f"the CalDAV address must start with https:// (got {base_url!r})")
        self.base_url = base_url
        self.username = username
        self._password = password
        self.timeout = timeout
        self.retry = retry or RetryPolicy()
        self.session = session or requests.Session()
        self.session.auth = requests.auth.HTTPBasicAuth(username, password)
        self.session.headers["User-Agent"] = f"calendar-bridge/{__version__}"

    # ------------------------------------------------------------ plumbing

    def _send(
        self,
        method: str,
        url: str,
        *,
        body: str | None = None,
        headers: dict[str, str] | None = None,
        context: str,
    ) -> requests.Response:
        def once() -> requests.Response:
            try:
                response = self.session.request(
                    method,
                    url,
                    data=body.encode("utf-8") if body is not None else None,
                    headers=headers or {},
                    timeout=self.timeout,
                    # Never follow redirects: requests drops the credentials on another host, and a
                    # write answered with a redirect has not happened.
                    allow_redirects=False,
                )
            except requests.exceptions.RequestException as exc:
                raise TransientError(
                    f"network error: {type(exc).__name__}", provider="icloud", reason=type(exc).__name__
                ) from exc
            if response.status_code >= 400:
                raise self._error(response, context)
            if not 200 <= response.status_code < 300:
                raise TransientError(
                    f"{context}: unexpected HTTP {response.status_code} redirect",
                    provider="icloud",
                    status=response.status_code,
                    reason="redirect",
                )
            return response

        return call_with_retries(once, self.retry, what=f"caldav {context}")

    def _error(self, response: requests.Response, context: str) -> ProviderError:
        status = response.status_code
        precondition, href = _precondition(response.content or b"")
        kw: dict[str, Any] = {"provider": "icloud", "status": status, "reason": precondition}
        message = f"{context}: HTTP {status}" + (f" ({precondition})" if precondition else "")
        if precondition == "valid-sync-token":
            return SyncTokenInvalid(message, **kw)
        if precondition == "no-uid-conflict":
            existing = normalize_path(response.url or self.base_url, href) if href else None
            return UidConflict(message, existing_href=existing, **kw)
        if status == 401:
            return AuthError(message, **kw)
        if status == 403:
            return PermissionDenied(message, **kw)
        if status in (404, 410):
            return NotFound(message, **kw)
        if status == 412:
            return PreconditionFailed(message, **kw)
        if status in (429, 503):
            return RateLimited(message, retry_after=parse_retry_after(response.headers.get("Retry-After")), **kw)
        if status >= 500 and status != 501:
            return TransientError(message, **kw)
        if context.startswith(("sync-collection", "propfind")) and status in (400, 405, 501):
            return UnsupportedFeature(message, **kw)
        return InvalidRequest(message, **kw)

    def _propfind(self, url: str, props: str, depth: int) -> list[DavResponse]:
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<d:propfind xmlns:d="DAV:" xmlns:c="{CALDAV}" xmlns:cs="{CS}" xmlns:ical="{APPLE}">'
            f"<d:prop>{props}</d:prop></d:propfind>"
        )
        response = self._send(
            "PROPFIND",
            url,
            body=body,
            headers={"Depth": str(depth), "Content-Type": "application/xml; charset=utf-8"},
            context=f"propfind depth={depth}",
        )
        responses, _ = parse_multistatus(response.content, response.url or url)
        return responses

    # ------------------------------------------------------------ discovery

    def discover(self) -> Discovery:
        principal_url, home_url, addresses = self._discover_principal()
        calendars = self._list_calendars(home_url)
        default_url = self._default_calendar(principal_url)
        for calendar in calendars:
            calendar.is_default = default_url is not None and normalize_path(
                calendar.url, calendar.url
            ) == normalize_path(default_url, default_url)
        return Discovery(principal_url, home_url, addresses, calendars)

    def _discover_principal(self) -> tuple[str, str, list[str]]:
        try:
            from caldav.davclient import DAVClient
            from caldav.lib import error as caldav_error
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            raise ProviderError("the caldav package is not installed", provider="icloud") from exc
        client = DAVClient(
            url=self.base_url,
            username=self.username,
            password=self._password,
            auth_type="basic",
            timeout=int(self.timeout),
            enable_rfc6764=False,
        )
        try:
            principal = client.principal()
            home = principal.calendar_home_set
            try:
                addresses = [a for a in principal.calendar_user_address_set() if a]
            except caldav_error.DAVError:
                addresses = []
            principal_url = str(principal.url)
            home_url = str(home.url)
        except caldav_error.AuthorizationError as exc:
            raise AuthError(
                "iCloud rejected the Apple ID or app-specific password", provider="icloud", status=401
            ) from exc
        except caldav_error.DAVError as exc:
            raise TransientError(f"CalDAV discovery failed: {type(exc).__name__}", provider="icloud") from exc
        except Exception as exc:  # network errors from the HTTP library caldav uses
            raise TransientError(f"CalDAV discovery failed: {type(exc).__name__}", provider="icloud") from exc
        finally:
            with contextlib.suppress(Exception):
                client.close()
        normalized = sorted(
            {
                a[7:].lower() if a.lower().startswith("mailto:") else a.lower()
                for a in addresses
                if a.lower().startswith("mailto:")
            }
        )
        return principal_url, home_url, normalized

    def _list_calendars(self, home_url: str) -> list[CalendarInfo]:
        props = (
            "<d:resourcetype/><d:displayname/><d:current-user-privilege-set/><d:sync-token/>"
            "<cs:getctag/><c:supported-calendar-component-set/><c:calendar-timezone/>"
        )
        calendars = []
        home_path = normalize_path(home_url, home_url)
        for response in self._propfind(home_url, props, depth=1):
            if response.path.rstrip("/") == home_path.rstrip("/"):
                continue
            resourcetype = response.props.get(_tag(DAV, "resourcetype"))
            kinds = {child.tag for child in resourcetype} if resourcetype is not None else set()
            if _tag(CALDAV, "calendar") not in kinds or _tag(CS, "subscribed") in kinds:
                continue
            privileges = response.props.get(_tag(DAV, "current-user-privilege-set"))
            granted = {p.tag for priv in privileges for p in priv} if privileges is not None else set()
            writable = bool(granted & {_tag(DAV, "write"), _tag(DAV, "write-content"), _tag(DAV, "all")}) or (
                privileges is None
            )
            components = response.props.get(_tag(CALDAV, "supported-calendar-component-set"))
            names = {c.get("name") for c in components} if components is not None else {"VEVENT"}
            calendars.append(
                CalendarInfo(
                    url=resource_url(home_url, response.path),
                    display_name=response.text(DAV, "displayname") or response.path.rstrip("/").rsplit("/", 1)[-1],
                    writable=writable,
                    supports_events="VEVENT" in names,
                    sync_token=response.text(DAV, "sync-token"),
                    ctag=response.text(CS, "getctag"),
                    timezone=_tzid_from_vtimezone(response.text(CALDAV, "calendar-timezone")),
                )
            )
        return calendars

    def _default_calendar(self, principal_url: str) -> str | None:
        try:
            principal = self._propfind(principal_url, "<c:schedule-inbox-URL/>", depth=0)
            inbox = next(
                (
                    r.props[_tag(CALDAV, "schedule-inbox-URL")]
                    for r in principal
                    if _tag(CALDAV, "schedule-inbox-URL") in r.props
                ),
                None,
            )
            href = inbox.findtext(_tag(DAV, "href")) if inbox is not None else None
            if not href:
                return None
            inbox_url = urljoin(principal_url, href)
            result = self._propfind(inbox_url, "<c:schedule-default-calendar-URL/>", depth=0)
            for response in result:
                element = response.props.get(_tag(CALDAV, "schedule-default-calendar-URL"))
                if element is not None and element.findtext(_tag(DAV, "href")):
                    return urljoin(inbox_url, element.findtext(_tag(DAV, "href")))
        except ProviderError:
            return None
        return None

    # ------------------------------------------------------------ change detection

    def collection_state(self, url: str) -> tuple[str | None, str | None]:
        responses = self._propfind(url, "<d:sync-token/><cs:getctag/>", depth=0)
        for response in responses:
            return response.text(DAV, "sync-token"), response.text(CS, "getctag")
        return None, None

    def sync_collection(self, url: str, token: str | None) -> SyncResult:
        """RFC 6578 sync-collection; follows truncated (507) result sets."""
        collection_path = normalize_path(url, url).rstrip("/")
        changed: dict[str, str | None] = {}
        deleted: set[str] = set()
        current = token or ""
        for _ in range(1000):
            body = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<d:sync-collection xmlns:d="DAV:">'
                f"<d:sync-token>{escape(current)}</d:sync-token>"
                "<d:sync-level>1</d:sync-level>"
                "<d:prop><d:getetag/></d:prop>"
                "</d:sync-collection>"
            )
            response = self._send(
                "REPORT",
                url,
                body=body,
                headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
                context="sync-collection",
            )
            responses, new_token = parse_multistatus(response.content, response.url or url)
            truncated = False
            for item in responses:
                if item.path.rstrip("/") == collection_path:
                    truncated = truncated or item.status == 507
                    continue
                if item.status in (404, 410):
                    deleted.add(item.path)
                    changed.pop(item.path, None)
                elif item.path.endswith("/"):
                    continue
                else:
                    changed[item.path] = item.text(DAV, "getetag")
                    deleted.discard(item.path)
            if not truncated:
                return SyncResult(changed, deleted, new_token or current or None)
            if not new_token or new_token == current:
                raise TransientError("server truncated sync results without a new token", provider="icloud")
            current = new_token
        raise TransientError("sync-collection did not converge", provider="icloud")

    def list_etags(self, url: str) -> dict[str, str | None]:
        """Complete membership listing (PROPFIND Depth 1) with ETags."""
        collection_path = normalize_path(url, url).rstrip("/")
        result: dict[str, str | None] = {}
        for response in self._propfind(url, "<d:getetag/><d:resourcetype/>", depth=1):
            if response.path.rstrip("/") == collection_path or response.path.endswith("/"):
                continue
            resourcetype = response.props.get(_tag(DAV, "resourcetype"))
            if resourcetype is not None and len(resourcetype):
                continue
            result[response.path] = response.text(DAV, "getetag")
        return result

    def multiget(self, url: str, paths: list[str]) -> dict[str, tuple[str | None, str] | None]:
        """Fetch calendar data for ``paths``. A path maps to None only when the server reported
        that specific resource as missing."""
        result: dict[str, tuple[str | None, str] | None] = {}
        for start in range(0, len(paths), MULTIGET_BATCH):
            batch = paths[start : start + MULTIGET_BATCH]
            hrefs = "".join(f"<d:href>{escape(p)}</d:href>" for p in batch)
            body = (
                '<?xml version="1.0" encoding="utf-8"?>'
                f'<c:calendar-multiget xmlns:d="DAV:" xmlns:c="{CALDAV}">'
                f"<d:prop><d:getetag/><c:calendar-data/></d:prop>{hrefs}</c:calendar-multiget>"
            )
            response = self._send(
                "REPORT",
                url,
                body=body,
                headers={"Content-Type": "application/xml; charset=utf-8"},
                context="calendar-multiget",
            )
            try:
                responses, _ = parse_multistatus(response.content, response.url or url)
            except TransientError:
                # One unreadable resource (e.g. data that is not UTF-8) spoils the whole document:
                # fetch this batch one by one, so it only affects that resource.
                log.warning("unreadable multiget response; fetching resources one by one", extra={"count": len(batch)})
                responses = []
            for item in responses:
                if item.status in (404, 410):
                    result[item.path] = None
                    continue
                data = item.text(CALDAV, "calendar-data")
                if data is not None:
                    result[item.path] = (item.text(DAV, "getetag"), data.removeprefix("﻿"))
            for path in batch:
                if path not in result:
                    # Not mentioned at all: unknown, fetch individually rather than assume.
                    fetched = self.get(resource_url(url, path))
                    result[path] = fetched
        return result

    # ------------------------------------------------------------ resources

    def etag(self, url: str) -> str | None:
        try:
            responses = self._propfind(url, "<d:getetag/>", depth=0)
        except NotFound:
            return None
        for response in responses:
            return response.text(DAV, "getetag")
        return None

    def get(self, url: str) -> tuple[str | None, str] | None:
        try:
            response = self._send("GET", url, headers={"Accept": "text/calendar"}, context="get")
        except NotFound:
            return None
        return response.headers.get("ETag"), _calendar_text(response)

    def put(self, url: str, ics: str, *, if_match: str | None = None, create: bool = False) -> str | None:
        headers = {"Content-Type": "text/calendar; charset=utf-8", "Schedule-Reply": "F"}
        if create:
            headers["If-None-Match"] = "*"
        elif if_match:
            headers["If-Match"] = if_match
        else:
            raise InvalidRequest("refusing an unconditional PUT", provider="icloud")
        response = self._send("PUT", url, body=ics, headers=headers, context="put")
        return response.headers.get("ETag")

    def delete(self, url: str, *, if_match: str | None) -> None:
        headers = {"Schedule-Reply": "F"}
        if not if_match:
            raise InvalidRequest("refusing an unconditional DELETE", provider="icloud")
        headers["If-Match"] = if_match
        self._send("DELETE", url, headers=headers, context="delete")


def _calendar_text(response: requests.Response) -> str:
    """The resource's iCalendar text. Data that cannot be decoded is returned as an empty text, so
    the event is reported as unreadable and left alone; it is never decoded approximately, because
    the bridge could then write mangled text back."""
    match = re.search(r"charset=\"?([\w.:-]+)", response.headers.get("Content-Type", ""), re.IGNORECASE)
    try:
        text = response.content.decode(match.group(1) if match else "utf-8")
    except (LookupError, UnicodeDecodeError):
        log.warning("calendar data is not valid text; the event is left alone", extra={"url": response.url})
        return ""
    return text.removeprefix("﻿")


def _tzid_from_vtimezone(text: str | None) -> str | None:
    if not text:
        return None
    for line in text.splitlines():
        if line.upper().startswith("TZID:"):
            return line.split(":", 1)[1].strip()
    return None
