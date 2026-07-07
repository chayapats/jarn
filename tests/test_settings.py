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


# -- T-1-4: fail-closed on corrupt YAML + backup ---------------------------


def test_missing_file_bootstrap(tmp_path):
    """A missing file is a legitimate bootstrap — set() creates it from {}."""
    p = tmp_path / "config.yaml"
    assert not p.exists()
    settings.ConfigStore(p).set("wiki.enabled", True)
    from ruamel.yaml import YAML
    assert p.is_file()
    assert YAML().load(p.read_text())["wiki"]["enabled"] is True


def test_corrupt_config_not_wiped(tmp_path):
    """A corrupt config.yaml must NOT be overwritten by set(); a .bak is saved."""
    p = tmp_path / "config.yaml"
    corrupt = "permission_mode: ask\nproviders: [oops, ,\n  unbalanced: ["
    p.write_text(corrupt, encoding="utf-8")
    store = settings.ConfigStore(p)
    with pytest.raises(settings.ConfigCorruptError, match="NOT modified"):
        store.set("wiki.enabled", True)
    # File is left untouched (still the corrupt content, not a 1-key dict).
    assert p.read_text() == corrupt
    # A backup was saved for repair.
    assert p.with_name(p.name + ".bak").is_file()


def test_corrupt_non_dict_top_level_rejected(tmp_path):
    """A YAML file whose top-level is a list (not a mapping) is corrupt."""
    p = tmp_path / "config.yaml"
    p.write_text("- a\n- b\n", encoding="utf-8")
    store = settings.ConfigStore(p)
    with pytest.raises(settings.ConfigCorruptError, match="mapping"):
        store.set("wiki.enabled", True)
    assert p.read_text() == "- a\n- b\n"


def test_write_rotates_backup(tmp_path):
    """A successful write backs up the previous file to <path>.bak."""
    p = tmp_path / "config.yaml"
    p.write_text("permission_mode: ask\n", encoding="utf-8")
    settings.ConfigStore(p).set("wiki.enabled", True)
    bak = p.with_name(p.name + ".bak")
    assert bak.is_file()
    assert bak.read_text() == "permission_mode: ask\n"


def test_backup_keeps_last_two(tmp_path):
    """A second write demotes the previous .bak to .bak.1 (last 2 kept)."""
    p = tmp_path / "config.yaml"
    p.write_text("v1: 1\n", encoding="utf-8")
    store = settings.ConfigStore(p)
    store.set("v2", 2)
    store.set("v3", 3)
    bak = p.with_name(p.name + ".bak")
    bak1 = p.with_name(p.name + ".bak.1")
    assert bak.is_file() and bak1.is_file()
    # .bak is the most recent previous content; .bak.1 is the one before.
    assert "v2" in bak.read_text()
    assert "v1" in bak1.read_text()


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


# -- interactive ConfigPanel state model ------------------------------------


def _make_apply(config):
    """A fake apply() that coerces + writes the value onto ``config`` so the
    panel's value_of() reflects it (mirrors the real controller.set_setting)."""
    calls: list[tuple[str, str]] = []

    def apply(key: str, raw: str) -> tuple[bool, str]:
        calls.append((key, raw))
        val = settings.coerce(key, raw)
        obj = config
        parts = key.split(".")
        for p in parts[:-1]:
            obj = getattr(obj, p)
        if key == "permission_mode":
            setattr(obj, parts[-1], PermissionMode(val))
        else:
            setattr(obj, parts[-1], val)
        return True, f"saved {key}"

    return apply, calls


def _panel(config, apply):
    return settings.ConfigPanel(get_config=lambda: config, apply=apply)


def test_panel_item_move_wraps_within_category(base_config):
    p = _panel(base_config, lambda k, r: (True, "ok"))
    p.cat_index = 0           # "general" has a single setting (permission_mode)
    p.move(-1)                # wraps within the category's items
    assert 0 <= p.item_index < len(p.items())


def test_panel_category_move_wraps(base_config):
    p = _panel(base_config, lambda k, r: (True, "ok"))
    n = len(p.groups)
    p.move_category(-1)
    assert p.cat_index == n - 1
    p.move_category(1)
    assert p.cat_index == 0
    # switching category resets the item selection
    p.item_index = 0
    p.move_category(1)
    assert p.item_index == 0


