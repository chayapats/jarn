"""Single source of truth for built-in slash-command metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CommandLayer = Literal["ui", "core", "both"]
HelpGroup = Literal["Daily", "Setup", "Session"]
CommandRoute = Literal["controller", "repl", "agent_template"]


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    description: str
    layer: CommandLayer
    group: HelpGroup = "Daily"
    usage: str = ""
    interactive_only: bool = False


COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec("help", "Show available commands and shortcuts.", "core", group="Daily"),
    CommandSpec(
        "model",
        "Show or switch the active model; /model refresh re-queries local endpoints.",
        "both",
        usage="[/ref|refresh]",
        group="Daily",
    ),
    CommandSpec(
        "mode",
        "Show or switch the permission mode (plan/ask/auto-edit/yolo).",
        "both",
        usage="[plan|ask|auto-edit|yolo]",
        group="Daily",
    ),
    CommandSpec(
        "theme",
        "Show or switch the color theme (dark/light/high-contrast/auto).",
        "ui",
        usage="[dark|light|high-contrast|auto]",
        group="Daily",
    ),
    CommandSpec("cost", "Show session token usage and cost.", "core", group="Daily"),
    CommandSpec("undo", "Revert the last agent turn's file changes.", "core", group="Daily"),
    CommandSpec(
        "redo",
        "Re-apply the last undone agent turn's file changes.",
        "core",
        group="Daily",
    ),
    CommandSpec("abort", "Cancel the running turn and roll back its file changes.", "ui", group="Daily"),
    CommandSpec(
        "commit",
        "Draft a commit message from the current diff and commit (with approval).",
        "ui",
        group="Daily",
    ),
    CommandSpec(
        "review",
        "Review the current working-tree diff for bugs and quality (read-only).",
        "ui",
        group="Daily",
    ),
    CommandSpec(
        "compact",
        "Summarize and compact the conversation context.",
        "both",
        group="Daily",
        interactive_only=True,
    ),
    CommandSpec(
        "expand",
        "Open the last turn's full tool output in the pager (same as Ctrl+O).",
        "ui",
        group="Daily",
    ),
    CommandSpec(
        "memory",
        "List, search, show, add, update, delete, or dump long-term memory.",
        "core",
        usage="[search|show|add|update|delete|dump] ...",
        group="Daily",
    ),
    CommandSpec("clear", "Clear the conversation and start a fresh thread.", "core", group="Daily"),
    CommandSpec(
        "config",
        "View or edit settings: /config, /config get <key>, /config set <key> <value> (persists).",
        "both",
        group="Setup",
    ),
    CommandSpec(
        "preset",
        "Show or apply a preset — a shortcut that sets mode + sandbox at once.",
        "core",
        usage="[<preset-name>]",
        group="Setup",
    ),
    CommandSpec(
        "sandbox",
        "Show or toggle the execution backend (local/sandbox).",
        "core",
        usage="[on|off]",
        group="Setup",
    ),
    CommandSpec(
        "trust",
        "Trust this project root and lift the untrusted review-only floor.",
        "core",
        group="Setup",
    ),
    CommandSpec(
        "add-dir",
        "Add a directory to this session's write scope (multi-root; approval-gated).",
        "ui",
        usage="<path>",
        group="Setup",
    ),
    CommandSpec(
        "mcp",
        "Show configured MCP servers with per-server health and last error.",
        "core",
        usage="[status] [--refresh|refresh]",
        group="Setup",
    ),
    CommandSpec(
        "telemetry",
        "Show telemetry opt-in status and local sink stats.",
        "core",
        usage="status",
        group="Setup",
    ),
    CommandSpec("skills", "List available skills.", "core", group="Setup"),
    CommandSpec("init", "Create a JARN.md project context file.", "core", group="Setup"),
    CommandSpec("permissions", "Show current permission rules and allowlist.", "core", group="Setup"),
    CommandSpec(
        "key",
        "Set or replace the API key for the current provider (stored in the keychain).",
        "ui",
        usage="[<key>]",
        group="Setup",
    ),
    CommandSpec(
        "doctor",
        "Diagnose configuration, providers, and keys.",
        "core",
        group="Setup",
    ),
    CommandSpec("resume", "Pick a previous session to resume.", "ui", group="Session"),
    CommandSpec(
        "rewind",
        "Rewind to an earlier turn and continue (forks a new thread); "
        "optionally restore files to that turn too.",
        "ui",
        group="Session",
    ),
    CommandSpec("sessions", "List and resume previous sessions.", "core", group="Session"),
    CommandSpec("checkpoints", "List recent auto-checkpoints.", "core", group="Session"),
    CommandSpec(
        "ps",
        "List or kill background processes (from run_in_background).",
        "core",
        usage="[kill <id>]",
        group="Session",
    ),
    CommandSpec(
        "queue",
        "Show or manage queued input lines (while a turn is running).",
        "ui",
        usage="[clear|cancel <n>|move <from> <to>|steer <n>]",
        group="Session",
    ),
    CommandSpec(
        "map",
        "Show the ranked repo map (codebase overview).",
        "core",
        usage="[focus] [--refresh]",
        group="Session",
    ),
    CommandSpec(
        "wiki",
        "Search or list wiki knowledge-base pages.",
        "core",
        usage="[search <q>|list]",
        group="Session",
    ),
    CommandSpec("quit", "Exit J.A.R.N.", "core", group="Session"),
)

# Keyed by the normalized (hyphen→underscore) name so hyphenated commands like
# ``add-dir`` resolve regardless of the caller's separator (``spec_by_name``
# normalizes its query the same way).
_SPEC_BY_NAME: dict[str, CommandSpec] = {
    spec.name.replace("-", "_"): spec for spec in COMMAND_SPECS
}

_HELP_GROUP_ORDER: tuple[HelpGroup, ...] = ("Daily", "Setup", "Session")

# Both-layer commands whose primary entry point is the REPL UI.
_BOTH_REPL_ROUTE: frozenset[str] = frozenset({"model", "mode", "compact"})


def spec_by_name(name: str) -> CommandSpec | None:
    return _SPEC_BY_NAME.get(name.replace("-", "_"))


def core_command_names() -> frozenset[str]:
    return frozenset(spec.name for spec in COMMAND_SPECS if spec.layer in ("core", "both"))


def ui_command_names() -> frozenset[str]:
    return frozenset(spec.name for spec in COMMAND_SPECS if spec.layer in ("ui", "both"))


def route_for_spec(spec: CommandSpec) -> CommandRoute:
    """Map a registry entry to the legacy ``route`` field on :class:`BuiltinCommand`."""
    if spec.layer == "core":
        return "controller"
    if spec.layer == "ui":
        return "repl"
    if spec.name in _BOTH_REPL_ROUTE:
        return "repl"
    return "controller"


def grouped_specs() -> dict[str, list[CommandSpec]]:
    grouped: dict[str, list[CommandSpec]] = {}
    for spec in COMMAND_SPECS:
        grouped.setdefault(spec.group, []).append(spec)
    return grouped


def help_group_order() -> tuple[HelpGroup, ...]:
    return _HELP_GROUP_ORDER
