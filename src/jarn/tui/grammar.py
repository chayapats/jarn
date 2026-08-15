"""Single source of truth for J.A.R.N. visual grammar.

Glyphs, spacing, width, context-pressure thresholds, tool-progress modes, and
the shortcut/legend copy that ``/help``, the splash, and the toolbar share.
Colors live in :mod:`jarn.tui.palette`; this module must not import it (palette
re-exports glyphs to keep existing ``palette.MODE_GLYPH`` call sites working).
"""

from __future__ import annotations

from typing import Literal

# ── Glyphs (frozen set — new glyphs need a spec amendment) ──────────────────

GLYPH_PROMPT = "›"
GLYPH_PLAY = "▶"  # plan / memory / skill notice titles
GLYPH_STEER = "»"
GLYPH_TOOL = "⏺"
GLYPH_RESULT = "⎿"
GLYPH_SUBAGENT = "┊"
GLYPH_THINKING = "✻"
GLYPH_OK = "✔"
GLYPH_FAIL = "✗"
GLYPH_WARN = "⚠"
GLYPH_KEY_OK = "●"
GLYPH_KEY_OFF = "○"
GLYPH_TODO_DONE = "✔"
GLYPH_TODO_RUN = "◐"
GLYPH_TODO_WAIT = "☐"
GLYPH_BAR_FILL = "█"
GLYPH_BAR_EMPTY = "░"
#: Host-shell banner. Same character as ``MODE_GLYPH["auto-edit"]``; that overlap
#: is intentional (both mean "runs without asking").
GLYPH_HOST_SHELL = "⚡"

MODE_GLYPH: dict[str, str] = {
    "plan": "◇",
    "ask": "◆",
    "auto-edit": "⚡",
    "yolo": "⚠",
}

HelpGroup = Literal["Work", "Setup", "Session"]

HELP_GROUP_ORDER: tuple[HelpGroup, ...] = ("Work", "Session", "Setup")

# ── Spacing ─────────────────────────────────────────────────────────────────

#: Consecutive items of the same kind (tool lines, help rows).
SPACE_TIGHT = 0
#: Between sections of one command page.
SPACE_SECTION = 1
#: Between user prompt, tool block, and assistant prose.
SPACE_BLOCK = 1
#: After a finished turn, before the next prompt — never two blanks.
SPACE_TURN = 1

HELP_NAME_WIDTH = 28
KV_LABEL_WIDTH = 14

# ── Width ───────────────────────────────────────────────────────────────────

#: Default wrap cap for committed markdown (0 = terminal width).
WRAP_AT = 120

TOOLBAR_FULL_MIN = 76
TOOLBAR_COMPACT_MIN = 52
CONTEXT_BAR_WIDTH = 10

# ── Context pressure (shared by toolbar, /context, /cost) ───────────────────

CTX_OK = 0.50
CTX_WARN = 0.80
CTX_HOT = 0.95

ContextLevel = Literal["ok", "warn", "hot", "exceeded"]


def context_level(frac: float) -> ContextLevel:
    if frac < CTX_OK:
        return "ok"
    if frac < CTX_WARN:
        return "warn"
    if frac < CTX_HOT:
        return "hot"
    return "exceeded"


def context_bar(frac: float, *, width: int = CONTEXT_BAR_WIDTH) -> str:
    """``[██████░░░░]`` fill for a 0–1 fraction."""
    n = max(1, width)
    clamped = max(0.0, min(1.0, frac))
    filled = int(round(clamped * n))
    filled = min(n, filled)
    return GLYPH_BAR_FILL * filled + GLYPH_BAR_EMPTY * (n - filled)


def format_tokens(n: int) -> str:
    """Compact token count: 12400 → ``12.4K``."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        value = n / 1000
        text = f"{value:.1f}".rstrip("0").rstrip(".")
        return f"{text}K"
    value = n / 1_000_000
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{text}M"


def format_duration(seconds: float) -> str:
    """Elapsed session time: ``45s``, ``12m``, ``1h 03m``."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m" if secs < 15 else f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


# ── Tool progress / reasoning ───────────────────────────────────────────────

ToolProgress = Literal["off", "new", "all", "verbose"]
TOOL_PROGRESS_VALUES: tuple[ToolProgress, ...] = ("off", "new", "all", "verbose")
TOOL_PROGRESS_DEFAULT: ToolProgress = "new"

ShowReasoning = Literal["collapsed", "full", "off"]
SHOW_REASONING_VALUES: tuple[ShowReasoning, ...] = ("collapsed", "full", "off")
SHOW_REASONING_DEFAULT: ShowReasoning = "collapsed"


def next_tool_progress(current: str) -> ToolProgress:
    values = TOOL_PROGRESS_VALUES
    try:
        idx = values.index(current)  # type: ignore[arg-type]
    except ValueError:
        return TOOL_PROGRESS_DEFAULT
    return values[(idx + 1) % len(values)]


# ── Shared copy ─────────────────────────────────────────────────────────────

SHORTCUTS: tuple[str, ...] = (
    "Tab complete",
    "↑/↓ history",
    "Shift+Tab mode",
    "Shift+Enter newline",
    "Ctrl+O or /expand last output",
    "Ctrl+V paste image (macOS)",
    "Esc cancel turn",
    "Ctrl+C twice to quit",
    f"! <cmd> run host shell ({GLYPH_FAIL} bypasses the agent)",
)

SHORTCUT_HINT = (
    "Type a message · /help for commands · Tab complete · "
    "Shift+Tab mode · Esc cancel"
)

HELP_COPY_HINT = "Copy: drag-select + ⌘C in your terminal (native scrollback)."


def shortcut_line() -> str:
    return " · ".join(SHORTCUTS)


def glyph_legend() -> str:
    """One-line legend for ``/help``. Glyphs come from this module only."""
    modes = " · ".join(
        f"{glyph} {name}" for name, glyph in MODE_GLYPH.items()
    )
    return (
        f"{modes} · {GLYPH_KEY_OK} key ok · {GLYPH_FAIL} key fail · "
        f"{GLYPH_TOOL} tool · {GLYPH_RESULT} result · "
        f"queue N = lines waiting while a turn runs"
    )
