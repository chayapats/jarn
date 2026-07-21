"""Config loading, merging, and secret resolution tests."""

from __future__ import annotations

import warnings

import pytest
import yaml

from jarn.config.loader import ConfigError, InlineSecretWarning, load_config
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


def test_ui_steering_default_true(tmp_path):
    """Mid-turn steering (T-4-6) is on by default."""
    cfg = load_config(
        global_path=tmp_path / "missing-global.yaml",
        project_path=tmp_path / "missing-project.yaml",
    )
    assert cfg.ui.steering is True


def test_ui_steering_parsed_false(tmp_path):
    """ui.steering: false round-trips through the pydantic validator + dataclass."""
    gp = tmp_path / "g.yaml"
    _write(gp, {"ui": {"steering": False}})
    cfg = load_config(global_path=gp, project_path=None)
    assert cfg.ui.steering is False


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


def test_network_policy_round_trip(tmp_path):
    """permissions.network host globs load through pydantic → dataclass (Wave B)."""
    gp = tmp_path / "g.yaml"
    _write(gp, {"permissions": {"network": {"allow": ["*.github.com"], "deny": ["evil.com"]}}})
    cfg = load_config(global_path=gp, project_path=None)
    assert cfg.permissions.network.allow == ["*.github.com"]
    assert cfg.permissions.network.deny == ["evil.com"]


def test_network_policy_merges_across_tiers(tmp_path):
    """Both tiers' network allow/deny concatenate (project extends global)."""
    gp = tmp_path / "g.yaml"
    pp = tmp_path / "p.yaml"
    _write(gp, {"permissions": {"network": {"allow": ["*.github.com"]}}})
    _write(pp, {"permissions": {"network": {"allow": ["*.pypi.org"], "deny": ["evil.com"]}}})
    cfg = load_config(global_path=gp, project_path=pp)
    assert cfg.permissions.network.allow == ["*.github.com", "*.pypi.org"]
    assert cfg.permissions.network.deny == ["evil.com"]


def test_network_policy_defaults_empty(tmp_path):
    cfg = load_config(
        global_path=tmp_path / "none-g.yaml", project_path=tmp_path / "none-p.yaml"
    )
    assert cfg.permissions.network.allow == []
    assert cfg.permissions.network.deny == []


def test_network_policy_coexists_with_allow_deny(tmp_path):
    """A permissions block can carry allow/deny AND network without losing either."""
    gp = tmp_path / "g.yaml"
    _write(gp, {
        "permissions": {
            "allow": ["git status"],
            "network": {"deny": ["evil.com"]},
        }
    })
    # Pin project_root to an empty tmp dir so project-config discovery can't
    # pick up the repo's own .jarn/config.yaml (this test asserts exact allow).
    cfg = load_config(global_path=gp, project_path=None, project_root=tmp_path)
    assert cfg.permissions.allow == ["git status"]
    assert cfg.permissions.network.deny == ["evil.com"]


def test_sensitive_read_globs_custom_survives_merge(tmp_path):
    """A configured custom ``sensitive_read_globs`` REPLACES the built-in defaults
    through the merge — previously it was silently dropped so the defaults always
    won (BUG D)."""
    gp = tmp_path / "g.yaml"
    _write(gp, {"permissions": {"sensitive_read_globs": ["custom/*.secret"]}})
    cfg = load_config(global_path=gp, project_path=None, project_root=tmp_path)
    assert cfg.permissions.sensitive_read_globs == ["custom/*.secret"]


def test_sensitive_read_globs_empty_opt_out_survives_merge(tmp_path):
    """The documented empty-list opt-out reaches the config, not the 11 defaults."""
    gp = tmp_path / "g.yaml"
    _write(gp, {"permissions": {"sensitive_read_globs": []}})
    cfg = load_config(global_path=gp, project_path=None, project_root=tmp_path)
    assert cfg.permissions.sensitive_read_globs == []


def test_sensitive_read_globs_default_when_unset(tmp_path):
    """When no tier sets it, the built-in defaults still apply (regression guard)."""
    from jarn.config.schema import DEFAULT_SENSITIVE_READ_GLOBS

    cfg = load_config(
        global_path=tmp_path / "none-g.yaml",
        project_path=tmp_path / "none-p.yaml",
        project_root=tmp_path,
    )
    assert cfg.permissions.sensitive_read_globs == list(DEFAULT_SENSITIVE_READ_GLOBS)


def test_sensitive_read_globs_project_replaces_global(tmp_path):
    """Last-writer wins: a project list replaces the global one (tier precedence)."""
    gp = tmp_path / "g.yaml"
    pp = tmp_path / "p.yaml"
    _write(gp, {"permissions": {"sensitive_read_globs": ["global/*.key"]}})
    _write(pp, {"permissions": {"sensitive_read_globs": ["proj/*.pem"]}})
    cfg = load_config(global_path=gp, project_path=pp)
    assert cfg.permissions.sensitive_read_globs == ["proj/*.pem"]


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


