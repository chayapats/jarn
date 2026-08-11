"""In-memory credential staging with rollback-safe final activation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from jarn.config.paths import global_secrets_dir
from jarn.config.secrets import (
    StoredSecret,
    delete_keychain_secret,
    redact_secrets,
    store_secret,
)


class CredentialActivationError(RuntimeError):
    """A staged setup credential could not be activated or rolled back."""


@dataclass(frozen=True, slots=True)
class ActivatedCredential:
    """A new, uniquely named credential that can be discarded on failure."""

    provider: str
    service: str
    account: str
    stored: StoredSecret

    @property
    def reference(self) -> str:
        return self.stored.reference


@dataclass(slots=True)
class PendingCredentials:
    """Process-memory-only API keys collected before final confirmation."""

    _values: dict[str, str] = field(default_factory=dict, repr=False)

    def set(self, provider: str, value: str) -> None:
        if not value:
            raise ValueError("credential value cannot be empty")
        self._values[provider] = value

    def get(self, provider: str) -> str | None:
        return self._values.get(provider)

    def contains(self, provider: str) -> bool:
        return provider in self._values

    def discard(self, provider: str) -> None:
        self._values.pop(provider, None)


def activate_pending_credential(
    provider: str,
    value: str,
    *,
    token: str | None = None,
) -> ActivatedCredential:
    """Persist under a fresh account so an active credential is never overwritten."""

    suffix = token or uuid.uuid4().hex[:16]
    account = f"{provider}.setup-{suffix}"
    try:
        stored = store_secret("jarn", account, value)
    except Exception as exc:  # noqa: BLE001 - convert backend-specific failures
        raise CredentialActivationError(
            f"could not store the staged {provider} key: {exc}"
        ) from exc
    return ActivatedCredential(
        provider=provider,
        service="jarn",
        account=account,
        stored=stored,
    )


def _delete_keychain(service: str, account: str, *, timeout: float = 5.0) -> None:
    try:
        delete_keychain_secret(service, account, timeout=timeout)
    except TimeoutError as exc:
        raise CredentialActivationError(
            f"timed out removing staged keychain entry {service}/{account}; "
            "the isolated worker was terminated"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - backend contract varies
        raise CredentialActivationError(
            f"could not remove staged keychain entry {service}/{account}: "
            f"{redact_secrets(str(exc))}"
        ) from exc


def _file_path(credential: ActivatedCredential) -> Path:
    return global_secrets_dir() / credential.service / credential.account


def rollback_activated_credential(credential: ActivatedCredential) -> None:
    """Delete only the fresh setup-owned entry after a failed config transaction."""

    if credential.stored.backend == "keychain":
        _delete_keychain(credential.service, credential.account)
        return
    path = _file_path(credential)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CredentialActivationError(
            f"could not remove staged file secret {path}: {exc}"
        ) from exc


def credential_storage_notice(credential: ActivatedCredential) -> str:
    """Describe the committed backend/path without exposing credential contents."""

    if credential.stored.backend == "keychain":
        return f"Credential stored in the OS keychain as {credential.reference}."
    return (
        f"OS keychain unavailable; credential stored at {_file_path(credential)} "
        f"(private file) as {credential.reference}."
    )


__all__ = [
    "ActivatedCredential",
    "CredentialActivationError",
    "PendingCredentials",
    "activate_pending_credential",
    "credential_storage_notice",
    "rollback_activated_credential",
]
