"""Adaptive bottom toolbar for the inline REPL."""

from __future__ import annotations

from dataclasses import dataclass

from prompt_toolkit.formatted_text import HTML

from jarn.cost import BudgetStatus
from jarn.tui import palette


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dataclass(frozen=True, slots=True)
class ToolbarSegment:
    html: str
    priority: int  # lower = kept longer when width is tight
    width: int


def _sep() -> str:
    return palette.styled_fg(palette.TOOLBAR_SEP, " | ")


def _ctx_color(frac: float) -> str:
    if frac < 0.70:
        return palette.CTX_OK
    if frac < 0.90:
        return palette.CTX_WARN
    return palette.CTX_EXCEEDED


def _cost_color(status: BudgetStatus) -> str:
    return {
        BudgetStatus.OK: palette.COST_OK,
        BudgetStatus.WARN: palette.COST_WARN,
        BudgetStatus.EXCEEDED: palette.COST_EXCEEDED,
    }[status]


def render_toolbar(
    *,
    model: str,
    mode: str,
    cost_line: str,
    cost_status: BudgetStatus,
    trusted: bool = True,
    queue_count: int = 0,
    context_frac: float | None = None,
    width: int = 120,
) -> HTML:
    """Compose toolbar HTML; drop low-priority segments on narrow terminals."""
    mcolor = palette.MODE_COLOR.get(mode, palette.ACCENT)
    glyph = palette.MODE_GLYPH.get(mode, "◆")

    segments: list[ToolbarSegment] = [
        ToolbarSegment(
            palette.styled_fg(palette.ACCENT, _esc(model), bold=True),
            priority=0,
            width=len(model) + 2,
        ),
        ToolbarSegment(
            palette.styled_fg(mcolor, f"{glyph} {mode}", bold=True),
            priority=1,
            width=len(mode) + 3,
        ),
    ]
    if trusted:
        trust_label = "\U0001f512 trusted"
        segments.append(
            ToolbarSegment(
                palette.styled_fg(palette.C_SUCCESS, _esc(trust_label)),
                priority=2,
                width=len(trust_label) + 2,
            )
        )
    else:
        trust_label = "⚠ untrusted · jarn trust"
        segments.append(
            ToolbarSegment(
                palette.styled_fg(palette.C_WARN, _esc(trust_label)),
                priority=2,
                width=len(trust_label) + 2,
            )
        )
    if queue_count > 0:
        label = f"queue {queue_count}"
        segments.append(
            ToolbarSegment(
                palette.styled_fg(palette.C_NOTICE, _esc(label)),
                priority=3,
                width=len(label) + 2,
            )
        )
    if context_frac is not None:
        ctx = f"ctx {context_frac * 100:.0f}%"
        segments.append(
            ToolbarSegment(
                palette.styled_fg(_ctx_color(context_frac), _esc(ctx)),
                priority=4,
                width=len(ctx) + 2,
            )
        )
    segments.append(
        ToolbarSegment(
            palette.styled_fg(_cost_color(cost_status), _esc(cost_line)),
            priority=5,
            width=len(cost_line) + 2,
        )
    )

    sep = _sep()
    sep_w = 3
    budget = max(20, width - 2)
    kept: list[ToolbarSegment] = []
    used = 0
    for seg in sorted(segments, key=lambda s: s.priority):
        need = seg.width + (sep_w if kept else 0)
        if used + need <= budget or not kept:
            if kept:
                used += sep_w
            kept.append(seg)
            used += seg.width
    kept.sort(key=lambda s: s.priority)

    parts = [f" {seg.html} " for seg in kept]
    body = sep.join(parts) if parts else " "
    return HTML(body)
