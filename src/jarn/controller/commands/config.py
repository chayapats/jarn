"""Built-in /config slash-command handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jarn.config.schema import PermissionMode
from jarn.controller.core import CommandResult
from jarn.tui import palette

if TYPE_CHECKING:
    from jarn.controller.core import Controller


def cmd_config(ctrl: Controller, args: str) -> CommandResult:
    """View or edit settings. ``/config`` lists them; ``/config get <key>``
    shows one; ``/config set <key> <value>`` persists to the global config."""
    from jarn.config import settings

    parts = args.strip().split(maxsplit=2)
    if not parts or not parts[0]:
        return CommandResult(settings.format_settings(ctrl.config))
    sub = parts[0]
    if sub == "get":
        if len(parts) < 2:
            return CommandResult("Usage: /config get <key>")
        key = parts[1]
        spec = settings.setting_for(key)
        if spec is None:
            return CommandResult(f"Unknown setting {key!r}. Run /config to list keys.")
        val = settings.get_value(ctrl.config, key)
        shown = "(unset)" if val is None or val == "" else str(val)
        tail = ""
        if spec.choices:
            opts = ", ".join(c if c else "(none)" for c in spec.choices)
            tail = f"  [{palette.C_DIM}](choices: {opts})[/{palette.C_DIM}]"
        return CommandResult(f"{key} = {shown}{tail}")
    if sub == "set":
        if len(parts) < 3:
            return CommandResult("Usage: /config set <key> <value>")
        return ctrl._config_set(parts[1], parts[2])
    return CommandResult(
        "Usage: /config  |  /config get <key>  |  /config set <key> <value>"
    )

def cmd_preset(ctrl, args: str) -> CommandResult:
    """Expand a preset — a launch-time shortcut that sets mode + OS sandbox +
    network at once — and echo exactly what it set. /mode and /sandbox remain
    the live axes; /preset is optional sugar."""
    from jarn.config.loader import ConfigError
    from jarn.config.profiles import PROFILE_NAMES, resolve_effective_profile

    available = ", ".join(sorted(PROFILE_NAMES))
    if not args.strip():
        current = ctrl.config.policy.profile or "none"
        return CommandResult(
            f"Current preset: {current}. Available: {available}. "
            "A preset bundles mode + OS sandbox + network into one pick."
        )
    choice = args.strip()
    # resolve_effective_profile expands the chosen preset (raising on an
    # unknown name) AND clamps untrusted sessions to the floor — a single
    # apply path, so the REPL can never loosen an untrusted session.
    try:
        effective = resolve_effective_profile(
            ctrl.config, project_trusted=ctrl.project_trusted, cli_profile=choice
        )
    except ConfigError:
        return CommandResult(f"Unknown preset {choice!r}. Choose one of: {available}")
    ctrl.config.policy.profile = effective or ""
    ctrl.engine.mode = ctrl.config.permission_mode
    ctrl.runtime = None  # mode/sandbox/web-tools changes require a rebuild
    # Echo the expansion so the user sees what the preset actually set.
    expansion = (
        f"mode={ctrl.config.permission_mode.value}, "
        f"backend={ctrl.config.execution.backend}, "
        f"sandbox={ctrl.config.execution.local_sandbox}, "
        f"network={'on' if ctrl.config.execution.sandbox_allow_network else 'off'}"
    )
    suffix = ""
    if effective != choice:
        suffix = f" (clamped to {effective} — project untrusted)"
    return CommandResult(
        f"preset '{effective}'{suffix} → {expansion} (rebuilding).", rebuilt=True
    )

def cmd_profile(ctrl: Controller, args: str) -> CommandResult:
    """Deprecated alias of /preset (kept working for back-compat)."""
    result = cmd_preset(ctrl, args)
    return CommandResult(
        f"(/profile is deprecated and will be removed in v0.6.0 — use /preset.) {result.text}",
        rebuilt=result.rebuilt,
    )

def cmd_sandbox(ctrl, args: str) -> CommandResult:
    current = ctrl.config.execution.backend
    if not args.strip():
        return CommandResult(
            f"Execution backend: {current} · isolation: {ctrl.isolation_level()}. "
            "Use /sandbox docker|on|off."
        )
    # Untrusted projects can't weaken isolation at runtime (defence in depth
    # alongside the untrusted-mode floor); viewing it (no-arg) stays allowed.
    if not ctrl.project_trusted:
        return CommandResult(
            "Project untrusted — execution backend is locked. "
            "Run `jarn trust` to change it."
        )
    choice = args.strip().lower()
    if choice == "docker":
        ctrl.config.execution.backend = "docker"
    elif choice in ("on", "sandbox"):
        ctrl.config.execution.backend = "sandbox"
    elif choice in ("off", "local"):
        ctrl.config.execution.backend = "local"
    else:
        return CommandResult("Usage: /sandbox docker|on|off")
    ctrl.runtime = None  # backend changes require a rebuild
    return CommandResult(
        f"Execution backend set to {ctrl.config.execution.backend} (rebuilding). "
        "A sandbox/docker backend requires an available runtime; fails closed "
        "unless execution.allow_local_fallback is true.",
        rebuilt=True,
    )

def cmd_model(ctrl, args: str) -> CommandResult:
    if not args.strip():
        return CommandResult(f"Current model: {ctrl.config.resolved_main_model()}")
    ctrl.config.routing.main = args.strip()
    ctrl.config.default_model = args.strip()
    ctrl.runtime = None  # force rebuild on next turn
    return CommandResult(f"Model set to {args.strip()} (rebuilding).", rebuilt=True)

def cmd_mode(ctrl, args: str) -> CommandResult:
    if not args.strip():
        return CommandResult(f"Current mode: {ctrl.config.permission_mode.value}")
    try:
        mode = PermissionMode(args.strip())
    except ValueError:
        valid = ", ".join(m.value for m in PermissionMode)
        return CommandResult(f"Unknown mode. Choose one of: {valid}")
    # Route through apply_mode so the untrusted-floor clamp applies here too.
    applied = ctrl.apply_mode(mode.value)
    if applied != mode.value:
        return CommandResult(
            f"Project untrusted — mode clamped to {applied}. "
            "Run `jarn trust` to unlock other modes. (rebuilding)",
            rebuilt=True,
        )
    return CommandResult(f"Permission mode set to {applied} (rebuilding).", rebuilt=True)

def cmd_trust(ctrl, args: str) -> CommandResult:
    """Trust the current project root and lift the untrusted review-only floor.

    Persists the trust grant via :class:`~jarn.config.trust.TrustStore`,
    flips ``project_trusted`` on, re-resolves the effective policy profile so
    the review-only clamp no longer applies, and forces a runtime rebuild.

    Honesty note: capability-granting keys (``hooks``/``mcp_servers``/
    ``providers``/…) were stripped from the in-memory config at LOAD time for
    an untrusted project; lifting the floor here cannot retroactively
    re-inject them, so we tell the user they take effect on the next launch.
    """
    if ctrl.project_root is None:
        return CommandResult("No project root — nothing to trust.")
    if ctrl.project_trusted:
        return CommandResult("This project is already trusted.")

    # Read the project config once: fingerprint the exact on-disk bytes and
    # re-verify they haven't changed before recording trust (TOCTOU guard).
    # The same parsed dict is then passed to load_config so the fingerprinted
    # content and the loaded content are identical.
    root = ctrl.project_root
    from jarn.config.trust import (
        TrustStore,
        commit_trust_if_unchanged,
        fingerprint,
        parse_project_config,
        project_config_bytes,
        project_dangerous,
    )

    store = TrustStore.load()
    raw_bytes = project_config_bytes(root)
    if raw_bytes is None:
        # No project config: nothing dangerous to fingerprint, but the user
        # still wants to lift the untrusted floor. Trust at the empty
        # fingerprint and reload from the global tier only.
        project_raw: dict[str, Any] = {}
        danger: dict[str, Any] = {}
        store.trust(root, fingerprint({}))
        store.save()
    else:
        project_raw = parse_project_config(raw_bytes, root)
        danger = project_dangerous(project_raw)
        err = commit_trust_if_unchanged(store, root, raw_bytes, project_raw)
        if err is not None:
            return CommandResult(f"{err}")

    ctrl.project_trusted = True
    # RELOAD the config from the already-read project tier now that the
    # project is trusted. A simple re-resolve can't fix this: the launch-time
    # untrusted floor already OVERWROTE config.permission_mode with the
    # review-only clamp (plan) and the loader had stripped the project's
    # capability keys. Reloading with project_trusted=True (and the same
    # project_raw) restores both the configured mode and the project
    # hooks / MCP / providers, so the rebuilt runtime is genuinely unlocked.
    from jarn.config.loader import load_config
    from jarn.config.profiles import resolve_effective_profile

    ctrl.config = load_config(
        project_root=root, project_trusted=True, project_raw=project_raw
    )
    resolve_effective_profile(ctrl.config, project_trusted=True, cli_profile=None)
    ctrl.engine.mode = ctrl.config.permission_mode
    ctrl.engine.rules = ctrl.config.permissions
    ctrl.runtime = None  # rebuild so trust-gated state is reapplied

    note = ""
    if danger:
        note = (
            "\n[dim]Project hooks / MCP servers / providers from "
            ".jarn/config.yaml are now active.[/dim]"
        )
    return CommandResult(
        f"Trusted {root}. Review-only floor lifted; "
        f"mode is now {ctrl.config.permission_mode.value} (rebuilding).{note}",
        rebuilt=True,
    )