@pytest.mark.parametrize("token", [".nan", ".inf", "-.inf"])
def test_nonfinite_budget_raises(tmp_path, token):
    """A non-finite per_session_usd (NaN/inf) is rejected at config validation: it
    would make every later budget comparison False, silently disabling the hard
    stop. ``raw < 0`` alone misses NaN (all NaN comparisons are False)."""
    gp = tmp_path / "g.yaml"
    gp.write_text(f"budget:\n  per_session_usd: {token}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="per_session_usd"):
        load_config(global_path=gp, project_path=None)


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


def test_nested_unknown_rejected(tmp_path):
    """Unknown keys inside nested sections are rejected (Pydantic extra=forbid)."""
    gp = tmp_path / "g.yaml"
    _write(gp, {"context": {"bogus_nested": 123}})
    with pytest.raises(ConfigError, match="bogus_nested"):
        load_config(global_path=gp, project_path=None)


def test_migration_from_prev_version(tmp_path):
    """Version-0 configs without config_version migrate via the v0→v1 shim."""
    gp = tmp_path / "g.yaml"
    _write(gp, {"log_level": "debug", "default_profile": "openrouter"})
    cfg = load_config(global_path=gp, project_path=None)
    assert cfg.observability.log_level == "debug"
    assert cfg.default_profile == "openrouter"


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


def test_mcp_validation_rejects_bad_transport(tmp_path):
    gp = tmp_path / "g.yaml"
    _write(gp, {"mcp_servers": [{"name": "x", "transport": "ftp", "command": "run"}]})
    with pytest.raises(ConfigError, match="transport"):
        load_config(global_path=gp, project_path=None)


def test_mcp_validation_rejects_shell_metacharacters(tmp_path):
    gp = tmp_path / "g.yaml"
    _write(
        gp,
        {"mcp_servers": [{"name": "x", "transport": "stdio", "command": "echo; rm"}]},
    )
    with pytest.raises(ConfigError, match="metacharacters"):
        load_config(global_path=gp, project_path=None)


def test_mcp_validation_rejects_non_absolute_url(tmp_path):
    gp = tmp_path / "g.yaml"
    _write(
        gp,
        {
            "mcp_servers": [
                {"name": "remote", "transport": "http", "url": "/relative/path"}
            ]
        },
    )
    with pytest.raises(ConfigError, match="absolute http"):
        load_config(global_path=gp, project_path=None)


def test_subagent_validation_rejects_bad_url(tmp_path):
    gp = tmp_path / "g.yaml"
    _write(
        gp,
        {
            "async_subagents": [
                {
                    "name": "worker",
                    "description": "remote worker",
                    "graph_id": "g1",
                    "url": "not-a-url",
                }
            ]
        },
    )
    with pytest.raises(ConfigError, match="absolute http"):
        load_config(global_path=gp, project_path=None)


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


# ---------------------------------------------------------------------------
# T-1-3: inline plaintext api_key warn/reject
# ---------------------------------------------------------------------------

_INLINE_KEY = "sk-proj-" + "A" * 40  # long enough to look like a real key


def test_inline_api_key_warns(tmp_path, recwarn):
    """A real-looking inline api_key loads with a visible warning (non-strict)."""
    gp = tmp_path / "g.yaml"
    _write(gp, {"providers": {"openrouter": {"type": "openrouter", "api_key": _INLINE_KEY}}})
    with pytest.warns(InlineSecretWarning, match="inline plaintext api_key"):
        cfg = load_config(global_path=gp, project_path=None)
    assert cfg.strict_secrets is False  # default
    assert cfg.providers["openrouter"].api_key == _INLINE_KEY  # still loads


def test_inline_api_key_strict_rejects(tmp_path):
    """strict_secrets: true turns an inline key into a ConfigError."""
    gp = tmp_path / "g.yaml"
    _write(
        gp,
        {
            "strict_secrets": True,
            "providers": {"openrouter": {"type": "openrouter", "api_key": _INLINE_KEY}},
        },
    )
    with pytest.raises(ConfigError, match="inline plaintext api_keys"):
        load_config(global_path=gp, project_path=None)


def test_local_provider_no_warn(tmp_path, recwarn):
    """Empty key and short local tokens (lm-studio) pass without warning."""
    gp = tmp_path / "g.yaml"
    _write(
        gp,
        {
            "providers": {
                "ollama": {"type": "ollama", "base_url": "http://localhost:11434"},
                "lmstudio": {"type": "lmstudio", "api_key": "lm-studio"},
            }
        },
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any InlineSecretWarning would fail
        cfg = load_config(global_path=gp, project_path=None)
    assert cfg.providers["ollama"].api_key is None
    assert cfg.providers["lmstudio"].api_key == "lm-studio"


def test_reference_api_key_no_warn(tmp_path):
    """A reference (keychain:/file:/${ENV}) is never flagged even when strict."""
    gp = tmp_path / "g.yaml"
    _write(
        gp,
        {
            "strict_secrets": True,
            "providers": {
                "a": {"type": "openrouter", "api_key": "${OPENROUTER_API_KEY}"},
                "b": {"type": "openrouter", "api_key": "keychain:jarn/openrouter"},
            },
        },
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        cfg = load_config(global_path=gp, project_path=None)
    assert cfg.providers["a"].api_key == "${OPENROUTER_API_KEY}"


def test_policy_profile_migrated_v2(tmp_path):
    """v1→v2 migration drops policy.profile and emits a UserWarning."""
    gp = tmp_path / "g.yaml"
    _write(gp, {"config_version": 1, "policy": {"profile": "offline", "web_tools": True}})
    with pytest.warns(UserWarning, match="policy.profile"):
        cfg = load_config(global_path=gp, project_path=None)
    # The profile key is gone — no attribute, no effect
    assert cfg.policy.web_tools is True  # other policy keys untouched
