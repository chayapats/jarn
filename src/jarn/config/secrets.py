"""Secret resolution — keys are *referenced*, never stored inline.

Three reference forms are supported in config values:

* ``${ENV_VAR}``               → read from the process environment
* ``keychain:service/account`` → read from the OS keychain via ``keyring``
* ``file:service/account``      → read from ``~/.jarn/secrets/<service>/<account>``

A plain string with no recognised prefix is returned as-is (useful for local
providers like Ollama that need no secret, or for tests). Resolution failures
raise :class:`SecretResolutionError` so onboarding can surface a clear message.
"""

from __future__ import annotations

import contextlib
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

_ENV_RE = re.compile(r"^\$\{(?P<var>[A-Za-z_][A-Za-z0-9_]*)\}$")
_KEYCHAIN_RE = re.compile(r"^keychain:(?P<service>[^/]+)/(?P<account>.+)$")
_FILE_RE = re.compile(r"^file:(?P<service>[^/]+)/(?P<account>.+)$")
_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: How long to wait on the OS keychain before falling back to file storage.
_KEYRING_TIMEOUT_SECS = 5.0


class SecretResolutionError(RuntimeError):
    """Raised when a referenced secret cannot be resolved."""


@dataclass(frozen=True)
class StoredSecret:
    """Result of persisting a secret during onboarding or ``/key``."""

    reference: str
    backend: Literal["keychain", "file"]


def resolve(reference: str | None) -> str | None:
    """Resolve a single secret reference into its concrete value.

    Returns ``None`` for ``None`` input (provider needs no key).
    """
    if reference is None:
        return None

    env_match = _ENV_RE.match(reference)
    if env_match:
        var = env_match.group("var")
        value = os.environ.get(var)
        if not value:
            raise SecretResolutionError(
                f"Environment variable ${{{var}}} is referenced in config but not set."
            )
        return value

    kc_match = _KEYCHAIN_RE.match(reference)
    if kc_match:
        return _resolve_keychain(kc_match.group("service"), kc_match.group("account"))

    file_match = _FILE_RE.match(reference)
    if file_match:
        return _resolve_file(file_match.group("service"), file_match.group("account"))

    # Literal value (e.g. a local-only base URL token, or a test stub).
    return reference


def _validate_account(account: str) -> None:
    if not _ACCOUNT_RE.match(account):
        raise ValueError(f"invalid secret account name {account!r}")


def _secret_file_path(service: str, account: str) -> Path:
    _validate_account(service)
    _validate_account(account)
    from jarn.config.paths import global_home

    return global_home() / "secrets" / service / account


def _keyring_call(
    op: Literal["get", "set"],
    service: str,
    account: str,
    value: str | None = None,
    *,
    timeout: float = _KEYRING_TIMEOUT_SECS,
) -> Any:
    """Run a blocking keyring operation with a hard timeout.

    A headless Linux host (e.g. Raspberry Pi over SSH) often has no Secret
    Service backend; without a timeout ``set_password`` / ``get_password`` can
    block forever waiting on D-Bus.
    """
    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            import keyring

            if op == "get":
                box["result"] = keyring.get_password(service, account)
            else:
                keyring.set_password(service, account, value)
                box["result"] = True
        except Exception as exc:  # noqa: BLE001 - any backend failure
            box["err"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"OS keychain did not respond within {timeout:.0f}s")
    if "err" in box:
        raise box["err"]
    return box.get("result")


def _resolve_keychain(service: str, account: str) -> str:
    try:
        value = _keyring_call("get", service, account)
    except TimeoutError as exc:
        raise SecretResolutionError(
            f"Timed out reading keychain entry service={service!r} account={account!r}. "
            "On headless Linux, install a keyring backend or re-run setup to store "
            "the key under ~/.jarn/secrets/."
        ) from exc
    except ImportError as exc:  # pragma: no cover - keyring is a hard dep
        raise SecretResolutionError(
            "keyring is required to resolve keychain:* references"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - surface backend failures clearly
        raise SecretResolutionError(
            f"Couldn't read keychain entry service={service!r} account={account!r}: {exc}"
        ) from exc

    if not value:
        raise SecretResolutionError(
            f"No keychain entry for service={service!r} account={account!r}. "
            f"Store it with: keyring set {service} {account}"
        )
    return value


def _resolve_file(service: str, account: str) -> str:
    path = _secret_file_path(service, account)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise SecretResolutionError(
            f"No file secret at {path}. Re-run jarn setup or use /key to store the key."
        ) from exc
    except OSError as exc:
        raise SecretResolutionError(f"Couldn't read secret file {path}: {exc}") from exc
    if not value:
        raise SecretResolutionError(f"Secret file {path} is empty.")
    return value


def _store_file_secret(service: str, account: str, value: str) -> Path:
    path = _secret_file_path(service, account)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    with contextlib.suppress(OSError):
        path.parent.chmod(0o700)
    return path


def store_secret(
    service: str,
    account: str,
    value: str,
    *,
    timeout: float = _KEYRING_TIMEOUT_SECS,
) -> StoredSecret:
    """Persist a secret, preferring the OS keychain with a file-store fallback.

    Tries ``keyring.set_password`` first (bounded by ``timeout``). On timeout
    or any backend error, writes ``~/.jarn/secrets/<service>/<account>`` with
    mode ``0600`` and returns a ``file:`` reference instead.
    """
    _validate_account(service)
    _validate_account(account)
    keychain_ref = f"keychain:{service}/{account}"
    file_ref = f"file:{service}/{account}"
    try:
        _keyring_call("set", service, account, value, timeout=timeout)
    except (TimeoutError, Exception):  # noqa: BLE001 - fall back on any keyring failure
        _store_file_secret(service, account, value)
        return StoredSecret(reference=file_ref, backend="file")
    return StoredSecret(reference=keychain_ref, backend="keychain")


def store_keychain(service: str, account: str, value: str) -> str:
    """Persist a secret (keychain preferred, file fallback). Returns the reference."""
    return store_secret(service, account, value).reference


def file_fallback_notice(
    stored: StoredSecret,
    *,
    provider: str,
    env_var: str | None = None,
) -> str | None:
    """Return a short what-to-do-next notice when ``stored`` used file fallback."""
    if stored.backend != "file":
        return None
    path = _secret_file_path("jarn", provider)
    lines = [
        "OS keychain unavailable on this machine — your API key was saved to "
        f"{path} (mode 600) instead.",
        "",
        "What to do next:",
        "  • Continue setup — the key is already stored; no further action needed.",
        "  • Launch with `jarn` when setup finishes.",
    ]
    if env_var:
        lines.append(
            f"  • Prefer environment variables? Export ${{{env_var}}} in your shell "
            "profile, re-run `jarn setup`, and choose \"Read from environment variable\"."
        )
    lines.append(
        "  • Want the OS keychain later? Install a Secret Service backend "
        "(e.g. gnome-keyring on Linux) and update the key with `/key`."
    )
    return "\n".join(lines)


def is_reference(value: str | None) -> bool:
    """True if ``value`` looks like an unresolved secret reference."""
    if value is None:
        return False
    return bool(_ENV_RE.match(value) or _KEYCHAIN_RE.match(value) or _FILE_RE.match(value))
