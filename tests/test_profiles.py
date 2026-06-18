"""M2 — policy profile tests.

Covers the profile table, precedence + untrusted-floor clamp, loader parsing,
the web-tools gate in build_runtime, doctor surfacing, and the /profile command.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import patch

import pytest
import yaml

from jarn.config.loader import ConfigError, load_config
from jarn.config.profiles import (
    PROFILE_NAMES,
    PROFILES,
    UNTRUSTED_FLOOR_PROFILE,
    apply_profile,
    resolve_effective_profile,
)
from jarn.config.schema import PermissionMode


def _fresh(base_config):
    """A deep-ish copy so each apply_profile case starts clean."""
    return dataclasses.replace(
        base_config,
        execution=dataclasses.replace(base_config.execution),
        policy=dataclasses.replace(base_config.policy),
    )


# -- the profile table ------------------------------------------------------


@pytest.mark.parametrize("name", sorted(PROFILE_NAMES))
def test_apply_profile_matches_table(base_config, name):
    cfg = _fresh(base_config)
    apply_profile(cfg, name)
    effect = PROFILES[name]
    assert cfg.permission_mode == PermissionMode(effect["permission_mode"])
    assert cfg.execution.local_sandbox == effect["local_sandbox"]
    assert cfg.execution.sandbox_allow_network == effect["sandbox_allow_network"]
    assert cfg.policy.web_tools == effect["web_tools"]
    # backend is unaffected by every profile.
    assert cfg.execution.backend == "local"


#: Hardcoded expected effect per profile, drawn from the SPEC — independent of
#: the PROFILES dict so a typo in the table is actually caught (the parametrized
#: test above compares apply_profile() against PROFILES, which can't catch a
#: wrong literal in PROFILES itself).
_EXPECTED = {
    "trusted-repo": (PermissionMode.ASK, "off", True, True),
    "review-only": (PermissionMode.PLAN, "off", True, True),
    "sandbox-required": (PermissionMode.ASK, "require", False, True),
    "ci": (PermissionMode.YOLO, "require", True, True),
    "offline": (PermissionMode.ASK, "auto", False, False),
}


@pytest.mark.parametrize("name", sorted(_EXPECTED))
def test_apply_profile_literal_values(base_config, name):
    cfg = _fresh(base_config)
    apply_profile(cfg, name)
    mode, sandbox, net, web = _EXPECTED[name]
    assert cfg.permission_mode == mode
    assert cfg.execution.local_sandbox == sandbox
    assert cfg.execution.sandbox_allow_network is net
    assert cfg.policy.web_tools is web


def test_expected_table_covers_every_profile():
    # Guard: the literal table and the implementation table cover the same names.
    assert set(_EXPECTED) == set(PROFILE_NAMES)


def test_offline_disables_web_tools(base_config):
    cfg = _fresh(base_config)
    apply_profile(cfg, "offline")
    assert cfg.policy.web_tools is False
    assert cfg.execution.sandbox_allow_network is False


def test_apply_unknown_profile_raises(base_config):
    cfg = _fresh(base_config)
    with pytest.raises(ConfigError):
        apply_profile(cfg, "nope")


# -- precedence + untrusted floor -------------------------------------------


def test_cli_profile_beats_config_profile(base_config):
    cfg = _fresh(base_config)
    cfg.policy.profile = "trusted-repo"
    effective = resolve_effective_profile(
        cfg, project_trusted=True, cli_profile="sandbox-required"
    )
    assert effective == "sandbox-required"
    # The CLI profile is applied in full, not partially.
    assert cfg.permission_mode == PermissionMode.ASK
    assert cfg.execution.local_sandbox == "require"
    assert cfg.execution.sandbox_allow_network is False
    assert cfg.policy.web_tools is True


def test_config_profile_used_when_no_cli(base_config):
    cfg = _fresh(base_config)
    cfg.policy.profile = "offline"
    effective = resolve_effective_profile(cfg, project_trusted=True)
    assert effective == "offline"
    assert cfg.policy.web_tools is False


def test_no_profile_returns_none(base_config):
    cfg = _fresh(base_config)
    effective = resolve_effective_profile(cfg, project_trusted=True)
    assert effective is None


def test_untrusted_floor_forces_review_only(base_config):
    cfg = _fresh(base_config)
    # Even when the CLI asks for the most permissive profile (ci/yolo)…
    effective = resolve_effective_profile(
        cfg, project_trusted=False, cli_profile="ci"
    )
    # …an untrusted project is clamped to the review-only floor — all four
    # dimensions, not just the mode.
    assert effective == UNTRUSTED_FLOOR_PROFILE == "review-only"
    assert cfg.permission_mode == PermissionMode.PLAN
    assert cfg.execution.local_sandbox == "off"
    assert cfg.execution.sandbox_allow_network is True
    assert cfg.policy.web_tools is True


def test_untrusted_floor_clamps_config_yolo(base_config):
    cfg = _fresh(base_config)
    cfg.policy.profile = "ci"
    effective = resolve_effective_profile(cfg, project_trusted=False)
    assert effective == "review-only"
    assert cfg.permission_mode == PermissionMode.PLAN


# -- loader parsing ---------------------------------------------------------


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_policy_profile_roundtrips(tmp_path):
    gp = tmp_path / "config.yaml"
    _write(gp, {"policy": {"profile": "offline", "web_tools": False}})
    cfg = load_config(global_path=gp, project_path=None)
    assert cfg.policy.profile == "offline"
    assert cfg.policy.web_tools is False


def test_policy_default_is_empty(tmp_path):
    cfg = load_config(
        global_path=tmp_path / "missing.yaml", project_path=None
    )
    assert cfg.policy.profile == ""
    assert cfg.policy.web_tools is True


def test_bad_profile_name_raises(tmp_path):
    gp = tmp_path / "config.yaml"
    _write(gp, {"policy": {"profile": "bogus"}})
    with pytest.raises(ConfigError):
        load_config(global_path=gp, project_path=None)


def test_bad_web_tools_type_raises(tmp_path):
    gp = tmp_path / "config.yaml"
    _write(gp, {"policy": {"web_tools": "maybe"}})
    with pytest.raises(ConfigError):
        load_config(global_path=gp, project_path=None)


# -- web-tools gate in build_runtime ----------------------------------------


def _build(base_config):
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from jarn.agent.builder import build_runtime

    fake = GenericFakeChatModel(messages=iter([]))
    with patch("jarn.providers.models.ModelFactory.build", return_value=fake):
        return build_runtime(base_config, project_root=None)


def test_web_tools_present_when_enabled(base_config, tmp_path):
    base_config.policy.web_tools = True
    rt = _build(base_config)
    names = _runtime_tool_names(rt)
    assert "web_search" in names
    assert "web_fetch" in names


def test_web_tools_absent_when_disabled(base_config):
    base_config.policy.web_tools = False
    rt = _build(base_config)
    names = _runtime_tool_names(rt)
    assert "web_search" not in names
    assert "web_fetch" not in names


def _runtime_tool_names(rt) -> set[str]:
    """Tool names registered on the compiled agent (best-effort across versions)."""
    names: set[str] = set()
    nodes = getattr(rt.agent, "nodes", {}) or {}
    tools_node = nodes.get("tools")
    bound = getattr(tools_node, "bound", None) if tools_node else None
    tools_by_name = getattr(bound, "tools_by_name", None)
    if isinstance(tools_by_name, dict):
        names |= set(tools_by_name)
    return names


# -- doctor -----------------------------------------------------------------


def test_doctor_includes_policy_profile(tmp_path, monkeypatch):
    from jarn import cli
    from jarn.config import paths

    gp = tmp_path / "config.yaml"
    _write(
        gp,
        {
            "providers": {
                "openrouter": {
                    "type": "openrouter",
                    "api_key": "sk-test",
                    "base_url": "http://localhost:9999/v1",
                }
            },
            "routing": {"main": "openrouter/some-model"},
            "policy": {"profile": "offline", "web_tools": False},
        },
    )
    monkeypatch.setattr(paths, "global_config_path", lambda: gp)
    monkeypatch.setattr(paths, "find_project_root", lambda *a, **k: None)

    diag: dict = {}
    cli._collect_doctor(diag)
    assert diag["policy_profile"] == "offline"
    assert diag["web_tools"] is False


# -- /profile command -------------------------------------------------------


def _controller(tmp_path, monkeypatch, base_config, *, trusted=True):
    from jarn.tui.controller import Controller

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    return Controller(base_config, root, project_trusted=trusted)


def test_profile_command_is_dispatchable():
    from jarn.extensibility.commands import builtin_command, route_for

    cmd = builtin_command("profile")
    assert cmd is not None
    assert route_for("profile") == "controller"


def test_profile_command_no_arg_lists(tmp_path, monkeypatch, base_config):
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    res = ctrl.handle_command("profile", "")
    assert "review-only" in res.text and "offline" in res.text
    assert res.rebuilt is False
    ctrl.close()


def test_profile_command_applies_and_rebuilds(tmp_path, monkeypatch, base_config):
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    res = ctrl.handle_command("profile", "sandbox-required")
    assert res.rebuilt is True
    assert ctrl.config.execution.local_sandbox == "require"
    assert ctrl.config.policy.profile == "sandbox-required"
    assert ctrl.runtime is None
    ctrl.close()


def test_profile_command_unknown_name(tmp_path, monkeypatch, base_config):
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    res = ctrl.handle_command("profile", "bogus")
    assert res.rebuilt is False
    assert "Unknown preset" in res.text
    ctrl.close()


def test_preset_command_echoes_expansion(tmp_path, monkeypatch, base_config):
    """/preset applies and echoes exactly what it expanded to (mode + sandbox)."""
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    res = ctrl.handle_command("preset", "ci")
    assert res.rebuilt is True
    assert "mode=yolo" in res.text and "sandbox=require" in res.text
    assert ctrl.config.permission_mode.value == "yolo"
    ctrl.close()


def test_profile_command_is_deprecated_alias(tmp_path, monkeypatch, base_config):
    """/profile still works but is flagged deprecated, delegating to /preset."""
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    res = ctrl.handle_command("profile", "ci")
    assert res.rebuilt is True
    assert "deprecated" in res.text.lower()
    assert ctrl.config.permission_mode.value == "yolo"  # same effect as /preset
    ctrl.close()


def test_profile_command_untrusted_clamps(tmp_path, monkeypatch, base_config):
    ctrl = _controller(tmp_path, monkeypatch, base_config, trusted=False)
    res = ctrl.handle_command("profile", "ci")
    # Untrusted session cannot be loosened: ci → review-only floor.
    assert ctrl.config.permission_mode == PermissionMode.PLAN
    assert ctrl.config.policy.profile == "review-only"
    assert res.rebuilt is True
    ctrl.close()


# -- untrusted-floor cannot be bypassed via other channels ------------------
# (regression guards for the M2 trust-safety review: the floor must hold across
# every mode/sandbox entry point, not just /profile and launch.)


def test_mode_command_untrusted_cannot_escalate(tmp_path, monkeypatch, base_config):
    ctrl = _controller(tmp_path, monkeypatch, base_config, trusted=False)
    ctrl.handle_command("mode", "yolo")
    # /mode yolo on an untrusted project is clamped to the plan floor.
    assert ctrl.config.permission_mode == PermissionMode.PLAN
    assert ctrl.engine.mode == PermissionMode.PLAN
    ctrl.close()


def test_apply_mode_untrusted_clamps_all_permissive(tmp_path, monkeypatch, base_config):
    ctrl = _controller(tmp_path, monkeypatch, base_config, trusted=False)
    for m in ("ask", "auto-edit", "yolo"):
        assert ctrl.apply_mode(m) == "plan"
        assert ctrl.config.permission_mode == PermissionMode.PLAN
    ctrl.close()


def test_cycle_mode_untrusted_stays_plan(tmp_path, monkeypatch, base_config):
    ctrl = _controller(tmp_path, monkeypatch, base_config, trusted=False)
    # Shift+Tab cycling can never climb above the floor on an untrusted project.
    for _ in range(4):
        assert ctrl.cycle_mode() == "plan"
        assert ctrl.config.permission_mode == PermissionMode.PLAN
    ctrl.close()


def test_apply_mode_trusted_allows_escalation(tmp_path, monkeypatch, base_config):
    ctrl = _controller(tmp_path, monkeypatch, base_config, trusted=True)
    assert ctrl.apply_mode("yolo") == "yolo"
    assert ctrl.config.permission_mode == PermissionMode.YOLO
    ctrl.close()


def test_sandbox_command_untrusted_locked(tmp_path, monkeypatch, base_config):
    ctrl = _controller(tmp_path, monkeypatch, base_config, trusted=False)
    before = ctrl.config.execution.backend
    res = ctrl.handle_command("sandbox", "docker")
    assert "untrusted" in res.text.lower()
    assert ctrl.config.execution.backend == before  # not changed
    assert res.rebuilt is False
    ctrl.close()
