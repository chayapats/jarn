"""Single source of truth for built-in slash-command metadata.

``/help``, completion, README parity, usage errors, and controller dispatch all
read this module. Add a command here first; handlers and docs follow.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from jarn.tui.grammar import HELP_GROUP_ORDER, HelpGroup

CommandLayer = Literal["ui", "core", "both"]
CommandRoute = Literal["controller", "repl", "agent_template"]


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    description: str
    layer: CommandLayer
    group: HelpGroup = "Work"
    usage: str = ""
    interactive_only: bool = False
    blurb: str = ""
    examples: tuple[str, ...] = ()
    related: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    alias_of: str = ""
    index: bool = True


def _c(
    name: str,
    description: str,
    layer: CommandLayer,
    *,
    group: HelpGroup = "Work",
    usage: str = "",
    interactive_only: bool = False,
    blurb: str = "",
    examples: tuple[str, ...] = (),
    related: tuple[str, ...] = (),
    aliases: tuple[str, ...] = (),
    alias_of: str = "",
    index: bool = True,
) -> CommandSpec:
    return CommandSpec(
        name,
        description,
        layer,
        group=group,
        usage=usage,
        interactive_only=interactive_only,
        blurb=blurb or description,
        examples=examples,
        related=related,
        aliases=aliases,
        alias_of=alias_of,
        index=index,
    )


COMMAND_SPECS: tuple[CommandSpec, ...] = (
    _c(
        "help",
        "Show commands, or details for one command.",
        "core",
        usage="[name]",
        examples=("/help", "/help compact"),
        related=("status", "config"),
        blurb="List every built-in command, grouped by what it is for. "
        "Pass a name for syntax, examples, and related commands.",
    ),
    _c(
        "status",
        "Show directory, model, mode, context, and a local recap.",
        "core",
        group="Session",
        related=("cost", "context", "model", "mode"),
        blurb="Offline session summary: where you are, which model and "
        "permission mode are active, how full the context window is, and a "
        "short recap of recent tools and files. No model call.",
    ),
    _c(
        "model",
        "Show or switch the active model.",
        "both",
        usage="[name|refresh]",
        examples=("/model", "/model refresh"),
        related=("status", "cost"),
        blurb="With no argument, opens the model picker. "
        "`/model refresh` re-queries local endpoints.",
    ),
    _c(
        "mode",
        "Show or switch how much J.A.R.N. may change.",
        "both",
        usage="[plan|ask|auto-edit|yolo]",
        examples=("/mode", "/mode ask"),
        related=("permissions", "preset", "sandbox"),
        blurb="How much the agent may change files and run commands. Mode ids stay English.",
    ),
    _c(
        "theme",
        "Show or switch the color theme.",
        "ui",
        usage="[dark|light|high-contrast|auto]",
        related=("config",),
    ),
    _c(
        "cost",
        "Show session tokens and estimated cost (alias: /usage).",
        "core",
        group="Session",
        aliases=("usage",),
        related=("context", "status"),
        blurb="Session spend, per-model totals, and cache reads. `/usage` is the same command.",
    ),
    _c(
        "usage",
        "Show session tokens and estimated cost (alias for /cost).",
        "core",
        group="Session",
        alias_of="cost",
        index=False,
        related=("cost", "context"),
    ),
    _c(
        "context",
        "Show what is filling the context window.",
        "core",
        group="Session",
        usage="[all]",
        related=("cost", "compact", "modules"),
        blurb="Visual context-window gauge plus the token size of each active "
        "prompt module. `/context all` includes inactive modules.",
    ),
    _c(
        "verbose",
        "Cycle how much tool activity is shown.",
        "both",
        related=("focus", "expand"),
        blurb="Cycles off → new → all → verbose. "
        "off hides tool lines. new is the default (one line per tool). "
        "all includes live output tails. verbose keeps more argument detail. "
        "Session-only; persist with /config set ui.tool_progress.",
    ),
    _c(
        "focus",
        "Hide tool chrome and show only the answer.",
        "both",
        usage="[on|off|status]",
        examples=("/focus", "/focus off"),
        related=("verbose", "expand"),
        blurb="Display-only. Hidden tool lines are still in /expand. "
        "Turning focus on remembers your /verbose setting and restores it after.",
    ),
    _c(
        "modules",
        "Open the prompt-module picker.",
        "both",
        usage="[active]",
        related=("module", "skills", "context"),
        blurb="No args opens the picker. `/modules active` prints modules "
        "currently in the assembled prompt.",
    ),
    _c(
        "module",
        "Activate or deactivate a prompt module.",
        "both",
        usage="[on <name> [turn|session] | off <name>]",
        related=("modules", "skill"),
    ),
    _c(
        "undo",
        "Revert the last agent turn's file changes.",
        "core",
        related=("redo", "abort", "checkpoints"),
    ),
    _c(
        "redo",
        "Re-apply the last undone file changes.",
        "core",
        related=("undo",),
    ),
    _c(
        "abort",
        "Stop this turn and roll back its file changes.",
        "ui",
        related=("undo", "queue"),
    ),
    _c(
        "commit",
        "Draft a commit from the current diff (asks first).",
        "ui",
        related=("review",),
    ),
    _c(
        "review",
        "Read-only review of the current diff.",
        "ui",
        related=("commit", "diff"),
    ),
    _c(
        "diff",
        "Show a git diff of staged, working-tree, or session files.",
        "core",
        usage="[staged|all|session]",
        examples=("/diff", "/diff staged", "/diff session"),
        related=("commit", "review"),
        blurb="Default: staged if the index is dirty, otherwise the working tree. "
        "`session` limits to files this thread edited.",
    ),
    _c(
        "compact",
        "Summarize and continue in a fresh thread.",
        "both",
        group="Session",
        usage="[status]",
        interactive_only=True,
        related=("clear", "context", "cost"),
        blurb="Summarize this conversation and keep going in a new thread. "
        "`/compact status` shows whether auto-compact is on.",
    ),
    _c(
        "expand",
        "Show the last tool output in full.",
        "ui",
        related=("verbose", "focus"),
        blurb="Opens the pager (same as Ctrl+O).",
    ),
    _c(
        "memory",
        "List or edit long-term memory.",
        "core",
        usage="[search|show|add|update|delete|dump] …",
        examples=("/memory", "/memory search flaky tests"),
        related=("skills", "wiki"),
    ),
    _c(
        "clear",
        "Start a fresh conversation (alias: /new).",
        "core",
        group="Session",
        aliases=("new",),
        related=("compact", "sessions"),
    ),
    _c(
        "new",
        "Start a fresh conversation (alias for /clear).",
        "core",
        group="Session",
        alias_of="clear",
        index=False,
        related=("clear",),
    ),
    _c(
        "config",
        "View or edit settings.",
        "both",
        group="Setup",
        usage="[get <key> | set <key> <value>]",
        examples=("/config", "/config set ui.theme light"),
        related=("theme", "doctor"),
        blurb="No args opens the settings panel. Changes persist to ~/.jarn/config.yaml.",
    ),
    _c(
        "preset",
        "Show or apply a mode+sandbox shortcut.",
        "core",
        group="Setup",
        usage="[<name>]",
        related=("mode", "sandbox"),
    ),
    _c(
        "sandbox",
        "Show or toggle where commands run.",
        "core",
        group="Setup",
        usage="[docker|on|off]",
        related=("preset", "permissions"),
    ),
    _c(
        "trust",
        "Trust this project and lift the read-only floor.",
        "core",
        group="Setup",
        related=("permissions", "doctor"),
    ),
    _c(
        "add-dir",
        "Add a directory to this session's write scope.",
        "ui",
        group="Setup",
        usage="<path>",
        related=("trust",),
    ),
    _c(
        "mcp",
        "MCP server health, prompts, and resources.",
        "core",
        group="Setup",
        usage="[status|refresh|prompts|prompt <server> <name>|resources|read <server> <uri>]",
        related=("tools", "doctor"),
    ),
    _c(
        "telemetry",
        "Show telemetry opt-in and local sink stats.",
        "core",
        group="Setup",
        usage="status",
    ),
    _c(
        "skill",
        "Invoke a skill by name.",
        "core",
        group="Setup",
        usage="<name>",
        related=("skills",),
        blurb="Injects the skill body into this turn. Installed skills are also "
        "slash commands: `/skill-name` works like `/skill skill-name`.",
    ),
    _c(
        "skills",
        "List available skills.",
        "core",
        group="Setup",
        related=("skill", "modules"),
    ),
    _c(
        "init",
        "Create a JARN.md project context file.",
        "core",
        group="Setup",
        related=("status",),
    ),
    _c(
        "permissions",
        "Show permission rules and the allowlist.",
        "core",
        group="Setup",
        related=("mode", "trust"),
    ),
    _c(
        "key",
        "Set the API key for the current provider (keychain).",
        "ui",
        group="Setup",
        usage="[<key>]",
        related=("login", "doctor"),
    ),
    _c(
        "login",
        "Sign in to ChatGPT.",
        "core",
        group="Setup",
        related=("logout", "key"),
        blurb="Sign in to ChatGPT. Reports success only after the account is verified.",
    ),
    _c(
        "logout",
        "Sign out of ChatGPT.",
        "core",
        group="Setup",
        related=("login",),
        blurb="Sign out of ChatGPT. Removes only Codex-managed ChatGPT "
        "credentials; provider API keys are kept.",
    ),
    _c(
        "doctor",
        "Diagnose configuration, providers, and keys.",
        "core",
        group="Setup",
        related=("status", "config"),
    ),
    _c(
        "tools",
        "List tools the agent can use this session.",
        "core",
        group="Setup",
        related=("mcp", "permissions"),
    ),
    _c(
        "sessions",
        "Pick a previous session, or list them (alias: /resume).",
        "core",
        group="Session",
        usage="[q]",
        aliases=("resume",),
        related=("title", "rewind"),
        blurb="In the REPL, opens the session picker. Pass a query to filter "
        "by title or id. Non-TTY callers get a text list.",
    ),
    _c(
        "resume",
        "Pick a previous session to resume (alias for /sessions).",
        "core",
        group="Session",
        alias_of="sessions",
        index=False,
        related=("sessions", "title"),
    ),
    _c(
        "rewind",
        "Rewind to an earlier turn (forks a new thread).",
        "ui",
        group="Session",
        related=("undo", "sessions"),
        blurb="Fork to an earlier turn and continue. Optionally restore files "
        "to that turn's checkpoint too.",
    ),
    _c(
        "title",
        "Show or set this session's title.",
        "core",
        group="Session",
        usage="[text]",
        examples=("/title", "/title fix toolbar wrap"),
        related=("sessions", "status"),
    ),
    _c(
        "checkpoints",
        "List recent auto-checkpoints.",
        "core",
        group="Session",
        related=("undo", "redo"),
    ),
    _c(
        "ps",
        "List or kill background processes.",
        "core",
        group="Session",
        usage="[kill <id>]",
        related=("queue",),
    ),
    _c(
        "queue",
        "Show or manage queued input lines.",
        "ui",
        group="Session",
        usage="[clear|cancel <n>|move <from> <to>|steer <n>]",
        related=("abort", "busy"),
    ),
    _c(
        "busy",
        "Set what Enter does while a turn is running.",
        "both",
        group="Session",
        usage="[interrupt|queue|steer|status]",
        examples=("/busy", "/busy steer", "/busy status"),
        related=("queue", "verbose"),
        blurb="Session-only. queue (default) holds the line until the turn "
        "ends. steer injects via the existing steer slot (needs ui.steering). "
        "interrupt aborts then runs the line. Persist with "
        "/config set ui.busy_input_mode.",
    ),
    _c(
        "map",
        "Show a map of this repository.",
        "core",
        group="Session",
        usage="[focus] [--refresh]",
        related=("wiki",),
    ),
    _c(
        "wiki",
        "Search or list wiki pages.",
        "core",
        group="Session",
        usage="[search <q>|list]",
        related=("memory", "map"),
    ),
    _c(
        "quit",
        "Exit J.A.R.N. (alias: /exit).",
        "core",
        group="Session",
        aliases=("exit",),
        related=("clear",),
    ),
    _c(
        "exit",
        "Exit J.A.R.N. (alias for /quit).",
        "core",
        group="Session",
        alias_of="quit",
        index=False,
        related=("quit",),
    ),
)


def _norm(name: str) -> str:
    return name.strip().lower().replace("-", "_")


_SPEC_BY_NAME: dict[str, CommandSpec] = {}
for _spec in COMMAND_SPECS:
    _SPEC_BY_NAME[_norm(_spec.name)] = _spec
    for _alias in _spec.aliases:
        _SPEC_BY_NAME.setdefault(_norm(_alias), _spec)


def spec_by_name(name: str) -> CommandSpec | None:
    """Resolve a command, alias, hyphen/underscore, or case variant."""
    return _SPEC_BY_NAME.get(_norm(name))


def canonical_name(name: str) -> str | None:
    spec = spec_by_name(name)
    if spec is None:
        return None
    return spec.alias_of or spec.name


def core_command_names() -> frozenset[str]:
    return frozenset(spec.name for spec in COMMAND_SPECS if spec.layer in ("core", "both"))


def ui_command_names() -> frozenset[str]:
    return frozenset(spec.name for spec in COMMAND_SPECS if spec.layer in ("ui", "both"))


_BOTH_REPL_ROUTE: frozenset[str] = frozenset({"model", "mode", "compact"})


def route_for_spec(spec: CommandSpec) -> CommandRoute:
    """Map a registry entry to the legacy ``route`` field on BuiltinCommand."""
    if spec.layer == "core":
        return "controller"
    if spec.layer == "ui":
        return "repl"
    if spec.name in _BOTH_REPL_ROUTE:
        return "repl"
    return "controller"


#: Dim extra on ``/help login`` / ``/help logout`` only — not on the index.
HELP_CLI_EQUIVALENT: dict[str, str] = {
    "login": "jarn auth login",
    "logout": "jarn auth logout",
}


def help_description_key(name: str) -> str:
    return f"help.cmd.{name}.description"


def help_blurb_key(name: str) -> str:
    return f"help.cmd.{name}.blurb"


def grouped_specs(*, index_only: bool = False) -> dict[str, list[CommandSpec]]:
    grouped: dict[str, list[CommandSpec]] = {}
    for spec in COMMAND_SPECS:
        if index_only and not spec.index:
            continue
        grouped.setdefault(spec.group, []).append(spec)
    return grouped


def help_group_order() -> tuple[HelpGroup, ...]:
    return HELP_GROUP_ORDER


def slash_usage(spec: CommandSpec) -> str:
    """``/name`` or ``/name usage`` for detail pages, usage errors, and README."""
    if spec.usage:
        return f"/{spec.name} {spec.usage}"
    return f"/{spec.name}"


def slash_index(spec: CommandSpec) -> str:
    """``/name`` for the ``/help`` index. Syntax lives on the detail page."""
    return f"/{spec.name}"


def parse_slash_line(text: str) -> tuple[str, str] | None:
    """``(name, args)`` for a leading slash line; strips a Telegram ``@bot`` suffix."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    head, _, tail = stripped.partition(" ")
    name = head[1:].split("@", 1)[0].strip().lower()
    if not name:
        return None
    return name, tail.strip()


