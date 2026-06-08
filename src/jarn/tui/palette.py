"""Single source of UI colors used in Rich markup and Python.

Centralizes mode colors/glyphs, semantic colors, spinner frames, and the
"thinking" vocabulary so widgets and the app share one definition.

Call :func:`configure_ui` at session start (from ``config.ui``) so the
inline REPL matches the theme chosen during setup.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

ThemeName = Literal["dark", "light", "high-contrast"]

_ACCENT_COLORS: dict[str, str] = {
    "cyan": "#22d3ee",
    "blue": "#5a9bf0",
    "teal": "#37d9f0",
    "green": "#3ee07a",
    "yellow": "#fbbf24",
    "orange": "#ffb454",
    "red": "#ff6b6b",
    "magenta": "#e879f9",
    "white": "#e8f0f0",
}


@dataclass(frozen=True, slots=True)
class _Palette:
    mode_color: dict[str, str]
    c_user: str
    c_tool: str
    c_notice: str
    c_error: str
    c_warn: str
    c_success: str
    c_dim: str
    code_theme: str
    toolbar_bg: str
    toolbar_fg: str
    toolbar_sep: str
    accent: str
    cost_ok: str
    cost_warn: str
    cost_exceeded: str
    ctx_ok: str
    ctx_warn: str
    ctx_exceeded: str


_PALETTES: dict[ThemeName, _Palette] = {
    "dark": _Palette(
        mode_color={"plan": "#5a9bf0", "ask": "#22d3ee", "auto-edit": "#fbbf24", "yolo": "#ff6b6b"},
        c_user="#38e1ff",
        c_tool="#5fb8d8",
        c_notice="#37d9f0",
        c_error="#ff6b6b",
        c_warn="#ffb454",
        c_success="#3ee07a",
        c_dim="#7c8f94",
        code_theme="nord-darker",
        toolbar_bg="#10201f",
        toolbar_fg="#e8f0f0",
        toolbar_sep="#3f5559",
        accent="#22d3ee",
        cost_ok="#9fb3b8",
        cost_warn="#ffb454",
        cost_exceeded="#ff6b6b",
        ctx_ok="#9fb3b8",
        ctx_warn="#ffb454",
        ctx_exceeded="#ff6b6b",
    ),
    "light": _Palette(
        mode_color={"plan": "#2563eb", "ask": "#0891b2", "auto-edit": "#d97706", "yolo": "#dc2626"},
        c_user="#0e7490",
        c_tool="#0369a1",
        c_notice="#0d9488",
        c_error="#dc2626",
        c_warn="#d97706",
        c_success="#15803d",
        c_dim="#64748b",
        code_theme="default",
        toolbar_bg="#f1f5f9",
        toolbar_fg="#0f172a",
        toolbar_sep="#94a3b8",
        accent="#0891b2",
        cost_ok="#475569",
        cost_warn="#d97706",
        cost_exceeded="#dc2626",
        ctx_ok="#475569",
        ctx_warn="#d97706",
        ctx_exceeded="#dc2626",
    ),
    "high-contrast": _Palette(
        mode_color={"plan": "#00e5ff", "ask": "#00ffe1", "auto-edit": "#ffb454", "yolo": "#ff6b6b"},
        c_user="#ffffff",
        c_tool="#00e5ff",
        c_notice="#00ffe1",
        c_error="#ff6b6b",
        c_warn="#ffb454",
        c_success="#00ff66",
        c_dim="#b0b0b0",
        code_theme="monokai",
        toolbar_bg="#000000",
        toolbar_fg="#ffffff",
        toolbar_sep="#666666",
        accent="#00ffe1",
        cost_ok="#cccccc",
        cost_warn="#ffb454",
        cost_exceeded="#ff6b6b",
        ctx_ok="#cccccc",
        ctx_warn="#ffb454",
        ctx_exceeded="#ff6b6b",
    ),
}

_active: _Palette = _PALETTES["dark"]

MODE_COLOR = _active.mode_color
MODE_GLYPH = {"plan": "◇", "ask": "◆", "auto-edit": "⚡", "yolo": "⚠"}
C_USER = _active.c_user
C_TOOL = _active.c_tool
C_NOTICE = _active.c_notice
C_ERROR = _active.c_error
C_WARN = _active.c_warn
C_SUCCESS = _active.c_success
C_DIM = _active.c_dim
CODE_THEME = _active.code_theme
TOOLBAR_BG = _active.toolbar_bg
TOOLBAR_FG = _active.toolbar_fg
TOOLBAR_SEP = _active.toolbar_sep
ACCENT = _active.accent
COST_OK = _active.cost_ok
COST_WARN = _active.cost_warn
COST_EXCEEDED = _active.cost_exceeded
CTX_OK = _active.ctx_ok
CTX_WARN = _active.ctx_warn
CTX_EXCEEDED = _active.ctx_exceeded

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

THINKING_WORDS = [
    "Ruminating",
    "Pondering",
    "Cogitating",
    "Deliberating",
    "Mulling",
    "Contemplating",
    "Synthesizing",
    "Scheming",
    "Noodling",
    "Percolating",
    "Conjuring",
    "Untangling",
    "Marinating",
    "Spelunking",
    "Tinkering",
    "Wrangling",
]


def no_color() -> bool:
    return bool(os.environ.get("NO_COLOR"))


def toolbar_style_dict() -> dict[str, str]:
    """prompt_toolkit Style dict for the bottom toolbar."""
    if no_color():
        return {"bottom-toolbar": "noreverse"}
    return {"bottom-toolbar": f"bg:{TOOLBAR_BG} {TOOLBAR_FG} noreverse"}


def styled_fg(color: str, text: str, *, bold: bool = False) -> str:
    """HTML segment for prompt_toolkit, or plain text when ``NO_COLOR``."""
    if no_color():
        return text if not bold else text  # no bold in plain mode
    b_open = "<b>" if bold else ""
    b_close = "</b>" if bold else ""
    return f'<style fg="{color}">{b_open}{text}{b_close}</style>'


def apply_ui_theme(theme: str) -> None:
    """Switch module-level color constants to match ``config.ui.theme``."""
    configure_ui(theme=theme)


def configure_ui(*, theme: str = "dark", accent: str = "cyan") -> None:
    """Apply theme and optional brand accent from config."""
    global _active, MODE_COLOR, C_USER, C_TOOL, C_NOTICE, C_ERROR, C_WARN
    global C_SUCCESS, C_DIM, CODE_THEME, TOOLBAR_BG, TOOLBAR_FG, TOOLBAR_SEP
    global ACCENT, COST_OK, COST_WARN, COST_EXCEEDED, CTX_OK, CTX_WARN, CTX_EXCEEDED

    name = theme if theme in _PALETTES else "dark"
    base = _PALETTES[name]  # type: ignore[index]
    accent_color = _ACCENT_COLORS.get(accent.lower(), base.accent)
    _active = _Palette(
        mode_color=base.mode_color,
        c_user=base.c_user,
        c_tool=base.c_tool,
        c_notice=base.c_notice,
        c_error=base.c_error,
        c_warn=base.c_warn,
        c_success=base.c_success,
        c_dim=base.c_dim,
        code_theme=base.code_theme,
        toolbar_bg=base.toolbar_bg,
        toolbar_fg=base.toolbar_fg,
        toolbar_sep=base.toolbar_sep,
        accent=accent_color,
        cost_ok=base.cost_ok,
        cost_warn=base.cost_warn,
        cost_exceeded=base.cost_exceeded,
        ctx_ok=base.ctx_ok,
        ctx_warn=base.ctx_warn,
        ctx_exceeded=base.ctx_exceeded,
    )
    MODE_COLOR = _active.mode_color
    C_USER = _active.c_user
    C_TOOL = _active.c_tool
    C_NOTICE = _active.c_notice
    C_ERROR = _active.c_error
    C_WARN = _active.c_warn
    C_SUCCESS = _active.c_success
    C_DIM = _active.c_dim
    CODE_THEME = _active.code_theme
    TOOLBAR_BG = _active.toolbar_bg
    TOOLBAR_FG = _active.toolbar_fg
    TOOLBAR_SEP = _active.toolbar_sep
    ACCENT = _active.accent
    COST_OK = _active.cost_ok
    COST_WARN = _active.cost_warn
    COST_EXCEEDED = _active.cost_exceeded
    CTX_OK = _active.ctx_ok
    CTX_WARN = _active.ctx_warn
    CTX_EXCEEDED = _active.ctx_exceeded


def mode_label(mode: str) -> str:
    color = MODE_COLOR.get(mode, ACCENT)
    glyph = MODE_GLYPH.get(mode, "◆")
    return f"[{color}]{glyph} {mode}[/{color}]"
