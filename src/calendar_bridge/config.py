"""Settings: defaults, overridden by an optional TOML file, overridden by ``CB_*`` variables.

Nothing secret belongs here except references to secret *files*; credentials obtained through
``calendar-bridge auth ...`` are stored encrypted in the database.
"""

from __future__ import annotations

import dataclasses
import difflib
import os
import tomllib
from pathlib import Path
from typing import Any

from calendar_bridge.errors import ConfigError
from calendar_bridge.model import timeutil

DEFAULT_CONFIG_PATHS = (Path("/config/calendar-bridge.toml"), Path("calendar-bridge.toml"))


def _default_data_dir() -> Path:
    container = Path("/data")
    return container if container.is_dir() else Path("data")


@dataclasses.dataclass
class Settings:
    data_dir: Path = dataclasses.field(default_factory=_default_data_dir)
    key_file: Path | None = Path("/run/secrets/calendar_bridge_key")

    sync_interval: int = 60
    full_reconcile_interval: int = 6 * 3600
    # Zone for times that carry none (floating times). "auto": the Google account's own zone.
    default_timezone: str = "auto"
    log_level: str = "INFO"
    log_format: str = "json"

    http_timeout: float = 30.0
    retry_attempts: int = 4
    retry_base_delay: float = 1.0
    retry_max_delay: float = 60.0
    item_retry_base: int = 60
    item_retry_max: int = 3600
    item_max_attempts: int = 12
    max_items_per_cycle: int = 500

    lag_alert_seconds: int = 900
    cycle_timeout: int = 1800
    mass_delete_count: int = 10
    mass_delete_ratio: float = 0.2

    meeting_policy: str = "authoritative"
    mirror_info_block: bool = True
    mirror_google_event_types: tuple[str, ...] = ("fromGmail",)
    default_history_days: int | None = 365

    status_calendar: bool = True
    status_calendar_name: str = "Calendar Bridge Status"
    status_update_interval: int = 300

    alert_ntfy_url: str | None = None
    alert_ntfy_token_file: Path | None = None
    alert_webhook_url: str | None = None
    alert_email_to: str | None = None
    alert_email_from: str | None = None
    alert_smtp_host: str | None = None
    alert_smtp_port: int = 587
    alert_smtp_user: str | None = None
    alert_smtp_password_file: Path | None = None
    alert_smtp_starttls: bool = True
    alert_repeat_hours: int = 24
    conflict_alert_grace_minutes: int = 10
    heartbeat_url: str | None = None

    tombstone_retention_days: int = 90
    backup_interval_hours: int = 24
    backup_keep: int = 14
    integrity_check_interval_hours: int = 6

    icloud_url: str = "https://caldav.icloud.com/"
    google_oauth_port: int = 8765

    # derived paths --------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "calendar-bridge.db"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def status_file(self) -> Path:
        return self.data_dir / "status.json"

    @property
    def lock_file(self) -> Path:
        return self.data_dir / "calendar-bridge.lock"

    @property
    def daemon_lock_file(self) -> Path:
        return self.data_dir / "calendar-bridge.daemon.lock"

    def validate(self) -> None:
        if self.default_timezone != "auto" and timeutil.canonical_tzid(self.default_timezone) is None:
            raise ConfigError(
                f"default_timezone {self.default_timezone!r} is not a known zone (use an IANA name such as "
                "'Europe/Brussels', or 'auto')"
            )
        for name, minimum in MINIMUMS.items():
            value = getattr(self, name)
            if value is not None and value < minimum:
                raise ConfigError(f"{_label(name)} must be at least {minimum} (it is {value})")
        if self.google_oauth_port > 65535 or not 0 < self.alert_smtp_port <= 65535:
            raise ConfigError("port numbers must be between 1 and 65535")
        choices = {
            "meeting_policy": ("authoritative", "propagate"),
            "log_format": ("json", "text"),
            "log_level": ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        }
        for name, allowed in choices.items():
            if getattr(self, name) not in allowed:
                raise ConfigError(f"{_label(name)} must be one of: {', '.join(allowed)}")
        if not 0 < self.mass_delete_ratio <= 1:
            raise ConfigError(f"{_label('mass_delete_ratio')} must be greater than 0 and at most 1")
        if not self.icloud_url.startswith("https://") and not os.environ.get("CB_ALLOW_INSECURE_CALDAV"):
            raise ConfigError(f"{_label('icloud_url')} must use https")
        for name in ("alert_ntfy_url", "alert_webhook_url", "heartbeat_url"):
            url = getattr(self, name)
            if url and not url.startswith(("https://", "http://")):
                raise ConfigError(f"{_label(name)} must be a full address starting with https://")
        for name in ("alert_ntfy_token_file", "alert_smtp_password_file"):
            path = getattr(self, name)
            if path is not None and not path.is_file():
                raise ConfigError(f"{_label(name)}: file {path} not found")
        if bool(self.alert_email_to) != bool(self.alert_smtp_host):
            raise ConfigError("e-mail alerts need both CB_ALERT_EMAIL_TO and CB_ALERT_SMTP_HOST")


