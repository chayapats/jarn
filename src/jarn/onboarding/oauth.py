"""OpenRouter OAuth PKCE login flow.

Public API
----------
pkce_verifier(length=64) -> str
    Generate a PKCE code verifier (43–128 chars, RFC 7636 unreserved set).

pkce_challenge(verifier) -> str
    Compute PKCE S256 challenge = BASE64URL(SHA256(verifier)) (no padding).

_make_callback_server(nonce=None) -> (HTTPServer, port)
    Create a loopback HTTP server on a random free port.  When *nonce* is given
    the server also answers ``/callback/<nonce>`` and marks codes arriving there
    as VERIFIED (see the loopback-race note below).

_wait_for_callback(server, *, timeout=300.0) -> str
    Block until a ``GET /callback…?code=X`` arrives and POP the next code,
    verified ones first.  Raises TimeoutError when timeout expires with no
    code left to hand out.

_exchange_and_store(code, verifier) -> StoredSecret
    POST the code + verifier to OpenRouter, receive the API key, and store
    it via the existing secret-storage path.  Never returns the raw key.

login_openrouter(open_browser, *, _timeout, _prompt_replace_or_keep) -> LoginResult
    Full OAuth PKCE flow.  ``open_browser`` is injectable so tests do not
    launch a real browser.  ``_prompt_replace_or_keep`` is injectable so
    tests can drive the replace/keep choice without a TTY.

Loopback-race note (GHSA-82cv-4xgg-jfqr)
----------------------------------------
The callback server binds an ephemeral port, and any other local process can
scan for it and deliver a bogus ``?code=`` first.  PKCE already makes that
harmless for CONFIDENTIALITY — an injected code is bound to the attacker's own
``code_challenge`` and cannot be redeemed with our verifier — but the original
"first request wins" handling turned it into a denial of service: the bogus code
was returned, redemption failed, and the real browser response arrived at a
closed socket.

Two changes close it, and neither can break the flow if the provider behaves
differently than expected:

1. Codes are QUEUED and tried in turn until one redeems.  An injected code fails
   the exchange and we simply move on to the next.  This needs nothing from the
   provider.
2. The ``callback_url`` carries an unguessable path segment, so a code arriving
   at ``/callback/<nonce>`` is known to correspond to the URL we handed out and
   is tried FIRST.  That keeps a flood of bogus codes from spending the window on
   exchange round-trips.

The nonce lives in the PATH rather than a ``state`` query parameter on purpose:
OpenRouter's PKCE flow (`docs/use-cases/oauth-pkce`) documents only
``callback_url``, ``code_challenge`` and ``code_challenge_method``, and echoes
back only ``code`` — it has no ``state`` support, so REQUIRING a ``state`` round
trip as RFC 6749 §10.12 describes would reject every legitimate callback and
break ``jarn login`` outright.  A path segment is part of the URL we supply, so
it survives an ordinary redirect.  A bare ``/callback`` is still accepted, and
its codes are tried after the verified ones, so the flow keeps working even if a
provider normalises the path away.

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
import select
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

#: How many callback codes to try redeeming before giving up. Bounded so a local
#: process flooding bogus codes cannot spend the whole login window on 30 s
#: exchange round-trips. A genuine flow uses exactly one.
_MAX_EXCHANGE_ATTEMPTS = 5

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
    """Captures ``?code=`` values onto the server's queue.

    Not one-shot: every accepted code is queued, because the first one to arrive
    is not necessarily the one from our browser (see the loopback-race note in
    the module docstring). A code on ``/callback/<nonce>`` came back from the URL
    we handed out and is marked verified; a bare ``/callback`` is accepted too but
    ranked below it.
    """

    #: Bound the read on an accepted connection. ``StreamRequestHandler`` defaults
    #: to ``None`` — no socket timeout — so a peer that connects and sends nothing
    #: blocks the handler forever. That is fatal inside :func:`_drain_pending`,
    #: which would then never return and would discard a genuine code it had
    #: already captured. ``handle_one_request`` catches the timeout, closes the
    #: connection and returns cleanly, so an idle peer costs one timeout and the
    #: loop moves on.
    timeout = 10.0

    def do_GET(self) -> None:  # noqa: N802 - HTTP method naming
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        codes = params.get("code", [])
        nonce: str | None = self.server._nonce  # type: ignore[attr-defined]
        suffix = parsed.path[len("/callback/"):]
        # ``compare_digest`` raises TypeError on a non-ASCII str, and the path is
        # attacker-controlled. The nonce is token_urlsafe, always ASCII, so a
        # non-ASCII suffix can never match — short-circuit instead of crashing.
        verified = bool(
            nonce
            and parsed.path.startswith("/callback/")
            and suffix.isascii()
            and _secrets_mod.compare_digest(suffix, nonce)
        )
        # Once the flow has latched to verified-only, a bare callback is noise: a
        # burst of them has already been tried and rejected.
        bare_ok = parsed.path == "/callback" and not getattr(
            self.server, "_verified_only", False
        )
        accepted = codes and (verified or bare_ok)
        if accepted:
            self.server._codes.append((verified, codes[0]))  # type: ignore[attr-defined]
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


def _make_callback_server(nonce: str | None = None) -> tuple[HTTPServer, int]:
    """Create a loopback HTTP server on a random free port.

    Parameters
    ----------
    nonce:
        Unguessable path segment. When set, ``/callback/<nonce>`` is accepted and
        its codes rank ahead of any arriving at a bare ``/callback``. ``None``
        disables the distinction (every code is unverified).

    Returns
    -------
    (server, port)
        The server instance and the port it is listening on.
    """
    server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    # Attach the queue the handler appends captured codes to, as
    # ``(verified, code)`` pairs, plus the nonce it checks the path against.
    server._codes = []  # type: ignore[attr-defined]
    server._nonce = nonce  # type: ignore[attr-defined]
    # Set by _pop_code to say whether the code it just handed out came back on the
    # nonce path. The redemption loop needs it to tell "our code was rejected"
    # (terminal) from "somebody else's code was rejected" (keep listening).
    server._last_pop_verified = False  # type: ignore[attr-defined]
    #: Latched by :func:`_verified_only` once a burst of bare-path codes has each
    #: been rejected; from then on only nonce-path codes are accepted.
    server._verified_only = False  # type: ignore[attr-defined]
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
    queued = _pop_code(server)
    if queued is not None:  # already arrived while we were exchanging another
        return queued
    server.timeout = min(timeout, 5.0)  # handle_request poll interval
    deadline = _monotonic() + timeout
    while _monotonic() < deadline:
        remaining = deadline - _monotonic()
        if remaining <= 0:
            break
        server.timeout = min(remaining, 5.0)
        server.handle_request()
        _drain_pending(server)
        queued = _pop_code(server)
        if queued is not None:
            return queued
    raise TimeoutError(
        f"No OAuth callback received within {timeout:.0f} s. "
        "If the browser did not open, use `jarn setup` to paste a key manually."
    )


#: Ceiling on the opportunistic drain, so a process flooding the port cannot keep
#: us in the drain loop instead of redeeming the code we already hold.
_MAX_DRAIN = 32


def _drain_pending(server: HTTPServer) -> None:
    """Handle any requests ALREADY waiting, without blocking for new ones.

    ``handle_request`` returns after a single request, so without this the
    verified-first ordering in :func:`_pop_code` would only ever see whichever
    request the OS happened to hand over first. Draining the backlog lets it pick
    the genuine callback out of a burst.
    """
    previous = server.timeout
    server.timeout = 0.0  # non-blocking accept
    try:
        for _ in range(_MAX_DRAIN):
            # Stop on "nothing is waiting", NOT on "that request carried no code":
            # a single health probe or favicon fetch would otherwise truncate the
            # drain and hand the ordering only whatever it had seen so far.
            if not select.select([server.socket], [], [], 0)[0]:
                return
            server.handle_request()
    finally:
        server.timeout = previous


def _pop_code(server: HTTPServer) -> str | None:
    """Remove and return the next queued code — VERIFIED ones first — or ``None``.

    Ordering is what keeps a flood of bogus codes from spending the whole window
    on exchange round-trips: a code that came back on the nonce path is tried
    before any that merely showed up at the bare callback.
    """
    queue: list[tuple[bool, str]] = server._codes  # type: ignore[attr-defined]
    for want_verified in (True, False):
        for i, (verified, code) in enumerate(queue):
            if verified is want_verified:
                del queue[i]
                server._last_pop_verified = verified  # type: ignore[attr-defined]
                return code
    return None


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


def _redeem_first_working_code(
    wait_fn: Callable[..., str],
    server: HTTPServer,
    verifier: str,
    timeout: float,
) -> StoredSecret:
    """Take callback codes in turn until one redeems, or the window closes.

    "First code wins" is what made the loopback race a denial of service: any
    local process that found the ephemeral port could deliver a bogus code, and
    the single attempt spent on it failed while the real callback arrived at a
    socket we had already closed. PKCE guarantees an injected code cannot be
    redeemed with OUR verifier, so a failed exchange is a reason to keep
    listening rather than to give up.

    Attempts are capped so a flood cannot spend the whole window on 30 s exchange
    round-trips; the nonce path ordering in :func:`_pop_code` means a genuine
    code is normally the very first one tried.
    """
    deadline = _monotonic() + timeout
    last_error: Exception | None = None
    unverified_attempts = 0
    while True:
        remaining = deadline - _monotonic()
        if remaining <= 0:
            break
        # A TimeoutError from here is terminal: no code is waiting and the window
        # is spent. Surface the last exchange failure instead when there was one,
        # since "the code we tried was rejected" is the more useful diagnosis.
        try:
            code = wait_fn(server, timeout=remaining)
        except TimeoutError:
            if last_error is not None:
                raise last_error from None
            raise
        verified = bool(getattr(server, "_last_pop_verified", False))
        try:
            return _exchange_and_store(code, verifier)
        except Exception as exc:  # noqa: BLE001 - decide below whether to go on
            last_error = exc
            if verified:
                # The nonce is 32 random bytes, so a code that came back on
                # /callback/<nonce> provably came from the URL we handed out. If
                # the provider rejects THAT, no later code can be ours either —
                # waiting out the rest of the window would only hide the reason.
                raise
            if not _is_code_rejection(exc):
                # A dead network, a provider 5xx or a failed keychain write is not
                # something another code repairs. Burying it behind the remaining
                # window leaves the user staring at nothing for five minutes.
                raise
            unverified_attempts += 1
            if unverified_attempts >= _MAX_EXCHANGE_ATTEMPTS:
                # Only UNVERIFIED codes are capped. An attacker cannot manufacture
                # a verified one, so this bounds a flood without letting the flood
                # end the login: we keep waiting for a nonce-path code below.
                _verified_only(server)
                unverified_attempts = 0
    if last_error is not None:
        raise last_error
    raise TimeoutError(
        f"No OAuth callback received within {timeout:.0f} s. "
        "If the browser did not open, use `jarn setup` to paste a key manually."
    )


def _verified_only(server: HTTPServer) -> None:
    """Drop queued bare-path codes and stop accepting more of them.

    Reached only after a burst of unverified codes has each been rejected — at
    which point the bare path is carrying nothing but noise, and continuing to
    exchange from it would spend the window an attacker is trying to exhaust.
    """
    queue: list[tuple[bool, str]] = server._codes  # type: ignore[attr-defined]
    queue[:] = [entry for entry in queue if entry[0]]
    server._verified_only = True  # type: ignore[attr-defined]


def _is_code_rejection(exc: Exception) -> bool:
    """True when *exc* says THIS CODE was refused, so another one may still work.

    A 4xx from the token endpoint is the provider rejecting the code — expected
    when we redeem one an attacker injected. Anything else (a transport error, a
    5xx, a failure storing the key) is a condition no other code improves, and is
    re-raised immediately instead of being retried into the timeout.
    """
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return isinstance(status, int) and 400 <= status < 500


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

    # Unguessable path segment on the callback URL. See the loopback-race note in
    # the module docstring for why this is a PATH and not a `state` parameter.
    nonce = _secrets_mod.token_urlsafe(32)
    server, port = _make_callback_server(nonce)
    cb_url = f"http://127.0.0.1:{port}/callback/{nonce}"

    authorize_url = (
        f"{_AUTHORIZE_URL}"
        f"?callback_url={urllib.parse.quote(cb_url, safe='')}"
        f"&code_challenge={urllib.parse.quote(challenge, safe='')}"
        f"&code_challenge_method=S256"
    )

    _open(authorize_url)

    # Always release the loopback socket — including on the 300 s timeout path.
    try:
        stored = _redeem_first_working_code(_wait_fn, server, verifier, _timeout)
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
