"""Tests for OpenRouter OAuth PKCE login flow.

Named tests (verbatim from T-4-1 brief):
  test_pkce_rfc_vector
  test_loopback_receives_code
  test_exchange_and_keychain_store
  test_login_with_existing_key_prompts

All network is mocked — no live HTTP in CI.
"""

from __future__ import annotations

import threading
import time
import urllib.request

import httpx
import pytest

# ---------------------------------------------------------------------------
# test_pkce_rfc_vector
# ---------------------------------------------------------------------------

def test_pkce_rfc_vector():
    """pkce_challenge must match the RFC 7636 Appendix B.2 S256 test vector.

    verifier  = dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk
    challenge = E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM
    """
    from jarn.onboarding.oauth import pkce_challenge

    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    expected = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert pkce_challenge(verifier) == expected


# ---------------------------------------------------------------------------
# test_loopback_receives_code
# ---------------------------------------------------------------------------

def test_loopback_receives_code():
    """Loopback server handles GET /callback?code=X and returns the code.

    Uses a short injected timeout (5 s) — if the server doesn't receive the
    callback it raises TimeoutError; the test drives the callback from a
    background thread so it always arrives.  The server also returns a
    'you can close this tab' page.
    """
    from jarn.onboarding.oauth import _make_callback_server, _wait_for_callback

    server, port = _make_callback_server()

    response_body: list[bytes] = []

    def _hit() -> None:
        time.sleep(0.05)  # tiny delay so server is already waiting
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/callback?code=loopback-test-code"
            ) as resp:
                response_body.append(resp.read())
        except Exception:  # noqa: BLE001
            pass

    thread = threading.Thread(target=_hit, daemon=True)
    thread.start()

    code = _wait_for_callback(server, timeout=5.0)

    thread.join(timeout=3.0)

    assert code == "loopback-test-code"
    # The response page must include a "close" cue.
    assert response_body, "server did not respond"
    page = response_body[0].decode("utf-8", errors="replace")
    assert "close" in page.lower() or "tab" in page.lower()


def test_loopback_timeout_raises():
    """_wait_for_callback raises TimeoutError when no callback arrives in time."""
    from jarn.onboarding.oauth import _make_callback_server, _wait_for_callback

    server, _port = _make_callback_server()
    with pytest.raises(TimeoutError):
        _wait_for_callback(server, timeout=0.3)


# ---------------------------------------------------------------------------
# test_exchange_and_keychain_store
# ---------------------------------------------------------------------------

def test_exchange_and_keychain_store(monkeypatch, tmp_path):
    """Code exchange posts the verifier and stores the key via the keychain path.

    The raw key must never appear in the returned reference; the reference must
    be 'keychain:jarn/openrouter'.
    """
    from jarn.onboarding.oauth import _exchange_and_store

    raw_key = "sk-or-v1-testkey1234567890abcdef"
    stored_calls: list[tuple[str, str, str]] = []

    # Mock httpx POST to return the key.
    def _mock_post(url: str, **kwargs):  # type: ignore[override]
        assert "openrouter.ai" in url, f"unexpected URL: {url}"
        body = kwargs.get("json") or {}
        assert body.get("code_verifier"), "code_verifier must be sent"
        assert body.get("code_challenge_method") == "S256"
        req = httpx.Request("POST", url)
        mock_resp = httpx.Response(200, json={"key": raw_key}, request=req)
        return mock_resp

    monkeypatch.setattr("httpx.post", _mock_post)

    # Mock keychain so we don't touch the OS keychain.
    def _mock_keyring(op, service, account, value=None, *, timeout):
        if op == "set":
            stored_calls.append((service, account, value or ""))
            return True
        return raw_key

    monkeypatch.setattr("jarn.config.secrets._keyring_call", _mock_keyring)

    stored = _exchange_and_store(code="testcode", verifier="testverifier")

    assert stored.reference == "keychain:jarn/openrouter"
    assert raw_key not in stored.reference, "raw key must never appear in the reference"
    assert stored_calls, "keychain must have been called"
    # Key was stored under service=jarn, account=openrouter.
    assert any(s == "jarn" and a == "openrouter" for s, a, _ in stored_calls)


# ---------------------------------------------------------------------------
# test_login_with_existing_key_prompts
# ---------------------------------------------------------------------------