def test_panel_select_key(base_config):
    p = _panel(base_config, lambda k, r: (True, "ok"))
    p.select_key("ui.theme")
    assert p.category == "Appearance" and p.current().key == "ui.theme"


def test_panel_toggle_bool(base_config):
    apply, calls = _make_apply(base_config)
    base_config.wiki.enabled = False
    p = _panel(base_config, apply)
    p.select_key("wiki.enabled")
    p.activate()
    assert calls[-1] == ("wiki.enabled", "true") and base_config.wiki.enabled is True
    p.activate()
    assert calls[-1] == ("wiki.enabled", "false") and base_config.wiki.enabled is False


def test_panel_cycle_enum(base_config):
    apply, calls = _make_apply(base_config)
    base_config.ui.theme = "dark"
    p = _panel(base_config, apply)
    p.select_key("ui.theme")
    p.activate()                       # dark -> light (next choice)
    assert calls[-1] == ("ui.theme", "light") and base_config.ui.theme == "light"


def test_panel_edit_str_commits(base_config):
    apply, calls = _make_apply(base_config)
    p = _panel(base_config, apply)
    p.select_key("execution.docker_image")
    p.activate()
    assert p.editing
    p.buffer = ""
    p.type_text("node:22")
    p.backspace()                      # node:2
    p.type_text("0")                   # node:20
    p.commit_edit()
    assert not p.editing
    assert calls[-1] == ("execution.docker_image", "node:20")
    assert base_config.execution.docker_image == "node:20"


def test_panel_cancel_edit_applies_nothing(base_config):
    apply, calls = _make_apply(base_config)
    p = _panel(base_config, apply)
    p.select_key("ui.accent")
    p.activate()
    p.type_text("zzz")
    p.cancel_edit()
    assert not p.editing and calls == []


def test_panel_render_has_tabs_label_and_detail(base_config):
    p = _panel(base_config, lambda k, r: (True, "ok"))
    p.select_key("ui.theme")
    frags = p.render_lines()
    assert any(style == "reverse bold" for style, _ in frags)  # active category tab
    assert any(style == "reverse" for style, _ in frags)       # selected setting row
    text = "".join(t for _, t in frags)
    # friendly category tabs + human label + description + enum cycle hint
    assert "Models" in text and "Sandbox" in text and "Appearance" in text
    assert "Theme" in text and "Color theme" in text and "cycle" in text


def test_panel_friendly_label_and_desc(base_config):
    p = _panel(base_config, lambda k, r: (True, "ok"))
    p.select_key("execution.local_sandbox")
    text = "".join(t for _, t in p.render_lines())
    assert "OS sandbox" in text          # human label, not the dotted key
    assert "Kernel-level isolation" in text   # description of the selected item


# ── T-2-1: ui.notify + ui.notify_min_secs config keys ─────────────────────


def test_ui_notify_defaults():
    """UIConfig defaults: notify=bell, notify_min_secs=10."""
    from jarn.config.schema import UIConfig

    ui = UIConfig()
    assert ui.notify == "bell"
    assert ui.notify_min_secs == 10


def test_ui_notify_valid_values():
    """ui.notify accepts off | bell | desktop | both."""
    from jarn.config.schema import UIConfig

    for v in ("off", "bell", "desktop", "both"):
        ui = UIConfig(notify=v)
        assert ui.notify == v


def test_ui_notify_pydantic_validation():
    """Pydantic model rejects bad ui.notify values."""
    import pytest
    from pydantic import ValidationError

    from jarn.config.pydantic_schema import UIConfigModel

    # valid
    m = UIConfigModel(notify="both")
    assert m.notify == "both"

    # invalid
    with pytest.raises(ValidationError):
        UIConfigModel(notify="ding")


def test_ui_notify_min_secs_pydantic():
    """ui.notify_min_secs coerces from string and rejects negatives."""
    import pytest
    from pydantic import ValidationError

    from jarn.config.pydantic_schema import UIConfigModel

    m = UIConfigModel(notify_min_secs="5")
    assert m.notify_min_secs == 5

    with pytest.raises(ValidationError):
        UIConfigModel(notify_min_secs=-1)


def test_ui_notify_in_settings_panel(base_config):
    """ui.notify and ui.notify_min_secs are surfaced in the /config panel."""
    from jarn.config import settings

    assert settings.is_settable("ui.notify")
    assert settings.is_settable("ui.notify_min_secs")