#: Display pages the Telegram worker runs locally via ``handle_command``.
#: Read-only catalog pages — never mutate config, memory, or sandbox.
#: ``sessions`` / ``checkpoints`` / ``ps`` stay here (text list, not pickers).
GATEWAY_READONLY_COMMANDS = frozenset(
    {
        "status",
        "cost",
        "usage",
        "context",
        "tools",
        "permissions",
        "mcp",
        "sessions",
        "telemetry",
        "ps",
        "checkpoints",
        "modules",
        "doctor",
        "skills",
        "help",
        "map",
        "wiki",
    }
)

#: Session chrome + local session mutations that already have controller APIs.
#: Does not write YAML unless the user later ``/config set``.
GATEWAY_SESSION_COMMANDS = frozenset(
    {
        "verbose",
        "focus",
        "title",
        "model",
        "mode",
        "compact",
        "undo",
        "redo",
        "resume",
        "skill",
    }
)

#: Blocked on Telegram: never local ``handle_command`` as the mutate path, never
#: ``submit_turn``. Closed set — YAML writers, trust/auth, REPL-picker-only
#: names, and CLI-only close-out verbs (``diff``, ``busy``). Aliases (``new`` →
#: ``clear``, ``exit`` → ``quit``) resolve via
#: :func:`is_gateway_mutating_command`.
GATEWAY_MUTATING_COMMANDS = frozenset(
    {
        "config",
        "preset",
        "memory",
        "sandbox",
        "trust",
        "key",
        "login",
        "logout",
        "add-dir",
        "init",
        "module",
        "theme",
        "rewind",
        "queue",
        "abort",
        "commit",
        "review",
        "clear",
        "quit",
        "exit",
        "expand",
        "diff",
        "busy",
    }
)