def test_login_with_existing_key_prompts(monkeypatch):
    """When a key already resolves, login_openrouter asks replace/keep.

    With 'keep', the existing reference is returned and the browser is never
    opened.  With 'replace', the OAuth flow runs (here mocked to return a
    fresh key immediately).
    """
    from jarn.onboarding.oauth import login_openrouter

    raw_key = "sk-or-v1-existingkey1234567890abcd"

    # Simulate existing key in keychain.
    def _mock_resolve(ref):
        if ref == "keychain:jarn/openrouter":
            return raw_key
        return None

    monkeypatch.setattr("jarn.onboarding.oauth._resolve_existing", _mock_resolve)

    browser_calls: list[str] = []

    # --- keep branch ---
    result_keep = login_openrouter(
        open_browser=browser_calls.append,
        _prompt_replace_or_keep=lambda _ref: "keep",
    )
    assert result_keep.reference == "keychain:jarn/openrouter"
    assert len(browser_calls) == 0, "browser must not open when user keeps existing key"
    assert raw_key not in result_keep.masked_key, "masked_key must not expose the raw key"

    # --- replace branch: inject the whole OAuth sequence ---
    browser_calls.clear()

    new_raw_key = "sk-or-v1-newkey1234567890abcdef"
    stored_calls2: list[tuple] = []

    def _mock_post(url: str, **kwargs):
        req = httpx.Request("POST", url)
        mock_resp = httpx.Response(200, json={"key": new_raw_key}, request=req)
        return mock_resp

    def _mock_keyring(op, service, account, value=None, *, timeout):
        if op == "set":
            stored_calls2.append((service, account, value or ""))
            return True
        return new_raw_key

    def _mock_wait(server, *, timeout: float = 300.0) -> str:
        return "replacement-code"

    monkeypatch.setattr("httpx.post", _mock_post)
    monkeypatch.setattr("jarn.config.secrets._keyring_call", _mock_keyring)
    monkeypatch.setattr("jarn.onboarding.oauth._wait_for_callback", _mock_wait)

    result_replace = login_openrouter(
        open_browser=browser_calls.append,
        _prompt_replace_or_keep=lambda _ref: "replace",
    )
    assert len(browser_calls) == 1, "browser must open for replace"
    assert result_replace.reference == "keychain:jarn/openrouter"
    assert new_raw_key not in result_replace.reference


# ---------------------------------------------------------------------------
# Wizard integration tests
# ---------------------------------------------------------------------------

def test_wizard_plain_openrouter_shows_oauth_option(monkeypatch):
    """Plain wizard _configure_key for openrouter offers 'oauth' as a choice.

    We monkeypatch Prompt.ask to capture the presented choices and return 'oauth',
    then verify login_openrouter is invoked (monkeypatched to a no-op).
    """
    from jarn.onboarding import wizard

    login_calls: list[int] = []

    def _fake_login(**_kwargs):
        login_calls.append(1)
        from jarn.onboarding.oauth import LoginResult
        return LoginResult(
            reference="keychain:jarn/openrouter",
            masked_key="sk-…test",
            backend="keychain",
        )

    monkeypatch.setattr("jarn.onboarding.wizard.login_openrouter", _fake_login)

    prompt_choices: list[list[str]] = []

    def _fake_prompt(msg, *, choices, default=""):
        prompt_choices.append(list(choices))
        return "oauth"

    monkeypatch.setattr("jarn.onboarding.wizard.Prompt.ask", _fake_prompt)

    ref = wizard._configure_key("openrouter")

    assert login_calls, "login_openrouter must be called when oauth is selected"
    assert ref == "keychain:jarn/openrouter"
    # The 'oauth' choice must appear BEFORE 'keychain' in the choices list.
    choices = prompt_choices[0] if prompt_choices else []
    assert "oauth" in choices
    assert choices.index("oauth") < choices.index("keychain"), (
        "'oauth' must come before 'keychain' in the choices"
    )


def test_wizard_tui_openrouter_storage_shows_login_first(monkeypatch):
    """TUI wizard _STORAGE list for openrouter has 'oauth' as the first entry."""
    from jarn.onboarding import tui_wizard

    # The constant _OPENROUTER_STORAGE must have 'oauth' as first key.
    assert hasattr(tui_wizard, "_OPENROUTER_STORAGE"), (
        "tui_wizard must expose _OPENROUTER_STORAGE for openrouter provider"
    )
    first_key = tui_wizard._OPENROUTER_STORAGE[0][0]
    assert first_key == "oauth", (
        f"First storage option for openrouter must be 'oauth', got {first_key!r}"
    )


# ===========================================================================
# Fix round 1 — socket lifecycle, existing-key from any source, config guard
# ===========================================================================


def _mock_ok_post(url: str, **kwargs):
    req = httpx.Request("POST", url)
    return httpx.Response(200, json={"key": "sk-or-v1-fresh1234567890abcdef"}, request=req)


def _mock_keyring_set(op, service, account, value=None, *, timeout):
    return True if op == "set" else None


