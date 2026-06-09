"""Render colored unified diffs for edit approvals."""

from __future__ import annotations

import difflib
from typing import Any

from rich.text import Text


def unified_diff_text(
    old: str, new: str, *, filename: str = "", context: int = 3,
    max_lines: int | None = None,
) -> Text:
    """Return a Rich ``Text`` of a colored unified diff.

    ``max_lines`` caps how many diff lines are rendered so a large write/edit
    doesn't flood the terminal; the remainder is collapsed to a dim
    ``… (+N more lines)`` footer. ``None`` (default) renders the whole diff.
    """
    old_lines = old.splitlines(keepends=False)
    new_lines = new.splitlines(keepends=False)
    diff = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=filename or "before", tofile=filename or "after",
        lineterm="", n=context,
    ))
    hidden = 0
    if max_lines is not None and len(diff) > max_lines:
        hidden = len(diff) - max_lines
        diff = diff[:max_lines]
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
    if hidden:
        text.append(f"… (+{hidden} more lines)\n", style="dim")
    if not text.plain:
        text.append("(no textual change)\n", style="dim")
    return text


def diff_from_edit_args(
    args: dict[str, Any], *, max_lines: int | None = None
) -> Text | None:
    """Best-effort diff for a ``write_file`` / ``edit_file`` tool call.

    Handles both shapes deepagents may use: ``old_string``/``new_string`` for an
    in-place edit, or full ``content`` for a write (shown as all-additions).
    ``max_lines`` caps the rendered diff (see :func:`unified_diff_text`) so a
    large file doesn't flood the approval prompt.
    """
    filename = str(args.get("file_path") or args.get("path") or "")
    from jarn.agent.files import is_multimodal_path, modality_of

    if filename and is_multimodal_path(filename):
        t = Text()
        t.append(f"({modality_of(filename)} file — binary, diff not shown)\n", style="dim")
        return t
    if "old_string" in args and "new_string" in args:
        return unified_diff_text(
            str(args["old_string"]), str(args["new_string"]),
            filename=filename, max_lines=max_lines,
        )
    if "content" in args:
        return unified_diff_text(
            "", str(args["content"]), filename=filename, max_lines=max_lines
        )
    return None
