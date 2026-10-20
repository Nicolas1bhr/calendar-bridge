"""Write-intent recovery, run before each mapping's reconciliation.

An open intent means the process stopped (or the network failed) between recording a remote
write and confirming it. For creations the intent names the exact remote object (pre-chosen
Google id or UID-derived CalDAV URL), so recovery simply looks it up:

* it exists  -> the write happened: link it, adopt its read-back as the baseline, and keep the
  source version recorded in the intent as the other side's baseline, so later edits on either
  side still propagate;
* it is gone -> the write never happened: release the planned id so a fresh create can run.

Updates and deletes need no special handling: they were conditional, and the item is simply
re-planned from freshly observed state (the three-way comparison recognises an applied write
as already converged).
"""

from __future__ import annotations

from typing import Any

from calendar_bridge.db.repo import IntentRow
from calendar_bridge.errors import NotFound, TransientError
from calendar_bridge.logs import get_logger
from calendar_bridge.model.canonical import INFO_GROUPS, CanonicalEvent, with_groups
from calendar_bridge.model.merge import differing_groups
from calendar_bridge.providers.icloud.client import resource_url
from calendar_bridge.sync.executor import Executor

log = get_logger("recovery")


class Recovery:
    def __init__(self, executor: Executor) -> None:
        self.executor = executor
        self.engine = executor.engine
        self.mc = executor.mc
        self.repo = executor.repo
        self.db = executor.db

    def run(self) -> set[int]:
        """Resolve open intents; returns item ids whose intents are still unresolved."""
        blocked: set[int] = set()
        for intent in self.repo.open_intents(self.mc.id):
            try:
                if intent.op == "create":
                    self._recover_create(intent)
                else:
                    with self.db.tx():
                        self.repo.update_intent(
                            intent.id, state="abandoned", last_error="re-planned from observed state"
                        )
                        if intent.item_id is not None:
                            self.repo.update_item(intent.item_id, dirty=self._requeue(intent.item_id))
            except TransientError as exc:
                log.warning("intent recovery deferred", extra={"intent_id": intent.id, "error": exc.short()})
                if intent.item_id is not None:
                    blocked.add(intent.item_id)
        return blocked

    def _recover_create(self, intent: IntentRow) -> None:
        item_id = intent.item_id
        source_json = (intent.payload or {}).get("source")
        source = CanonicalEvent.from_json(source_json) if source_json else None
        if intent.provider == "google":
            try:
                raw = self.executor.google.get_event(self.mc.google_cal.remote_id, intent.target)
            except NotFound:
                raw = None
            if raw is None or raw.get("status") == "cancelled":
                self._abandon(intent, google_event_id=intent.target)
                return
            view = self.executor.google_readback(intent.target, set())
            with self.db.tx():
                self.repo.update_intent(intent.id, state="verified", last_error="recovered after restart")
                if item_id is not None:
                    fields = {"google_event_id": intent.target, "google_ical_uid": raw.get("iCalUID"), "dirty": 1}
                    if view.event is not None and source is not None:
                        source_base = self._unfinished(source, view.event, self.mc.project_google)
                        fields.update(
                            google_synced_json=view.event.dumps(),
                            google_synced_fp=view.event.fingerprint(),
                            icloud_synced_json=source_base.dumps(),
                            icloud_synced_fp=source_base.fingerprint(),
                        )
                    self.repo.update_item(item_id, **fields)
                self.repo.audit("recovered_create", mapping_id=self.mc.id, item_id=item_id, provider="google")
            return

        url = resource_url(self.mc.icloud_cal.remote_id, intent.target)
        if self.executor.icloud.get(url) is None:
            self._abandon(intent, icloud_href=intent.target)
            return
        icloud_view = self.executor.icloud_readback(intent.target)
        with self.db.tx():
            self.repo.update_intent(intent.id, state="verified", last_error="recovered after restart")
            if item_id is not None:
                fields = {"icloud_href": intent.target, "dirty": 1}
                if icloud_view.event is not None and source is not None:
                    source_base = self._unfinished(source, icloud_view.event, self.mc.project_icloud)
                    fields.update(
                        icloud_synced_json=icloud_view.event.dumps(),
                        icloud_synced_fp=icloud_view.event.fingerprint(),
                        google_synced_json=source_base.dumps(),
                        google_synced_fp=source_base.fingerprint(),
                    )
                self.repo.update_item(item_id, **fields)
            self.repo.audit("recovered_create", mapping_id=self.mc.id, item_id=item_id, provider="icloud")

    def _requeue(self, item_id: int) -> int:
        """Re-plan now, unless the item is waiting out a retry backoff (then it is picked up
        when its next attempt is due)."""
        item = self.repo.item(item_id)
        if item.next_attempt_at and item.next_attempt_at > self.repo.now():
            return 0
        return 1

    @staticmethod
    def _unfinished(source: CanonicalEvent, created: CanonicalEvent, project: Any) -> CanonicalEvent:
        """Baseline for the source side of a create that was interrupted part-way (e.g. the
        event was created but not all modified occurrences were applied). Groups the new copy is
        missing are recorded as not yet synchronized, so the next cycle sees them as source-side
        changes and completes the copy instead of treating the partial copy as agreed."""
        missing = differing_groups(project(source), created) - set(INFO_GROUPS)
        return with_groups(source, created, missing)

    def _abandon(self, intent: IntentRow, **planned: str) -> None:
        with self.db.tx():
            self.repo.update_intent(intent.id, state="abandoned", last_error="create never reached the provider")
            if intent.item_id is not None:
                item = self.repo.item(intent.item_id)
                clear = {k: None for k, v in planned.items() if getattr(item, k) == v}
                self.repo.update_item(intent.item_id, dirty=self._requeue(intent.item_id), state="pending", **clear)
