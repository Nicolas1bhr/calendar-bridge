"""One synchronization cycle across all active mappings.

Per mapping: recover open write intents -> observe Google -> observe iCloud -> reconcile.
If either provider cannot be read, the mapping is not reconciled this cycle (changes keep
accumulating durably on the side that is reachable and are propagated once both are back).
Authentication failures stop all work for that provider and raise an alert.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import sqlite3
from typing import Any

from calendar_bridge.clock import is_due
from calendar_bridge.credentials import ProviderFactory
from calendar_bridge.db.repo import MappingRow
from calendar_bridge.errors import AuthError, CredentialsMissing, ProviderError
from calendar_bridge.logs import get_logger
from calendar_bridge.sync.context import EngineContext, MappingContext
from calendar_bridge.sync.discovery import refresh_google_calendars
from calendar_bridge.sync.executor import Executor
from calendar_bridge.sync.observe import GoogleObserver, ICloudObserver
from calendar_bridge.sync.reconcile import Reconciler
from calendar_bridge.sync.recovery import Recovery

log = get_logger("engine")


@dataclasses.dataclass
class MappingReport:
    mapping_id: int
    name: str
    ok: bool = False
    full: bool = False
    google: dict[str, Any] | None = None
    icloud: dict[str, Any] | None = None
    reconcile: dict[str, Any] | None = None
    plans: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class CycleReport:
    started_at: str
    finished_at: str = ""
    ok: bool = True
    mappings: list[MappingReport] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class Engine:
    def __init__(self, ctx: EngineContext, providers: ProviderFactory) -> None:
        self.ctx = ctx
        self.providers = providers
        self.repo = ctx.repo
        self.settings = ctx.settings

    def run_cycle(
        self,
        *,
        mapping_ids: list[int] | None = None,
        force_full: bool = False,
        dry_run: bool = False,
    ) -> CycleReport:
        report = CycleReport(started_at=self.repo.now())
        with self.ctx.db.tx():
            run_id = self.repo.start_run("dry-run" if dry_run else ("full" if force_full else "cycle"))
        self._check_integrity_if_due()
        auth_failed: set[str] = set()
        try:
            self._refresh_google_metadata_if_due()
        except AuthError as exc:
            self._auth_failed("google", exc)
            auth_failed.add("google")
        except (ProviderError, CredentialsMissing) as exc:
            report.errors.append(f"google calendar list: {exc}")

        mappings = self.repo.mappings(("active",))
        if mapping_ids is not None:
            mappings = [m for m in mappings if m.id in mapping_ids]
        for mapping in mappings:
            if self.ctx.stopping:
                break
            mapping_report = MappingReport(mapping.id, mapping.name)
            report.mappings.append(mapping_report)
            if auth_failed:
                mapping_report.errors.append("skipped: " + ", ".join(sorted(auth_failed)) + " authorization failed")
                continue
            try:
                self.sync_mapping(mapping, mapping_report, force_full=force_full, dry_run=dry_run)
            except AuthError as exc:
                provider = exc.provider or "google"
                self._auth_failed(provider, exc)
                auth_failed.add(provider)
                mapping_report.errors.append(f"{provider} authorization failed")
            except CredentialsMissing as exc:
                mapping_report.errors.append(str(exc))
            except Exception as exc:
                log.exception("mapping cycle failed", extra={"mapping_id": mapping.id})
                mapping_report.errors.append(f"internal error: {type(exc).__name__}: {exc}")

        try:
            self.providers.after_cycle()
        except Exception:
            log.exception("could not persist refreshed Google credentials")
        report.ok = not report.errors and all(m.ok for m in report.mappings)
        report.finished_at = self.repo.now()
        stats = {"mappings": [dataclasses.asdict(m) | {"plans": len(m.plans)} for m in report.mappings]}
        with self.ctx.db.tx():
            self.repo.finish_run(
                run_id,
                ok=report.ok,
                stats=stats,
                error="; ".join(report.errors + [e for m in report.mappings for e in m.errors])[:2000] or None,
            )
            if report.ok and not dry_run:
                self.repo.set_meta("last_ok_cycle_at", report.finished_at)
            self.repo.set_meta("last_cycle_at", report.finished_at)
        return report

    def sync_mapping(self, mapping: MappingRow, report: MappingReport, *, force_full: bool, dry_run: bool) -> None:
        mc = MappingContext.build(self.ctx, mapping)
        google = self.providers.google()
        icloud = self.providers.icloud()
        executor = Executor(self.ctx, mc, google, icloud)
        blocked = Recovery(executor).run() if not dry_run else set()

        full = force_full or self._full_due(mapping)
        report.full = full
        readable = True
        for provider, observer in (
            ("google", GoogleObserver(self.ctx, mc, google)),
            ("icloud", ICloudObserver(self.ctx, mc, icloud)),
        ):
            try:
                stats = observer.observe(force_full=full)
                setattr(report, provider, stats.as_dict())
                with self.ctx.db.tx():
                    self.repo.set_auth_state(provider, "ok")
            except AuthError:
                raise
            except ProviderError as exc:
                readable = False
                report.errors.append(f"{provider} read failed: {exc.short()}")
                with self.ctx.db.tx():
                    self.repo.checkpoint_failed(mapping.id, provider, exc.short())
                log.warning(
                    "observation failed", extra={"mapping_id": mapping.id, "provider": provider, "error": exc.short()}
                )
        if not readable:
            return
        if full:
            with self.ctx.db.tx():
                self.repo.mark_all_dirty(mapping.id)
        reconciler = Reconciler(self.ctx, mc, executor, dry_run=dry_run, blocked_items=blocked)
        outcome = reconciler.run()
        report.reconcile = outcome.as_dict()
        report.plans = outcome.plans
        report.ok = outcome.errors == 0
        if not dry_run:
            with self.ctx.db.tx():
                fields: dict[str, Any] = {}
                if report.ok:
                    fields["last_cycle_ok_at"] = self.repo.now()
                if full:
                    fields["last_full_reconcile_at"] = self.repo.now()
                self.repo.update_mapping(mapping.id, **fields)

    # ------------------------------------------------------------ helpers

    def _full_due(self, mapping: MappingRow) -> bool:
        every = dt.timedelta(seconds=self.settings.full_reconcile_interval)
        return is_due(mapping.last_full_reconcile_at, self.ctx.now(), every)

    def _auth_failed(self, provider: str, exc: Exception) -> None:
        with self.ctx.db.tx():
            self.repo.set_auth_state(provider, "invalid", str(exc)[:300])
        log.error(
            "authorization failed; reauthorize with `calendar-bridge auth %s`", provider, extra={"provider": provider}
        )

    def _check_integrity_if_due(self) -> None:
        interval = dt.timedelta(hours=self.settings.integrity_check_interval_hours)
        if not is_due(self.repo.get_meta("last_integrity_check_at"), self.ctx.now(), interval):
            return
        try:
            problems = self.ctx.db.quick_check()
        except sqlite3.DatabaseError as exc:  # damage severe enough that the check itself fails
            problems = [f"the check could not run ({exc})"]
        with self.ctx.db.tx():
            self.repo.set_meta("last_integrity_check_at", self.repo.now())
            if problems:
                reason = "database integrity check failed: " + "; ".join(problems[:3])
                self.repo.set_meta("global_hold", reason)
                self.repo.audit("global_hold", detail=reason)
                log.error("database integrity check failed; destructive writes suspended")
            elif (self.repo.get_meta("global_hold") or "").startswith("database integrity"):
                self.repo.set_meta("global_hold", None)
                self.repo.audit("global_hold_cleared", detail="integrity check passed")

    def _refresh_google_metadata_if_due(self) -> None:
        if not is_due(self.repo.get_meta("google_calendars_refreshed_at"), self.ctx.now(), dt.timedelta(minutes=30)):
            return
        if not self.repo.mappings(("active",)):
            return
        refresh_google_calendars(self.ctx, self.providers.google())
