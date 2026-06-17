"""`/key` — fix the current provider's API key in-session.

The secret must go to the OS keychain (never inlined into committed config); the
provider's config is pointed at a ``keychain:jarn/<provider>`` reference and the
runtime is dropped so the next turn rebuilds with the new key.
"""

from __future__ import annotations

import pytest

from jarn.config import paths
from jarn.config.schema import Config, ProviderConfig, ProviderType, RoutingConfig
from jarn.extensibility.commands import BUILTIN_COMMANDS, format_help
from jarn.repl import InlineApp
from jarn.tui.controller import Controller


def _config() -> Config:
    return Config(
        default_profile="openrouter",
        providers={
            "openrouter": ProviderConfig(
                type=ProviderType.OPENROUTER, api_key="${OPENROUTER_API_KEY}"
            ),
        },
        routing=RoutingConfig(main="openrouter/anthropic/claude-opus-4-8"),
    )


def _controller(tmp_path, monkeypatch) -> Controller:
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    return Controller(_config(), root)


# -- controller core --------------------------------------------------------


def test_current_provider_from_main_model_ref(tmp_path, monkeypatch):
    ctrl = _controller(tmp_path, monkeypatch)
    assert ctrl.current_provider() == "openrouter"
    ctrl.close()


def test_set_provider_key_stores_in_keychain_not_config(tmp_path, monkeypatch):
    ctrl = _controller(tmp_path, monkeypatch)
    stored: dict[str, tuple[str, str]] = {}
    monkeypatch.setattr(
        "jarn.config.secrets.store_keychain",
        lambda service, account, value: stored.__setitem__(account, (service, value)),
    )
    # Pretend we already have a built runtime so we can prove it gets dropped.
    ctrl.runtime = object()  # type: ignore[assignment]

    result = ctrl.set_provider_key("sk-new-secret")

    assert result.rebuilt is True
    assert ctrl.runtime is None, "runtime must be dropped so the next turn rebuilds"
    # Secret went to the keychain; config holds only a reference (no raw secret).
    assert stored == {"openrouter": ("jarn", "sk-new-secret")}
    assert ctrl.config.providers["openrouter"].api_key == "keychain:jarn/openrouter"
    assert "sk-new-secret" not in (ctrl.config.providers["openrouter"].api_key or "")
    ctrl.close()


def test_set_provider_key_persists_reference_to_global_config(tmp_path, monkeypatch):
    ctrl = _controller(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "jarn.config.secrets.store_keychain", lambda service, account, value: None
    )
    ctrl.set_provider_key("sk-new-secret")

    text = paths.global_config_path().read_text(encoding="utf-8")
    assert "keychain:jarn/openrouter" in text
    assert "sk-new-secret" not in text, "the raw secret must never hit committed config"
    ctrl.close()


def test_set_provider_key_unknown_provider_is_rejected(tmp_path, monkeypatch):
    ctrl = _controller(tmp_path, monkeypatch)
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "jarn.config.secrets.store_keychain",
        lambda service, account, value: calls.append((service, account, value)),
    )
    result = ctrl.set_provider_key("sk-x", provider="nope")
    assert not result.rebuilt
    assert "isn't configured" in result.text
    assert calls == [], "no secret should be stored for an unknown provider"
    ctrl.close()


def test_set_provider_key_empty_is_noop(tmp_path, monkeypatch):
    ctrl = _controller(tmp_path, monkeypatch)
    calls: list[object] = []
    monkeypatch.setattr(
        "jarn.config.secrets.store_keychain",
        lambda *a: calls.append(a),
    )
    result = ctrl.set_provider_key("   ")
    assert not result.rebuilt
    assert calls == []
    assert ctrl.config.providers["openrouter"].api_key == "${OPENROUTER_API_KEY}"
    ctrl.close()


# -- /help registration -----------------------------------------------------


def test_key_command_registered_and_in_help():
    assert "key" in BUILTIN_COMMANDS
    rendered = format_help(None)
    assert "/key" in rendered


# -- repl command path (mock the rebuild) -----------------------------------


class _FakeRuntime:
    """Stand-in for a built runtime: just enough surface for ``_command`` to route
    past the custom-command check. Its presence lets us prove ``/key`` drops it."""

    commands: dict[str, object] = {}


def _app(tmp_path, monkeypatch) -> InlineApp:
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    app = InlineApp(_config(), root)

    # Keep the command path hermetic: don't build a real runtime (network/keychain),
    # and simulate an already-built runtime so we can assert it gets dropped.
    async def _noop_extensions() -> None:
        return None

    monkeypatch.setattr(app, "_ensure_extensions", _noop_extensions)
    app.controller.runtime = _FakeRuntime()  # type: ignore[assignment]
    return app


@pytest.mark.asyncio
async def test_key_command_prompts_then_rebuilds(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "jarn.config.secrets.store_keychain", lambda service, account, value: None
    )
    # Prompt returns the pasted key.
    asked: list[str] = []

    async def _fake_ask(prompt: str) -> str:
        asked.append(prompt)
        return "sk-from-prompt"

    monkeypatch.setattr(app, "_ask", _fake_ask)

    await app._command("key", "")

    assert asked, "no-arg /key must prompt for the key"
    assert app.controller.runtime is None, "runtime must be dropped so the next turn rebuilds"
    assert app.controller.config.providers["openrouter"].api_key == "keychain:jarn/openrouter"
    app.controller.close()


@pytest.mark.asyncio
async def test_key_command_inline_arg_does_not_prompt(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "jarn.config.secrets.store_keychain", lambda service, account, value: None
    )

    async def _fail_ask(prompt: str) -> str:  # pragma: no cover - must not be called
        raise AssertionError("inline /key must not prompt")

    monkeypatch.setattr(app, "_ask", _fail_ask)

    await app._command("key", "sk-inline")

    assert app.controller.runtime is None
    assert app.controller.config.providers["openrouter"].api_key == "keychain:jarn/openrouter"
    app.controller.close()
