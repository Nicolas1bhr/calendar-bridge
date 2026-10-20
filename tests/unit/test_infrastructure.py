"""Configuration, credential encryption, database, backups, logging redaction."""

from __future__ import annotations

import io
import json
import logging
import sqlite3
from pathlib import Path

import pytest

from calendar_bridge.clock import FrozenClock, SystemClock
from calendar_bridge.config import Settings, load_settings
from calendar_bridge.crypto import SecretBox, generate_key, load_keys
from calendar_bridge.db.backup import create_backup, list_backups, restore_backup
from calendar_bridge.db.database import Database
from calendar_bridge.db.repo import Repo
from calendar_bridge.errors import ConfigError, CredentialsMissing, IntegrityFailure
from calendar_bridge.logs import JsonFormatter, RedactingFilter
from calendar_bridge.sync.context import home_timezone


def test_settings_precedence_and_validation(tmp_path: Path) -> None:
    config = tmp_path / "cb.toml"
    config.write_text(
        'sync_interval = 120\ndefault_timezone = "Europe/Paris"\nmirror_google_event_types = ["fromGmail"]\n'
    )
    env = {"CB_SYNC_INTERVAL": "90", "CB_DEFAULT_HISTORY_DAYS": "all", "CB_ICLOUD_URL": "https://caldav.icloud.com/"}
    settings = load_settings(config, env=env)
    assert settings.sync_interval == 90  # env beats file
    assert settings.default_timezone == "Europe/Paris"
    assert settings.default_history_days is None
    with pytest.raises(ConfigError):
        load_settings(config, env={"CB_SYNC_INTERVAL": "5"})
    with pytest.raises(ConfigError):
        load_settings(config, env={"CB_DEFAULT_TIMEZONE": "Mars/Olympus"})
    bad = tmp_path / "bad.toml"
    bad.write_text("unknown_setting = 1\n")
    with pytest.raises(ConfigError):
        load_settings(bad, env={})
    with pytest.raises(ConfigError):
        load_settings(None, env={"CB_ICLOUD_URL": "http://insecure/"})


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"CB_SYNC_INTERVAL": "abc"}, "CB_SYNC_INTERVAL: 'abc' is not a whole number"),
        ({"CB_STATUS_CALENDAR": "ture"}, "is not true or false"),
        ({"CB_MASS_DELETE_RATIO": "lots"}, "is not a number"),
        ({"CB_SYNC_INTERVL": "60"}, "did you mean CB_SYNC_INTERVAL?"),
        ({"CB_BACKUP_KEEP": "0"}, "backup_keep (CB_BACKUP_KEEP) must be at least 1"),
        ({"CB_ALERT_NTFY_URL": "ntfy.sh/my-topic"}, "starting with https://"),
        ({"CB_ALERT_EMAIL_TO": "me@example.com"}, "CB_ALERT_SMTP_HOST"),
        ({"CB_LOG_LEVEL": "loud"}, "must be one of"),
    ],
)
def test_settings_mistakes_are_explained(env: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigError) as caught:
        load_settings(None, env=env)
    assert message in str(caught.value)


def test_settings_accept_the_documented_forms() -> None:
    env = {"CB_STATUS_CALENDAR": "No", "CB_LOG_LEVEL": "debug", "CB_HEARTBEAT_URL": "", "CB_LIVE": "1"}
    settings = load_settings(None, env=env)
    assert settings.status_calendar is False and settings.log_level == "DEBUG" and settings.heartbeat_url is None


def test_secret_box_rotation_and_wrong_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old, new = generate_key(), generate_key()
    token = SecretBox([old]).encrypt_json({"password": "s3cret"})
    assert "s3cret" not in token
    both = SecretBox([new, old])
    assert both.decrypt_json(token) == {"password": "s3cret"}
    rotated = both.rotate(token)
    assert SecretBox([new]).decrypt_json(rotated) == {"password": "s3cret"}
    with pytest.raises(ConfigError):
        SecretBox([generate_key()]).decrypt_json(token)
    with pytest.raises(CredentialsMissing):
        SecretBox([])
    key_file = tmp_path / "key"
    key_file.write_text(f"# comment\n{new}\n{old}\n")
    monkeypatch.delenv("CB_ENCRYPTION_KEY", raising=False)
    assert load_keys(key_file) == [new, old]
    with pytest.raises(ConfigError, match="not valid"):
        SecretBox(["not-a-key"])


