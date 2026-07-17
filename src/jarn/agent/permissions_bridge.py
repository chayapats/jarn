"""Translate deepagents tool calls into permission-engine :class:`Action`s.

deepagents exposes a fixed set of built-in tools. This module maps a tool name +
arguments to an :class:`~jarn.permissions.Action`, and lists which tools mutate
state (and therefore must be gated behind an interrupt).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from langchain.agents.middleware import InterruptOnConfig

from jarn.permissions import Action, ActionKind

#: Tools that change the world and must be evaluated by the permission engine.
MUTATING_TOOLS = ("write_file", "edit_file", "execute")

#: Wiki tools that mutate the knowledge base — gated like file writes.
WIKI_MUTATING_TOOLS = ("wiki_write", "wiki_append")

#: Wiki read-only tools — never gated.
WIKI_READONLY_TOOLS = ("wiki_search", "wiki_read")

#: Fixed-name tools deepagents' ``AsyncSubAgentMiddleware`` injects when async
#: subagents are configured. They make remote HTTP calls to a LangGraph
#: deployment (launch/check/update/cancel/list background runs), so they must
#: route through the permission engine (→ ``ActionKind.NETWORK``) rather than
#: bypass it. Only gated when async subagents are actually configured (see
#: ``interrupt_map(include_async=...)``); otherwise the names don't exist.
ASYNC_SUBAGENT_TOOLS = (
    "start_async_task",
    "check_async_task",
    "update_async_task",
    "cancel_async_task",
    "list_async_tasks",
)

#: Read-only tools — always permitted, never interrupted.
READONLY_TOOLS = ("read_file", "ls", "glob", "grep")

#: Planning / internal tools — always permitted.
INTERNAL_TOOLS = ("write_todos", "task")


def interrupt_map(
    extra_tools: Iterable[str] = (),
    *,
    include_async: bool = False,
) -> dict[str, bool | InterruptOnConfig]:
    """Build the ``interrupt_on`` dict for ``create_deep_agent``.

    **Every** mutating tool is gated in **every** mode: the permission engine —
    not this map — decides ALLOW/ASK/DENY. This is deliberate. An in-scope file
    edit in auto-edit/yolo auto-resolves to ALLOW (no prompt), but the engine's
    danger-guard still inspects it, so a write to a sensitive path (``.git/``,
    ``.ssh/``) or out of scope is caught even in YOLO. Gating ``edit_file`` only
    in some modes (the old behaviour) let edits skip the danger-guard entirely.

    ``extra_tools`` gates additional tools — the built-in web tools and any
    MCP-loaded tools — so network / external-mutating tools route through the
    same policy (they map to ``ActionKind.NETWORK`` → ASK by default) instead of
    bypassing it.

    ``include_async`` adds the five :data:`ASYNC_SUBAGENT_TOOLS`. Pass it only
    when async subagents are configured (deepagents injects those tools then and
    only then); otherwise gating phantom names is harmless but pointless.
    """
    gated = [*MUTATING_TOOLS, *extra_tools]
    if include_async:
        gated.extend(ASYNC_SUBAGENT_TOOLS)
    # Wiki mutating tools are gated here when present in extra_tools so the
    # engine evaluates them (write → WRITE action with the page path as target).
    # We rely on extra_tools already containing them when wiki is enabled;
    # no special flag needed because they flow through the same mechanism as
    # other extra tools (web, MCP).
    return {t: True for t in gated}


#: Background-process tools. Starting one runs a shell command, so it is gated
#: exactly like ``execute`` (SHELL → danger-guard inspects the command). Inspecting
#: / killing / listing only touch processes the agent itself started, so they are
#: read-only controls that never need a prompt.
BACKGROUND_START_TOOL = "run_in_background"
BACKGROUND_CONTROL_TOOLS = ("check_background", "kill_background", "list_background")

#: Canonical NAMES of the read-only/local builtin tools the engine would
#: auto-ALLOW in every mode, so gating them buys no policy and costs a full graph
#: pause/checkpoint/resume per call. This set is used ONLY to select which
#: locally-built tool instances ``builtin_tools._wire_builtin_tools`` collects as
#: ungated; enforcement in the runtime is by OBJECT IDENTITY of those instances,
#: never by matching names at gate time. Names — and metadata — are both forgeable
#: by an MCP server (langchain-mcp-adapters copies server ToolAnnotations into
#: BaseTool.metadata), so a server exposing ``wiki_read``/``repo_map``/
#: ``check_background`` (or tagging itself ungated) stays gated: it is not one of
#: the objects we constructed.
UNGATED_EXTRA_TOOLS = frozenset({
    *WIKI_READONLY_TOOLS,
    "repo_map",
    *BACKGROUND_CONTROL_TOOLS,
})


def tool_to_action(tool_name: str, args: dict[str, Any]) -> Action:
    """Map a tool call to an Action the permission engine can evaluate.

    Classification is name-keyed, and that is SOUND because MCP-provided tools can
    never reach here under a builtin name: the loader namespaces every MCP tool to
    ``mcp__<server>__<tool>`` (:func:`jarn.extensibility.mcp._namespace_tool`), and
    the runtime drops any un-prefixed extra tool whose name collides with a reserved
    builtin table (:data:`jarn.agent.runtime._RESERVED_BUILTIN_NAMES`). So a
    non-prefixed name matching a builtin table below is only ever produced by jarn
    itself; a ``mcp__``-prefixed name falls through to ``ActionKind.NETWORK`` and is
    gated. This closes the plan-mode bypass where an MCP tool named ``wiki_read``
    would classify as READ and be auto-allowed.
    """
    if tool_name == "execute":
        command = args.get("command") or args.get("cmd") or _stringify(args)
        return Action(ActionKind.SHELL, target=str(command), tool=tool_name)
    if tool_name == BACKGROUND_START_TOOL:
        command = args.get("command") or args.get("cmd") or _stringify(args)
        return Action(ActionKind.SHELL, target=str(command), tool=tool_name)
    if tool_name in BACKGROUND_CONTROL_TOOLS:
        return Action(ActionKind.READ, target=str(args.get("id", "") or "bg"), tool=tool_name)
    if tool_name in ("write_file", "edit_file"):
        path = args.get("file_path") or args.get("path") or args.get("filename") or ""
        return Action(ActionKind.WRITE, target=str(path), tool=tool_name)
    if tool_name in READONLY_TOOLS:
        path = args.get("file_path") or args.get("path") or args.get("pattern") or ""
        return Action(ActionKind.READ, target=str(path), tool=tool_name)
    # Wiki mutating tools map to WRITE so the engine evaluates them exactly
    # like file writes: auto-allowed in auto-edit/yolo, prompted in ask.
    if tool_name in WIKI_MUTATING_TOOLS:
        page = args.get("page") or args.get("name") or ""
        return Action(ActionKind.WRITE, target=str(page), tool=tool_name)
    # Wiki read-only tools never need gating — map to READ for completeness.
    if tool_name in WIKI_READONLY_TOOLS:
        query = args.get("query") or args.get("page") or ""
        return Action(ActionKind.READ, target=str(query), tool=tool_name)
    # repo_map is a read-only, purely local codebase overview — it reads source
    # and returns a map, so it must not prompt (the NETWORK fallback would ASK).
    if tool_name == "repo_map":
        return Action(ActionKind.READ, target=str(args.get("focus", "") or "repo"), tool=tool_name)
    # Unknown/other tools: treat as network-ish actions requiring evaluation.
    return Action(ActionKind.NETWORK, target=_network_target(tool_name, args), tool=tool_name)


def _network_target(tool_name: str, args: dict[str, Any]) -> str:
    """Human-readable target for approval UI (URL, query, MCP server/tool, args)."""
    if tool_name == "web_fetch":
        url = args.get("url") or ""
        return f"web_fetch → {url}" if url else "web_fetch"
    if tool_name == "web_search":
        query = args.get("query") or args.get("q") or ""
        return f'web_search → {query!r}' if query else "web_search"
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__", 2)
        server = parts[1] if len(parts) > 1 else "?"
        tool = parts[2] if len(parts) > 2 else tool_name
        preview = _stringify(args)
        base = f"mcp/{server}/{tool}"
        if preview:
            preview = preview if len(preview) <= 120 else preview[:117] + "..."
            return f"{base} ({preview})"
        return base
    if tool_name in ASYNC_SUBAGENT_TOOLS:
        preview = _stringify(args)
        if preview:
            preview = preview if len(preview) <= 120 else preview[:117] + "..."
            return f"{tool_name} ({preview})"
        return tool_name
    return tool_name


def _stringify(args: dict[str, Any]) -> str:
    return " ".join(f"{v}" for v in args.values())
