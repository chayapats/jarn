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
