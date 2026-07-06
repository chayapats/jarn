"""OpenRouter OAuth PKCE login flow.

Public API
----------
pkce_verifier(length=64) -> str
    Generate a PKCE code verifier (43–128 chars, RFC 7636 unreserved set).

pkce_challenge(verifier) -> str
    Compute PKCE S256 challenge = BASE64URL(SHA256(verifier)) (no padding).

_make_callback_server() -> (HTTPServer, port)
    Create a loopback HTTP server on a random free port.

_wait_for_callback(server, *, timeout=300.0) -> str
    Block until GET /callback?code=X arrives; return the code.  Raises
    TimeoutError when timeout expires without a callback.

_exchange_and_store(code, verifier) -> StoredSecret
    POST the code + verifier to OpenRouter, receive the API key, and store
    it via the existing secret-storage path.  Never returns the raw key.

login_openrouter(open_browser, *, _timeout, _prompt_replace_or_keep) -> LoginResult
    Full OAuth PKCE flow.  ``open_browser`` is injectable so tests do not
    launch a real browser.  ``_prompt_replace_or_keep`` is injectable so
    tests can drive the replace/keep choice without a TTY.

Security notes
--------------
- Public client (no client secret in the authorize URL or exchange POST).
- Code verifier is never stored; it lives only in memory for the duration of
  this call.
- The raw API key is passed directly to ``store_secret``; the *reference*
  (not the key) is what callers receive and what ends up in config.yaml.
- All printed output goes through ``redact_secrets``.
"""

from __future__ import annotations

import base64
import hashlib
import secrets as _secrets_mod
import string
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from jarn.config.secrets import StoredSecret

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: RFC 7636 §4.1 unreserved characters for the code verifier.
_PKCE_ALPHABET: str = string.ascii_letters + string.digits + "-._~"

#: OpenRouter authorize and exchange endpoints (verified 2026-07-06).
_AUTHORIZE_URL = "https://openrouter.ai/auth"
_EXCHANGE_URL = "https://openrouter.ai/api/v1/auth/keys"

#: Keychain coordinates for the OpenRouter API key.
_SERVICE = "jarn"
_ACCOUNT = "openrouter"
_OPENROUTER_REF = f"keychain:{_SERVICE}/{_ACCOUNT}"


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass
class LoginResult:
    """Result of a successful ``login_openrouter`` call."""

    reference: str
    """Secret reference — e.g. ``keychain:jarn/openrouter`` or ``${ENV}``."""

    masked_key: str
    """Tail-masked representation for display — e.g. ``sk-…XXXX``."""

    backend: str
    """Where the key lives — ``keychain`` / ``file`` / ``env`` (from the reference)."""

    changed: bool = True
    """False when an existing key was kept (nothing to persist)."""


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------

def pkce_verifier(length: int = 64) -> str:
    """Generate a PKCE code verifier.

    Parameters
    ----------
    length:
        Number of characters, 43–128 (RFC 7636 §4.1 constraint).

    Returns
    -------
    str
        A random string drawn from the RFC 7636 unreserved character set.
    """
    if not (43 <= length <= 128):
        raise ValueError(f"PKCE verifier length must be 43–128 chars; got {length}")
    return "".join(_secrets_mod.choice(_PKCE_ALPHABET) for _ in range(length))


