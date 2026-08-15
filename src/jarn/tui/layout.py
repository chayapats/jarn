"""String-markup SSOT — the only module that may compose ``[color]`` / HTML tags.

Command pages, the live turn stream, CLI human text, and Telegram notices all
call helpers here so spacing, glyphs, and semantic colors cannot drift.

Dialects: ``rich`` (REPL), ``html`` (Telegram ``<b>`` ``<i>`` ``<code>``),
``plain`` (CLI ``--help`` / ``NO_COLOR``). Specialized renderers that are not
strings — ``toolbar.py`` (prompt_toolkit HTML) and ``widgets/diff.py`` (Rich
``Text``) — consume ``grammar`` + ``palette`` directly and must not invent
named colors.

This module escapes user text. Callers must not pre-escape.
"""

from __future__ import annotations

import re
from typing import Literal

from rich.markup import escape as _escape_rich

from jarn.tui import grammar, palette

Dialect = Literal["rich", "html", "plain"]

_MARKUP_TAG = re.compile(r"\[(/?)(?:(bold)\s+)?(#[0-9A-Fa-f]{6}|b)\]")


def escape(text: object, *, dialect: Dialect = "rich") -> str:
    value = "" if text is None else str(text)
    if dialect == "html":
        from jarn.telegram.htmlutil import escape_html

        return escape_html(value)
    if dialect == "plain":
        return value
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
    if dialect == "plain":
        return text
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
    if dialect in {"html", "plain"}:
        return f"<b>{body}</b>" if dialect == "html" else body
    return f"[b]{body}[/b]"


def field(label: str, value: str = "", *, dialect: Dialect = "rich") -> str:
    """``Label: value`` with a strong label — onboarding and CLI ceremonies."""
    head = strong(f"{label}:", dialect=dialect)
    if value == "":
        return head
    return f"{head} {escape(value, dialect=dialect)}"


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


def truncate(text: str, width: int) -> str:
    """Shorten *text* to *width* characters, appending ``…`` when it overflows.

    Operates on raw text (no markup) so callers can paint/escape afterwards
    without double-escaping. Width is ``len()``, matching ``row`` / ``todo_item``.
    """
    value = "" if text is None else str(text)
    n = max(1, int(width))
    if len(value) <= n:
        return value
    if n == 1:
        return "…"
    return value[: n - 1] + "…"


def bullet(text: str, *, glyph: str = "·", dialect: Dialect = "rich") -> str:
    """``· text`` list line. *text* is escaped; *glyph* is frozen punctuation."""
    return f"{escape(glyph, dialect=dialect)} {escape(text, dialect=dialect)}"


def rule(title: str = "", *, dialect: Dialect = "rich") -> str:
    """Muted ``── title ──`` separator, or ``──`` when *title* is empty."""
    body = f"── {title} ──" if title else "──"
    return muted(body, dialect=dialect)


def item(
    name: str,
    description: str = "",
    *,
    meta: str = "",
    dialect: Dialect = "rich",
) -> str:
    """Accent *name*, optional muted *meta*, then *description*."""
    head = accent(name, dialect=dialect)
    if meta:
        head = f"{head} {muted(meta, dialect=dialect)}"
    if description == "":
        return head
    return f"{head}  {escape(description, dialect=dialect)}"


def code(text: str, *, dialect: Dialect = "rich") -> str:
    """Inline code: accent (Rich), ``<code>`` (HTML), escaped as-is (plain)."""
    body = escape(text, dialect=dialect)
    if dialect == "html":
        return f"<code>{body}</code>"
    if dialect == "plain":
        return body
    return paint(palette.ACCENT, body, dialect=dialect)


def pre(text: str, *, dialect: Dialect = "rich") -> str:
    """Preformatted block: ``<pre>`` in HTML; escaped text in Rich/plain."""
    body = escape(text, dialect=dialect)
    if dialect == "html":
        return f"<pre>{body}</pre>"
    return body


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


def sep(*, dialect: Dialect = "rich") -> str:
    """Dim middle-dot used in status lines and hints."""
    return f" {muted('·', dialect=dialect)} "


def user(text: str = grammar.GLYPH_PROMPT, *, dialect: Dialect = "rich") -> str:
    return paint(palette.C_USER, escape(text, dialect=dialect), dialect=dialect)


