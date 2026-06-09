"""Tests for the easy-config layer (`/config`): coercion, persistence, apply."""

from __future__ import annotations

import pytest

from jarn.config import settings
from jarn.config.schema import PermissionMode

# -- coercion ---------------------------------------------------------------


def test_coerce_types():
    assert settings.coerce("budget.warn_at_pct", "80") == 80
    assert settings.coerce("budget.per_session_usd", "2.5") == 2.5
    assert settings.coerce("wiki.enabled", "true") is True
    assert settings.coerce("wiki.enabled", "off") is False
    assert settings.coerce("ui.theme", "light") == "light"
    assert settings.coerce("execution.docker_image", "node:22") == "node:22"


def test_coerce_rejects_bad_values():
    with pytest.raises(settings.SettingError):
        settings.coerce("ui.theme", "neon")          # bad enum
    with pytest.raises(settings.SettingError):
        settings.coerce("budget.warn_at_pct", "lots")  # bad int
    with pytest.raises(settings.SettingError):
        settings.coerce("wiki.enabled", "maybe")     # bad bool
    with pytest.raises(settings.SettingError):
        settings.coerce("providers", "x")            # not a settable key


def test_get_value_reads_nested_and_enum(base_config):
    assert settings.get_value(base_config, "permission_mode") == base_config.permission_mode.value
    assert settings.get_value(base_config, "ui.theme") == base_config.ui.theme


# -- ConfigStore round-trip -------------------------------------------------


def test_config_store_sets_nested_and_preserves_comments(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("# my config\npermission_mode: ask  # inline\nui:\n  theme: dark\n", encoding="utf-8")
    store = settings.ConfigStore(p)
    store.set("ui.theme", "light")
    store.set("wiki.enabled", True)          # creates a new top-level section
    text = p.read_text()
    assert "# my config" in text and "# inline" in text   # comments preserved
    from ruamel.yaml import YAML
    data = YAML().load(text)
    assert data["ui"]["theme"] == "light"
    assert data["wiki"]["enabled"] is True
    assert data["permission_mode"] == "ask"


def test_config_store_restore(tmp_path):
    p = tmp_path / "config.yaml"
    store = settings.ConfigStore(p)
    assert store.read_text() is None
    store.set("wiki.enabled", True)
    snap = store.read_text()
    store.set("wiki.enabled", False)
    store.restore(snap)
    from ruamel.yaml import YAML
    assert YAML().load(p.read_text())["wiki"]["enabled"] is True


# -- /config command via the controller -------------------------------------


def _controller(tmp_path, monkeypatch, base_config, *, trusted=True):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("JARN_HOME", str(home))
    (home / "config.yaml").write_text("permission_mode: ask\n", encoding="utf-8")
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    from jarn.tui.controller import Controller

    return Controller(base_config, root, project_trusted=trusted), home / "config.yaml"


def test_config_show_lists_settings(tmp_path, monkeypatch, base_config):
    ctrl, _ = _controller(tmp_path, monkeypatch, base_config)
    out = ctrl.handle_command("config", "")
    assert "ui.theme" in out.text and "permission_mode" in out.text
    ctrl.close()


def test_config_get(tmp_path, monkeypatch, base_config):
    ctrl, _ = _controller(tmp_path, monkeypatch, base_config)
    out = ctrl.handle_command("config", "get ui.theme")
    assert "ui.theme =" in out.text
    bad = ctrl.handle_command("config", "get nope.key")
    assert "Unknown setting" in bad.text
    ctrl.close()


def test_config_set_persists_and_applies(tmp_path, monkeypatch, base_config):
    ctrl, gpath = _controller(tmp_path, monkeypatch, base_config)
    out = ctrl.handle_command("config", "set wiki.enabled true")
    assert out.rebuilt is True
    assert ctrl.config.wiki.enabled is True       # applied live
    assert ctrl.runtime is None                   # rebuild forced
    from ruamel.yaml import YAML
    assert YAML().load(gpath.read_text())["wiki"]["enabled"] is True   # persisted
    ctrl.close()


def test_config_set_bad_value_rejected_no_write(tmp_path, monkeypatch, base_config):
    ctrl, gpath = _controller(tmp_path, monkeypatch, base_config)
    before = gpath.read_text()
    out = ctrl.handle_command("config", "set ui.theme neon")
    assert "must be one of" in out.text and out.rebuilt is False
    assert gpath.read_text() == before            # nothing written
    ctrl.close()


def test_config_set_invalid_rolls_back(tmp_path, monkeypatch, base_config):
    ctrl, gpath = _controller(tmp_path, monkeypatch, base_config)
    before = gpath.read_text()
    # warn_at_pct must be within [0, 100]; 999 passes int-coercion but fails
    # loader validation, so the write must roll back.
    out = ctrl.handle_command("config", "set budget.warn_at_pct 999")
    assert "Rejected" in out.text and out.rebuilt is False
    assert gpath.read_text() == before            # rolled back
    ctrl.close()


def test_config_set_untrusted_clamps_but_persists(tmp_path, monkeypatch, base_config):
    ctrl, gpath = _controller(tmp_path, monkeypatch, base_config, trusted=False)
    out = ctrl.handle_command("config", "set permission_mode yolo")
    assert out.rebuilt is True
    # Persisted intent is yolo, but the untrusted session stays floored to plan.
    from ruamel.yaml import YAML
    assert YAML().load(gpath.read_text())["permission_mode"] == "yolo"
    assert ctrl.config.permission_mode == PermissionMode.PLAN
    ctrl.close()


def test_config_command_registered():
    from jarn.extensibility.commands import builtin_command, route_for

    assert builtin_command("config") is not None
    assert route_for("config") == "controller"
