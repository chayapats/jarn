"""Session rollback and memory helpers for :class:`~jarn.controller.core.Controller`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from jarn.agent.session import SuggestedMemory, SuggestedSkill

if TYPE_CHECKING:
    from jarn.controller.core import Controller


def save_suggested_memory(
    ctrl: Controller, suggestion: SuggestedMemory
) -> tuple[bool, str]:
    from jarn.controller.commands.memory import save_suggested_memory as _save

    return _save(ctrl, suggestion)


def save_suggested_skill(
    ctrl: Controller, suggestion: SuggestedSkill
) -> tuple[bool, str]:
    """Persist an agent-suggested (and user-approved) skill under the active root.

    Writes ``<project_root>/.jarn/skills/<slug>/SKILL.md``. Refused when there is
    no project root or the project is untrusted — same trust gate as project
    skill loading. Returns ``(saved, message)`` for the approver to report.
    """
    from jarn.extensibility.skills import load_skills, write_skill

    name = suggestion.name.strip()
    if not name:
        return False, "Skill has no name; nothing saved."
    if "/" in name or "\\" in name:
        return False, "Skill name must not contain path separators."
    if ctrl.project_root is None:
        return False, "No project root found; cannot save a skill."
    if not ctrl.project_trusted:
        return False, (
            "Project skills are disabled until this project is trusted "
            "(`jarn trust`)."
        )
    description = suggestion.description.strip() or name
    body = suggestion.body.strip() or description
    trigger = suggestion.trigger.strip() or "auto"
    try:
        path = write_skill(
            ctrl.project_root,
            name=name,
            description=description,
            body=body,
            trigger=trigger,
        )
    except ValueError as exc:
        return False, str(exc)
    # Refresh the in-session catalog so /skills and /skill see the new file.
    if ctrl.runtime is not None:
        ctrl.runtime.skills = load_skills(
            ctrl.project_root,
            project_trusted=ctrl.project_trusted,
            read_claude_dir=ctrl.config.compat.read_claude_dir,
        )
    return True, f"Saved skill: {path.relative_to(ctrl.project_root)}"


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
