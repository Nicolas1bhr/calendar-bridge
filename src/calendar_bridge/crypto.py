"""Credential encryption.

Credentials (OAuth refresh token, OAuth client secret, iCloud app-specific password) are stored
in the database only as Fernet tokens (AES-128-CBC + HMAC-SHA256). The key comes from outside
the database - an environment variable or, preferably, a Docker secret file - so a copied
database or backup alone discloses nothing.

Several keys may be supplied (one per line / comma separated). The first encrypts; all decrypt,
which allows rotating keys without downtime (``calendar-bridge keys rotate``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from calendar_bridge.errors import ConfigError, CredentialsMissing


def generate_key() -> str:
    return Fernet.generate_key().decode("ascii")


def _parse_keys(text: str) -> list[str]:
    keys = []
    for line in text.replace(",", "\n").splitlines():
        chunk = line.strip()
        if chunk and not chunk.startswith("#"):
            keys.append(chunk)
    return keys


MISSING_KEY = (
    "no encryption key configured. With Docker Compose, create it once with `docker compose run --rm keygen` "
    "(it writes secrets/calendar_bridge_key); otherwise generate one with `calendar-bridge keys generate` "
    "and point CB_ENCRYPTION_KEY_FILE at it"
)


def read_key_file(path: Path) -> list[str]:
    """Keys from a key file, with an actionable error for each way the file can be wrong."""
    if path.is_dir():
        raise ConfigError(
            f"the encryption key file {path} is a directory: Docker creates an empty one when the file is "
            "missing. Create the key with `docker compose run --rm keygen` (it replaces the empty directory)"
        )
    try:
        text = path.read_text(encoding="utf-8-sig")  # editors such as Notepad add a byte-order mark
    except PermissionError as exc:
        uid = os.getuid() if hasattr(os, "getuid") else "?"
        raise ConfigError(
            f"the encryption key file {path} is not readable by this process (uid {uid}). The container runs "
            "as uid 10001: `sudo chown 10001:10001 secrets/calendar_bridge_key && "
            "sudo chmod 400 secrets/calendar_bridge_key`"
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read the encryption key file {path}: {type(exc).__name__}") from exc
    keys = _parse_keys(text)
    if not keys:
        raise ConfigError(
            f"the encryption key file {path} is empty: create the key with `docker compose run --rm keygen`"
        )
    return keys


def key_file_is_empty(path: Path) -> bool:
    """A readable key file without any key (e.g. left by a failed `keys generate > file`)."""
    try:
        return path.is_file() and not _parse_keys(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError):
        return False


def load_keys(key_file: Path | None) -> list[str]:
    env = os.environ.get("CB_ENCRYPTION_KEY", "").strip()
    if env:
        return _parse_keys(env)
    if key_file is not None and key_file.exists():
        return read_key_file(key_file)
    return []


class SecretBox:
    def __init__(self, keys: list[str]) -> None:
        if not keys:
            raise CredentialsMissing(MISSING_KEY)
        try:
            self._fernets = [Fernet(k.encode("ascii")) for k in keys]
        except (ValueError, TypeError, UnicodeEncodeError) as exc:
            raise ConfigError(
                "the encryption key is not valid (expected the 44-character value printed by "
                "`calendar-bridge keys generate`)"
            ) from exc
        self._multi = MultiFernet(self._fernets)

    def encrypt(self, data: bytes) -> str:
        return self._multi.encrypt(data).decode("ascii")

    def decrypt(self, token: str) -> bytes:
        try:
            return self._multi.decrypt(token.encode("ascii"))
        except InvalidToken as exc:
            raise ConfigError(
                "stored data cannot be decrypted with the configured encryption key. If the key file was "
                "replaced, put the original key back; if it is lost, authorize again with `calendar-bridge "
                "setup` (synchronization state is kept)"
            ) from exc

    def rotate(self, token: str) -> str:
        try:
            return self._multi.rotate(token.encode("ascii")).decode("ascii")
        except InvalidToken as exc:
            raise ConfigError(
                "a stored credential cannot be decrypted with any configured key: add the new key as the first "
                "line of the key file and keep the old key below it until the rotation is done"
            ) from exc

    def encrypt_json(self, value: Any) -> str:
        return self.encrypt(json.dumps(value, sort_keys=True).encode("utf-8"))

    def decrypt_json(self, token: str) -> Any:
        return json.loads(self.decrypt(token).decode("utf-8"))
