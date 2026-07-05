"""Built-in /session slash-command handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.markup import escape as _escape_markup

from jarn.controller.core import CommandResult

if TYPE_CHECKING:
    from jarn.controller.core import Controller


def cmd_sessions(ctrl: Controller, args: str) -> CommandResult:
    sessions = ctrl.sessions.list()
    if not sessions:
        return CommandResult("No previous sessions.")
    lines = ["[b]Recent sessions[/b] [dim](use /resume to pick one)[/dim]"]
    for s in sessions:
        marker = "→ " if s.thread_id == ctrl.thread_id else "  "
        lines.append(
            f"{marker}{s.updated_human}  {_escape_markup(s.title)}  "
            f"[dim]{s.thread_id[:8]}[/dim]"
        )
    return CommandResult("\n".join(lines))


def cmd_clear(ctrl: Controller, args: str) -> CommandResult:
    ctrl.new_thread()
    return CommandResult("Started a fresh conversation.", clear_screen=True)


def cmd_compact(ctrl: Controller, args: str) -> CommandResult:
    sub = args.strip().lower()
    if sub and sub != "status":
        return CommandResult(
            f"Unknown /compact subcommand: {sub!r}. Try /compact status."
        )
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


def cmd_undo(ctrl: Controller, args: str) -> CommandResult:
    """Revert the last agent turn's file changes via the checkpoint stack.

    Capturing the current state as a redo-point first guarantees that undo
    is itself reversible: the user can always /redo to get back here.
    """
    if not ctrl.checkpoint_manager.enabled:
        return CommandResult(
            "No checkpoints — /undo needs autocheckpoint. "
            "Enable it with /config (git.autocheckpoint: true) or 'jarn config'."
        )
    result = ctrl.checkpoint_manager.undo()
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
    result = ctrl.checkpoint_manager.redo()
    if result.ok:
        return CommandResult(f"Redone. {result.message}")
    return CommandResult(f"Cannot redo: {result.message}")


def cmd_quit(ctrl: Controller, args: str) -> CommandResult:
    return CommandResult("Bye.", quit=True)
