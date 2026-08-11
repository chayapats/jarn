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
import json
import os
import re
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from jarn.util.atomic import atomic_write_text

_ENV_RE = re.compile(r"^\$\{(?P<var>[A-Za-z_][A-Za-z0-9_]*)\}$")
_KEYCHAIN_RE = re.compile(r"^keychain:(?P<service>[^/]+)/(?P<account>.+)$")
_FILE_RE = re.compile(r"^file:(?P<service>[^/]+)/(?P<account>.+)$")
_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: How long to wait on the OS keychain before falling back to file storage.
_KEYRING_TIMEOUT_SECS = 5.0

#: Placeholder for fully-redacted secrets.
_REDACTED = "[REDACTED]"

# Resolved credentials can have completely provider-specific shapes, so pattern
# matching alone cannot keep them out of transcripts, logs, or support output.
# Keep a small process-local registry of values that have crossed the secret
# resolver/storage boundary.  Every central redaction call consults it.  The
# registry is deliberately bounded: it is a defensive sink filter, not another
# credential store, and it must not grow without limit in a long-running gateway.
_MAX_RESOLVED_SECRETS = 512
_resolved_secrets: dict[str, None] = {}
_resolved_secrets_lock = threading.Lock()


def _remember_resolved_secret(value: str | None) -> None:
    """Register one concrete credential for exact-value sink redaction."""

    if not value:
        return
    with _resolved_secrets_lock:
        # Reinsert so the most recently used values survive bounded eviction.
        _resolved_secrets.pop(value, None)
        _resolved_secrets[value] = None
        while len(_resolved_secrets) > _MAX_RESOLVED_SECRETS:
            del _resolved_secrets[next(iter(_resolved_secrets))]


def _known_resolved_secrets() -> set[str]:
    """Return a copy; callers must never receive the mutable registry itself."""

    with _resolved_secrets_lock:
        return set(_resolved_secrets)


def _clear_resolved_secrets_for_testing() -> None:
    """Reset process-local redaction state for an isolated unit test."""

    with _resolved_secrets_lock:
        _resolved_secrets.clear()

# ── Central secret redaction ────────────────────────────────────────────────
# A single source of truth for scrubbing secret-shaped substrings out of
# transcripts, logs, and error messages. Pattern-based (catches accidentally
# pasted keys) plus an optional ``known`` set of live secret values to scrub
# verbatim (catches a real resolved key that has no vendor prefix).
#
# This is a defensive net, not a guarantee — it targets the common vendor key
# prefixes, PEM private-key blocks, Bearer tokens, NAME=value env assignments,
# and long high-entropy base64 blobs. ``sk-``-style keys keep their prefix and
# last 4 chars (``sk-…XXXX``) so a user can identify which key leaked without
# exposing it; everything else is fully replaced.
_SK_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._-]+", re.IGNORECASE)
_PEM = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
#: Long base64 blob with no vendor prefix (a raw secret). The replacement fn
#: skips single-character runs (e.g. test padding "xxxxx") so legitimately
#: truncated tool output that happens to be all one char is not wiped.
_BASE64_BLOB = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
_VENDOR_KEYS = re.compile(
    r"\b(?:ghp|gho|ghs|ghr|ghu)_[A-Za-z0-9]{20,}\b"
    r"|\bxox[baprs]-[A-Za-z0-9-]{10,}\b"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bAIza[0-9A-Za-z_-]{20,}\b"
    r"|\bglpat-[A-Za-z0-9_-]{16,}\b"
)
_NAME_VALUE = re.compile(
    r"\b([A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|PASSWD|ACCESS_KEY)[A-Z0-9_]*)"
    r"\s*[=:]\s*\S+"
)


def _sk_replacement(m: re.Match[str]) -> str:
    token = m.group(0)
    return f"sk-…{token[-4:]}" if len(token) >= 4 else _REDACTED


def _bearer_replacement(m: re.Match[str]) -> str:
    return f"{m.group(0).split()[0]} {_REDACTED}"


def _base64_replacement(m: re.Match[str]) -> str:
    blob = m.group(0).rstrip("=")
    if len(set(blob)) < 2:  # single-char run (e.g. test padding) — not a secret
        return m.group(0)
    return _REDACTED


def _name_value_replacement(m: re.Match[str]) -> str:
    return f"{m.group(1)}={_REDACTED}"


