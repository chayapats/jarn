"""Config loading, merging, and secret resolution tests."""

from __future__ import annotations

import pytest
import yaml

from jarn.config.loader import ConfigError, load_config
from jarn.config.schema import PermissionMode
from jarn.config.secrets import SecretResolutionError, is_reference, resolve


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_empty_config_has_defaults(tmp_path):
    cfg = load_config(
        global_path=tmp_path / "missing-global.yaml",
        project_path=tmp_path / "missing-project.yaml",
    )
    assert cfg.default_profile == "openrouter"
    assert cfg.permission_mode is PermissionMode.ASK


def test_ui_theme_and_accent_parsed(tmp_path):
    gp = tmp_path / "g.yaml"
    _write(gp, {"ui": {"theme": "light", "accent": "magenta"}})
    cfg = load_config(global_path=gp, project_path=None)
    assert cfg.ui.theme == "light"
    assert cfg.ui.accent == "magenta"


def test_ui_defaults(tmp_path):
    cfg = load_config(
        global_path=tmp_path / "missing-global.yaml",
        project_path=tmp_path / "missing-project.yaml",
    )
    assert cfg.ui.theme == "dark"
    assert cfg.ui.accent == "cyan"
    assert cfg.ui.approval_diff_lines == 40  # default cap


def test_ui_approval_diff_lines_parsed(tmp_path):
    gp = tmp_path / "g.yaml"
    _write(gp, {"ui": {"approval_diff_lines": 12}})
    cfg = load_config(global_path=gp, project_path=None)
    assert cfg.ui.approval_diff_lines == 12


def test_global_loaded(tmp_path):
    gp = tmp_path / "g.yaml"
    _write(gp, {"default_profile": "anthropic", "permission_mode": "yolo"})
    cfg = load_config(global_path=gp, project_path=None)
    assert cfg.default_profile == "anthropic"
    assert cfg.permission_mode is PermissionMode.YOLO


def test_project_overrides_global(tmp_path):
    gp = tmp_path / "g.yaml"
    pp = tmp_path / "p.yaml"
    _write(gp, {"default_model": "openrouter/a", "permission_mode": "ask"})
    _write(pp, {"default_model": "openrouter/b"})
    cfg = load_config(global_path=gp, project_path=pp)
    assert cfg.default_model == "openrouter/b"
    assert cfg.permission_mode is PermissionMode.ASK  # unchanged from global


def test_permission_rules_concatenate(tmp_path):
    gp = tmp_path / "g.yaml"
    pp = tmp_path / "p.yaml"
    _write(gp, {"permissions": {"allow": ["git status"]}})
    _write(pp, {"permissions": {"allow": ["npm test"], "deny": ["rm *"]}})
    cfg = load_config(global_path=gp, project_path=pp)
    assert cfg.permissions.allow == ["git status", "npm test"]
    assert cfg.permissions.deny == ["rm *"]


def test_hooks_and_mcp_extend(tmp_path):
    gp = tmp_path / "g.yaml"
    pp = tmp_path / "p.yaml"
    _write(gp, {"hooks": [{"event": "post_edit", "command": "ruff check"}]})
    _write(pp, {"hooks": [{"event": "pre_commit", "command": "pytest", "blocking": True}]})
    cfg = load_config(global_path=gp, project_path=pp)
    assert len(cfg.hooks) == 2
    assert cfg.hooks[1].blocking is True


def test_invalid_mode_raises(tmp_path):
    gp = tmp_path / "g.yaml"
    _write(gp, {"permission_mode": "nope"})
    with pytest.raises(ConfigError):
        load_config(global_path=gp, project_path=None)


def test_invalid_provider_type_raises(tmp_path):
    gp = tmp_path / "g.yaml"
    _write(gp, {"providers": {"x": {"type": "bogus"}}})
    with pytest.raises(ConfigError):
        load_config(global_path=gp, project_path=None)