# Lower bounds that keep the service sane; anything below is almost certainly a typo.
MINIMUMS = {
    "sync_interval": 15,
    "full_reconcile_interval": 300,
    "http_timeout": 1,
    "retry_attempts": 1,
    "retry_base_delay": 0,
    "retry_max_delay": 0,
    "item_retry_base": 0,
    "item_retry_max": 0,
    "item_max_attempts": 1,
    "max_items_per_cycle": 1,
    "lag_alert_seconds": 60,
    "cycle_timeout": 60,
    "mass_delete_count": 0,
    "default_history_days": 0,
    "status_update_interval": 60,
    "alert_repeat_hours": 1,
    "conflict_alert_grace_minutes": 0,
    "tombstone_retention_days": 1,
    "backup_interval_hours": 1,
    "backup_keep": 1,
    "integrity_check_interval_hours": 1,
    "google_oauth_port": 0,
}
# Variables read outside the settings (and the opt-in live tests' CB_LIVE*).
OTHER_VARIABLES = frozenset({"CB_CONFIG", "CB_ENCRYPTION_KEY", "CB_ENCRYPTION_KEY_FILE", "CB_ALLOW_INSECURE_CALDAV"})
TRUE, FALSE = ("1", "true", "yes", "on"), ("0", "false", "no", "off")


def _label(name: str) -> str:
    return f"{name} (CB_{name.upper()})"


def _coerce(source: str, field: dataclasses.Field[Any], value: Any) -> Any:
    kind = str(field.type)
    if value is None or value == "":
        return None if "None" in kind else field.default
    text = str(value).strip()
    if "Path" in kind:
        return Path(text)
    if kind.startswith("bool"):
        if isinstance(value, bool):
            return value
        if text.lower() in TRUE + FALSE:
            return text.lower() in TRUE
        raise ConfigError(f"{source}: {value!r} is not true or false")
    if kind.startswith("int"):
        if "None" in kind and text.lower() in ("all", "none", "unlimited"):
            return None
        if isinstance(value, bool) or not text.lstrip("-").isdigit():
            raise ConfigError(f"{source}: {value!r} is not a whole number")
        return int(text)
    if kind.startswith("float"):
        try:
            return float(text)
        except ValueError:
            raise ConfigError(f"{source}: {value!r} is not a number") from None
    if kind.startswith("tuple"):
        if isinstance(value, (list, tuple)):
            return tuple(str(v) for v in value)
        return tuple(v.strip() for v in text.split(",") if v.strip())
    return text


def _unknown(name: str, known: list[str]) -> str:
    close = difflib.get_close_matches(name, known, n=1)
    return f"unknown setting {name}" + (f" (did you mean {close[0]}?)" if close else "")


def load_settings(config_path: Path | None = None, env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ) if env is None else env
    values: dict[str, Any] = {}
    # An explicit --config file must exist; CB_CONFIG (set by docker-compose) is optional.
    if config_path is not None and not config_path.exists():
        raise ConfigError(f"config file {config_path} not found")
    path = config_path or (Path(env["CB_CONFIG"]) if env.get("CB_CONFIG") else None)
    candidates = [path] if path else list(DEFAULT_CONFIG_PATHS)
    fields = {f.name: f for f in dataclasses.fields(Settings)}
    for candidate in candidates:
        if candidate is not None and candidate.exists():
            try:
                with candidate.open("rb") as handle:
                    data = tomllib.load(handle)
            except (OSError, tomllib.TOMLDecodeError) as exc:
                raise ConfigError(f"cannot read {candidate}: {exc}") from exc
            for key, value in data.items():
                if key not in fields:
                    raise ConfigError(f"{_unknown(key, sorted(fields))} in {candidate}")
                values[key] = _coerce(f"{key} in {candidate}", fields[key], value)
            break
    known = {f"CB_{name.upper()}": name for name in fields}
    for env_name, raw in env.items():
        if not env_name.startswith("CB_") or env_name in OTHER_VARIABLES or env_name.startswith("CB_LIVE"):
            continue
        if env_name not in known:
            raise ConfigError(_unknown(env_name, sorted(known)))
        # Compose reads `NAME=   # comment` (an empty setting followed by a comment, as in older
        # .env templates) as the comment itself: the setting was meant to be empty.
        value = "" if raw.strip().startswith("#") else raw
        values[known[env_name]] = _coerce(env_name, fields[known[env_name]], value)
    if "CB_ENCRYPTION_KEY_FILE" in env:
        values["key_file"] = Path(env["CB_ENCRYPTION_KEY_FILE"])
    settings = Settings(**values)
    settings.log_level = settings.log_level.upper()
    settings.validate()
    return settings