def redact_secrets(value: str, *, known: set[str] | None = None) -> str:
    """Replace recognised secret-shaped substrings in *value* with placeholders.

    The single source of truth for transcript/log/error scrubbing. Pattern-based
    (``sk-…``, ``Bearer …``, PEM blocks, vendor prefixes, ``NAME=secret`` env
    assignments, long base64 blobs) plus an optional ``known`` set of live secret
    values to scrub verbatim — call it with the resolved provider keys so a real
    key that lacks a vendor prefix is still caught. ``sk-``-style keys are masked
    to ``sk-…XXXX`` (prefix + last 4); everything else becomes ``[REDACTED]``.
    """
    if not value:
        return value
    text = value
    all_known = _known_resolved_secrets()
    if known:
        all_known.update(known)
    if all_known:
        # Longest first so a short secret that is a substring of a longer one
        # doesn't get partially redacted before the longer one is handled.
        for secret in sorted(all_known, key=len, reverse=True):
            # Known values are user-declared exact secrets — scrub any non-empty
            # value regardless of length; the >=8 floor is only for heuristic
            # pattern detection (below), not for values the caller marked secret.
            if secret:
                text = text.replace(secret, _REDACTED)
    text = _PEM.sub(_REDACTED, text)
    text = _BEARER.sub(_bearer_replacement, text)
    text = _SK_KEY.sub(_sk_replacement, text)
    text = _VENDOR_KEYS.sub(_REDACTED, text)
    text = _BASE64_BLOB.sub(_base64_replacement, text)
    text = _NAME_VALUE.sub(_name_value_replacement, text)
    return text


_SENSITIVE_FIELD = re.compile(
    r"(?:^|_)(?:api_?key|access_?key|auth|authorization|credential|password|passwd|"
    r"refresh_?token|secret|token)(?:$|_)",
    re.IGNORECASE,
)


def redact_structure(
    value: Any,
    *,
    known: set[str] | None = None,
    max_depth: int = 12,
    _depth: int = 0,
) -> Any:
    """Recursively scrub a JSON-like value with the central secret redactor.

    Support reports, structured errors, gateway frames, and transcripts used to
    grow their own slightly different recursive scrubbers.  That is dangerous:
    adding a token pattern to :func:`redact_secrets` then protected only some of
    the sinks.  This helper is the shared structured boundary.  In addition to
    scrubbing secret-shaped *values*, fields whose names are credential-shaped
    are replaced wholesale, including short PINs that a heuristic cannot safely
    recognise.

    Containers are copied, never mutated.  A depth bound makes the function safe
    on model/provider supplied structures; values past the bound are omitted.
    """
    if _depth >= max_depth:
        return "[OMITTED: structure too deep]"
    if isinstance(value, str):
        return redact_secrets(value, known=known)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if _SENSITIVE_FIELD.search(key):
                out[key] = _REDACTED
            else:
                out[key] = redact_structure(
                    item, known=known, max_depth=max_depth, _depth=_depth + 1
                )
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            redact_structure(item, known=known, max_depth=max_depth, _depth=_depth + 1)
            for item in value
        ]
    return value


def is_sensitive_field_name(value: object) -> bool:
    """Return whether a config/map field name conventionally carries a secret."""

    return bool(_SENSITIVE_FIELD.search(str(value)))


def resolve_secret_mapping(mapping: dict[str, str]) -> dict[str, str]:
    """Resolve secret references in a header/environment mapping without mutation."""

    return {key: resolve(value) or "" for key, value in mapping.items()}


class SecretResolutionError(RuntimeError):
    """Raised when a referenced secret cannot be resolved."""


class SecretStorageError(RuntimeError):
    """Raised when secret persistence cannot be completed unambiguously."""


class KeyringOperationError(RuntimeError):
    """A keyring worker reported a typed backend failure without its raw message."""

    def __init__(self, operation: str, error_type: str) -> None:
        self.operation = operation
        self.error_type = error_type
        super().__init__(f"OS keychain {operation} failed ({error_type})")


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
        _remember_resolved_secret(value)
        return value

    kc_match = _KEYCHAIN_RE.match(reference)
    if kc_match:
        value = _resolve_keychain(kc_match.group("service"), kc_match.group("account"))
        _remember_resolved_secret(value)
        return value

    file_match = _FILE_RE.match(reference)
    if file_match:
        value = _resolve_file(file_match.group("service"), file_match.group("account"))
        _remember_resolved_secret(value)
        return value

    # Literal value (e.g. a local-only base URL token, or a test stub).
    if looks_like_secret(reference):
        _remember_resolved_secret(reference)
    return reference


def _validate_account(account: str) -> None:
    if not _ACCOUNT_RE.match(account):
        raise ValueError(f"invalid secret account name {account!r}")