def test_login_closes_socket_on_success(monkeypatch):
    """login_openrouter must close the loopback socket after a successful flow."""
    from jarn.onboarding import oauth

    captured: dict[str, object] = {}
    real_make = oauth._make_callback_server

    def _capturing_make():
        server, port = real_make()
        captured["server"] = server
        return server, port

    monkeypatch.setattr(oauth, "_make_callback_server", _capturing_make)
    monkeypatch.setattr(oauth, "_resolve_existing", lambda ref: None)
    monkeypatch.setattr("httpx.post", _mock_ok_post)
    monkeypatch.setattr("jarn.config.secrets._keyring_call", _mock_keyring_set)

    def _mock_wait(server, *, timeout: float = 300.0) -> str:
        return "the-code"

    result = oauth.login_openrouter(
        open_browser=lambda url: None,
        _wait_for_callback=_mock_wait,
    )

    assert result.reference == "keychain:jarn/openrouter"
    server = captured["server"]
    # A closed HTTPServer socket has fileno() == -1.
    assert server.socket.fileno() == -1, "loopback socket must be closed after success"


def test_login_closes_socket_on_timeout(monkeypatch):
    """login_openrouter must close the loopback socket even when the wait times out."""
    from jarn.onboarding import oauth

    captured: dict[str, object] = {}
    real_make = oauth._make_callback_server

    def _capturing_make():
        server, port = real_make()
        captured["server"] = server
        return server, port

    monkeypatch.setattr(oauth, "_make_callback_server", _capturing_make)
    monkeypatch.setattr(oauth, "_resolve_existing", lambda ref: None)

    def _mock_wait(server, *, timeout: float = 300.0) -> str:
        raise TimeoutError("no callback arrived")

    with pytest.raises(TimeoutError):
        oauth.login_openrouter(
            open_browser=lambda url: None,
            _wait_for_callback=_mock_wait,
        )

    server = captured["server"]
    assert server.socket.fileno() == -1, "loopback socket must be closed after timeout"


def test_login_env_key_prompts_and_keep_skips_browser(monkeypatch):
    """An existing ${ENV} key (not keychain) must still trigger the replace/keep prompt.

    keep → no browser opens, the ${ENV} reference is returned unchanged, backend
    reflects the real source ('env'), and changed is False (nothing to write).
    """
    from jarn.config import paths
    from jarn.onboarding import oauth

    cfg = paths.global_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        "providers:\n"
        "  openrouter:\n"
        "    type: openrouter\n"
        "    api_key: ${OPENROUTER_API_KEY}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-fromenv1234567890abcd")

    browser_calls: list[str] = []
    prompted_refs: list[str] = []

    def _prompt(ref: str):
        prompted_refs.append(ref)
        return "keep"

    def _fail_fast_wait(server, *, timeout: float = 300.0) -> str:
        # keep must short-circuit before the wait; if it doesn't, fail fast
        # instead of blocking the suite for the full timeout.
        raise AssertionError("keep must not reach the callback wait")

    result = oauth.login_openrouter(
        open_browser=browser_calls.append,
        _prompt_replace_or_keep=_prompt,
        _wait_for_callback=_fail_fast_wait,
    )

    assert prompted_refs == ["${OPENROUTER_API_KEY}"], (
        "prompt must be shown against the actual configured reference"
    )
    assert browser_calls == [], "browser must not open when keeping an env key"
    assert result.reference == "${OPENROUTER_API_KEY}"
    assert result.backend == "env"
    assert result.changed is False


def test_cmd_login_skips_config_write_when_kept(monkeypatch):
    """`jarn login` must not overwrite config when the user keeps an existing key."""
    from jarn import cli
    from jarn.onboarding.oauth import LoginResult

    writes: list[str] = []
    monkeypatch.setattr(
        cli, "_write_openrouter_key_ref", lambda ref: writes.append(ref) or True
    )
    monkeypatch.setattr(
        "jarn.onboarding.oauth.login_openrouter",
        lambda: LoginResult(
            reference="${OPENROUTER_API_KEY}",
            masked_key="sk-…env",
            backend="env",
            changed=False,
        ),
    )

    rc = cli._cmd_login()
    assert rc == 0
    assert writes == [], "config must not be rewritten when the key is kept"


def test_write_openrouter_key_ref_refuses_on_malformed_config(monkeypatch, capsys):
    """A malformed existing config must not be silently wiped — refuse and warn."""
    from jarn import cli
    from jarn.config import paths

    cfg = paths.global_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    malformed = "providers: {openrouter: {api_key: 'unterminated\n"
    cfg.write_text(malformed, encoding="utf-8")

    result = cli._write_openrouter_key_ref("keychain:jarn/openrouter")

    assert result is False, "must refuse to overwrite a config it cannot parse"
    assert cfg.read_text(encoding="utf-8") == malformed, "malformed config must be untouched"
    err = capsys.readouterr().err
    assert "config" in err.lower(), "must warn on stderr about the unparseable config"
