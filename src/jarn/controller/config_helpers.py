"""Config mutation helpers for :class:`~jarn.controller.core.Controller`."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from rich.markup import escape as _escape_markup

from jarn.config.schema import PermissionMode

if TYPE_CHECKING:
    from jarn.controller.core import CommandResult, Controller


def _config(ctrl: Controller) -> dict:
    return {"configurable": {"thread_id": ctrl.thread_id}}


def _invalidate_model_cache(ctrl: Controller) -> None:
    """Clear cached chat models when config or secrets change."""
    runtime = ctrl.runtime
    if runtime is None:
        return
    factory = getattr(runtime, "factory", None)
    if factory is not None:
        factory.invalidate_cache()


def _apply_reloaded_config(ctrl: Controller) -> None:
    """Re-sync engine + fallback candidates after ``ctrl.config`` is replaced
    (e.g. by ``/config set`` or ``/trust``) and force a runtime rebuild.

    Honours the untrusted floor: an untrusted project can never end up more
    permissive than ``plan`` via a reload.
    """
    ctrl.engine.mode = ctrl.config.permission_mode
    ctrl.engine.rules = ctrl.config.permissions
    if (
        not ctrl.project_trusted
        and ctrl.config.permission_mode.rank > PermissionMode.PLAN.rank
    ):
        ctrl.config.permission_mode = PermissionMode.PLAN
        ctrl.engine.mode = PermissionMode.PLAN
    main = ctrl.config.resolved_main_model()
    ctrl._candidates = ([main] if main else []) + list(ctrl.config.routing.fallback)
    ctrl._candidate_idx = 0
    _invalidate_model_cache(ctrl)
    ctrl.runtime = None  # rebuild with the new config


def set_setting(ctrl: Controller, key: str, raw: str) -> tuple[bool, str]:
    """Coerce + validate + persist a setting; apply it live. Returns
    ``(ok, message)``. Shared by ``/config set`` and the interactive panel.

    On success: writes to the global ``~/.jarn/config.yaml`` (comments
    preserved), reloads + re-validates the merged config (rolling the file
    back if the result is invalid), and applies it to the running session.
    """
    from jarn.config import paths, settings
    from jarn.config.consistency import check_consistency
    from jarn.config.loader import ConfigError, load_config

    try:
        value = settings.coerce(key, raw)
    except settings.SettingError as exc:
        return False, str(exc)
    store = settings.ConfigStore(paths.global_config_path())
    backup = store.read_text()
    try:
        store.set(key, value)
    except settings.ConfigCorruptError as exc:
        # Corrupt global config: do not wipe it. Surface the repair hint
        # (the message names the .bak backup). The file is left untouched.
        return False, str(exc)
    try:
        new_cfg = load_config(
            project_root=ctrl.project_root, project_trusted=ctrl.project_trusted
        )
    except ConfigError as exc:
        store.restore(backup)   # never leave the config broken on disk
        return False, f"Rejected — invalid value: {exc}"
    # Cross-setting consistency. Block only conflicts this edit *introduces*:
    # the changed key must take part in the conflict, and that key must not
    # have already been tangled in one (so a pre-existing, hand-edited
    # contradiction neither blocks unrelated edits nor traps the user from
    # editing their way out of it).
    new_errors, new_warnings = check_consistency(new_cfg)
    prior_keys = {k for e in check_consistency(ctrl.config)[0] for k in e.keys}
    introduced = [e for e in new_errors
                  if e.involves(key) and key not in prior_keys]
    if introduced:
        store.restore(backup)
        return False, f"Rejected — {introduced[0].message}"
    ctrl.config = new_cfg
    _apply_reloaded_config(ctrl)
    shown = "(none)" if value == "" else value
    msg = f"saved {key} = {shown}"
    note = next((w.message for w in new_warnings if w.involves(key)), None)
    if note:
        msg += f"  ⚠ {note}"
    return True, msg


def _config_set(ctrl: Controller, key: str, raw: str) -> CommandResult:
    from jarn.controller.core import CommandResult

    ok, msg = set_setting(ctrl, key, raw)
    if ok:
        return CommandResult(f"{msg} → ~/.jarn/config.yaml (rebuilding).", rebuilt=True)
    return CommandResult(_escape_markup(msg))


def set_provider_key(
    ctrl: Controller, raw_key: str, *, provider: str | None = None
) -> CommandResult:
    """Set the API key for ``provider`` (defaults to the current provider).

    The secret is stored in the OS keychain when available, otherwise under
    ``~/.jarn/secrets/``. The provider's ``api_key`` is pointed at the
    resulting ``keychain:`` or ``file:`` reference — never inlined into
    committed config. The reference is persisted to the global config so it
    survives a restart, and the runtime is dropped so the next turn rebuilds
    with the new key."""
    from jarn.config import paths
    from jarn.config.defaults import PROVIDER_ENV_VARS
    from jarn.config.secrets import file_fallback_notice, store_secret
    from jarn.config.settings import ConfigStore
    from jarn.controller.core import CommandResult

    prov = (provider or ctrl.current_provider() or "").strip()
    if not prov:
        return CommandResult(
            "No active provider to set a key for — configure a model first "
            "with /model or run jarn setup."
        )
    if prov not in ctrl.config.providers:
        return CommandResult(
            f"Provider {prov!r} isn't configured. Run jarn setup to add it, "
            "then use /key to update its API key."
        )
    secret = raw_key.strip()
    if not secret:
        return CommandResult("No key entered — unchanged.")

    try:
        stored = store_secret("jarn", prov, secret)
    except ValueError as exc:
        return CommandResult(f"Couldn't store the key: {_escape_markup(str(exc))}")
    ref = stored.reference
    # Update the live config and persist the *reference* (not the secret) so a
    # restart still finds the key. Best-effort persistence: even if the file
    # write fails the in-session key already works.
    ctrl.config.providers[prov].api_key = ref
    # Persistence is best-effort: even if the file write fails the in-session
    # key already works.
    with contextlib.suppress(Exception):
        ConfigStore(paths.global_config_path()).set(f"providers.{prov}.api_key", ref)
    _invalidate_model_cache(ctrl)
    ctrl.runtime = None  # force rebuild on next turn with the new key
    notice = file_fallback_notice(
        stored,
        provider=prov,
        env_var=PROVIDER_ENV_VARS.get(prov),
    )
    if notice:
        return CommandResult(
            f"Updated the {prov} API key.\n\n{notice}\n\nRebuilding on the next turn.",
            rebuilt=True,
        )
    return CommandResult(
        f"Updated the {prov} API key (stored in the OS keychain). "
        "Rebuilding on the next turn.",
        rebuilt=True,
    )