def test_key_file_mistakes_get_actionable_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CB_ENCRYPTION_KEY", raising=False)
    assert load_keys(tmp_path / "absent") == []
    directory = tmp_path / "calendar_bridge_key"
    directory.mkdir()  # Docker creates this when the bind-mounted file is missing
    with pytest.raises(ConfigError, match="keygen"):
        load_keys(directory)
    empty = tmp_path / "empty"
    empty.write_text("\n# nothing here\n")
    with pytest.raises(ConfigError, match="empty"):
        load_keys(empty)


def test_default_time_zone_follows_the_google_account(tmp_path: Path) -> None:
    db = Database(tmp_path / "tz.db", SystemClock())
    db.connect()
    db.migrate()
    repo = Repo(db)
    auto = Settings(data_dir=tmp_path, key_file=None)
    assert auto.default_timezone == "auto"
    assert home_timezone(auto, repo) == "UTC", "nothing known yet"
    with db.tx():
        repo.upsert_calendar(
            provider="google",
            remote_id="me@example.com",
            display_name="me",
            access="owner",
            writable=True,
            supports_events=True,
            is_primary=True,
            timezone="America/New_York",
        )
    assert home_timezone(auto, repo) == "America/New_York"
    fixed = Settings(data_dir=tmp_path, key_file=None, default_timezone="Romance Standard Time")
    assert home_timezone(fixed, repo) == "Europe/Paris"
    db.close()


def test_database_migrations_wal_and_savepoints(tmp_path: Path) -> None:
    db = Database(tmp_path / "x.db", SystemClock())
    assert db.migrate() == ["0001_initial.sql"]
    assert db.migrate() == []
    assert db.scalar("PRAGMA journal_mode") == "wal"
    assert db.scalar("PRAGMA foreign_keys") == 1
    repo = Repo(db)
    with db.tx():
        repo.set_meta("a", "1")
        with pytest.raises(RuntimeError), db.tx():
            repo.set_meta("b", "2")
            raise RuntimeError("inner failure")
    assert repo.get_meta("a") == "1" and repo.get_meta("b") is None
    assert db.integrity_check() == []


def test_one_to_one_mapping_constraint(tmp_path: Path) -> None:
    db = Database(tmp_path / "x.db", SystemClock())
    db.migrate()
    repo = Repo(db)
    with db.tx():
        g = repo.upsert_calendar(
            provider="google",
            remote_id="g",
            display_name="G",
            access="owner",
            writable=True,
            supports_events=True,
            is_primary=True,
            timezone=None,
        )
        i = repo.upsert_calendar(
            provider="icloud",
            remote_id="i",
            display_name="I",
            access="read-write",
            writable=True,
            supports_events=True,
            is_primary=True,
            timezone=None,
        )
        first = repo.create_mapping(name="a", google_calendar_id=g, icloud_calendar_id=i, history_days=None)
    with pytest.raises(sqlite3.IntegrityError), db.tx():
        repo.create_mapping(name="b", google_calendar_id=g, icloud_calendar_id=i, history_days=None)
    with db.tx():
        repo.update_mapping(first, state="removed")
        repo.create_mapping(name="c", google_calendar_id=g, icloud_calendar_id=i, history_days=None)


def test_encrypted_backup_and_restore(tmp_path: Path) -> None:
    clock = FrozenClock(__import__("datetime").datetime(2030, 1, 1, tzinfo=__import__("datetime").UTC))
    db_path = tmp_path / "data" / "calendar-bridge.db"
    db = Database(db_path, clock)
    db.migrate()
    repo = Repo(db)
    with db.tx():
        repo.set_meta("marker", "before")
    box = SecretBox([generate_key()])
    backup = create_backup(db, tmp_path / "backups", box=box, keep=3)
    assert backup.name.endswith(".db.enc")
    assert b"SQLite format" not in backup.read_bytes()
    with db.tx():
        repo.set_meta("marker", "after")
    db.close()
    aside = restore_backup(backup, db_path, box=box, clock=clock)
    assert aside.exists()
    restored = Database(db_path, clock)
    assert Repo(restored).get_meta("marker") == "before"
    restored.close()
    with pytest.raises(IntegrityFailure):
        restore_backup(backup, db_path, box=None, clock=clock)
    corrupt = tmp_path / "backups" / "calendar-bridge-corrupt-manual.db"
    corrupt.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
    with pytest.raises((IntegrityFailure, sqlite3.DatabaseError)):
        restore_backup(corrupt, db_path, box=None, clock=clock)


