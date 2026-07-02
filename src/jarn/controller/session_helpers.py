"""Session rollback and memory helpers for :class:`~jarn.controller.core.Controller`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from jarn.agent.session import SuggestedMemory

if TYPE_CHECKING:
    from jarn.controller.core import Controller


def save_suggested_memory(
    ctrl: Controller, suggestion: SuggestedMemory
) -> tuple[bool, str]:
    from jarn.controller.commands.memory import save_suggested_memory as _save

    return _save(ctrl, suggestion)


def abort_rollback(ctrl: Controller) -> str:
    """Roll back the working tree to the current turn's start checkpoint.

    Used by ``/abort`` *after* the turn has been cancelled in the REPL.
    The turn-start snapshot sits on top of the undo stack, so reverting it
    is exactly :meth:`CheckpointManager.undo`. Degrades gracefully when
    autocheckpoint is off (no checkpoint to roll back to) — mirroring the
    ``/undo`` wording that points the user at how to enable it.
    """
    if not ctrl.checkpoint_manager.enabled:
        return (
            "Turn cancelled. Rollback unavailable — /abort needs autocheckpoint. "
            "Enable it with /config (git.autocheckpoint: true) or 'jarn config'."
        )
    result = ctrl.checkpoint_manager.undo()
    if result.ok:
        return f"Turn cancelled and rolled back. {result.message}"
    return f"Turn cancelled. Cannot roll back: {result.message}"


def can_rollback_turn(ctrl: Controller) -> bool:
    """Whether a turn-start checkpoint is available to roll back to.

    Autocheckpoint snapshots the working tree before each agent turn (see
    ``SessionDriver._run``), so when autocheckpoint is on in a git repo there
    is a checkpoint ``/abort`` can revert to."""
    return ctrl.checkpoint_manager.enabled and ctrl.checkpoint_manager.is_repo


def cancel_edit_note(ctrl: Controller) -> str | None:
    """Message for an Esc/Ctrl+C cancel that left this turn's file edits on
    disk.

    Esc cancels the turn but does *not* revert edits (unlike ``/abort``).
    Return text that says edits remain and how to revert them, offering
    rollback when a turn-start checkpoint exists. Returns ``None`` only when
    nothing actionable can be said (no rollback path) — but we always at
    least point at ``/abort``, so this currently always returns a string.
    """
    if can_rollback_turn(ctrl):
        return (
            "Edits from this turn are still on disk. "
            "Run /abort to roll them back, or /undo later."
        )
    return (
        "Edits from this turn are still on disk. "
        "/abort can roll them back once autocheckpoint is on "
        "(enable it with /config: git.autocheckpoint: true)."
    )


def autocheckpoint_off_hint(ctrl: Controller) -> str | None:
    """Return a one-time per-session hint when autocheckpoint is off.

    Call after the agent writes a file.  Returns the hint string on the
    first call in a session; returns ``None`` on all subsequent calls (so
    callers can gate ``console.print`` on a truthy return value).
    """
    if ctrl.checkpoint_manager.enabled:
        return None
    if ctrl._autocheckpoint_hint_shown:
        return None
    ctrl._autocheckpoint_hint_shown = True
    return (
        "Hint: /undo is unavailable while autocheckpoint is off. "
        "Enable it with /config (git.autocheckpoint: true) or 'jarn config'."
    )
