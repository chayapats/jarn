"""Built-in /session slash-command handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.markup import escape as _escape_markup

from jarn.commands.help import usage_error
from jarn.controller.core import CommandResult

if TYPE_CHECKING:
    from jarn.agent.checkpoint import RestorePreview
    from jarn.controller.core import Controller


def cmd_sessions(ctrl: Controller, args: str) -> CommandResult:
    sessions = ctrl.sessions.list()
    if not sessions:
        return CommandResult("No previous sessions.")
    lines = ["[b]Recent sessions[/b] [dim](use /resume to pick one)[/dim]"]
    for s in sessions:
        marker = "→ " if s.thread_id == ctrl.thread_id else "  "
        project = f"  [dim]{_escape_markup(s.project_root)}[/dim]" if s.project_root else ""
        model = f"  [dim]{_escape_markup(s.model)}[/dim]" if s.model else ""
        state = "complete" if s.state == "complete" else "interrupted"
        lines.append(
            f"{marker}{s.updated_human}  {_escape_markup(s.title)}  "
            f"[dim]{s.thread_id[:8]} · {state}[/dim]{project}{model}"
        )
    return CommandResult("\n".join(lines))


def cmd_clear(ctrl: Controller, args: str) -> CommandResult:
    ctrl.new_thread()
    return CommandResult("Started a fresh conversation.", clear_screen=True)


def cmd_compact(ctrl: Controller, args: str) -> CommandResult:
    sub = args.strip().lower()
    if sub and sub != "status":
        return CommandResult(usage_error("compact", extra=f"Unknown /compact subcommand: {sub!r}."))
    ctx = ctrl.config.context
    if ctx.auto_compact:
        summarizer = (
            ctrl.config.resolved_summarizer_model()
            or ctrl.config.resolved_main_model()
            or "the main model"
        )
        main_model = ctrl.config.resolved_main_model() or "the main model"
        # Report the RESOLVED trigger, not the raw percentage: compact_at_pct only
        # bites when jarn knows the main model's window (else deepagents' 170k
        # token default applies and the percentage is inert).
        from jarn.agent.builder import resolved_auto_summarize_tokens

        tokens = resolved_auto_summarize_tokens(ctrl.config)
        if tokens is not None:
            trigger = (
                f"auto-summarize at ~{tokens:,} tokens "
                f"({ctx.compact_at_pct}% of the {main_model} window)"
            )
        else:
            trigger = (
                "auto-summarize at deepagents default (170k tokens) — "
                f"{main_model} window unknown, so context.compact_at_pct has no "
                "effect until the window is known"
            )
        auto = f"Auto-compaction is on: {trigger} (summarizer: {summarizer})."
    else:
        auto = "Auto-compaction is off."
    return CommandResult(
        auto + " Run /compact to summarize now and continue in a fresh thread, "
        "or /clear to start fresh without a summary."
    )


def format_undo_preview(preview: RestorePreview) -> str:
    """Render a checkpoint preview for any user-facing command frontend."""
    count = preview.file_count
    noun = "file" if count == 1 else "files"
    lines = [
        f"Undo preview — {count} affected {noun}",
        f"Checkpoint: {_escape_markup(preview.message)}",
    ]
    if preview.files:
        lines.append("Affected changes:")
        lines.extend(f"  {_escape_markup(line)}" for line in preview.files)
    else:
        lines.append("Affected changes: none (the working tree already matches).")
    return "\n".join(lines)


def _undo_unavailable(ctrl: Controller) -> CommandResult:
    return CommandResult(
        "No checkpoints — /undo needs autocheckpoint. "
        "Enable it with /config (git.autocheckpoint: true) or 'jarn config'."
    )


def cmd_undo(ctrl: Controller, args: str) -> CommandResult:
    """Preview `/undo` without mutating files.

    The synchronous command registry cannot collect an explicit confirmation,
    so it deliberately stops after the preview. Interactive frontends use the
    controller's async ``undo(confirm=...)`` API to perform the confirmed
    restore. This prevents a new frontend from silently bypassing the gate.
    """
    if not ctrl.checkpoint_manager.enabled:
        return _undo_unavailable(ctrl)
    preview = ctrl.checkpoint_manager.preview_undo(thread_id=ctrl.thread_id)
    if not preview.ok:
        return CommandResult(f"Cannot undo: {preview.message}")
    return CommandResult(
        format_undo_preview(preview)
        + "\nConfirmation required before restore; no files were changed."
    )


def cmd_undo_confirmed(ctrl: Controller, preview: RestorePreview) -> CommandResult:
    """Apply the exact restore represented by a user-confirmed ``preview``."""
    if not ctrl.checkpoint_manager.enabled:
        return _undo_unavailable(ctrl)
    result = ctrl.checkpoint_manager.undo(
        thread_id=ctrl.thread_id,
        expected_sha=preview.sha,
        expected_current_tree=preview.current_tree or None,
    )
    if result.ok:
        return CommandResult(f"Undone. {result.message}")
    return CommandResult(f"Cannot undo: {result.message}")


def cmd_redo(ctrl: Controller, args: str) -> CommandResult:
    """Re-apply the most recently undone agent turn's file changes."""
    if not ctrl.checkpoint_manager.enabled:
        return CommandResult(
            "No checkpoints — /redo needs autocheckpoint. "
            "Enable it with /config (git.autocheckpoint: true) or 'jarn config'."
        )
    result = ctrl.checkpoint_manager.redo(thread_id=ctrl.thread_id)
    if result.ok:
        return CommandResult(f"Redone. {result.message}")
    return CommandResult(f"Cannot redo: {result.message}")


def cmd_quit(ctrl: Controller, args: str) -> CommandResult:
    return CommandResult("Bye.", quit=True)