def _secret_file_path(service: str, account: str) -> Path:
    _validate_account(service)
    _validate_account(account)
    from jarn.config.paths import global_secrets_dir

    return global_secrets_dir() / service / account


_KEYRING_WORKER_SELECTOR = "__jarn_internal_keyring_worker__"
_KEYRING_WORKER_CODE = (
    "from jarn.config.secrets import _keyring_worker_main; "
    "raise SystemExit(_keyring_worker_main())"
)


def _keyring_worker_main() -> int:
    """Process one keyring request from stdin and emit one redacted response."""

    try:
        request = json.load(sys.stdin)
        import keyring

        result: Any
        if request["op"] == "get":
            result = keyring.get_password(request["service"], request["account"])
        elif request["op"] == "set":
            keyring.set_password(request["service"], request["account"], request["value"])
            result = True
        elif request["op"] == "delete":
            keyring.delete_password(request["service"], request["account"])
            result = True
        elif request["op"] == "metadata":
            backend = keyring.get_keyring()
            priority = getattr(backend, "priority", None)
            result = {
                "available": not isinstance(priority, (int, float)) or priority > 0,
                "backend": f"{type(backend).__module__}.{type(backend).__name__}",
                "priority": priority if isinstance(priority, (int, float)) else None,
            }
        else:
            raise ValueError("unsupported keyring operation")
        response = {"ok": True, "result": result}
    except BaseException as exc:
        # Backend messages are not returned: some implementations interpolate
        # the credential they were given. The exception class is enough to
        # explain the failure without putting a secret on stdout/stderr.
        response = {"ok": False, "error_type": type(exc).__name__}
    sys.stdout.write(json.dumps(response))
    return 0


def _keyring_worker_command() -> list[str]:
    """Return a source- or PyInstaller-compatible isolated worker command."""

    if getattr(sys, "frozen", False):
        return [sys.executable, _KEYRING_WORKER_SELECTOR]
    return [sys.executable, "-c", _KEYRING_WORKER_CODE]


def _kill_keyring_worker(process: subprocess.Popen[bytes]) -> None:
    """Terminate and reap the isolated worker, including its POSIX process group."""

    if process.poll() is None:
        if os.name == "posix":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        else:  # pragma: no cover - native Windows is not a supported runtime
            process.kill()
    # ``communicate`` after a timeout is supported and preserves buffered output.
    # Reaping here is mandatory: callers must never continue with a live worker
    # that can mutate the keychain after a timeout was reported.
    process.communicate()


