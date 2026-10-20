"""The long-running synchronization service.

Runs a cycle every ``sync_interval`` seconds, then publishes status, evaluates alerts and runs
housekeeping (backups, expiry of old records). Status, alerts and the heartbeat ping run even
when a cycle fails, so a failure is always reported. A watchdog thread terminates the process if
a cycle hangs, so the container restart policy brings it back; open write intents are recovered
on the next start. SIGTERM stops the loop between items, never in the middle of a write.
"""

from __future__ import annotations

import datetime as dt
import os
import signal
import sqlite3
import threading
import time
from typing import Any

from calendar_bridge import __version__
from calendar_bridge.app import App, LockBusy, cycle_lock, daemon_lock
from calendar_bridge.clock import is_due, seconds_since, to_iso
from calendar_bridge.db.backup import create_backup
from calendar_bridge.errors import AuthError, CredentialsMissing, ProviderError
from calendar_bridge.logs import get_logger
from calendar_bridge.status.alerts import Alerter, channels_from_settings, ping_heartbeat
from calendar_bridge.status.report import build_status, write_status_file
from calendar_bridge.status.status_calendar import StatusCalendar
from calendar_bridge.sync.engine import CycleReport

log = get_logger("daemon")
WATCHDOG_EXIT = 70
# Consecutive cycles whose bookkeeping could not be written before the process exits, so that
# the restart policy gets a chance to clear whatever holds the database (locks, a full disk).
MAX_DATABASE_FAILURES = 10
BACKUP_RETRY = dt.timedelta(hours=1)


