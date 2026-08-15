"""Adaptive bottom toolbar for the inline REPL."""

from __future__ import annotations

from dataclasses import dataclass

from prompt_toolkit.formatted_text import HTML

from jarn.cost import BudgetStatus
from jarn.tui import grammar, palette


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dataclass(frozen=True, slots=True)
class ToolbarSegment:
    html: str
    priority: int  # lower = kept longer when width is tight
    width: int
    order: int = 0


def _sep() -> str:
    return palette.styled_fg(palette.TOOLBAR_SEP, " | ")


def _ctx_color(frac: float) -> str:
    return {
        "ok": palette.CTX_OK,
        "warn": palette.CTX_WARN,
        "hot": palette.CTX_HOT,
        "exceeded": palette.CTX_EXCEEDED,
    }[grammar.context_level(frac)]


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
    cwd: str = "",
    provider: str = "",
    auth: str = "",
    reasoning: str = "",
    trusted: bool = True,
    queue_count: int = 0,
    context_frac: float | None = None,
    context_used: int | None = None,
    context_window: int | None = None,
    elapsed_s: float | None = None,
    context_bar: bool = True,
    compact_count: int = 0,
    title: str = "",
    width: int = 120,
) -> HTML:
    """Compose toolbar HTML; drop low-priority segments on narrow terminals."""
    mcolor = palette.MODE_COLOR.get(mode, palette.ACCENT)
    glyph = palette.MODE_GLYPH.get(mode, palette.MODE_GLYPH["ask"])
    yolo = mode == "yolo"

    order = 0
    segments: list[ToolbarSegment] = [
        ToolbarSegment(
            palette.styled_fg(palette.ACCENT, _esc(model), bold=True),
            priority=0,
            width=len(model) + 2,
            order=order,
        ),
    ]
    order += 1
    segments.append(
        ToolbarSegment(
            palette.styled_fg(mcolor, f"{glyph} {mode}", bold=True),
            priority=0 if yolo else 1,
            width=len(mode) + 3,
            order=order,
        )
    )
    order += 1
    if yolo:
        badge = f"{grammar.GLYPH_WARN} YOLO"
        segments.append(
            ToolbarSegment(
                palette.styled_fg(palette.C_ERROR, _esc(badge), bold=True),
                priority=-1,
                width=len(badge) + 2,
                order=order,
            )
        )
        order += 1
    if cwd:
        label = f"cwd {cwd}"
        segments.append(
            ToolbarSegment(
                palette.styled_fg(palette.C_DIM, _esc(label)),
                priority=2,
                width=len(label) + 2,
                order=order,
            )
        )
        order += 1
    if provider:
        label = provider if not auth else f"{provider} · {auth}"
        segments.append(
            ToolbarSegment(
                palette.styled_fg(palette.C_NOTICE, _esc(label)),
                priority=3,
                width=len(label) + 2,
                order=order,
            )
        )
        order += 1
    if reasoning:
        label = f"reasoning {reasoning}"
        segments.append(
            ToolbarSegment(
                palette.styled_fg(palette.C_DIM, _esc(label)),
                priority=4,
                width=len(label) + 2,
                order=order,
            )
        )
        order += 1
    if trusted:
        trust_label = f"{grammar.GLYPH_OK} trusted"
        segments.append(
            ToolbarSegment(
                palette.styled_fg(palette.C_SUCCESS, _esc(trust_label)),
                priority=5,
                width=len(trust_label) + 2,
                order=order,
            )
        )
    else:
        trust_label = f"{grammar.GLYPH_WARN} untrusted · jarn trust"
        segments.append(
            ToolbarSegment(
                palette.styled_fg(palette.C_WARN, _esc(trust_label)),
                priority=5,
                width=len(trust_label) + 2,
                order=order,
            )
        )
    order += 1
    if queue_count > 0:
        label = f"queue {queue_count}"
        segments.append(
            ToolbarSegment(
                palette.styled_fg(palette.C_NOTICE, _esc(label)),
                priority=6,
                width=len(label) + 2,
                order=order,
            )
        )
        order += 1
    if compact_count > 0:
        label = f"compact {compact_count}"
        segments.append(
            ToolbarSegment(
                palette.styled_fg(palette.C_NOTICE, _esc(label)),
                priority=6,
                width=len(label) + 2,
                order=order,
            )
        )
        order += 1
    if context_frac is not None:
        color = _ctx_color(context_frac)
        if (
            context_bar
            and context_used is not None
            and context_window is not None
            and width >= grammar.TOOLBAR_COMPACT_MIN
        ):
            gauge = (
                f"{grammar.format_tokens(context_used)}/"
                f"{grammar.format_tokens(context_window)} "
                f"{grammar.context_bar(context_frac)} "
                f"{context_frac * 100:.0f}%"
            )
        else:
            gauge = f"ctx {context_frac * 100:.0f}%"
        segments.append(
            ToolbarSegment(
                palette.styled_fg(color, _esc(gauge)),
                priority=7,
                width=len(gauge) + 2,
                order=order,
            )
        )
        order += 1
    if elapsed_s is not None:
        label = grammar.format_duration(elapsed_s)
        segments.append(
            ToolbarSegment(
                palette.styled_fg(palette.C_DIM, _esc(label)),
                priority=8,
                width=len(label) + 2,
                order=order,
            )
        )
        order += 1
    segments.append(
        ToolbarSegment(
            palette.styled_fg(_cost_color(cost_status), _esc(cost_line)),
            priority=9,
            width=len(cost_line) + 2,
            order=order,
        )
    )
    order += 1
    if title:
        # Pinned right (high order). Dropped before the fill bar (priority > 7)
        # and first among {YOLO, model, bar, title}.
        shown = title if len(title) <= 24 else title[:23] + "…"
        segments.append(
            ToolbarSegment(
                palette.styled_fg(palette.C_DIM, _esc(shown)),
                priority=8,
                width=len(shown) + 2,
                order=order,
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
    kept.sort(key=lambda s: s.order)

    parts = [f" {seg.html} " for seg in kept]
    body = sep.join(parts) if parts else " "
    return HTML(body)
