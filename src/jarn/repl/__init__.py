"""The terminal front-end (``jarn``) — a Claude-Code-style persistent app.

A **prompt_toolkit** :class:`Application` (``full_screen=False``) pins the input
box at the bottom of the *normal* terminal buffer while all conversation output
is printed *above* it — through :func:`patch_stdout` — into the terminal's native
scrollback (one scroll for everything; native selection/copy). The in-progress
assistant paragraph previews in a small region just above the input.

The agent turn runs as a **cancellable task**, so **Esc** (or Ctrl+C) interrupts
mid-stream while the input stays live. Enter sends, Shift+Enter (Ctrl+J) inserts a
newline, Shift+Tab cycles the permission mode, Tab completes ``/commands`` and
``@`` mentions (``@file``, ``@folder:``, ``@symbol:``), ↑/↓ navigate history,
Ctrl+O expands the last tool output. Approvals
and pickers are app-native (captured through the same input). It reuses the
UI-agnostic :class:`~jarn.tui.controller.Controller` and
:class:`~jarn.agent.session.SessionDriver`.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from jarn.config.schema import Config
from jarn.repl import turn
from jarn.repl.app import InlineApp
from jarn.repl.auth_errors import _friendly_auth_error, _provider_hint
from jarn.repl.completer import _ShellEscapeLexer
from jarn.repl.turn import (
    _EDIT_BEFORE_APPLY,
    _EDIT_MEMORY,
    _VIEW_FULL_DIFF,
    _apply_mode_ref,
    _apply_model_ref,
    _approval_options,
    _approve,
    _edit_text_in_editor,
    _editable_field,
    _run_turn,
)
from jarn.tui import palette


def _resolve_theme(config: Config) -> str:
    """Return the palette name to apply for ``config.ui.theme``.

    When theme is ``"auto"``, run the OSC-11 terminal-background probe
    **before** prompt_toolkit's Application starts so we still own the tty.
    Falls back to ``"dark"`` when the terminal does not reply (non-tty, CI,
    unresponsive terminal).

    Must be called after :func:`~jarn.tui.keyfix.apply_repl_keyfix` (which
    pops any stray kitty flags) but before the PT Application is created.
    """
    if config.ui.theme != "auto":
        return config.ui.theme
    from jarn.tui.termbg import detect
    detected = detect()
    return detected if detected in ("light", "dark") else "dark"


def run_inline(
    config: Config,
    project_root: Path | None,
    *,
    resume: bool = False,
    project_trusted: bool = True,
) -> int:
    """CLI entry point for native inline mode."""
    from jarn.tui.keyfix import apply_repl_keyfix

    apply_repl_keyfix()
    resolved_theme = _resolve_theme(config)
    palette.configure_ui(theme=resolved_theme, accent=config.ui.accent)
    # Cache the startup background detection so a runtime ``/theme auto`` reuses it
    # instead of re-probing (a runtime OSC-11 probe races prompt_toolkit's input
    # reader). Only meaningful when startup theme is "auto"; else the resolved
    # value is a fixed palette name, not a detected background.
    detected_theme = resolved_theme if config.ui.theme == "auto" else None
    with contextlib.suppress(KeyboardInterrupt, EOFError):
        asyncio.run(
            InlineApp(
                config,
                project_root,
                resume=resume,
                project_trusted=project_trusted,
                detected_theme=detected_theme,
            ).run()
        )
    return 0


__all__ = [
    "InlineApp",
    "turn",
    "_EDIT_BEFORE_APPLY",
    "_EDIT_MEMORY",
    "_ShellEscapeLexer",
    "_VIEW_FULL_DIFF",
    "_apply_mode_ref",
    "_apply_model_ref",
    "_approval_options",
    "_approve",
    "_editable_field",
    "_edit_text_in_editor",
    "_friendly_auth_error",
    "_provider_hint",
    "_resolve_theme",
    "_run_turn",
    "run_inline",
]
