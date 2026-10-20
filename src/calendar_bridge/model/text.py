"""Text normalization and the mirror information block.

Mirrors never receive real attendees or conference objects (that would let the mirror send
invitations or impersonate the organizer). Instead the owning provider's guest list and call
links are appended to the mirror's description below a fixed marker line. Everything from the
marker on is stripped again when the mirror is read, so the block never syncs back.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from html.parser import HTMLParser

from calendar_bridge.model.canonical import Conference, EventData, Person

FOOTER_MARKER = "--- Calendar Bridge: synced details (not editable) ---"
FooterFn = Callable[[EventData], str]

_HTML_HINT = re.compile(
    r"<\s*(?:br|p|div|span|a\s|a>|b>|i>|u>|strong|em|ul|ol|li|h[1-6]|blockquote|font)\b",
    re.IGNORECASE,
)
_STATUS_LABEL = {
    "accepted": "accepted",
    "declined": "declined",
    "tentative": "maybe",
    "needs-action": "no reply",
}


def norm_text(value: str | None) -> str:
    """Normalize line endings and trailing whitespace of multi-line text."""
    if not value:
        return ""
    return value.replace("\r\n", "\n").replace("\r", "\n").rstrip()


def norm_line(value: str | None) -> str:
    return norm_text(value).strip()


def looks_like_html(value: str) -> bool:
    return bool(_HTML_HINT.search(value))


class _TextExtractor(HTMLParser):
    _BLOCK = frozenset({"p", "div", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.out.append("\n")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in self._BLOCK:
            self.out.append("\n")
        elif tag == "a":
            self._href = dict(attrs).get("href")
            self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCK:
            self.out.append("\n")
        elif tag == "a":
            text = "".join(self._link_text)
            if self._href and self._href not in text:
                self.out.append(f" ({self._href})")
            self._href = None

    def handle_data(self, data: str) -> None:
        self.out.append(data)
        if self._href is not None:
            self._link_text.append(data)


def html_to_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    text = html.unescape("".join(parser.out))
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return norm_text(text.strip("\n"))


def plain_description(value: str) -> str:
    return html_to_text(value) if looks_like_html(value) else value


def strip_footer(description: str) -> str:
    index = description.find(FOOTER_MARKER)
    if index < 0:
        return description
    return description[:index].rstrip()


def _person_line(person: Person) -> str:
    status = _STATUS_LABEL.get(person.status, "")
    label = person.label()
    if person.role == "optional":
        label += " [optional]"
    return f"{label} ({status})" if status else label


def render_footer(data: EventData, owner_label: str) -> str:
    """Render the read-only information block for a mirror, or "" when there is nothing to show."""
    lines: list[str] = []
    if data.is_meeting:
        lines.append(f"Guests are managed in {owner_label}; change the guest list there.")
        if data.organizer is not None:
            lines.append(f"Organizer: {data.organizer.label()}")
        guests = [p for p in data.attendees if data.organizer is None or p.email != data.organizer.email]
        if guests:
            lines.append("Guests: " + "; ".join(_person_line(p) for p in guests))
    for conference in data.conference:
        label = conference.label or "Video call"
        lines.append(f"{label}: {conference.uri}")
    if not lines:
        return ""
    return "\n".join([FOOTER_MARKER, *lines])


def compose_description(description: str, footer: str) -> str:
    if not footer:
        return description
    if not description:
        return footer
    return f"{description}\n\n{footer}"


def conference_from_text(uri: str, label: str = "") -> Conference:
    return Conference(uri=uri.strip(), label=label.strip())


def no_footer(data: EventData) -> str:
    del data
    return ""
