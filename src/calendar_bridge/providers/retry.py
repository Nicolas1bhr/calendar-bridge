"""Retries with exponential backoff and full jitter."""

from __future__ import annotations

import dataclasses
import datetime as dt
import email.utils
import math
import random
import time
from collections.abc import Callable

from calendar_bridge.errors import RateLimited, TransientError
from calendar_bridge.logs import get_logger

log = get_logger("retry")


@dataclasses.dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 4
    base_delay: float = 1.0
    max_delay: float = 60.0

    def delay(self, attempt: int, retry_after: float | None = None) -> float:
        if retry_after is not None:
            return min(max(retry_after, 0.0), self.max_delay)
        ceiling = min(self.max_delay, self.base_delay * (2**attempt))
        return random.uniform(0, ceiling)


def parse_retry_after(value: str | None) -> float | None:
    """Seconds to wait from a Retry-After header: a number of seconds or an HTTP date."""
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.UTC)
        seconds = (when - dt.datetime.now(dt.UTC)).total_seconds()
    return max(0.0, seconds) if math.isfinite(seconds) else None


def call_with_retries[T](
    fn: Callable[[], T],
    policy: RetryPolicy,
    *,
    what: str,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run ``fn``; retry ``TransientError`` (incl. rate limiting) up to ``policy.attempts`` times.
    Every operation passed here must be idempotent (conditional or deterministic-id writes)."""
    attempt = 0
    while True:
        try:
            return fn()
        except TransientError as exc:
            attempt += 1
            if attempt >= policy.attempts:
                raise
            retry_after = exc.retry_after if isinstance(exc, RateLimited) else None
            delay = policy.delay(attempt, retry_after)
            log.warning(
                "transient provider error, retrying",
                extra={"operation": what, "error": exc.short(), "attempt": attempt, "delay_s": round(delay, 2)},
            )
            sleep(delay)
