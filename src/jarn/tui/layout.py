"""Rich-markup layout primitives — the only way command pages should be built.

Handlers return ``CommandResult(text)``; they compose that text here so spacing,
column width, and semantic colors cannot drift between ``/help``, ``/status``,
``/doctor``, and friends.

Telegram can reuse the same functions with ``dialect="html"``.
"""

from __future__ import annotations

from typing import Literal

from rich.markup import escape as _escape_rich

from jarn.tui import grammar, palette

Dialect = Literal["rich", "html"]


def escape(text: object, *, dialect: Dialect = "rich") -> str:
    value = "" if text is None else str(text)
    if dialect == "html":
        from jarn.telegram.htmlutil import escape_html

        return escape_html(value)
    return _escape_rich(value)


def paint(
    color: str,
    text: str,
    *,
    bold: bool = False,
    dialect: Dialect = "rich",
) -> str:
    """Wrap *text* (already escaped if needed) in a semantic color.

    HTML is Telegram ``parse_mode=HTML``: ``<b>``, ``<i>``, ``<code>`` only —
    no ``<span>`` and no inline CSS (Telegram strips them).
    """
    if dialect == "html":
        if bold or color in {palette.C_ERROR, palette.C_WARN, palette.C_SUCCESS}:
            return f"<b>{text}</b>"
        if color == palette.C_DIM:
            return f"<i>{text}</i>"
        if color == palette.ACCENT:
            return f"<code>{text}</code>"
        return text
    if bold:
        return f"[bold {color}]{text}[/bold {color}]"
    return f"[{color}]{text}[/{color}]"


def accent(text: str, *, bold: bool = False, dialect: Dialect = "rich") -> str:
    return paint(palette.ACCENT, escape(text, dialect=dialect), bold=bold, dialect=dialect)


def muted(text: str, *, dialect: Dialect = "rich") -> str:
    return paint(palette.C_DIM, escape(text, dialect=dialect), dialect=dialect)


def ok(text: str, *, dialect: Dialect = "rich") -> str:
    return paint(palette.C_SUCCESS, escape(text, dialect=dialect), dialect=dialect)


def warn(text: str, *, dialect: Dialect = "rich") -> str:
    return paint(palette.C_WARN, escape(text, dialect=dialect), dialect=dialect)


def err(text: str, *, dialect: Dialect = "rich") -> str:
    return paint(palette.C_ERROR, escape(text, dialect=dialect), dialect=dialect)


def notice(text: str, *, dialect: Dialect = "rich") -> str:
    return paint(palette.C_NOTICE, escape(text, dialect=dialect), dialect=dialect)


def strong(text: str, *, dialect: Dialect = "rich") -> str:
    """Inline bold (not a page title)."""
    body = escape(text, dialect=dialect)
    if dialect == "html":
        return f"<b>{body}</b>"
    return f"[b]{body}[/b]"


def title(text: str, *, dialect: Dialect = "rich") -> str:
    return strong(text, dialect=dialect)


def heading(text: str, hint: str = "", *, dialect: Dialect = "rich") -> str:
    """Page title with an optional muted hint on the same line."""
    if not hint:
        return title(text, dialect=dialect)
    return f"{title(text, dialect=dialect)} {muted(hint, dialect=dialect)}"


def section(text: str, *, dialect: Dialect = "rich") -> str:
    """Section header with a leading blank line (spacing level 1)."""
    return "\n" + title(text, dialect=dialect)


def hint(text: str, *, dialect: Dialect = "rich") -> str:
    return muted(text, dialect=dialect)


def kv(
    label: str,
    value: str,
    *,
    label_width: int = grammar.KV_LABEL_WIDTH,
    dialect: Dialect = "rich",
) -> str:
    pad = label.ljust(label_width)
    return f"  {muted(pad, dialect=dialect)} {escape(value, dialect=dialect)}"


def row(
    name: str,
    description: str,
    *,
    name_width: int = grammar.HELP_NAME_WIDTH,
    dialect: Dialect = "rich",
) -> str:
    """Command index row: accent name, padded, then description."""
    visible = name if len(name) <= name_width else name[: name_width - 1] + "…"
    pad = " " * max(1, name_width - len(name) + 2) if len(name) <= name_width else "  "
    return f"  {accent(visible, dialect=dialect)}{pad}{escape(description, dialect=dialect)}"


def usage_block(
    command: str,
    syntax: str,
    *,
    examples: tuple[str, ...] = (),
    related: tuple[str, ...] = (),
    extra: str = "",
    dialect: Dialect = "rich",
) -> str:
    """Standard failed-command / detail usage page."""
    lines = [
        err(f"Usage: {syntax}", dialect=dialect) if extra == "" else err(extra, dialect=dialect),
    ]
    if extra:
        lines.append(muted(f"Usage: {syntax}", dialect=dialect))
    if examples:
        lines.append("")
        lines.append(muted("Examples", dialect=dialect))
        for item in examples:
            lines.append(f"  {escape(item, dialect=dialect)}")
    if related:
        lines.append("")
        rel = "  ".join(f"/{item}" for item in related)
        lines.append(muted(f"Related  {rel}", dialect=dialect))
    lines.append("")
    lines.append(muted(f"Type /help {command} for details.", dialect=dialect))
    return "\n".join(lines)


def banner_ok(text: str, *, dialect: Dialect = "rich") -> str:
    return f"{ok(grammar.GLYPH_OK, dialect=dialect)} {escape(text, dialect=dialect)}"


def banner_warn(text: str, *, dialect: Dialect = "rich") -> str:
    return f"{warn(grammar.GLYPH_WARN, dialect=dialect)} {escape(text, dialect=dialect)}"


def banner_err(text: str, *, dialect: Dialect = "rich") -> str:
    return f"{err(grammar.GLYPH_FAIL, dialect=dialect)} {escape(text, dialect=dialect)}"


def flag(good: bool, yes: str = "ok", no: str = "missing", *, dialect: Dialect = "rich") -> str:
    return ok(yes, dialect=dialect) if good else err(no, dialect=dialect)


def key_mark(active: bool, *, dialect: Dialect = "rich") -> str:
    """``●`` / ``○`` for on/off rows (modules, MCP, skills)."""
    if active:
        return ok(grammar.GLYPH_KEY_OK, dialect=dialect)
    return muted(grammar.GLYPH_KEY_OFF, dialect=dialect)


def more(n: int, *, dialect: Dialect = "rich") -> str:
    """``… (N more)`` footer used by truncated lists."""
    return muted(f"… ({n} more)", dialect=dialect)


def context_gauge(
    frac: float,
    *,
    used: int | None = None,
    window: int | None = None,
    dialect: Dialect = "rich",
) -> str:
    """``12.4K/200K [██████░░░░] 6%`` colored by the shared pressure ramp."""
    from jarn.tui.grammar import context_bar, context_level, format_tokens

    color = {
        "ok": palette.CTX_OK,
        "warn": palette.CTX_WARN,
        "hot": palette.CTX_HOT,
        "exceeded": palette.CTX_EXCEEDED,
    }[context_level(frac)]
    bar = paint(color, context_bar(frac), dialect=dialect)
    pct = paint(color, f"{frac * 100:.0f}%", dialect=dialect)
    if used is not None and window is not None:
        pair = f"{format_tokens(used)}/{format_tokens(window)}"
        return f"{escape(pair, dialect=dialect)} {bar} {pct}"
    return f"{bar} {pct}"
