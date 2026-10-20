"""Alerts through private channels (ntfy, a generic JSON webhook, e-mail).

Alert texts describe conditions only - never event titles or other calendar content - because
alert channels are third-party services. Each condition alerts when it starts, repeats every
``alert_repeat_hours`` while it persists, and sends a "resolved" notice when it clears.
"""

from __future__ import annotations

import datetime as dt
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any, Protocol

import requests

from calendar_bridge.clock import from_iso
from calendar_bridge.config import Settings
from calendar_bridge.logs import get_logger
from calendar_bridge.sync.context import EngineContext

log = get_logger("alerts")


class Channel(Protocol):
    name: str

    def send(self, title: str, message: str, severity: str) -> None: ...


class NtfyChannel:
    name = "ntfy"

    def __init__(self, url: str, token: str | None, timeout: float) -> None:
        self.url, self.token, self.timeout = url, token, timeout

    def send(self, title: str, message: str, severity: str) -> None:
        headers = {"Title": title, "Priority": "high" if severity == "critical" else "default", "Tags": "calendar"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        response = requests.post(self.url, data=message.encode("utf-8"), headers=headers, timeout=self.timeout)
        response.raise_for_status()


class WebhookChannel:
    name = "webhook"

    def __init__(self, url: str, timeout: float) -> None:
        self.url, self.timeout = url, timeout

    def send(self, title: str, message: str, severity: str) -> None:
        text = f"{title}\n{message}"
        payload = {"title": title, "message": message, "severity": severity, "text": text, "content": text}
        response = requests.post(self.url, json=payload, timeout=self.timeout)
        response.raise_for_status()


class EmailChannel:
    name = "email"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, title: str, message: str, severity: str) -> None:
        s = self.settings
        msg = EmailMessage()
        msg["Subject"] = f"[Calendar Bridge] {title}"
        msg["From"] = s.alert_email_from or s.alert_smtp_user or "calendar-bridge@localhost"
        msg["To"] = s.alert_email_to or ""
        msg.set_content(f"{message}\n\nSeverity: {severity}")
        password = (
            s.alert_smtp_password_file.read_text(encoding="utf-8").strip()
            if s.alert_smtp_password_file and s.alert_smtp_password_file.exists()
            else None
        )
        with smtplib.SMTP(s.alert_smtp_host or "localhost", s.alert_smtp_port, timeout=s.http_timeout) as smtp:
            if s.alert_smtp_starttls:
                smtp.starttls(context=ssl.create_default_context())
            if s.alert_smtp_user and password:
                smtp.login(s.alert_smtp_user, password)
            smtp.send_message(msg)


def channels_from_settings(settings: Settings) -> list[Channel]:
    channels: list[Channel] = []
    if settings.alert_ntfy_url:
        token = None
        if settings.alert_ntfy_token_file and settings.alert_ntfy_token_file.exists():
            token = settings.alert_ntfy_token_file.read_text(encoding="utf-8").strip()
        channels.append(NtfyChannel(settings.alert_ntfy_url, token, settings.http_timeout))
    if settings.alert_webhook_url:
        channels.append(WebhookChannel(settings.alert_webhook_url, settings.http_timeout))
    if settings.alert_email_to and settings.alert_smtp_host:
        channels.append(EmailChannel(settings))
    return channels