def pkce_challenge(verifier: str) -> str:
    """Compute the S256 PKCE code challenge from *verifier*.

    Returns BASE64URL(SHA256(ASCII(verifier))) with no ``=`` padding,
    matching RFC 7636 §4.2.
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# ---------------------------------------------------------------------------
# Loopback callback server
# ---------------------------------------------------------------------------

class _CallbackHandler(BaseHTTPRequestHandler):
    """One-shot HTTP handler that captures the ``?code=`` query parameter."""

    def do_GET(self) -> None:  # noqa: N802 - HTTP method naming
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        codes = params.get("code", [])
        if parsed.path == "/callback" and codes:
            self.server._code_box["code"] = codes[0]  # type: ignore[attr-defined]
            body = (
                b"<html><body>"
                b"<h2>Authorisation complete</h2>"
                b"<p>You can close this tab and return to your terminal.</p>"
                b"</body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: D102
        # Suppress the default stderr logging from BaseHTTPRequestHandler.
        pass


def _make_callback_server() -> tuple[HTTPServer, int]:
    """Create a loopback HTTP server on a random free port.

    Returns
    -------
    (server, port)
        The server instance and the port it is listening on.
    """
    server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    # Attach the box the handler drops the captured code into.
    server._code_box = {}  # type: ignore[attr-defined]
    port = server.server_address[1]
    return server, port


def _wait_for_callback(server: HTTPServer, *, timeout: float = 300.0) -> str:
    """Block until a ``GET /callback?code=X`` arrives or *timeout* expires.

    Parameters
    ----------
    server:
        A server created by :func:`_make_callback_server`.
    timeout:
        Maximum seconds to wait (default 300 = 5 min).

    Returns
    -------
    str
        The OAuth authorization code.

    Raises
    ------
    TimeoutError
        When no callback arrives within *timeout* seconds.
    """
    server.timeout = min(timeout, 5.0)  # handle_request poll interval
    deadline = _monotonic() + timeout
    while _monotonic() < deadline:
        remaining = deadline - _monotonic()
        if remaining <= 0:
            break
        server.timeout = min(remaining, 5.0)
        server.handle_request()
        if server._code_box.get("code"):  # type: ignore[attr-defined]
            return server._code_box["code"]  # type: ignore[attr-defined]
    raise TimeoutError(
        f"No OAuth callback received within {timeout:.0f} s. "
        "If the browser did not open, use `jarn setup` to paste a key manually."
    )


def _monotonic() -> float:
    import time
    return time.monotonic()


def _module_wait_for_callback(server: HTTPServer, *, timeout: float = 300.0) -> str:
    """Module-level trampoline so the real ``_wait_for_callback`` can be injected."""
    return _wait_for_callback(server, timeout=timeout)


# ---------------------------------------------------------------------------
# Code exchange + key storage
# ---------------------------------------------------------------------------

def _exchange_and_store(code: str, verifier: str) -> StoredSecret:
    """Exchange an authorization code for an API key and store it securely.

    Parameters
    ----------
    code:
        The authorization code from the OAuth callback.
    verifier:
        The PKCE code verifier (never logged or stored).

    Returns
    -------
    StoredSecret
        The stored secret descriptor (reference + backend).  The raw key is
        never returned — callers receive only the opaque reference.
    """
    import httpx

    from jarn.config.secrets import store_secret

    resp = httpx.post(
        _EXCHANGE_URL,
        json={
            "code": code,
            "code_verifier": verifier,
            "code_challenge_method": "S256",
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    raw_key: str = data["key"]

    stored = store_secret(_SERVICE, _ACCOUNT, raw_key)
    return stored


# ---------------------------------------------------------------------------
# Existing-key helpers
# ---------------------------------------------------------------------------

def _resolve_existing(ref: str) -> str | None:
    """Return the resolved value for *ref*, or None if it cannot be resolved.

    Intentionally swallows resolution errors (missing keychain entry, etc.)
    so callers get a simple bool-like None rather than an exception.
    """
    from jarn.config.secrets import SecretResolutionError, resolve

    try:
        return resolve(ref)
    except (SecretResolutionError, Exception):  # noqa: BLE001
        return None


def _configured_openrouter_ref() -> str | None:
    """Read ``providers.openrouter.api_key`` from the global config, if present.

    Returns the raw reference string (``${ENV}`` / ``keychain:…`` / ``file:…``)
    so the existing-key check honours **any** configured key source, not just the
    keychain default.  Returns None when there is no config, no openrouter entry,
    or the file cannot be parsed.
    """
    from jarn.config import paths

    config_path = paths.global_config_path()
    if not config_path.is_file():
        return None
    try:
        from jarn.config.loader import _read_yaml

        data = _read_yaml(config_path)
    except Exception:  # noqa: BLE001 - a malformed config must not crash login
        return None
    providers = data.get("providers") or {}
    entry = providers.get("openrouter") or {}
    ref = entry.get("api_key")
    return ref if isinstance(ref, str) and ref else None


def _backend_for_ref(ref: str) -> str:
    """Map a secret reference to a human backend label (``env``/``keychain``/``file``)."""
    return key_source(ref)


def _mask_key(raw: str) -> str:
    """Return a tail-masked display form — ``sk-…XXXX``."""
    from jarn.config.secrets import redact_secrets

    return redact_secrets(raw)


# ---------------------------------------------------------------------------
# Replace/keep prompt
# ---------------------------------------------------------------------------

def _default_prompt_replace_or_keep(existing_ref: str) -> Literal["replace", "keep"]:
    """Interactive replace/keep prompt.

    Uses a small Textual OptionList when stdin/stdout are a TTY (the
    project's standard arrow-key UX); falls back to a Rich Prompt on pipes
    and CI.
    """
    import sys

    if sys.stdin.isatty() and sys.stdout.isatty():
        return _tui_replace_or_keep(existing_ref)
    return _plain_replace_or_keep(existing_ref)


def _plain_replace_or_keep(existing_ref: str) -> Literal["replace", "keep"]:
    from rich.prompt import Prompt

    choice = Prompt.ask(
        f"A key already exists ({existing_ref}).  Replace or keep?",
        choices=["replace", "keep"],
        default="keep",
    )
    return "replace" if choice == "replace" else "keep"


def _tui_replace_or_keep(existing_ref: str) -> Literal["replace", "keep"]:
    """Textual mini-app for the replace/keep decision."""
    from textual.app import App, ComposeResult
    from textual.widgets import OptionList, Static
    from textual.widgets.option_list import Option

    class _ReplaceKeepApp(App):
        CSS = """
        Screen { align: center middle; }
        #card { width: 60; height: auto; padding: 1 2; border: thick $primary; background: $surface; }
        OptionList { height: auto; border: none; }
        """

        def __init__(self) -> None:
            super().__init__()
            self.choice: str = "keep"

        def compose(self) -> ComposeResult:
            from textual.containers import Vertical

            with Vertical(id="card"):
                yield Static(f"A key already exists for OpenRouter ({existing_ref}).")
                yield Static("What would you like to do?")
                yield OptionList(
                    Option("  Keep existing key", id="opt:keep"),
                    Option("  Replace with a new browser login", id="opt:replace"),
                    id="step-list",
                )

        async def on_mount(self) -> None:
            self.query_one(OptionList).focus()

        async def on_option_list_option_selected(
            self, event: OptionList.OptionSelected
        ) -> None:
            key = (event.option.id or "").removeprefix("opt:")
            self.choice = key
            self.exit()

    app = _ReplaceKeepApp()
    app.run()
    result = app.choice
    return "replace" if result == "replace" else "keep"


# ---------------------------------------------------------------------------
# Main login function
# ---------------------------------------------------------------------------

def login_openrouter(
    open_browser: Callable[..., object] | None = None,
    *,
    _timeout: float = 300.0,
    _prompt_replace_or_keep: Callable[[str], Literal["replace", "keep"]] | None = None,
    _wait_for_callback: Callable[..., str] | None = None,
) -> LoginResult:
    """Run the OpenRouter OAuth PKCE login flow.

    Parameters
    ----------
    open_browser:
        Callable that opens a URL in a browser.  Defaults to
        ``webbrowser.open``.  Injected in tests to avoid real browser launches.
    _timeout:
        Seconds to wait for the OAuth callback (default 300 s).  Injected
        in tests to keep them fast.
    _prompt_replace_or_keep:
        Callable that asks the user what to do when a key already exists.
        Returns ``"replace"`` or ``"keep"``.  Injected in tests.
    _wait_for_callback:
        Callable ``(server, *, timeout)`` → code.  Injected in tests.
    """
    import webbrowser as _wb

    _open: Callable[..., object] = open_browser if open_browser is not None else _wb.open
    _prompt_fn: Callable[[str], Literal["replace", "keep"]] = (
        _prompt_replace_or_keep
        if _prompt_replace_or_keep is not None
        else _default_prompt_replace_or_keep
    )
    _wait_fn: Callable[..., str] = (
        _wait_for_callback
        if _wait_for_callback is not None
        else _module_wait_for_callback
    )

    # -- check for an existing key from ANY source --------------------------
    # Honour the actual configured reference (${ENV} / file: / keychain:), not
    # just the keychain default — otherwise a working ${ENV} config would get no
    # replace/keep prompt and be silently clobbered.
    existing_ref = _configured_openrouter_ref() or _OPENROUTER_REF
    existing_value = _resolve_existing(existing_ref)
    if existing_value is not None:
        decision = _prompt_fn(existing_ref)
        if decision == "keep":
            return LoginResult(
                reference=existing_ref,
                masked_key=_mask_key(existing_value),
                backend=_backend_for_ref(existing_ref),
                changed=False,
            )
        # "replace" — fall through to the full OAuth flow below.

    # -- PKCE flow -----------------------------------------------------------
    verifier = pkce_verifier()
    challenge = pkce_challenge(verifier)

    server, port = _make_callback_server()
    cb_url = f"http://127.0.0.1:{port}/callback"

    authorize_url = (
        f"{_AUTHORIZE_URL}"
        f"?callback_url={urllib.parse.quote(cb_url, safe='')}"
        f"&code_challenge={urllib.parse.quote(challenge, safe='')}"
        f"&code_challenge_method=S256"
    )

    _open(authorize_url)

    # Always release the loopback socket — including on the 300 s timeout path.
    try:
        code = _wait_fn(server, timeout=_timeout)
        stored = _exchange_and_store(code, verifier)
    finally:
        server.server_close()

    raw_value = _resolve_existing(stored.reference) or ""
    masked = _mask_key(raw_value) if raw_value else f"{stored.reference[-4:]}"

    return LoginResult(
        reference=stored.reference,
        masked_key=masked,
        backend=stored.backend,
        changed=True,
    )


# ---------------------------------------------------------------------------
# Doctor helper
# ---------------------------------------------------------------------------

def key_source(ref: str | None) -> str:
    """Return a short label describing the source of a key reference.

    ``env`` / ``keychain`` / ``file`` / ``(none)`` — never the raw value.
    """
    if ref is None:
        return "(none)"
    if ref.startswith("${"):
        return "env"
    if ref.startswith("keychain:"):
        return "keychain"
    if ref.startswith("file:"):
        return "file"
    return "inline"
