"""Policy profiles — named bundles of trust-relevant settings.

A *profile* is a single name the user can select (``--profile``, ``policy.profile``
in YAML, or ``/profile`` in the REPL) that overlays a coherent set of
trust-relevant knobs at once: the coarse :class:`PermissionMode`, the OS-level
``local_sandbox`` mode, whether the sandbox may reach the network, and whether
the in-process web tools are registered.

Profiles are applied at the *launch boundary* (where ``project_trusted`` is
known), never inside :func:`jarn.config.loader.load_config` — that keeps config
loading pure and lets the untrusted-floor clamp run last.

The untrusted floor is a one-way clamp: an untrusted project can never be
loosened below :data:`UNTRUSTED_FLOOR_PROFILE`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jarn.config.loader import ConfigError
from jarn.config.schema import PermissionMode

if TYPE_CHECKING:
    from jarn.config.schema import Config

#: name -> effect. ``backend`` stays ``"local"`` for every profile.
PROFILES: dict[str, dict] = {
    "trusted-repo": {
        "permission_mode": "ask",
        "local_sandbox": "off",
        "sandbox_allow_network": True,
        "web_tools": True,
    },
    "review-only": {
        "permission_mode": "plan",
        "local_sandbox": "off",
        "sandbox_allow_network": True,
        "web_tools": True,
    },
    "sandbox-required": {
        "permission_mode": "ask",
        "local_sandbox": "require",
        "sandbox_allow_network": False,
        "web_tools": True,
    },
    "ci": {
        "permission_mode": "yolo",
        "local_sandbox": "require",
        "sandbox_allow_network": True,
        "web_tools": True,
    },
    "offline": {
        "permission_mode": "ask",
        "local_sandbox": "auto",
        "sandbox_allow_network": False,
        "web_tools": False,
    },
}

PROFILE_NAMES = frozenset(PROFILES)

#: The most permissive profile an untrusted project is allowed to run under.
UNTRUSTED_FLOOR_PROFILE = "review-only"


def apply_profile(config: Config, name: str) -> None:
    """Overlay the named profile's effect onto ``config`` in place.

    Sets ``permission_mode`` (as a :class:`PermissionMode`), the execution
    ``local_sandbox`` and ``sandbox_allow_network`` knobs, and
    ``policy.web_tools``. Raises :class:`ConfigError` for an unknown name.
    """
    effect = PROFILES.get(name)
    if effect is None:
        raise ConfigError(
            f"Unknown policy profile {name!r}; expected one of {sorted(PROFILE_NAMES)}"
        )
    config.permission_mode = PermissionMode(effect["permission_mode"])
    config.execution.local_sandbox = effect["local_sandbox"]
    config.execution.sandbox_allow_network = effect["sandbox_allow_network"]
    config.policy.web_tools = effect["web_tools"]


def _clamp_untrusted_floor(config: Config) -> None:
    """Force the untrusted floor as a one-way clamp — *directly*, not by applying
    a preset.

    An untrusted project is pinned to plan mode (the agent may look but not act)
    with the review-only sandbox posture (sandbox off, network on, web on). This
    mirrors the live mode-clamp in :meth:`Controller.apply_mode` and runs last at
    the launch boundary so nothing can loosen it. The values are byte-for-byte
    equivalent to the old ``apply_profile("review-only")`` floor — pinned by
    ``tests/test_preset_unify.py`` so the equivalence can't drift.
    """
    config.permission_mode = PermissionMode.PLAN
    config.execution.local_sandbox = "off"
    config.execution.sandbox_allow_network = True
    config.policy.web_tools = True


def resolve_effective_profile(
    config: Config,
    *,
    project_trusted: bool,
    cli_profile: str | None = None,
) -> str | None:
    """Expand the effective preset onto ``config`` and return its name (or None).

    Precedence: ``cli_profile`` > ``config.policy.profile`` > nothing. The chosen
    preset (if any) is expanded first; THEN, when the project is untrusted, the
    untrusted floor is forced as a direct clamp — an untrusted session can never
    be loosened below it, regardless of what the CLI or config asked for.

    (``profile`` is the legacy name for ``preset``: a launch-time shortcut that
    expands to a mode + sandbox posture. It is not a persistent parallel axis.)
    """
    chosen = cli_profile or config.policy.profile or None
    if chosen:
        apply_profile(config, chosen)
    if not project_trusted:
        _clamp_untrusted_floor(config)
        return UNTRUSTED_FLOOR_PROFILE
    return chosen
