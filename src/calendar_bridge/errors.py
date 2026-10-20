"""Error taxonomy.

Provider errors are classified by what the sync engine should *do* about them, not by HTTP status:

* ``TransientError`` (and ``RateLimited``): retry later with backoff; never interpreted as data.
* ``AuthError``: credentials need human attention; stop touching that provider and alert.
* ``NotFound``: the addressed resource does not exist. Only a ``NotFound`` from a *direct* read
  of a specific resource counts as deletion evidence; listing gaps never do.
* ``PreconditionFailed``: a conditional write lost a race; re-observe and re-plan.
* everything else is permanent for the request as sent.
"""

from __future__ import annotations


class BridgeError(Exception):
    """Base class for all Calendar Bridge errors."""


class ConfigError(BridgeError):
    pass


class CredentialsMissing(BridgeError):
    pass


class IntegrityFailure(BridgeError):
    """The local database failed an integrity check; destructive writes must stop."""


class ProviderError(BridgeError):
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        status: int | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status = status
        self.reason = reason

    def short(self) -> str:
        parts = [type(self).__name__]
        if self.provider:
            parts.append(self.provider)
        if self.status is not None:
            parts.append(str(self.status))
        if self.reason:
            parts.append(self.reason)
        return ":".join(parts)


class TransientError(ProviderError):
    retryable = True


class RateLimited(TransientError):
    def __init__(self, message: str, *, retry_after: float | None = None, **kw: object) -> None:
        super().__init__(message, **kw)  # type: ignore[arg-type]
        self.retry_after = retry_after


class AuthError(ProviderError):
    """Authentication failed (bad password, revoked or expired refresh token)."""


class PermissionDenied(ProviderError):
    pass


class NotFound(ProviderError):
    pass


class AlreadyExists(ProviderError):
    pass


class PreconditionFailed(ProviderError):
    pass


class InvalidRequest(ProviderError):
    pass


class SyncTokenInvalid(ProviderError):
    """The provider no longer accepts our sync token; a full resynchronization is required."""


class UidConflict(ProviderError):
    """CalDAV refused a resource because another resource already uses the same UID."""

    def __init__(self, message: str, *, existing_href: str | None = None, **kw: object) -> None:
        super().__init__(message, **kw)  # type: ignore[arg-type]
        self.existing_href = existing_href


class UnsupportedFeature(ProviderError):
    """The server does not support a protocol feature (e.g. sync-collection)."""
