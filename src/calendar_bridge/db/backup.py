"""Database backup and restore.

Backups use SQLite's online backup API (consistent while the daemon runs) and are encrypted with
the credential key when one is available, because the database contains calendar content. The
encrypted file holds one Fernet token per line, each for one chunk of the database, so memory use
stays small whatever the database size (a single-token file from older versions is just the
one-chunk case). A backup only appears under its final name once it is complete.

Restoring requires the daemon to be stopped; afterwards every mapping is forced through a full
re-observation so that anything that changed after the backup is re-detected, and the bridge's
markers on mirrored events let it re-link rather than duplicate them.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from calendar_bridge.clock import Clock
from calendar_bridge.crypto import SecretBox
from calendar_bridge.db.database import Database
from calendar_bridge.errors import ConfigError, IntegrityFailure

PREFIX = "calendar-bridge-"
CHUNK = 1024 * 1024
STALE_TEMP = 3600  # seconds after which a leftover working folder (an interrupted backup) is removed


def _stamp(clock: Clock) -> str:
    return clock.now().strftime("%Y%m%dT%H%M%SZ")


def create_backup(db: Database, backup_dir: Path, *, box: SecretBox | None, keep: int, label: str = "auto") -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    _remove_stale_work(backup_dir)
    base = backup_dir / f"{PREFIX}{_stamp(db.clock)}-{label}.db"
    final = base if box is None else base.with_suffix(".db.enc")
    with tempfile.TemporaryDirectory(dir=backup_dir) as tmp:
        plain = Path(tmp) / "snapshot.db"
        db.backup_to(plain)
        staged = plain
        if box is not None:
            staged = Path(tmp) / "snapshot.enc"
            with plain.open("rb") as source, staged.open("w", encoding="ascii") as target:
                while chunk := source.read(CHUNK):
                    target.write(box.encrypt(chunk) + "\n")
                target.flush()
                os.fsync(target.fileno())
        if os.name == "posix":
            staged.chmod(0o600)
        os.replace(staged, final)  # same folder: atomic, never a partial file under the final name
    prune_backups(backup_dir, keep)
    return final


def _remove_stale_work(backup_dir: Path) -> None:
    """Working folders left by a backup that was interrupted (e.g. the process was killed). They
    hold a plain copy of the database, so they must not linger."""
    cutoff = time.time() - STALE_TEMP
    for entry in backup_dir.iterdir():
        if entry.is_dir() and entry.name.startswith("tmp") and entry.stat().st_mtime < cutoff:
            shutil.rmtree(entry, ignore_errors=True)


def list_backups(backup_dir: Path) -> list[Path]:
    if not backup_dir.exists():
        return []
    found = [p for p in backup_dir.iterdir() if p.name.startswith(PREFIX) and p.suffix in (".db", ".enc")]
    return sorted(found, key=lambda p: p.name)


def prune_backups(backup_dir: Path, keep: int) -> None:
    automatic = [p for p in list_backups(backup_dir) if "-auto." in p.name]
    for stale in automatic[: max(0, len(automatic) - keep)]:
        stale.unlink(missing_ok=True)


def _decrypt_to(source: Path, target: Path, box: SecretBox) -> None:
    try:
        with source.open("r", encoding="ascii") as lines, target.open("wb") as out:
            for line in lines:
                if line.strip():
                    out.write(box.decrypt(line.strip()))
    except (ConfigError, UnicodeDecodeError) as exc:
        raise IntegrityFailure(
            f"{source.name} cannot be decrypted: it was encrypted with another key (keep old keys in the key "
            "file while you may need backups made with them), or the file is damaged"
        ) from exc


def restore_backup(source: Path, db_path: Path, *, box: SecretBox | None, clock: Clock) -> Path:
    """Replace the database with ``source``. Returns the path the old database was moved to."""
    with tempfile.TemporaryDirectory(dir=db_path.parent) as tmp:
        candidate = Path(tmp) / "restore.db"
        if source.name.endswith(".enc"):
            if box is None:
                raise IntegrityFailure("backup is encrypted but no encryption key is configured")
            _decrypt_to(source, candidate, box)
        else:
            shutil.copyfile(source, candidate)
        try:
            conn = sqlite3.connect(candidate)
            try:
                result = [r[0] for r in conn.execute("PRAGMA integrity_check")]
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            raise IntegrityFailure(f"{source.name} is not a usable database: {exc}") from exc
        if result != ["ok"]:
            raise IntegrityFailure(f"backup failed integrity check: {result[:3]}")

        aside = db_path.with_name(f"{db_path.name}.pre-restore-{_stamp(clock)}")
        if db_path.exists():
            # Fold the WAL into the main file so the set-aside copy is self-contained.
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
            shutil.move(str(db_path), aside)
        for suffix in ("-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        shutil.move(str(candidate), db_path)
    if os.name == "posix":
        db_path.chmod(0o600)
    return aside
