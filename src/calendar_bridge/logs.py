"""Structured logging with content redaction.

Log records carry identifiers (mapping ids, item ids, provider event ids) and outcomes only.
Event titles, descriptions, locations, attendees, tokens and passwords must never be logged; the
redaction filter is a safety net that blanks such fields if a caller passes them by mistake.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from typing import Any

SENSITIVE_KEYS = frozenset(
    {
        "summary",
        "description",
        "location",
        "attendees",
        "organizer",
        "title",
        "body",
        "ics",
        "raw",
        "token",
        "access_token",
        "refresh_token",
        "password",
        "client_secret",
        "secret",
        "authorization",
        "cookie",
        "email",
        "content",
    }
)
_RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for key in list(vars(record)):
            if key.lower() in SENSITIVE_KEYS and key not in _RESERVED:
                setattr(record, key, "[redacted]")
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": dt.datetime.fromtimestamp(record.created, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value if isinstance(value, (str, int, float, bool, type(None))) else str(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extras = " ".join(f"{k}={v}" for k, v in vars(record).items() if k not in _RESERVED and not k.startswith("_"))
        line = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} {record.getMessage()}"
        if extras:
            line += f"  [{extras}]"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def setup_logging(level: str = "INFO", fmt: str = "json") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("urllib3", "niquests", "caldav", "google", "googleapiclient", "oauthlib", "requests_oauthlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"calendar_bridge.{name}")