def test_malformed_yaml_raises(tmp_path):
    gp = tmp_path / "g.yaml"
    gp.write_text("default_profile: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(global_path=gp, project_path=None)


# -- secrets ---------------------------------------------------------------

def test_resolve_env(monkeypatch):
    monkeypatch.setenv("MY_KEY", "secret123")
    assert resolve("${MY_KEY}") == "secret123"


def test_resolve_missing_env_raises():
    with pytest.raises(SecretResolutionError):
        resolve("${DEFINITELY_NOT_SET_VAR_XYZ}")


def test_resolve_literal_passthrough():
    assert resolve("plain-value") == "plain-value"
    assert resolve(None) is None


def test_is_reference():
    assert is_reference("${X}")
    assert is_reference("file:jarn/openrouter")
    assert is_reference("keychain:jarn/openrouter")
    assert not is_reference("literal")
    assert not is_reference(None)


# -- strict validation -----------------------------------------------------

@pytest.mark.parametrize("value", ["false", "no", "off", "0", 0])
def test_bool_coercion_false(tmp_path, value):
    gp = tmp_path / "g.yaml"
    _write(gp, {"observability": {"langsmith": value}})
    cfg = load_config(global_path=gp, project_path=None)
    assert cfg.observability.langsmith is False


@pytest.mark.parametrize("value", ["true", "yes", "on", "1", 1, True])
def test_bool_coercion_true(tmp_path, value):
    gp = tmp_path / "g.yaml"
    _write(gp, {"observability": {"langsmith": value}})
    cfg = load_config(global_path=gp, project_path=None)
    assert cfg.observability.langsmith is True


def test_bool_coercion_garbage_raises(tmp_path):
    gp = tmp_path / "g.yaml"
    _write(gp, {"observability": {"langsmith": "maybe"}})
    with pytest.raises(ConfigError, match="observability.langsmith"):
        load_config(global_path=gp, project_path=None)


def test_negative_budget_raises(tmp_path):
    gp = tmp_path / "g.yaml"
    _write(gp, {"budget": {"per_session_usd": -1.0}})
    with pytest.raises(ConfigError, match="per_session_usd"):
        load_config(global_path=gp, project_path=None)


def test_zero_budget_valid(tmp_path):
    gp = tmp_path / "g.yaml"
    _write(gp, {"budget": {"per_session_usd": 0}})
    cfg = load_config(global_path=gp, project_path=None)
    assert cfg.budget.per_session_usd == 0


@pytest.mark.parametrize("pct", [-5, 150, 101])
def test_pct_out_of_range_raises(tmp_path, pct):
    gp = tmp_path / "g.yaml"
    _write(gp, {"budget": {"warn_at_pct": pct}})
    with pytest.raises(ConfigError, match="warn_at_pct"):
        load_config(global_path=gp, project_path=None)


@pytest.mark.parametrize("pct", [0, 100])
def test_pct_bounds_valid(tmp_path, pct):
    gp = tmp_path / "g.yaml"
    _write(gp, {"context": {"compact_at_pct": pct}})
    cfg = load_config(global_path=gp, project_path=None)
    assert cfg.context.compact_at_pct == pct


def test_hook_blocking_non_bool_raises(tmp_path):
    gp = tmp_path / "g.yaml"
    _write(gp, {"hooks": [{"event": "post_edit", "command": "x", "blocking": "huh"}]})
    with pytest.raises(ConfigError, match="blocking"):
        load_config(global_path=gp, project_path=None)


def test_mcp_args_non_list_raises(tmp_path):
    gp = tmp_path / "g.yaml"
    _write(gp, {"mcp_servers": [{"name": "fs", "args": "notalist"}]})
    with pytest.raises(ConfigError, match="args"):
        load_config(global_path=gp, project_path=None)


def test_mcp_env_non_dict_raises(tmp_path):
    gp = tmp_path / "g.yaml"
    _write(gp, {"mcp_servers": [{"name": "fs", "env": ["not", "a", "dict"]}]})
    with pytest.raises(ConfigError, match="env"):
        load_config(global_path=gp, project_path=None)


def test_async_subagent_name_non_string_raises(tmp_path):
    gp = tmp_path / "g.yaml"
    _write(
        gp,
        {
            "async_subagents": [
                {"name": ["x"], "description": "d", "graph_id": "g"}
            ]
        },
    )
    with pytest.raises(ConfigError, match="name"):
        load_config(global_path=gp, project_path=None)


def test_unknown_top_level_key_raises(tmp_path):
    gp = tmp_path / "g.yaml"
    _write(gp, {"bogus_key": 123})
    with pytest.raises(ConfigError, match="bogus_key"):
        load_config(global_path=gp, project_path=None)


# -- FIX 7: enum-like field validation --------------------------------------


def test_invalid_execution_backend_raises(tmp_path):
    """execution.backend with an unknown value must raise ConfigError."""
    gp = tmp_path / "g.yaml"
    _write(gp, {"execution": {"backend": "cloud"}})
    with pytest.raises(ConfigError, match="execution.backend"):
        load_config(global_path=gp, project_path=None)


def test_valid_execution_backend_local(tmp_path):
    """execution.backend: local is a valid value."""
    gp = tmp_path / "g.yaml"
    _write(gp, {"execution": {"backend": "local"}})
    cfg = load_config(global_path=gp, project_path=None)
    assert cfg.execution.backend == "local"


def test_valid_execution_backend_sandbox(tmp_path):
    """execution.backend: sandbox is a valid value."""
    gp = tmp_path / "g.yaml"
    _write(gp, {"execution": {"backend": "sandbox"}})
    cfg = load_config(global_path=gp, project_path=None)
    assert cfg.execution.backend == "sandbox"


def test_invalid_observability_log_level_raises(tmp_path):
    """observability.log_level with an unknown value must raise ConfigError."""
    gp = tmp_path / "g.yaml"
    _write(gp, {"observability": {"log_level": "verbose"}})
    with pytest.raises(ConfigError, match="observability.log_level"):
        load_config(global_path=gp, project_path=None)


@pytest.mark.parametrize("level", ["debug", "info", "warning", "error"])
def test_valid_observability_log_level(tmp_path, level):
    """Each of the four recognised log_level values must parse without error."""
    gp = tmp_path / "g.yaml"
    _write(gp, {"observability": {"log_level": level}})
    cfg = load_config(global_path=gp, project_path=None)
    assert cfg.observability.log_level == level


def test_doctor_json_is_valid(tmp_path, monkeypatch, capsys):
    import json

    from jarn.config import paths

    # A config that resolves end-to-end: a provider with an inline key so the
    # main model both resolves its secret and constructs without network.
    gp = tmp_path / "config.yaml"
    _write(
        gp,
        {
            "default_profile": "openrouter",
            "providers": {"openrouter": {"type": "openrouter", "api_key": "sk-test"}},
        },
    )
    monkeypatch.setattr(paths, "global_config_path", lambda: gp)
    monkeypatch.setattr(paths, "find_project_root", lambda *a, **k: None)
    monkeypatch.setattr(
        paths, "project_config_path", lambda *a, **k: tmp_path / "missing.yaml"
    )

    from jarn import cli

    cli._cmd_doctor(as_json=True)
    out = capsys.readouterr().out
    data = json.loads(out)
    assert isinstance(data, dict)
    assert "ok" in data
    assert data["global_config"] == str(gp)
    assert data["global_config_present"] is True
    assert "providers" in data
    assert "permission_mode" in data
    assert "main_model" in data