def tool(text: str = grammar.GLYPH_TOOL, *, dialect: Dialect = "rich") -> str:
    return paint(palette.C_TOOL, escape(text, dialect=dialect), dialect=dialect)


def prompt(text: str, *, dialect: Dialect = "rich") -> str:
    """User echo: cyan ``›`` then uncolored escaped text."""
    return f"{user(grammar.GLYPH_PROMPT, dialect=dialect)} {escape(text, dialect=dialect)}"


def steer(
    text: str,
    *,
    queued: bool = False,
    hint: str = "",
    dialect: Dialect = "rich",
) -> str:
    """Queue / mid-turn steer line. Whole line is dim, including ``»``."""
    body = f"{grammar.GLYPH_STEER} queued: {text}" if queued else f"{grammar.GLYPH_STEER} {text}"
    if hint:
        body += hint
    return muted(body, dialect=dialect)


def host_shell(cmd: str, *, dialect: Dialect = "rich") -> str:
    """Host-direct ``!`` escape — stays error-red; never a friendly accent."""
    return (
        f"{err('!', dialect=dialect)} "
        f"{err(cmd, dialect=dialect)} "
        f"{muted('(host shell)', dialect=dialect)}"
    )


def thinking(*, dialect: Dialect = "rich") -> str:
    return muted(f"{grammar.GLYPH_THINKING} thinking", dialect=dialect)


def subagent_prefix(name: str, *, dialect: Dialect = "rich") -> str:
    """Dim ``┊ name `` prefix on a subagent's tool lines (trailing space)."""
    return muted(f"{grammar.GLYPH_SUBAGENT} {name} ", dialect=dialect)


def subagent_done(
    name: str,
    n: int,
    *,
    hint: str = "",
    dialect: Dialect = "rich",
) -> str:
    extra = f" · {hint}" if hint else ""
    return muted(
        f"{grammar.GLYPH_SUBAGENT} {name} {grammar.GLYPH_RESULT} done · {n} tool calls{extra}",
        dialect=dialect,
    )


def tool_open(name: str, args: str = "", *, dialect: Dialect = "rich") -> str:
    line = f"{tool(grammar.GLYPH_TOOL, dialect=dialect)} {strong(name, dialect=dialect)}"
    if args:
        line += f"  {muted(args, dialect=dialect)}"
    return line


def tool_result(
    summary: str,
    *,
    duration: str = "",
    hint: str = "",
    indent: str = "  ",
    dialect: Dialect = "rich",
) -> str:
    body = muted(f"{grammar.GLYPH_RESULT} {summary}{duration}", dialect=dialect)
    extra = f" {muted(hint, dialect=dialect)}" if hint else ""
    return f"{indent}{body}{extra}"


def cancelled(*, dialect: Dialect = "rich") -> str:
    return muted("cancelled", dialect=dialect)


def todo_glyph(status: str = "pending", *, dialect: Dialect = "rich") -> str:
    """Plan-checklist mark: done / in-progress / waiting."""
    key = (status or "pending").lower()
    if key == "completed":
        return ok(grammar.GLYPH_TODO_DONE, dialect=dialect)
    if key in {"in_progress", "running"}:
        return accent(grammar.GLYPH_TODO_RUN, dialect=dialect)
    return muted(grammar.GLYPH_TODO_WAIT, dialect=dialect)


def todo_item(
    content: str,
    status: str = "pending",
    *,
    truncate: int | None = None,
    dialect: Dialect = "rich",
) -> str:
    """One checklist row: ``  <glyph> <content>`` (completed items are dim)."""
    body = str(content)
    if truncate is not None:
        limit = max(8, truncate - 4)
        if len(body) > limit:
            body = body[: limit - 1] + "…"
    painted = (
        muted(body, dialect=dialect)
        if (status or "").lower() == "completed"
        else escape(body, dialect=dialect)
    )
    return f"  {todo_glyph(status, dialect=dialect)} {painted}"