def test_backup_rotation_keeps_manual_backups(tmp_path: Path) -> None:
    import datetime as dt

    clock = FrozenClock(dt.datetime(2030, 1, 1, tzinfo=dt.UTC))
    db = Database(tmp_path / "x.db", clock)
    db.migrate()
    create_backup(db, tmp_path / "b", box=None, keep=2, label="manual")
    for _ in range(4):
        clock.advance(hours=1)
        create_backup(db, tmp_path / "b", box=None, keep=2)
    names = [p.name for p in list_backups(tmp_path / "b")]
    assert sum("-auto." in n for n in names) == 2 and sum("-manual." in n for n in names) == 1


def test_logging_redacts_content_and_emits_json() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger("calendar_bridge.test_redaction")
    logger.addHandler(handler)
    logger.propagate = False
    logger.warning("synced item", extra={"item_id": 7, "summary": "Doctor appointment", "password": "x"})
    payload = json.loads(stream.getvalue())
    assert payload["item_id"] == 7 and payload["summary"] == "[redacted]" and payload["password"] == "[redacted]"
    assert "Doctor" not in stream.getvalue()


def test_settings_paths() -> None:
    settings = Settings(data_dir=Path("/data"))
    assert settings.db_path == Path("/data/calendar-bridge.db")
    assert settings.backup_dir == Path("/data/backups")


def test_large_encrypted_backups_are_chunked_and_old_single_token_backups_still_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import datetime as dt
    import os
    import time

    from calendar_bridge.db import backup as backup_module

    monkeypatch.setattr(backup_module, "CHUNK", 4096)  # several chunks even for a small database
    clock = FrozenClock(dt.datetime(2030, 1, 1, tzinfo=dt.UTC))
    db_path = tmp_path / "data" / "calendar-bridge.db"
    db = Database(db_path, clock)
    db.migrate()
    with db.tx():
        Repo(db).set_meta("marker", "x" * 20000)
    box = SecretBox([generate_key()])
    stale = tmp_path / "backups" / "tmpleftover"
    stale.mkdir(parents=True)
    (stale / "snapshot.db").write_bytes(b"plain copy left by a killed backup")
    os.utime(stale, (time.time() - 7200, time.time() - 7200))
    chunked = create_backup(db, tmp_path / "backups", box=box, keep=5)
    assert len(chunked.read_text().splitlines()) > 3 and not stale.exists()
    single = tmp_path / "backups" / "calendar-bridge-old-manual.db.enc"
    plain = tmp_path / "plain.db"
    db.backup_to(plain)
    single.write_text(box.encrypt(plain.read_bytes()), encoding="ascii")  # the format of earlier versions
    db.close()
    for source in (chunked, single):
        restore_backup(source, db_path, box=box, clock=clock)
        restored = Database(db_path, clock)
        assert Repo(restored).get_meta("marker") == "x" * 20000
        restored.close()
    truncated = tmp_path / "backups" / "calendar-bridge-cut-manual.db.enc"
    truncated.write_text(chunked.read_text()[:5000], encoding="ascii")
    with pytest.raises(IntegrityFailure, match=r"another key|damaged"):
        restore_backup(truncated, db_path, box=box, clock=clock)
    with pytest.raises(IntegrityFailure, match="another key"):
        restore_backup(chunked, db_path, box=SecretBox([generate_key()]), clock=clock)


def test_periodic_work_is_due_again_after_the_clock_is_set_back() -> None:
    import datetime as dt

    from calendar_bridge.clock import is_due, to_iso

    now = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)
    hour = dt.timedelta(hours=1)
    assert is_due(None, now, hour)
    assert not is_due(to_iso(now - dt.timedelta(minutes=10)), now, hour)
    assert is_due(to_iso(now - dt.timedelta(hours=2)), now, hour)
    assert is_due(to_iso(now + dt.timedelta(days=700)), now, hour), "a timestamp from a clock that ran ahead"


def test_an_empty_setting_followed_by_a_comment_stays_empty() -> None:
    # What Compose makes of `CB_ALERT_NTFY_URL=      # e.g. ...` in a .env file.
    settings = load_settings(None, env={"CB_ALERT_NTFY_URL": "# e.g. https://ntfy.sh/<topic>", "CB_BACKUP_KEEP": "#"})
    assert settings.alert_ntfy_url is None and settings.backup_keep == 14