class Daemon:
    def __init__(self, app: App) -> None:
        self.app = app
        self.settings = app.settings
        self.stop = threading.Event()
        self.app.stop = self.stop
        self.cycle_started: float | None = None
        self.database_failures = 0
        self._last_fallback_alert = 0.0

    def run(self, *, max_cycles: int | None = None) -> int:
        with daemon_lock(self.settings):
            self._install_signals()
            with self.app.db.tx():
                self.app.repo.set_meta("daemon_started_at", self.app.repo.now())
                self.app.repo.set_meta("daemon_pid", str(os.getpid()))
                self.app.repo.set_meta("daemon_version", __version__)
            log.info(
                "calendar bridge started", extra={"version": __version__, "interval_s": self.settings.sync_interval}
            )
            threading.Thread(target=self._watchdog, name="watchdog", daemon=True).start()
            cycles = 0
            while not self.stop.is_set():
                started = time.monotonic()
                self.run_once()
                if self.database_failures >= MAX_DATABASE_FAILURES:
                    log.critical("the database keeps failing; exiting so the service is restarted")
                    return 1
                cycles += 1
                if max_cycles is not None and cycles >= max_cycles:
                    break
                remaining = self.settings.sync_interval - (time.monotonic() - started)
                self.stop.wait(max(1.0, remaining))
            log.info("calendar bridge stopped")
        return 0

    def run_once(self) -> dict[str, Any] | None:
        report: CycleReport | None = None
        self.cycle_started = time.monotonic()
        try:
            with cycle_lock(self.settings, timeout=self.settings.sync_interval):
                # Tells `health` that a long cycle (a large first copy) is alive, not stuck.
                self._record("cycle_started_at")
                try:
                    if self.app.reload_credentials_if_changed():
                        log.info("credentials changed; reconnecting to providers")
                    report = self.app.engine().run_cycle()
                except Exception:
                    log.exception("cycle failed")
                self._after_cycle(report, locked=True)
        except LockBusy:
            log.warning("previous cycle or a CLI command still holds the lock; skipping this tick")
            self._after_cycle(None, locked=False)
        finally:
            self.cycle_started = None
        self._record("heartbeat_at")
        return report.as_dict() if report is not None else None

    def _record(self, key: str) -> None:
        try:
            with self.app.db.tx():
                self.app.repo.set_meta(key, self.app.repo.now())
            self.database_failures = 0
        except sqlite3.Error:
            self.database_failures += 1
            log.exception("could not write to the database", extra={"failures": self.database_failures})

    # ------------------------------------------------------------ post-cycle work

    def _after_cycle(self, report: CycleReport | None, *, locked: bool) -> None:
        """Report what happened, whatever happened. Writing work (status calendar, backups,
        housekeeping) only runs under the cycle lock, after a cycle that completed."""
        ctx = self.app.ctx
        try:
            status = build_status(ctx)
        except Exception as exc:
            log.exception("post-cycle task failed", extra={"task": "status"})
            self._fallback_alert(exc)
            status = None
        settings = self.settings
        tasks: list[tuple[str, Any]] = [
            (
                "heartbeat ping",
                lambda: ping_heartbeat(settings.heartbeat_url, self._synced_recently(), settings.http_timeout),
            )
        ]
        if status is not None:
            tasks += [
                ("status file", lambda: write_status_file(self.settings.status_file, status)),
                ("alerts", lambda: Alerter(ctx, channels_from_settings(self.settings)).evaluate(status)),
            ]
            if locked:
                tasks.append(("status calendar", lambda: self._publish_status(status)))
        if locked and report is not None and not self.stop.is_set():
            tasks += [("backup", self._backup_if_due), ("housekeeping", self._housekeeping_if_due)]
        for name, task in tasks:
            try:
                task()
            except Exception:
                log.exception("post-cycle task failed", extra={"task": name})

    def _synced_recently(self) -> bool:
        """What the dead-man's switch is told: one failed cycle (a network blip) is not an
        outage; no fully successful cycle within the lag limit is."""
        try:
            age = seconds_since(self.app.repo.get_meta("last_ok_cycle_at"), self.app.clock.now())
        except sqlite3.Error:
            return False
        return age is not None and age <= self.settings.lag_alert_seconds

    def _fallback_alert(self, exc: Exception) -> None:
        """The status (and with it the alert bookkeeping) cannot be read: alert directly, at most
        once per repeat period, so that a broken database is never silent."""
        repeat = self.settings.alert_repeat_hours * 3600
        if self._last_fallback_alert and time.monotonic() - self._last_fallback_alert < repeat:
            return
        self._last_fallback_alert = time.monotonic()
        message = f"Calendar Bridge cannot read its own database ({type(exc).__name__}). Check the logs."
        for channel in channels_from_settings(self.settings):
            try:
                channel.send("Problem: database", message, "critical")
            except Exception as error:
                log.warning("alert delivery failed", extra={"channel": channel.name, "error": type(error).__name__})

    def _publish_status(self, status: dict[str, Any]) -> None:
        if not self.settings.status_calendar or status["google"]["state"] != "ok":
            return
        try:
            StatusCalendar(self.app.ctx, self.app.providers.google()).publish(status)
        except (CredentialsMissing, AuthError):
            return
        except ProviderError as exc:
            log.warning("status calendar update failed", extra={"error": exc.short()})

    def _due(self, key: str, every: dt.timedelta) -> bool:
        return is_due(self.app.repo.get_meta(key), self.app.clock.now(), every)

    def _backup_if_due(self) -> None:
        if not self._due("last_backup_at", dt.timedelta(hours=self.settings.backup_interval_hours)):
            return
        if not self._due("last_backup_attempt_at", BACKUP_RETRY):
            return  # the last attempt failed (or the process died during it): wait before retrying
        with self.app.db.tx():
            self.app.repo.set_meta("last_backup_attempt_at", self.app.repo.now())
        path = create_backup(
            self.app.db, self.settings.backup_dir, box=self.app.maybe_box(), keep=self.settings.backup_keep
        )
        with self.app.db.tx():
            self.app.repo.set_meta("last_backup_at", self.app.repo.now())
        log.info("database backup written", extra={"file": path.name})

    def _housekeeping_if_due(self) -> None:
        if not self._due("last_housekeeping_at", dt.timedelta(hours=24)):
            return
        now = self.app.clock.now()
        retention = self.settings.tombstone_retention_days
        cutoff = to_iso(now - dt.timedelta(days=retention))
        # The audit trail outlives the tombstones it describes (`tombstones restore` reads it).
        audit_before = to_iso(now - dt.timedelta(days=max(365, retention)))
        with self.app.db.tx():
            purged = self.app.repo.purge_tombstones(cutoff)
            pruned = self.app.repo.prune_history(cutoff, audit_before=audit_before)
            retried = self.app.repo.requeue_failed(self.settings.item_max_attempts)
            if retried:
                self.app.repo.audit("failed_retried", detail=str(retried))
            self.app.repo.prune_runs()
            self.app.repo.set_meta("last_housekeeping_at", self.app.repo.now())
        if purged or pruned:
            log.info("expired records removed", extra={"tombstones": purged, "rows": pruned})
        if retried:
            log.info("failed items queued for another attempt", extra={"count": retried})

    # ------------------------------------------------------------ process control

    def _install_signals(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return

        def handler(signum: int, _frame: Any) -> None:
            log.info("stop requested", extra={"signal": signum})
            self.stop.set()

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)

    def _watchdog(self) -> None:
        while not self.stop.is_set():
            started = self.cycle_started
            if started is not None and time.monotonic() - started > self.settings.cycle_timeout:
                log.critical("cycle exceeded timeout; exiting so the supervisor restarts the service")
                os._exit(WATCHDOG_EXIT)
            self.stop.wait(15)
