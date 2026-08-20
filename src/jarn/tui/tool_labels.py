"""Human activity labels for tool lines (Claude Code verbs).

Density ``new`` prints a catalog verb plus a primary object — no internal
tool name, no ``k=v``. ``fmt_args`` stays on ``all`` / ``verbose`` in the CLI.
Telegram progress bubbles reuse :func:`activity_open` / :func:`activity_result`.
"""

from __future__ import annotations

import re
from typing import Any

from jarn.tui.i18n import t

# Catalog key for each built-in tool. ``execute`` shares the bash/shell verb.
_VERB_KEYS: dict[str, str] = {
    "read_file": "tool.verb.read_file",
    "edit_file": "tool.verb.edit_file",
    "write_file": "tool.verb.write_file",
    "bash": "tool.verb.bash",
    "shell": "tool.verb.shell",
    "execute": "tool.verb.bash",
}

_OBJECT_KEYS: dict[str, tuple[str, ...]] = {
    "read_file": ("path", "file_path", "file", "filename"),
    "edit_file": ("path", "file_path", "file", "filename"),
    "write_file": ("path", "file_path", "file", "filename"),
    "bash": ("command", "cmd"),
    "shell": ("command", "cmd"),
    "execute": ("command", "cmd"),
}

_OBJECT_MAX = 60
_LINES_RE = re.compile(r"^(\d+)\s+lines?$", re.IGNORECASE)

#: Checklist tools already render via the todo block — no extra activity line.
CHECKLIST_TOOLS = frozenset({"write_todos"})


def is_checklist_tool(name: str) -> bool:
    return name in CHECKLIST_TOOLS


def _clip(value: str) -> str:
    if len(value) <= _OBJECT_MAX:
        return value
    return value[: _OBJECT_MAX - 1] + "…"


def _is_mcp_id(name: str) -> bool:
    return name.startswith("mcp") or "__" in name


def _looks_like_path_or_url(value: str) -> bool:
    return "://" in value or "/" in value or "\\" in value


def display_verb(name: str, locale: str) -> str:
    """Catalog verb, or a title-cased fallback. MCP ids are never translated."""
    key = _VERB_KEYS.get(name)
    if key is not None:
        return t(key, locale)
    if _is_mcp_id(name):
        return name
    return name.replace("_", " ").title()


def primary_object(name: str, args: dict[str, Any] | None) -> str:
    """Path, command, or obvious path/url. Empty when nothing is obvious."""
    payload = args or {}
    keys = _OBJECT_KEYS.get(name)
    if keys:
        for key in keys:
            raw = payload.get(key)
            if raw is not None and str(raw).strip():
                return _clip(str(raw))
        return ""
    for raw in payload.values():
        text = str(raw)
        if text.strip() and _looks_like_path_or_url(text):
            return _clip(text)
    return ""


def activity_open(name: str, args: dict[str, Any] | None, *, locale: str) -> tuple[str, str]:
    """``(verb, object)`` for density ``new``. Join with two spaces at the call site."""
    return display_verb(name, locale), primary_object(name, args)


def activity_result(summary: str, *, locale: str) -> str:
    """Localize ``N lines`` / ``done``; leave other engine summaries untouched."""
    text = (summary or "").strip()
    match = _LINES_RE.fullmatch(text)
    if match:
        return t("tool.result.lines", locale, n=int(match.group(1)))
    if text.lower() in {"done", "ok"}:
        return t("tool.result.done", locale)
    return summary
