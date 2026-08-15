"""HTML helpers for Telegram ``parse_mode=HTML`` (#40) — not MarkdownV2.

``escape_html`` is the context-free escape used by :mod:`jarn.tui.layout`.
Inline ``<code>`` / ``<pre>`` composition lives in ``layout.code`` / ``layout.pre``.
"""

from __future__ import annotations

import html
from collections.abc import Iterator

__all__ = [
    "TELEGRAM_MESSAGE_MAX",
    "chunk_html",
    "escape_html",
]

#: Telegram Bot API hard cap for message text after entity parsing.
TELEGRAM_MESSAGE_MAX = 4096

#: Leave headroom so we never land exactly on a broken entity boundary.
_CHUNK = 3900


def escape_html(text: str) -> str:
    """Escape ``&``, ``<``, ``>`` for HTML parse mode (context-free)."""
    return html.escape(text or "", quote=False)


def chunk_html(text: str, *, limit: int = TELEGRAM_MESSAGE_MAX) -> list[str]:
    """Split *text* into ≤*limit* pieces without mid-entity splits when possible.

    Input is expected to already be HTML-escaped (or contain only well-formed
    tags we introduced). We split on newlines preferentially, then hard-cut.
    """
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]
    soft = min(limit, _CHUNK) if limit >= _CHUNK else limit
    return list(_chunks(text, soft))


def _chunks(text: str, limit: int) -> Iterator[str]:
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            yield remaining
            return
        window = remaining[:limit]
        break_at = window.rfind("\n")
        if break_at < limit // 4:
            break_at = window.rfind(" ")
        if break_at < limit // 4:
            break_at = limit
        piece = remaining[:break_at].rstrip("\n")
        if not piece:
            piece = remaining[:limit]
            break_at = limit
        yield piece
        remaining = remaining[break_at:].lstrip("\n")
