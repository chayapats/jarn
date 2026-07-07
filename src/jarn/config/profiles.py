"""Policy presets — named bundles of trust-relevant settings.

A *preset* is a single name the user can select (``--preset`` CLI flag or
``/preset`` in the REPL) that overlays a coherent set of trust-relevant knobs
at once: the coarse :class:`PermissionMode`, the OS-level ``local_sandbox``
mode, whether the sandbox may reach the network, and whether the in-process web
tools are registered.

Presets are applied at the *launch boundary* (where ``project_trusted`` is
known), never inside :func:`jarn.config.loader.load_config` — that keeps config
loading pure and lets the untrusted-floor clamp run last.

The untrusted floor is a one-way clamp: an untrusted project can never be
loosened below :data:`UNTRUSTED_FLOOR_PROFILE`.

Note: ``policy.profile`` / ``--profile`` / ``/profile`` were removed in v0.6.0.
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
        # Docker is the isolation for CI — not the OS-level local sandbox. The
        # docker backend fails closed (SandboxUnavailable) when Docker isn't
        # available, so YOLO never silently runs on a bare CI host: you get a
        # clear "Docker is not available" launch error instead. (Set
        # execution.allow_local_fallback: true to opt INTO host fallback.)
        "backend": "docker",
        "local_sandbox": "off",
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
    if "backend" in effect:
        config.execution.backend = effect["backend"]
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
    cli_permission_mode: str | None = None,
) -> str | None:
    """Expand the effective preset onto ``config`` and return its name (or None).

    Precedence for the coarse permission mode: **explicit ``cli_permission_mode``
    > preset default > config default**.  A preset (if any) is expanded first —
    which sets the mode from its table — then an explicitly-passed
    ``cli_permission_mode`` (argparse sentinel ``None`` == not passed) overrides
    just that mode, so ``--permission-mode`` is never silently stomped by
    ``--preset``.  The preset still governs the other trust-relevant knobs
    (sandbox, network, web tools).  Finally, when the project is untrusted, the
    untrusted floor is forced as a direct clamp — an untrusted session can never
    be loosened below it, regardless of what the CLI asked for (the floor beats
    an explicit mode too).
    """
    chosen = cli_profile or None
    if chosen:
        apply_profile(config, chosen)
    if cli_permission_mode is not None:
        # Explicit CLI mode wins over the preset's default mode.
        config.permission_mode = PermissionMode(cli_permission_mode)
    if not project_trusted:
        _clamp_untrusted_floor(config)
        return UNTRUSTED_FLOOR_PROFILE
    return chosen
