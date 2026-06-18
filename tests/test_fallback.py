"""Turn-level fallback model rotation tests."""

from __future__ import annotations

from jarn.config.schema import Config, ProviderConfig, ProviderType, RoutingConfig
from jarn.tui.controller import Controller


def _ctrl(tmp_path, monkeypatch, fallback):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="openrouter",
        providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
        routing=RoutingConfig(main="openrouter/main", fallback=fallback),
    )
    return Controller(cfg, root)


def test_rotate_advances_through_chain(tmp_path, monkeypatch):
    ctrl = _ctrl(tmp_path, monkeypatch, ["openrouter/f1", "openrouter/f2"])
    assert ctrl.config.resolved_main_model() == "openrouter/main"
    assert ctrl.rotate_to_fallback() == "openrouter/f1"
    assert ctrl.config.routing.main == "openrouter/f1"
    assert ctrl.rotate_to_fallback() == "openrouter/f2"
    assert ctrl.rotate_to_fallback() is None  # exhausted
    ctrl.close()


def test_reset_returns_to_primary(tmp_path, monkeypatch):
    ctrl = _ctrl(tmp_path, monkeypatch, ["openrouter/f1"])
    ctrl.rotate_to_fallback()
    assert ctrl.config.routing.main == "openrouter/f1"
    ctrl.reset_model_rotation()
    assert ctrl.config.routing.main == "openrouter/main"
    ctrl.close()


def test_no_fallback_chain(tmp_path, monkeypatch):
    ctrl = _ctrl(tmp_path, monkeypatch, [])
    assert ctrl.rotate_to_fallback() is None
    ctrl.close()


def _ctrl_multi(tmp_path, monkeypatch, providers, *, main, fallback):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="primary",
        providers=providers,
        routing=RoutingConfig(main=main, fallback=fallback),
    )
    return Controller(cfg, root)


def test_keyed_fallback_rotates_to_other_provider_with_key(tmp_path, monkeypatch):
    """On auth failure, rotate to a *different* provider that has a usable key."""
    providers = {
        "primary": ProviderConfig(type=ProviderType.OPENROUTER, api_key="bad-but-present"),
        "backup": ProviderConfig(type=ProviderType.ANTHROPIC, api_key="good"),
    }
    ctrl = _ctrl_multi(
        tmp_path, monkeypatch, providers,
        main="primary/m", fallback=["backup/m"],
    )
    assert ctrl.rotate_to_keyed_fallback() == "backup/m"
    assert ctrl.config.routing.main == "backup/m"
    # A later success returns to the primary (shared rotation state).
    ctrl.reset_model_rotation()
    assert ctrl.config.routing.main == "primary/m"
    ctrl.close()


def test_keyed_fallback_skips_same_provider(tmp_path, monkeypatch):
    """A fallback on the SAME provider would reuse the rejected key → skip it."""
    providers = {
        "primary": ProviderConfig(type=ProviderType.OPENROUTER, api_key="bad"),
        "backup": ProviderConfig(type=ProviderType.ANTHROPIC, api_key="good"),
    }
    ctrl = _ctrl_multi(
        tmp_path, monkeypatch, providers,
        main="primary/m", fallback=["primary/other", "backup/m"],
    )
    assert ctrl.rotate_to_keyed_fallback() == "backup/m"
    ctrl.close()


def test_keyed_fallback_skips_unresolvable_key(tmp_path, monkeypatch):
    """A fallback whose key reference can't resolve isn't viable → None."""
    monkeypatch.delenv("MISSING_KEY", raising=False)
    providers = {
        "primary": ProviderConfig(type=ProviderType.OPENROUTER, api_key="bad"),
        "backup": ProviderConfig(type=ProviderType.ANTHROPIC, api_key="${MISSING_KEY}"),
    }
    ctrl = _ctrl_multi(
        tmp_path, monkeypatch, providers,
        main="primary/m", fallback=["backup/m"],
    )
    assert ctrl.rotate_to_keyed_fallback() is None
    assert ctrl.config.routing.main == "primary/m"  # unchanged
    ctrl.close()


def test_keyed_fallback_none_when_no_fallback(tmp_path, monkeypatch):
    providers = {"primary": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")}
    ctrl = _ctrl_multi(tmp_path, monkeypatch, providers, main="primary/m", fallback=[])
    assert ctrl.rotate_to_keyed_fallback() is None
    ctrl.close()


def test_keyed_fallback_local_provider_needs_no_key(tmp_path, monkeypatch):
    """A local (Ollama/LM Studio) fallback is viable even with no key set."""
    providers = {
        "primary": ProviderConfig(type=ProviderType.OPENROUTER, api_key="bad"),
        "local": ProviderConfig(type=ProviderType.OLLAMA, base_url="http://x"),
    }
    ctrl = _ctrl_multi(
        tmp_path, monkeypatch, providers,
        main="primary/m", fallback=["local/llama"],
    )
    assert ctrl.rotate_to_keyed_fallback() == "local/llama"
    ctrl.close()
