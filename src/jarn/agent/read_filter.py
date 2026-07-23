"""Result-filter middleware: strip sensitive-file hits from broad read tools.

Pre-execution gating (``permissions_bridge`` + the permission engine) blocks a
read whose *target* (its ``path``/``glob``) is sensitive or denied, but a BROAD
content search — e.g. ``grep(pattern='TOKEN=', path='/repo')`` — is gated only on
its benign search *scope* (``/repo``) while the tool then returns the CONTENTS of
every matching file, including ``.env`` / SSH keys. That is the exfiltration the
scope-only gate cannot see (Codex second-eye #1).

This middleware closes the gap from the RESULT side: after the deepagents
filesystem ``grep`` tool runs, any hit from a file matching ``sensitive_read_globs``
or an explicit read-``deny`` rule is removed before the content reaches the model.
``read_file`` gets a defense-in-depth backstop for a directly-denied path. The
:class:`~jarn.permissions.PermissionEngine` is the single source of truth for what
counts as sensitive/denied — this module never re-implements the glob logic.

Wired as a ``middleware=`` entry on ``create_deep_agent`` (a jarn-owned seam), so
deepagents in site-packages is never edited. It wraps tool *execution* via the
``wrap_tool_call`` / ``awrap_tool_call`` hook, which composes around the shared
tool node regardless of stack position.

Residual B — BEST-EFFORT, not a hard guarantee. This result-filter parses grep
DISPLAY text (deepagents' formatted output), so it is DEFENSE-IN-DEPTH, not a hard
boundary: it recovers each hit's file path by parsing the formatter's line
structure. An attacker-controlled path that contains a NEWLINE can split the file
header across lines and bypass the per-file drop, so a determined attacker who can
name files (or grep across untrusted content) may still surface a matched line. The
HARD controls remain (a) pre-execution sensitive-path GATING — which catches
explicit read targets (an explicit ``read_file``/``glob`` of a secret) via the
permission engine before any tool runs — and (b) OS-level isolation
(``execution.backend: docker`` or an OS sandbox) for running untrusted code. This
filter narrows the broad-grep exfiltration window; it does not replace either.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

if TYPE_CHECKING:
    from langgraph.prebuilt.tool_node import ToolCallRequest

    from jarn.permissions import PermissionEngine

#: Appended when some (but not all) hits were dropped, so the model isn't misled
#: into thinking the search was exhaustive.
_REDACTION_NOTICE = (
    "[jarn: omitted matches in read-restricted files (e.g. .env / keys); "
    "read a specific approved path to view it]"
)

#: Returned when redaction emptied the result entirely — mirrors the backend's
#: own "No matches found" sentinel so the model sees a normal empty search.
_EMPTY_AFTER_REDACTION = "No matches found (matches in read-restricted files were omitted)"


class ReadResultFilterMiddleware(AgentMiddleware):
    """Filter sensitive/denied file hits out of ``grep`` output (and back-stop a
    directly-denied ``read_file``) before the content reaches the model.

    Holds the session's AUTHORITATIVE :class:`PermissionEngine` — the controller's
    request-scoped instance that gates tool calls and receives runtime
    ``deny_session``/``remember`` (see ``jarn.agent.interrupts``). Because it shares
    that one engine (rather than a fresh rule-only copy), a runtime SESSION deny of a
    path is honored here too: a user who denies reading a secret has that file's hits
    stripped from a later broad grep, on the main agent and every subagent/fan-out
    stack alike (BUG A fix). It reuses the engine's
    :meth:`~jarn.permissions.PermissionEngine.read_content_blocked` /
    :meth:`~jarn.permissions.PermissionEngine.is_read_denied_path` — the single
    source of truth for config deny/allow rules, sensitive-read globs, and session
    denies/allows. See the module docstring's residual B note: this is best-effort
    defense-in-depth (it parses grep display text), not a hard boundary.
    """

    def __init__(self, engine: PermissionEngine) -> None:
        self._engine = engine
        # AgentMiddleware.tools is read by create_agent; a wrap-only middleware
        # registers none (mirrors langchain's own ToolRetryMiddleware).
        self.tools = []

    # -- interception -------------------------------------------------------

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Any],
    ) -> ToolMessage | Any:
        """Sync path (``invoke``/``stream``)."""
        return self._filter(request, handler(request))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Any]],
    ) -> ToolMessage | Any:
        """Async path (``ainvoke``/``astream`` — jarn's production driver)."""
        return self._filter(request, await handler(request))

    # -- filtering (pure/sync — shared by both paths) -----------------------

    def _filter(self, request: ToolCallRequest, result: Any) -> Any:
        # Only content-returning read tools that produce a plain-text ToolMessage
        # are in scope; anything else (Command results, errors we can't parse)
        # passes through untouched.
        if not isinstance(result, ToolMessage):
            return result
        call = getattr(request, "tool_call", None) or {}
        name = call.get("name") or result.name
        if name == "grep":
            return self._filter_grep(call, result)
        if name == "read_file":
            return self._filter_read_file(call, result)
        return result

    def _filter_grep(self, call: dict[str, Any], result: ToolMessage) -> ToolMessage:
        content = result.content
        if not isinstance(content, str) or not content:
            return result
        args = call.get("args") or {}
        output_mode = str(args.get("output_mode") or "files_with_matches")
        new_content, removed = _filter_grep_content(
            content, output_mode, self._engine.read_content_blocked
        )
        if not removed:
            return result
        return result.model_copy(update={"content": new_content})

    def _filter_read_file(self, call: dict[str, Any], result: ToolMessage) -> ToolMessage:
        # Backstop only: a denied read is already blocked pre-exec, so this fires
        # only if that gate is somehow bypassed. Sensitive-but-approved reads are
        # NOT re-filtered here (the user was already prompted and approved).
        args = call.get("args") or {}
        path = str(args.get("file_path") or "")
        if path and self._engine.is_read_denied_path(path):
            return ToolMessage(
                content=f"Error: permission denied for read on {path}",
                name="read_file",
                tool_call_id=call.get("id") or result.tool_call_id,
                status="error",
            )
        return result


#: A Windows OS-native absolute path prefix: a drive-letter root (``C:\`` or ``C:/``)
#: or a UNC share (``\\host\share``). The real local backend (``virtual_mode=False``)
#: reports grep hits by OS-native path, so on Windows a header has NO leading ``/`` —
#: deepagents ``to_posix_path`` documents this ("Backends running on Windows return
#: OS-native paths using backslashes"). Missing these made the redaction filter a
#: silent no-op on Windows (every secret grep header leaked through).
_ABS_WINDOWS = re.compile(r"[A-Za-z]:[\\/]|\\\\")


def _looks_absolute(line: str) -> bool:
    """True for a grep-output path line (backend paths are always absolute).

    Absolute means a leading ``/`` (POSIX / the virtual-mode backend) OR a Windows
    drive-letter/UNC prefix (the real local backend on Windows). Match lines are
    space/tab-indented and relative spellings have neither prefix, so both are
    correctly rejected as non-headers.
    """
    return line[:1] == "/" or _ABS_WINDOWS.match(line) is not None


def _filter_grep_content(
    content: str,
    output_mode: str,
    blocked: Callable[[str], bool],
) -> tuple[str, bool]:
    """Return ``(filtered_content, removed_any)`` for a formatted grep result.

    Parses deepagents' stable grep formatting (``deepagents.backends.utils.
    _format_grep_results``): every match is grouped under its file path, and file
    paths are absolute. Per output mode:

    - ``files_with_matches``: one absolute path per line -> drop blocked paths.
    - ``count``: ``"{path}: {count}"`` per line -> drop blocked paths.
    - ``content``: ``"{path}:"`` header then ``"  {n}: {text}"`` indented match
      lines -> drop a blocked file's header and all its (space-indented) lines.

    Non-path lines (an error prefix, ``"Partial matches:"``, the truncation
    guidance, ``"No matches found"``) are not absolute path lines (see
    :func:`_looks_absolute` — no ``/`` or Windows drive/UNC prefix) and pass through.
    """
    if output_mode == "content":
        filtered, removed = _filter_content_mode(content, blocked)
    elif output_mode == "count":
        filtered, removed = _filter_line_mode(content, blocked, _count_line_path)
    else:  # files_with_matches (default)
        filtered, removed = _filter_line_mode(content, blocked, lambda ln: ln)
    if not removed:
        return content, False
    if not filtered.strip():
        return _EMPTY_AFTER_REDACTION, True
    return filtered + "\n" + _REDACTION_NOTICE, True


def _count_line_path(line: str) -> str:
    """Extract the path from a ``count``-mode line ``"{path}: {count}"``."""
    return line.rsplit(": ", 1)[0]


def _filter_line_mode(
    content: str,
    blocked: Callable[[str], bool],
    path_of: Callable[[str], str],
) -> tuple[str, bool]:
    """Filter one-path-per-line modes (files_with_matches / count)."""
    kept: list[str] = []
    removed = False
    for line in content.split("\n"):
        if _looks_absolute(line) and blocked(path_of(line)):
            removed = True
            continue
        kept.append(line)
    return "\n".join(kept), removed


def _filter_content_mode(
    content: str,
    blocked: Callable[[str], bool],
) -> tuple[str, bool]:
    """Filter ``content``-mode output: drop a blocked file's header + match lines.

    A header is a non-indented absolute path ending in ``:``; match lines are
    space/tab-indented (the formatter always prefixes ``  {n}: ``), so leading
    whitespace reliably distinguishes them from headers even when the matched
    text itself begins with ``/``.
    """
    kept: list[str] = []
    removed = False
    skip = False
    for line in content.split("\n"):
        if _looks_absolute(line) and line.endswith(":"):
            path = line[:-1]
            if blocked(path):
                removed = True
                skip = True
                continue
            skip = False
            kept.append(line)
        elif line.startswith((" ", "\t")):
            # A match line belongs to the current file: drop iff it's blocked.
            if skip:
                continue
            kept.append(line)
        elif line == "":
            # Blank line preserves the current skip state (a blocked file's block
            # has no internal blanks, so this only matters across sections).
            if skip:
                continue
            kept.append(line)
        else:
            # A genuine non-path, non-indented line (error prefix / guidance):
            # a new section, so stop skipping.
            skip = False
            kept.append(line)
    return "\n".join(kept), removed
