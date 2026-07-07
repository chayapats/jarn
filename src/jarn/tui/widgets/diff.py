"""Render colored unified diffs for edit approvals."""

from __future__ import annotations

import difflib
from typing import Any

from rich.text import Text

_EMPHASIS_MAX_LINE = 200   # lines longer than this skip intraline emphasis
_EMPHASIS_MIN_RATIO = 0.3  # SequenceMatcher.ratio() below this → plain


def _emphasize_pair(del_content: str, add_content: str) -> tuple[Text, Text] | None:
    """Return ``(del_text, add_text)`` with intraline emphasis, or ``None`` if skipped.

    *del_content* and *add_content* are the line bodies **without** the leading
    ``-``/``+`` prefix character.  Returns ``None`` when the lines are too long
    or too dissimilar to make emphasis useful.
    """
    if len(del_content) > _EMPHASIS_MAX_LINE or len(add_content) > _EMPHASIS_MAX_LINE:
        return None

    matcher = difflib.SequenceMatcher(None, del_content, add_content, autojunk=False)
    if matcher.ratio() < _EMPHASIS_MIN_RATIO:
        return None

    del_text = Text()
    del_text.append("-", style="red")
    add_text = Text()
    add_text.append("+", style="green")

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            del_text.append(del_content[i1:i2], style="red")
            add_text.append(add_content[j1:j2], style="green")
        elif tag == "replace":
            del_text.append(del_content[i1:i2], style="bold reverse red")
            add_text.append(add_content[j1:j2], style="bold reverse green")
        elif tag == "delete":
            del_text.append(del_content[i1:i2], style="bold reverse red")
        elif tag == "insert":
            add_text.append(add_content[j1:j2], style="bold reverse green")

    return del_text, add_text


def unified_diff_text(
    old: str, new: str, *, filename: str = "", context: int = 3,
    max_lines: int | None = None,
) -> Text:
    """Return a Rich ``Text`` of a colored unified diff.

    ``max_lines`` caps how many diff lines are rendered so a large write/edit
    doesn't flood the terminal; the remainder is collapsed to a dim
    ``… (+N more lines)`` footer. ``None`` (default) renders the whole diff.

    Adjacent equal-count runs of deleted/added lines are post-processed with
    ``difflib.SequenceMatcher`` to add word-level (intraline) ``bold reverse``
    emphasis on changed character spans.  Lines longer than
    :data:`_EMPHASIS_MAX_LINE` (200) chars or with similarity ratio below
    :data:`_EMPHASIS_MIN_RATIO` (0.3) fall back to plain line-level rendering.
    Full syntax highlighting inside diffs is deliberately excluded (readability
    + YAGNI).
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
    i = 0
    while i < len(diff):
        line = diff[i]
        if line.startswith("+++") or line.startswith("---"):
            text.append(line + "\n", style="bold")
            i += 1
        elif line.startswith("@@"):
            text.append(line + "\n", style="cyan")
            i += 1
        elif line.startswith("-"):
            # Collect an adjacent run of deletions …
            j = i
            while j < len(diff) and diff[j].startswith("-") and not diff[j].startswith("---"):
                j += 1
            del_lines = diff[i:j]
            # … immediately followed by a run of additions.
            k = j
            while k < len(diff) and diff[k].startswith("+") and not diff[k].startswith("+++"):
                k += 1
            add_lines = diff[j:k]

            if len(del_lines) == len(add_lines):
                # Equal counts: attempt intraline emphasis per pair.
                for dl, al in zip(del_lines, add_lines, strict=True):
                    result = _emphasize_pair(dl[1:], al[1:])
                    if result is not None:
                        del_t, add_t = result
                        text.append_text(del_t)
                        text.append("\n")
                        text.append_text(add_t)
                        text.append("\n")
                    else:
                        text.append(dl + "\n", style="red")
                        text.append(al + "\n", style="green")
            else:
                # Unequal counts: plain line-level rendering.
                for dl in del_lines:
                    text.append(dl + "\n", style="red")
                for al in add_lines:
                    text.append(al + "\n", style="green")
            i = k
        elif line.startswith("+"):
            # Unpaired addition (no preceding deletion run).
            text.append(line + "\n", style="green")
            i += 1
        else:
            text.append(line + "\n", style="dim")
            i += 1

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
