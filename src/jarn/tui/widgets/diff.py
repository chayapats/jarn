"""Render colored unified diffs for edit approvals."""

from __future__ import annotations

import difflib
from typing import Any

from rich.text import Text


def unified_diff_text(
    old: str, new: str, *, filename: str = "", context: int = 3
) -> Text:
    """Return a Rich ``Text`` of a colored unified diff."""
    old_lines = old.splitlines(keepends=False)
    new_lines = new.splitlines(keepends=False)
    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile=filename or "before", tofile=filename or "after",
        lineterm="", n=context,
    )
    text = Text()
    for line in diff:
        if line.startswith("+++") or line.startswith("---"):
            text.append(line + "\n", style="bold")
        elif line.startswith("@@"):
            text.append(line + "\n", style="cyan")
        elif line.startswith("+"):
            text.append(line + "\n", style="green")
        elif line.startswith("-"):
            text.append(line + "\n", style="red")
        else:
            text.append(line + "\n", style="dim")
    if not text.plain:
        text.append("(no textual change)\n", style="dim")
    return text


def diff_from_edit_args(args: dict[str, Any]) -> Text | None:
    """Best-effort diff for a ``write_file`` / ``edit_file`` tool call.

    Handles both shapes deepagents may use: ``old_string``/``new_string`` for an
    in-place edit, or full ``content`` for a write (shown as all-additions).
    """
    filename = str(args.get("file_path") or args.get("path") or "")
    from jarn.agent.files import is_multimodal_path, modality_of

    if filename and is_multimodal_path(filename):
        t = Text()
        t.append(f"({modality_of(filename)} file — binary, diff not shown)\n", style="dim")
        return t
    if "old_string" in args and "new_string" in args:
        return unified_diff_text(
            str(args["old_string"]), str(args["new_string"]), filename=filename
        )
    if "content" in args:
        return unified_diff_text("", str(args["content"]), filename=filename)
    return None
