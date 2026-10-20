"""Application wiring shared by the CLI and the daemon."""

from __future__ import annotations

import contextlib
import dataclasses
import os
import threading
from collections.abc import Iterator

from filelock import FileLock, Timeout

from calendar_bridge.clock import Clock, SystemClock
from calendar_bridge.config import Settings, load_settings
from calendar_bridge.credentials import CredentialStore, ProviderFactory, StoredCredentialProviders
from calendar_bridge.crypto import SecretBox, load_keys
from calendar_bridge.db.backup import create_backup
from calendar_bridge.db.database import Database
from calendar_bridge.db.repo import Repo
from calendar_bridge.errors import BridgeError
from calendar_bridge.sync.context import HOME_TIMEZONE, EngineContext
from calendar_bridge.sync.engine import Engine


class LockBusy(BridgeError):
    pass


@dataclasses.dataclass
class App:
    settings: Settings
    db: Database
    repo: Repo
    clock: Clock
    _providers: ProviderFactory | None = None
    _store: CredentialStore | None = None
    stop: threading.Event | None = None
    _credentials_seen: tuple[str | None] | None = None

    @property
    def ctx(self) -> EngineContext:
        return EngineContext(self.settings, self.db, self.repo, self.clock, self.stop)

    def reload_credentials_if_changed(self) -> bool:
        """Pick up re-authorization done from another process (``calendar-bridge auth``)."""
        current = (self.repo.get_meta("credentials_changed_at"),)
        previous, self._credentials_seen = self._credentials_seen, current
        if previous is None or previous == current:
            return False
        if isinstance(self._providers, StoredCredentialProviders):
            self._providers = None
        self._store = None
        return True

    def box(self) -> SecretBox:
        return SecretBox(load_keys(self.settings.key_file))

    def maybe_box(self) -> SecretBox | None:
        keys = load_keys(self.settings.key_file)
        return SecretBox(keys) if keys else None

    @property
    def store(self) -> CredentialStore:
        if self._store is None:
            self._store = CredentialStore(self.repo, self.box())
        return self._store

    @property
    def providers(self) -> ProviderFactory:
        if self._providers is None:
            # The store is opened on first use, so a missing or wrong key surfaces as an
            # authorization problem of the provider being used (see StoredCredentialProviders).
            self._providers = StoredCredentialProviders(self.settings, lambda: self.store)
        return self._providers

    def engine(self) -> Engine:
        return Engine(self.ctx, self.providers)

    def close(self) -> None:
        self.db.close()


def open_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    providers: ProviderFactory | None = None,
    migrate: bool = True,
) -> App:
    settings = settings or load_settings()
    if os.name == "posix":
        os.umask(0o077)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    clock = clock or SystemClock()
    db = Database(settings.db_path, clock)
    existed = settings.db_path.exists()
    db.connect()
    app = App(settings, db, Repo(db), clock, _providers=providers)
    if migrate and db.pending_migrations():
        if existed and db.applied_migrations():
            # Never migrate a live database without a restorable copy.
            create_backup(
                db, settings.backup_dir, box=app.maybe_box(), keep=settings.backup_keep, label="pre-migration"
            )
        db.migrate()
    if migrate and app.repo.get_meta(HOME_TIMEZONE) is None and app.repo.mappings(("active", "paused")):
        # Calendar pairs started by 0.1.0 used the configured zone, or Europe/Brussels by default.
        explicit = settings.default_timezone != "auto"
        with db.tx():
            app.repo.set_meta(HOME_TIMEZONE, app.ctx.default_tz if explicit else "Europe/Brussels")
    return app


@contextlib.contextmanager
def cycle_lock(settings: Settings, timeout: float) -> Iterator[None]:
    """Serializes everything that writes synchronization state (daemon cycles, CLI changes)."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(settings.lock_file))
    try:
        lock.acquire(timeout=timeout)
    except Timeout as exc:
        raise LockBusy("another synchronization is running; try again shortly") from exc
    try:
        yield
    finally:
        lock.release()


@contextlib.contextmanager
def daemon_lock(settings: Settings) -> Iterator[None]:
    lock = FileLock(str(settings.daemon_lock_file))
    try:
        lock.acquire(timeout=0)
    except Timeout as exc:
        raise LockBusy("a calendar-bridge daemon is already running against this data directory") from exc
    try:
        yield
    finally:
        lock.release()
