"""Secret resolution — keys are *referenced*, never stored inline.

Two reference forms are supported in config values:

* ``${ENV_VAR}``               → read from the process environment
* ``keychain:service/account`` → read from the OS keychain via ``keyring``

A plain string with no recognised prefix is returned as-is (useful for local
providers like Ollama that need no secret, or for tests). Resolution failures
raise :class:`SecretResolutionError` so onboarding can surface a clear message.
"""

from __future__ import annotations

import os
import re

_ENV_RE = re.compile(r"^\$\{(?P<var>[A-Za-z_][A-Za-z0-9_]*)\}$")
_KEYCHAIN_RE = re.compile(r"^keychain:(?P<service>[^/]+)/(?P<account>.+)$")


class SecretResolutionError(RuntimeError):
    """Raised when a referenced secret cannot be resolved."""


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

    # Literal value (e.g. a local-only base URL token, or a test stub).
    return reference


def _resolve_keychain(service: str, account: str) -> str:
    try:
        import keyring
    except ImportError as exc:  # pragma: no cover - keyring is a hard dep
        raise SecretResolutionError(
            "keyring is required to resolve keychain:* references"
        ) from exc

    value = keyring.get_password(service, account)
    if not value:
        raise SecretResolutionError(
            f"No keychain entry for service={service!r} account={account!r}. "
            f"Store it with: keyring set {service} {account}"
        )
    return value


def store_keychain(service: str, account: str, value: str) -> None:
    """Persist a secret to the OS keychain (used by the onboarding wizard)."""
    import keyring

    keyring.set_password(service, account, value)


def is_reference(value: str | None) -> bool:
    """True if ``value`` looks like an unresolved secret reference."""
    if value is None:
        return False
    return bool(_ENV_RE.match(value) or _KEYCHAIN_RE.match(value))
