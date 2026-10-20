"""A real CalDAV server (Radicale) running in-process for integration tests, with a WSGI
middleware that can inject failures (e.g. iCloud's 403 valid-sync-token, 503 outages)."""

from __future__ import annotations

import dataclasses
import socket
import threading
import wsgiref.simple_server
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import radicale
import radicale.config
import requests


@dataclasses.dataclass
class DavFault:
    predicate: Callable[[str, str, bytes], bool]
    status: str
    body: bytes = b""
    remaining: int = 1


class FaultMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.faults: list[DavFault] = []
        self.requests: list[tuple[str, str]] = []

    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        method = environ["REQUEST_METHOD"]
        path = environ.get("PATH_INFO", "")
        length = int(environ.get("CONTENT_LENGTH") or 0)
        body = environ["wsgi.input"].read(length) if length else b""
        import io

        environ["wsgi.input"] = io.BytesIO(body)
        self.requests.append((method, path))
        for fault in list(self.faults):
            if fault.remaining > 0 and fault.predicate(method, path, body):
                fault.remaining -= 1
                if fault.remaining == 0:
                    self.faults.remove(fault)
                start_response(fault.status, [("Content-Type", "application/xml; charset=utf-8")])
                return [fault.body]
        return self.app(environ, start_response)


class _QuietHandler(wsgiref.simple_server.WSGIRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return


class _Server(wsgiref.simple_server.WSGIServer):
    allow_reuse_address = True


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class CalDAVServer:
    user = "tester@example.com"
    password = "abcd-efgh-ijkl-mnop"

    def __init__(self, folder: Path) -> None:
        configuration = radicale.config.load()
        configuration.update(
            {
                "auth": {"type": "none"},
                "rights": {"type": "owner_only"},
                "storage": {"filesystem_folder": str(folder)},
                "logging": {"level": "warning", "mask_passwords": "True"},
            },
            "test",
            privileged=True,
        )
        self.middleware = FaultMiddleware(radicale.Application(configuration))
        self.port = _free_port()
        self.httpd = wsgiref.simple_server.make_server(
            "127.0.0.1", self.port, self.middleware, server_class=_Server, handler_class=_QuietHandler
        )
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.session = requests.Session()
        self.session.auth = (self.user, self.password)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def calendar_url(self, name: str) -> str:
        return f"{self.base_url}{self.user}/{name}/"

    def make_calendar(self, name: str, display_name: str | None = None) -> str:
        url = self.calendar_url(name)
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<C:mkcalendar xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"><D:set><D:prop>'
            f"<D:displayname>{display_name or name}</D:displayname>"
            '<C:supported-calendar-component-set><C:comp name="VEVENT"/></C:supported-calendar-component-set>'
            "</D:prop></D:set></C:mkcalendar>"
        )
        response = self.session.request("MKCALENDAR", url, data=body.encode())
        assert response.status_code in (201, 405), response.text
        return url

    # "User" operations on the server (as if made on an iPhone/Mac)
    def put(self, url: str, ics: str, etag: str | None = None) -> str | None:
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if etag:
            headers["If-Match"] = etag
        response = self.session.put(url, data=ics.encode("utf-8"), headers=headers)
        assert response.status_code in (201, 204), (response.status_code, response.text)
        return response.headers.get("ETag")

    def get(self, url: str) -> tuple[str | None, str] | None:
        response = self.session.get(url)
        if response.status_code == 404:
            return None
        assert response.status_code == 200, response.status_code
        return response.headers.get("ETag"), response.text

    def delete(self, url: str) -> None:
        response = self.session.delete(url)
        assert response.status_code in (200, 204), response.status_code

    def list(self, calendar_url: str) -> dict[str, str]:
        """href -> ics for every resource in the collection."""
        body = '<?xml version="1.0" encoding="utf-8"?><D:propfind xmlns:D="DAV:"><D:prop><D:getetag/></D:prop></D:propfind>'
        response = self.session.request("PROPFIND", calendar_url, data=body, headers={"Depth": "1"})
        assert response.status_code == 207
        from lxml import etree

        root = etree.fromstring(response.content)
        hrefs = [h.text for h in root.iter("{DAV:}href") if h.text and h.text.endswith(".ics")]
        out = {}
        for href in hrefs:
            got = self.get(f"{self.base_url.rstrip('/')}{href}")
            if got is not None:
                out[href] = got[1]
        return out

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