def conditions(status: dict[str, Any], settings: Settings, now: dt.datetime) -> dict[str, tuple[str, str]]:
    """Map of alert key -> (severity, message) for every condition that currently holds."""
    found: dict[str, tuple[str, str]] = {}
    active = [m for m in status["mappings"] if m["state"] == "active"]
    for provider, label in (("google", "Google"), ("icloud", "iCloud")):
        state = status[provider]["state"]
        if active and state != "ok":
            reason = status[provider].get("error") or state
            found[f"auth:{provider}"] = (
                "critical",
                f"{label} access needs attention: {reason}. Fix it with `calendar-bridge setup`.",
            )
    last_ok = status["daemon"]["last_ok_cycle_at"]
    started = status["daemon"]["started_at"]
    reference = last_ok or started
    if active and reference:
        age = (now - from_iso(reference)).total_seconds()
        if age > settings.lag_alert_seconds:
            found["sync_stopped"] = (
                "critical",
                f"No fully successful synchronization for {int(age // 60)} minutes.",
            )
    if status.get("global_hold"):
        found["hold:global"] = ("critical", f"Destructive writes suspended: {status['global_hold']}")
    for mapping in active:
        if mapping["destructive_writes"] == "suspended":
            found[f"hold:{mapping['id']}"] = (
                "critical",
                f"Deletions suspended for '{mapping['name']}': {mapping['destructive_hold_reason']}",
            )
        oldest = mapping.get("oldest_open_write_intent_at")
        if oldest and (now - from_iso(oldest)).total_seconds() > settings.lag_alert_seconds:
            minutes = int((now - from_iso(oldest)).total_seconds() // 60)
            found[f"stuck:{mapping['id']}"] = (
                "warning",
                f"A write in '{mapping['name']}' has waited {minutes} minutes for confirmation from the provider.",
            )
    grace = dt.timedelta(minutes=settings.conflict_alert_grace_minutes)
    old_conflicts = [c for c in status["conflicts"]["items"] if now - from_iso(c["since"]) >= grace]
    if old_conflicts:
        found["conflicts"] = (
            "warning",
            f"{len(old_conflicts)} unresolved conflict(s). Review with `calendar-bridge conflicts list`.",
        )
    if status["failures"]["permanent"]:
        found["failures"] = (
            "warning",
            f"{status['failures']['permanent']} event(s) could not be synchronized. "
            "See `calendar-bridge items list --state failed`.",
        )
    return found


class Alerter:
    def __init__(self, ctx: EngineContext, channels: list[Channel]) -> None:
        self.ctx = ctx
        self.channels = channels
        self.repo = ctx.repo

    def evaluate(self, status: dict[str, Any]) -> list[str]:
        now = self.ctx.now()
        current = conditions(status, self.ctx.settings, now)
        sent: list[str] = []
        repeat = dt.timedelta(hours=self.ctx.settings.alert_repeat_hours)
        for key, (severity, message) in current.items():
            with self.ctx.db.tx():
                new = self.repo.raise_alert(key, severity, message)
            row = self.repo.alert(key)
            assert row is not None
            due = new or row["last_sent_at"] is None or now - from_iso(row["last_sent_at"]) >= repeat
            if due and self._send(f"Problem: {key}", message, severity):
                with self.ctx.db.tx():
                    self.repo.mark_alert_sent(key)
                sent.append(key)
        for row in self.repo.alerts("active"):
            if row["key"] not in current:
                with self.ctx.db.tx():
                    self.repo.clear_alert(row["key"])
                if row["last_sent_at"]:
                    self._send(f"Resolved: {row['key']}", f"Resolved: {row['message']}", "info")
                sent.append(f"cleared:{row['key']}")
        return sent

    def _send(self, title: str, message: str, severity: str) -> bool:
        if not self.channels:
            return False
        delivered = False
        for channel in self.channels:
            try:
                channel.send(title, message, severity)
                delivered = True
            except Exception as exc:
                log.warning("alert delivery failed", extra={"channel": channel.name, "error": type(exc).__name__})
        return delivered


def ping_heartbeat(url: str | None, ok: bool, timeout: float) -> None:
    """Dead-man's switch (e.g. healthchecks.io): an external service alerts when pings stop."""
    if not url:
        return
    target = url if ok else url.rstrip("/") + "/fail"
    try:
        requests.get(target, timeout=timeout)
    except requests.RequestException as exc:
        log.warning("heartbeat ping failed", extra={"error": type(exc).__name__})