def _keyring_call(
    op: Literal["get", "set", "delete", "metadata"],
    service: str,
    account: str,
    value: str | None = None,
    *,
    timeout: float = _KEYRING_TIMEOUT_SECS,
) -> Any:
    """Run a blocking keyring operation in a killable subprocess.

    A headless Linux host (e.g. Raspberry Pi over SSH) often has no Secret
    Service backend; without a timeout ``set_password`` / ``get_password`` can
    block forever waiting on D-Bus.  The request is sent over stdin so the
    credential never appears in argv, logs, or a temporary file.  A timed-out
    worker is killed and reaped before this function returns.
    """
    if timeout <= 0:
        raise ValueError("keyring timeout must be greater than zero")
    if op == "set" and value is None:
        raise ValueError("value is required for keyring set")

    request = json.dumps(
        {"op": op, "service": service, "account": account, "value": value}
    ).encode("utf-8")
    process = subprocess.Popen(  # noqa: S603 - fixed interpreter and worker code
        _keyring_worker_command(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    try:
        stdout, _stderr = process.communicate(input=request, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_keyring_worker(process)
        raise TimeoutError(f"OS keychain did not respond within {timeout:.0f}s") from exc

    if process.returncode != 0:
        raise RuntimeError(f"OS keychain worker exited with status {process.returncode}")
    try:
        response = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("OS keychain worker returned an invalid response") from exc
    if not isinstance(response, dict) or not response.get("ok"):
        error_type = (
            response.get("error_type", "backend error")
            if isinstance(response, dict)
            else "backend error"
        )
        raise KeyringOperationError(op, str(error_type))
    return response.get("result")


def _resolve_keychain(service: str, account: str) -> str:
    try:
        _validate_account(service)
        _validate_account(account)
    except ValueError as exc:
        raise SecretResolutionError(str(exc)) from exc
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
            redact_secrets(
                f"Couldn't read keychain entry service={service!r} "
                f"account={account!r}: {exc}"
            )
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


def _ensure_secret_tree_permissions(path: Path) -> None:
    """Ensure ``~/.jarn/secrets/`` and every ancestor up to it are mode ``0700``.

    Called after writing a file secret so a pre-existing permissive directory
    (e.g. ``~/.jarn`` left at ``755``) cannot expose secrets via group/other read.
    """
    from jarn.config.paths import global_secrets_dir

    secrets_root = global_secrets_dir()
    current = path if path.is_dir() else path.parent
    while True:
        with contextlib.suppress(OSError):
            current.chmod(0o700)
        if current == secrets_root:
            break
        if current == current.parent:
            break
        current = current.parent


def _store_file_secret(service: str, account: str, value: str) -> Path:
    path = _secret_file_path(service, account)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Mode is set on the temp file BEFORE the rename: a plain write-then-chmod
    # leaves the key readable at the process umask for the window in between.
    atomic_write_text(path, value, mode=0o600)
    _ensure_secret_tree_permissions(path)
    return path


def store_secret(
    service: str,
    account: str,
    value: str,
    *,
    timeout: float = _KEYRING_TIMEOUT_SECS,
) -> StoredSecret:
    """Persist a secret, preferring the OS keychain with a file-store fallback.

    Tries ``keyring.set_password`` first (bounded by ``timeout``). A definite
    backend error writes ``~/.jarn/secrets/<service>/<account>`` with mode
    ``0600`` and returns a ``file:`` reference. A timeout is indeterminate and
    fails closed: writing a fallback then could leave the credential in both
    stores if the keychain operation completed at the timeout boundary.
    """
    _remember_resolved_secret(value)
    _validate_account(service)
    _validate_account(account)
    keychain_ref = f"keychain:{service}/{account}"
    file_ref = f"file:{service}/{account}"
    try:
        _keyring_call("set", service, account, value, timeout=timeout)
    except TimeoutError as exc:
        raise SecretStorageError(
            "OS keychain write timed out; its completion state is unknown, so no "
            "file fallback was created. Check the keychain service and retry."
        ) from exc
    except Exception:  # noqa: BLE001 - a definite backend failure permits fallback
        _store_file_secret(service, account, value)
        return StoredSecret(reference=file_ref, backend="file")
    return StoredSecret(reference=keychain_ref, backend="keychain")


def store_keychain(service: str, account: str, value: str) -> str:
    """Persist a secret (keychain preferred, file fallback). Returns the reference."""
    return store_secret(service, account, value).reference


def delete_keychain_secret(
    service: str,
    account: str,
    *,
    timeout: float = _KEYRING_TIMEOUT_SECS,
) -> None:
    """Delete one keychain entry through the bounded, killable worker."""

    _validate_account(service)
    _validate_account(account)
    _keyring_call("delete", service, account, timeout=timeout)


def keyring_backend_metadata(*, timeout: float = 2.0) -> dict[str, Any]:
    """Return non-credential backend metadata through the bounded worker."""

    result = _keyring_call("metadata", "jarn", "doctor", timeout=timeout)
    if not isinstance(result, dict):
        raise KeyringOperationError("metadata", "InvalidResponse")
    return {
        "available": bool(result.get("available")),
        "backend": str(result["backend"]) if result.get("backend") else None,
        "priority": (
            result["priority"] if isinstance(result.get("priority"), (int, float)) else None
        ),
        "credentials_read": False,
    }


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


#: A long, mixed-alphabet string with no spaces — a likely raw API key with no
#: vendor prefix. Requires a reasonable character variety so short or repetitive
#: test fixtures (``sk-test``, ``lm-studio``) and URLs are not flagged.
_HIGH_ENTROPY = re.compile(r"^[A-Za-z0-9+/_=\-]{32,}$")


def looks_like_secret(value: object) -> bool:
    """Heuristic: does *value* look like a real secret rather than a reference?

    Used by the config loader to catch inline plaintext ``api_key`` values. It
    matches the vendor prefixes shared with :func:`redact_secrets`
    (``sk-…``, ``Bearer …``, PEM blocks, ``ghp_``/``xoxb-``/``AKIA``/``AIza``/
    ``glpat-``) plus a ≥32-char high-entropy fallback for prefixless keys. Short
    or repetitive values (``sk-test``, ``lm-studio``, empty) return ``False`` so
    local providers and test fixtures don't trip the check.
    """
    if not isinstance(value, str) or not value:
        return False
    if _SK_KEY.search(value) or _VENDOR_KEYS.search(value) or _BEARER.search(value):
        return True
    if "-----BEGIN" in value and "PRIVATE KEY-----" in value:
        return True
    return bool(_HIGH_ENTROPY.match(value)) and len(set(value)) >= 8
