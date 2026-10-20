"""SQLite connection management: WAL mode, durable commits, migrations, integrity checks.

Every state transition the sync engine relies on is committed with ``synchronous=FULL`` in WAL
mode, so a committed checkpoint or write intent survives a crash or power loss.
"""

from __future__ import annotations

import contextlib
import importlib.resources
import itertools
import os
import sqlite3
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from calendar_bridge.clock import Clock, to_iso

Params = Sequence[Any] | dict[str, Any]


def _restrict_permissions(path: Path) -> None:
    if os.name == "posix" and path.exists():
        path.chmod(0o600)


class Database:
    def __init__(self, path: Path, clock: Clock) -> None:
        self.path = path
        self.clock = clock
        self._conn: sqlite3.Connection | None = None
        self._savepoints = itertools.count()

    # ------------------------------------------------------------ connection

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._conn = conn
            _restrict_permissions(self.path)
        return self._conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self.connect()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @contextlib.contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Transaction; nested use becomes a savepoint so inner failures roll back cleanly."""
        conn = self.conn
        if conn.in_transaction:
            name = f"sp{next(self._savepoints)}"
            conn.execute(f"SAVEPOINT {name}")
            try:
                yield conn
            except BaseException:
                conn.execute(f"ROLLBACK TO {name}")
                conn.execute(f"RELEASE {name}")
                raise
            conn.execute(f"RELEASE {name}")
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def execute(self, sql: str, params: Params = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def query(self, sql: str, params: Params = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params))

    def query_one(self, sql: str, params: Params = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Params = ()) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return None if row is None else row[0]

    def now(self) -> str:
        return to_iso(self.clock.now())

    # ------------------------------------------------------------ migrations

    @staticmethod
    def available_migrations() -> list[tuple[int, str, str]]:
        folder = importlib.resources.files("calendar_bridge.db") / "migrations"
        found = []
        for entry in folder.iterdir():
            name = entry.name
            if name.endswith(".sql") and name[:4].isdigit():
                found.append((int(name[:4]), name, entry.read_text(encoding="utf-8")))
        return sorted(found)

    def applied_migrations(self) -> set[int]:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        return {row[0] for row in self.conn.execute("SELECT version FROM schema_migrations")}

    def pending_migrations(self) -> list[tuple[int, str, str]]:
        applied = self.applied_migrations()
        return [m for m in self.available_migrations() if m[0] not in applied]

    def migrate(self) -> list[str]:
        """Apply pending migrations, each in its own transaction. Returns applied names."""
        applied_names = []
        for version, name, sql in self.pending_migrations():
            with self.tx() as conn:
                for statement in _split_sql(sql):
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                    (version, name, self.now()),
                )
            applied_names.append(name)
        return applied_names

    def schema_version(self) -> int:
        applied = self.applied_migrations()
        return max(applied) if applied else 0

    # ------------------------------------------------------------ integrity

    def quick_check(self) -> list[str]:
        rows = [r[0] for r in self.conn.execute("PRAGMA quick_check")]
        problems = [] if rows == ["ok"] else rows
        problems += [f"foreign key violation in {r[0]}" for r in self.conn.execute("PRAGMA foreign_key_check")]
        return problems

    def integrity_check(self) -> list[str]:
        rows = [r[0] for r in self.conn.execute("PRAGMA integrity_check")]
        problems = [] if rows == ["ok"] else rows
        problems += [f"foreign key violation in {r[0]}" for r in self.conn.execute("PRAGMA foreign_key_check")]
        return problems

    def backup_to(self, destination: Path) -> None:
        """Consistent online copy using SQLite's backup API (safe while the daemon runs)."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(destination)
        try:
            self.conn.backup(target)
        finally:
            target.close()
        _restrict_permissions(destination)


def _split_sql(script: str) -> list[str]:
    """Split a migration script into statements (no triggers/procedures are used)."""
    statements = []
    buffer: list[str] = []
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") or not stripped:
            continue
        buffer.append(line)
        if stripped.endswith(";"):
            statements.append("\n".join(buffer))
            buffer = []
    if buffer:
        statements.append("\n".join(buffer))
    return statements
