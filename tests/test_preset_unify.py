"""P3.A/UNIFY — equivalence + back-compat for the unified permission model.

The untrusted floor was refactored from ``apply_profile("review-only")`` into a
direct clamp, and ``profile`` is being reframed as a launch-time ``preset``.
These tests pin the BYTE-FOR-BYTE effective settings the floor and the presets
must produce, so the refactor cannot drift: they pass identically before and
after the change.
"""

from __future__ import annotations

import pytest

from jarn.config.profiles import PROFILES, resolve_effective_profile
from jarn.config.schema import Config, PermissionMode


def _cfg(*, mode="ask", local_sandbox="off", network=True, web=True, profile=""):
    c = Config()
    c.permission_mode = PermissionMode(mode)
    c.execution.local_sandbox = local_sandbox
    c.execution.sandbox_allow_network = network
    c.policy.web_tools = web
    c.policy.profile = profile
    return c


def _effective(c):
    return (
        c.permission_mode,
        c.execution.local_sandbox,
        c.execution.sandbox_allow_network,
        c.policy.web_tools,
    )


#: The review-only posture the untrusted floor must always produce.
FLOOR = (PermissionMode.PLAN, "off", True, True)


@pytest.mark.parametrize("start_mode", ["plan", "ask", "auto-edit", "yolo"])
@pytest.mark.parametrize("preset", [None, *PROFILES])
def test_untrusted_floor_is_byte_for_byte_review_only(start_mode, preset):
    """However the session is configured, an untrusted project collapses to the
    review-only posture: PLAN + sandbox off + network on + web on. This is the
    equivalence the direct-clamp refactor must preserve exactly."""
    c = _cfg(mode=start_mode, profile=preset or "")
    resolve_effective_profile(c, project_trusted=False, cli_profile=None)
    assert _effective(c) == FLOOR


@pytest.mark.parametrize("preset", list(PROFILES))
def test_trusted_preset_expands_to_table_values(preset):
    """A trusted project with a preset gets exactly the table's four knobs."""
    c = _cfg()
    resolve_effective_profile(c, project_trusted=True, cli_profile=preset)
    eff = PROFILES[preset]
    assert _effective(c) == (
        PermissionMode(eff["permission_mode"]),
        eff["local_sandbox"],
        eff["sandbox_allow_network"],
        eff["web_tools"],
    )


def test_trusted_no_preset_leaves_config_untouched():
    """No preset + trusted → the session keeps exactly what the user configured."""
    c = _cfg(mode="auto-edit", local_sandbox="require", network=False, web=False)
    resolve_effective_profile(c, project_trusted=True, cli_profile=None)
    assert _effective(c) == (PermissionMode.AUTO_EDIT, "require", False, False)


def test_cli_preset_overrides_config_preset_when_trusted():
    """CLI preset wins over the config's preset (precedence unchanged)."""
    c = _cfg(profile="offline")
    resolve_effective_profile(c, project_trusted=True, cli_profile="ci")
    assert c.permission_mode == PermissionMode(PROFILES["ci"]["permission_mode"])


def test_policy_profile_config_key_warns_once(capsys, monkeypatch):
    """The deprecated policy.profile config key emits a one-time launch notice
    that names what it expands to."""
    import jarn.cli as cli

    monkeypatch.setattr(cli, "_warned_policy_profile", False)
    c = _cfg(profile="ci")
    cli._warn_policy_profile_deprecated(c)
    err = capsys.readouterr().err
    assert "policy.profile is deprecated" in err
    assert "mode=yolo" in err  # names the expansion
    # one-time: a second call is silent
    cli._warn_policy_profile_deprecated(c)
    assert capsys.readouterr().err == ""


def test_policy_profile_unset_is_silent(capsys, monkeypatch):
    import jarn.cli as cli

    monkeypatch.setattr(cli, "_warned_policy_profile", False)
    cli._warn_policy_profile_deprecated(_cfg())  # no preset set
    assert capsys.readouterr().err == ""
