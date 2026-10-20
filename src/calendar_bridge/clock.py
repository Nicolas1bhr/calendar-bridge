"""UTC clock abstraction and timestamp helpers.

All timestamps the bridge stores are UTC ISO-8601 strings with a ``Z`` suffix so that they sort
lexicographically and never depend on the host's local time zone.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol

UTC = dt.UTC


class Clock(Protocol):
    def now(self) -> dt.datetime: ...


class SystemClock:
    def now(self) -> dt.datetime:
        return dt.datetime.now(UTC)


class FrozenClock:
    """A controllable clock for tests."""

    def __init__(self, start: dt.datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FrozenClock needs an aware datetime")
        self._now = start.astimezone(UTC)

    def now(self) -> dt.datetime:
        return self._now

    def advance(self, **delta: float) -> None:
        self._now = self._now + dt.timedelta(**delta)

    def set(self, value: dt.datetime) -> None:
        self._now = value.astimezone(UTC)


def to_iso(value: dt.datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("refusing to serialise a naive datetime as a UTC timestamp")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def from_iso(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def seconds_since(value: str | None, now: dt.datetime) -> float | None:
    if not value:
        return None
    return (now - from_iso(value)).total_seconds()


def is_due(last: str | None, now: dt.datetime, every: dt.timedelta) -> bool:
    """True when periodic work last done at ``last`` is due again. A ``last`` in the future (the
    clock was set back) counts as due, so a clock correction never freezes periodic work."""
    if not last:
        return True
    elapsed = now - from_iso(last)
    return elapsed >= every or elapsed < -dt.timedelta(minutes=5)