def format_todos(todos: list[dict], width: int, *, cap: int | None = None) -> list[str]:
    """Render a plan checklist to Rich-markup lines: ``["⏺ Todos", <item>, …]``.

    Shared by BOTH the live in-turn region and the committed end-of-turn render so
    glyphs and layout stay identical.

    ``cap`` (live region only) bounds the body to ``cap`` lines so a long plan
    can't push the input off-screen: completed items collapse to one ``✔ N done``
    summary, the in-progress + upcoming items fill the remaining budget, and any
    overflow is elided behind a ``… +N more`` line. ``cap is None`` (committed
    render) shows every item, unwrapped, exactly as before.
    """
    header = f"{tool()} {strong('Todos')}"
    lines = [header]
    trunc = width if cap is not None else None

    def _line(todo: dict) -> str:
        return todo_item(
            str(todo.get("content", "")),
            str(todo.get("status", "pending")),
            truncate=trunc,
        )

    if cap is None or len(todos) <= cap:
        lines.extend(_line(t) for t in todos)
        return lines
    # Windowed live block: keep it focused on what is happening *now*.
    done = [t for t in todos if t.get("status") == "completed"]
    tail = [t for t in todos if t.get("status") != "completed"]  # in-progress + pending
    budget = cap
    if done:
        lines.append(f"  {todo_glyph('completed')} {muted(f'{len(done)} done')}")
        budget -= 1
    if len(tail) > budget:
        show = max(1, budget - 1)  # reserve a line for the "… +N more" summary
        lines.extend(_line(t) for t in tail[:show])
        hidden = len(tail) - show
        lines.append(f"  {muted(f'… +{hidden} more')}")
    else:
        lines.extend(_line(t) for t in tail)
    return lines


def spinner(frame: str, text: str, *, dialect: Dialect = "rich") -> str:
    """Live thinking/working line: tool-colored frame + dim status text."""
    return f"{tool(frame, dialect=dialect)} {muted(text, dialect=dialect)}"


def host_shell_banner(*, dialect: Dialect = "rich") -> str:
    """One-line reminder that ``!`` is host-direct (danger-guard skipped)."""
    return (
        f"{err(f'{grammar.GLYPH_HOST_SHELL} host shell', dialect=dialect)} "
        + muted(
            "— runs on your machine directly; no agent, no approval, danger-guard skipped",
            dialect=dialect,
        )
    )


def link(url: str, text: str | None = None, *, dialect: Dialect = "rich") -> str:
    """Clickable URL for Rich / Telegram; plain dialect prints the address."""
    href = escape(url, dialect=dialect)
    label = escape(url if text is None else text, dialect=dialect)
    if dialect == "plain":
        return label if text is None else f"{label} ({url})"
    if dialect == "html":
        return f'<a href="{href}">{label}</a>'
    return f"[link={href}]{label}[/link]"


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


def looks_like_layout_markup(text: str) -> bool:
    """True when *text* contains layout-generated Rich tags (not user prose)."""
    return _MARKUP_TAG.search(text or "") is not None


def _html_tag_for(token: str, *, bold: bool) -> str:
    color = token.lower()
    if token == "b":
        return "b"
    if color == palette.C_DIM.lower():
        return "i"
    if color == palette.ACCENT.lower():
        return "code"
    if bold or color in {
        palette.C_ERROR.lower(),
        palette.C_WARN.lower(),
        palette.C_SUCCESS.lower(),
        palette.C_NOTICE.lower(),
        palette.C_TOOL.lower(),
        palette.C_USER.lower(),
    }:
        return "b"
    return "b"


def to_html(markup: str) -> str:
    """Transcode layout-generated Rich markup to Telegram HTML.

    Text nodes are HTML-escaped; tags become ``<b>`` / ``<i>`` / ``<code>``.
    """
    from jarn.telegram.htmlutil import escape_html

    parts: list[str] = []
    pos = 0
    stack: list[str] = []
    for match in _MARKUP_TAG.finditer(markup):
        parts.append(escape_html(markup[pos : match.start()]))
        closing, bold_token, token = match.group(1), match.group(2), match.group(3)
        if closing:
            if stack:
                parts.append(f"</{stack.pop()}>")
        else:
            tag = _html_tag_for(token, bold=bool(bold_token))
            stack.append(tag)
            parts.append(f"<{tag}>")
        pos = match.end()
    parts.append(escape_html(markup[pos:]))
    while stack:
        parts.append(f"</{stack.pop()}>")
    return "".join(parts)