#: Bot-layer names that never reach the agent. ``help`` is also readonly local;
#: ``new`` aliases mutating ``clear`` but Telegram ``/new`` is a fresh thread.
GATEWAY_ONLY_COMMANDS = frozenset({"stop", "new", "repo", "help", "reset"})

#: Union consumed by the gateway worker.
GATEWAY_LOCAL_COMMANDS = GATEWAY_READONLY_COMMANDS | GATEWAY_SESSION_COMMANDS

#: One-line refuse copy for :data:`GATEWAY_MUTATING_COMMANDS` (English fallback).
GATEWAY_MUTATING_NOTICE = "This command is not available on Telegram. Use the terminal / jarn CLI."

_TELEGRAM_BOTFATHER_NAME = re.compile(r"^[a-z0-9_]{1,32}$")

_GATEWAY_ONLY_MENU: dict[str, str] = {
    "stop": "Cancel the in-flight turn",
    "new": "Start a fresh thread",
    "repo": "Switch the active repo",
    "help": "Show commands",
    "reset": "Start a fresh thread",
}


def is_gateway_local_command(name: str) -> bool:
    """True when the Telegram worker should run this slash name locally."""
    spec = spec_by_name(name)
    if spec is None:
        return False
    return (spec.alias_of or spec.name) in GATEWAY_LOCAL_COMMANDS


