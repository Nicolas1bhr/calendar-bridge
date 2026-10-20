"""Encrypted credential storage and construction of authenticated provider clients."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from calendar_bridge.config import Settings
from calendar_bridge.crypto import SecretBox, load_keys
from calendar_bridge.db.repo import Repo
from calendar_bridge.errors import AuthError, ConfigError, CredentialsMissing
from calendar_bridge.providers.google.auth import (
    ClientSecrets,
    authorized_session,
    build_credentials,
    token_record,
)
from calendar_bridge.providers.google.client import GoogleCalendarClient
from calendar_bridge.providers.icloud.client import CalDAVClient
from calendar_bridge.providers.retry import RetryPolicy

GOOGLE_CLIENT = "google_client"
GOOGLE_TOKEN = "google_token"
ICLOUD = "icloud"


class CredentialStore:
    def __init__(self, repo: Repo, box: SecretBox) -> None:
        self.repo = repo
        self.box = box

    @classmethod
    def from_settings(cls, repo: Repo, settings: Settings) -> CredentialStore:
        return cls(repo, SecretBox(load_keys(settings.key_file)))

    def _get(self, name: str) -> dict[str, Any] | None:
        token = self.repo.get_credential(name)
        return None if token is None else dict(self.box.decrypt_json(token))

    def _put(self, name: str, value: dict[str, Any]) -> None:
        self.repo.put_credential(name, self.box.encrypt_json(value))

    def google_client_secrets(self) -> ClientSecrets | None:
        data = self._get(GOOGLE_CLIENT)
        return ClientSecrets.from_json(data) if data else None

    def save_google_client_secrets(self, secrets: ClientSecrets) -> None:
        self._put(GOOGLE_CLIENT, secrets.to_json())

    def google_token(self) -> dict[str, Any] | None:
        return self._get(GOOGLE_TOKEN)

    def save_google_token(self, token: dict[str, Any]) -> None:
        self._put(GOOGLE_TOKEN, token)

    def icloud(self) -> dict[str, Any] | None:
        return self._get(ICLOUD)

    def save_icloud(self, *, username: str, password: str, url: str) -> None:
        self._put(ICLOUD, {"username": username, "password": password, "url": url})

    def delete(self, name: str) -> None:
        self.repo.delete_credential(name)

    def rotate(self) -> int:
        count = 0
        for name in self.repo.credential_names():
            token = self.repo.get_credential(name)
            if token is not None:
                self.repo.put_credential(name, self.box.rotate(token))
                count += 1
        return count


class ProviderFactory(Protocol):
    def google(self) -> GoogleCalendarClient: ...

    def icloud(self) -> CalDAVClient: ...

    def after_cycle(self) -> None: ...


class StoredCredentialProviders:
    """Builds real API clients from the encrypted credential store."""

    def __init__(self, settings: Settings, store: CredentialStore | Callable[[], CredentialStore]) -> None:
        self.settings = settings
        self._store = store
        self._google: GoogleCalendarClient | None = None
        self._google_creds: Any = None
        self._icloud: CalDAVClient | None = None
        self.retry = RetryPolicy(settings.retry_attempts, settings.retry_base_delay, settings.retry_max_delay)

    @property
    def store(self) -> CredentialStore:
        return self._store if isinstance(self._store, CredentialStore) else self._store()

    def _read[T](self, provider: str, read: Callable[[CredentialStore], T]) -> T:
        """Stored credentials that cannot be used (missing or wrong encryption key) are an
        authorization problem for that provider: it is reported and alerted on as such."""
        try:
            return read(self.store)
        except (ConfigError, CredentialsMissing) as exc:
            raise AuthError(f"stored credentials cannot be used: {exc}", provider=provider) from exc

    def google(self) -> GoogleCalendarClient:
        if self._google is None:
            secrets, token = self._read("google", lambda s: (s.google_client_secrets(), s.google_token()))
            if secrets is None or token is None or not token.get("refresh_token"):
                raise CredentialsMissing("Google is not authorized; run `calendar-bridge setup`")
            self._google_creds = build_credentials(secrets, token)
            self._google = GoogleCalendarClient(
                authorized_session(self._google_creds), timeout=self.settings.http_timeout, retry=self.retry
            )
        return self._google

    def icloud(self) -> CalDAVClient:
        if self._icloud is None:
            data = self._read("icloud", lambda s: s.icloud())
            if data is None:
                raise CredentialsMissing("iCloud is not authorized; run `calendar-bridge setup`")
            self._icloud = CalDAVClient(
                base_url=data.get("url") or self.settings.icloud_url,
                username=data["username"],
                password=data["password"],
                timeout=self.settings.http_timeout,
                retry=self.retry,
            )
        return self._icloud

    def after_cycle(self) -> None:
        """Persist a rotated refresh token (Google rarely rotates, but must not be lost)."""
        creds = self._google_creds
        if creds is None:
            return
        stored = self.store.google_token() or {}
        if creds.refresh_token and creds.refresh_token != stored.get("refresh_token"):
            self.store.save_google_token(token_record(creds))

    def reset(self) -> None:
        self._google = None
        self._google_creds = None
        self._icloud = None