def is_gateway_mutating_command(name: str) -> bool:
    """True when Telegram must refuse this builtin (never an agent prompt)."""
    spec = spec_by_name(name)
    if spec is None:
        return name.strip().lower() in GATEWAY_MUTATING_COMMANDS
    return (spec.alias_of or spec.name) in GATEWAY_MUTATING_COMMANDS


def gateway_mutating_notice(name: str = "", locale: str | None = None) -> str:
    """One-line terminal hint for a blocked Telegram slash name."""
    from jarn.tui.i18n import CATALOGS, t

    loc = locale if locale in CATALOGS else "en"
    shown = canonical_name(name) if name else ""
    if shown:
        return t("telegram.mutating.named", loc, name=shown)
    return t("telegram.mutating", loc)


def gateway_botfather_commands() -> tuple[tuple[str, str], ...]:
    """``(command, description)`` rows for Telegram ``setMyCommands``.

    Local catalog names plus gateway-only ``stop`` / ``new`` / ``repo`` /
    ``help`` / ``reset``. Mutating names are excluded (``/new`` is gateway-only
    and stays). Names must be Telegram-legal: ``[a-z0-9_]{1,32}``.
    """
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name in sorted(GATEWAY_LOCAL_COMMANDS | GATEWAY_ONLY_COMMANDS):
        if name in seen or not _TELEGRAM_BOTFATHER_NAME.fullmatch(name):
            continue
        if is_gateway_mutating_command(name) and name not in GATEWAY_ONLY_COMMANDS:
            continue
        seen.add(name)
        spec = spec_by_name(name)
        desc = _GATEWAY_ONLY_MENU.get(name) or (spec.description if spec else name)
        rows.append((name, desc[:256]))
    return tuple(rows)
