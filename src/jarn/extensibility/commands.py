"""Slash commands — built-in plus user-defined.

Built-in commands are handled by the app (``/model``, ``/cost``, ``/clear`` …).
Custom commands are markdown files under ``~/.jarn/commands`` and
``<project>/.jarn/commands``; the file body is a prompt template expanded with
the user's arguments and sent to the agent. Frontmatter::

    ---
    name: review            # defaults to filename
    description: Review the current diff for bugs.
    ---
    Review the staged diff. Focus on: $ARGS
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from rich.markup import escape as _escape_rich

from jarn.config import paths
from jarn.extensibility.frontmatter import discover, parse

CommandRoute = Literal["controller", "repl", "agent_template"]
HelpGroup = Literal["Daily", "Setup", "Session"]


@dataclass(frozen=True, slots=True)
class BuiltinCommand:
    name: str
    description: str
    route: CommandRoute
    usage: str = ""
    group: HelpGroup = "Daily"


BUILTINS: tuple[BuiltinCommand, ...] = (
    BuiltinCommand("help", "Show available commands and shortcuts.", "controller", group="Daily"),
    BuiltinCommand(
        "model",
        "Show or switch the active model; /model refresh re-queries local endpoints.",
        "repl",
        usage="[/ref|refresh]",
        group="Daily",
    ),
    BuiltinCommand(
        "mode",
        "Show or switch the permission mode (plan/ask/auto-edit/yolo).",
        "repl",
        usage="[plan|ask|auto-edit|yolo]",
        group="Daily",
    ),
    BuiltinCommand("cost", "Show session token usage and cost.", "controller", group="Daily"),
    BuiltinCommand("undo", "Revert the last agent turn's file changes.", "controller", group="Daily"),
    BuiltinCommand("redo", "Re-apply the last undone agent turn's file changes.", "controller", group="Daily"),
    BuiltinCommand("abort", "Cancel the running turn and roll back its file changes.", "repl", group="Daily"),
    BuiltinCommand(
        "commit",
        "Draft a commit message from the current diff and commit (with approval).",
        "repl",
        group="Daily",
    ),
    BuiltinCommand(
        "review",
        "Review the current working-tree diff for bugs and quality (read-only).",
        "repl",
        group="Daily",
    ),
    BuiltinCommand(
        "compact",
        "Summarize and compact the conversation context.",
        "repl",
        group="Daily",
    ),
    BuiltinCommand(
        "expand",
        "Open the last turn's full tool output in the pager (same as Ctrl+O).",
        "repl",
        group="Daily",
    ),
    BuiltinCommand(
        "memory",
        "List, search, show, add, update, delete, or dump long-term memory.",
        "controller",
        usage="[search|show|add|update|delete|dump] ...",
        group="Daily",
    ),
    BuiltinCommand("clear", "Clear the conversation and start a fresh thread.", "controller", group="Daily"),
    BuiltinCommand(
        "config",
        "View or edit settings: /config, /config get <key>, /config set <key> <value> (persists).",
        "controller",
        group="Setup",
    ),
    BuiltinCommand(
        "preset",
        "Show or apply a preset — a shortcut that sets mode + sandbox at once.",
        "controller",
        usage="[<preset-name>]",
        group="Setup",
    ),
    BuiltinCommand(
        "profile",
        "Deprecated alias of /preset (kept working).",
        "controller",
        usage="[<preset-name>]",
        group="Setup",
    ),
    BuiltinCommand(
        "sandbox",
        "Show or toggle the execution backend (local/sandbox).",
        "controller",
        usage="[on|off]",
        group="Setup",
    ),
    BuiltinCommand(
        "trust",
        "Trust this project root and lift the untrusted review-only floor.",
        "controller",
        group="Setup",
    ),
    BuiltinCommand(
        "mcp",
        "Show configured MCP servers with per-server health and last error.",
        "controller",
        usage="[status]",
        group="Setup",
    ),
    BuiltinCommand("skills", "List available skills.", "controller", group="Setup"),
    BuiltinCommand("init", "Create a JARN.md project context file.", "controller", group="Setup"),
    BuiltinCommand("permissions", "Show current permission rules and allowlist.", "controller", group="Setup"),
    BuiltinCommand(
        "doctor",
        "Diagnose configuration, providers, and keys.",
        "controller",
        group="Setup",
    ),
    BuiltinCommand("resume", "Pick a previous session to resume.", "repl", group="Session"),
    BuiltinCommand("sessions", "List and resume previous sessions.", "controller", group="Session"),
    BuiltinCommand("checkpoints", "List recent auto-checkpoints.", "controller", group="Session"),
    BuiltinCommand(
        "ps",
        "List or kill background processes (from run_in_background).",
        "controller",
        usage="[kill <id>]",
        group="Session",
    ),
    BuiltinCommand(
        "queue",
        "Show or manage queued input lines (while a turn is running).",
        "repl",
        usage="[clear|cancel <n>|move <from> <to>]",
        group="Session",
    ),
    BuiltinCommand(
        "map",
        "Show the ranked repo map (codebase overview).",
        "controller",
        usage="[focus] [--refresh]",
        group="Session",
    ),
    BuiltinCommand(
        "wiki",
        "Search or list wiki knowledge-base pages.",
        "controller",
        usage="[search <q>|list]",
        group="Session",
    ),
    BuiltinCommand("quit", "Exit J.A.R.N.", "controller", group="Session"),
)

# Backward-compatible name → description map.
BUILTIN_COMMANDS: dict[str, str] = {cmd.name: cmd.description for cmd in BUILTINS}

_BUILTIN_BY_NAME: dict[str, BuiltinCommand] = {cmd.name: cmd for cmd in BUILTINS}

HELP_SHORTCUTS = (
    "Tab complete · ↑/↓ history · Shift+Tab mode · "
    "Ctrl+O or /expand last output · Ctrl+V paste image (macOS) · "
    "Esc cancel turn · Ctrl+C twice to quit · "
    "! <cmd> run shell command directly"
)
HELP_COPY_HINT = "Copy: drag-select + ⌘C in your terminal (native scrollback)."


def builtin_names() -> list[str]:
    return [cmd.name for cmd in BUILTINS]


def builtin_command(name: str) -> BuiltinCommand | None:
    return _BUILTIN_BY_NAME.get(name)


def route_for(name: str) -> CommandRoute | Literal["custom", "unknown"]:
    cmd = builtin_command(name)
    if cmd is not None:
        return cmd.route
    return "unknown"


def completion_names(custom: dict[str, Any] | None = None) -> list[str]:
    return sorted(completion_catalog(custom))


def completion_catalog(custom: dict[str, Any] | None = None) -> dict[str, str]:
    """Slash-command names mapped to short descriptions (built-ins + custom)."""
    catalog = {cmd.name: cmd.description for cmd in BUILTINS}
    if custom:
        for name in sorted(custom):
            cmd = custom[name]
            catalog[name] = getattr(cmd, "description", "") or ""
    return catalog


_HELP_GROUP_ORDER: tuple[HelpGroup, ...] = ("Daily", "Setup", "Session")

HELP_GLYPH_LEGEND = (
    "◇ plan · ◆ ask · ⚡ auto-edit · ⚠ yolo · "
    "● key ok · ✗ key fail · queue N = lines waiting while a turn runs"
)


def format_help(
    custom: dict[str, Any] | None = None,
    *,
    custom_description: Callable[[Any], str] | None = None,
) -> str:
    """Build ``/help`` body (Rich markup), grouped by section."""
    lines: list[str] = []

    # Group builtins by their group field, preserving BUILTINS declaration order.
    grouped: dict[str, list[BuiltinCommand]] = {}
    for cmd in BUILTINS:
        grouped.setdefault(cmd.group, []).append(cmd)

    for group_name in _HELP_GROUP_ORDER:
        cmds = grouped.get(group_name, [])
        if not cmds:
            continue
        lines.append(f"[b]{group_name}[/b]")
        for cmd in cmds:
            suffix = f" {_escape_rich(cmd.usage)}" if cmd.usage else ""
            lines.append(
                f"  [cyan]/{cmd.name}{suffix}[/cyan] — {_escape_rich(cmd.description)}"
            )

    if custom:
        lines.append("\n[b]Project commands[/b]")
        for command in custom.values():
            name = getattr(command, "name", "")
            desc = (
                custom_description(command)
                if custom_description is not None
                else getattr(command, "description", "")
            )
            lines.append(
                f"  [cyan]/{_escape_rich(name)}[/cyan] — {_escape_rich(desc)}"
            )

    lines.append("\n[b]Shortcuts[/b]")
    lines.append(f"  [dim]{HELP_SHORTCUTS}[/dim]")
    lines.append(f"  [dim]{HELP_COPY_HINT}[/dim]")

    lines.append("\n[b]Toolbar glyphs[/b]")
    lines.append(f"  [dim]{HELP_GLYPH_LEGEND}[/dim]")

    return "\n".join(lines)


def readme_command_rows() -> list[tuple[str, str]]:
    """(command cell, description) rows for README parity tests."""
    rows: list[tuple[str, str]] = []
    for cmd in BUILTINS:
        if cmd.usage:
            rows.append((f"`/{cmd.name} {cmd.usage}`", cmd.description))
        else:
            rows.append((f"`/{cmd.name}`", cmd.description))
    return rows


@dataclass(slots=True)
class CustomCommand:
    name: str
    description: str
    template: str
    path: Path

    def render(self, args: str) -> str:
        """Expand the template with the given argument string."""
        text = self.template
        if "$ARGS" in text:
            return text.replace("$ARGS", args.strip())
        return f"{text.rstrip()}\n\n{args.strip()}" if args.strip() else text


def command_dirs(
    project_root: Path | None = None,
    *,
    read_claude_dir: bool = True,
) -> list[Path]:
    """Return the ordered list of command directories to scan.

    ``.jarn`` directories are listed before ``.claude`` so that
    project-specific overrides always take precedence on name conflicts
    (last-write-wins in :func:`load_commands`).
    """
    # Lower-priority (.claude) dirs first so that higher-priority (.jarn)
    # entries overwrite them in the load loop.
    dirs: list[Path] = []
    if read_claude_dir:
        dirs.append(paths.global_claude_subdir("commands"))
        claude_pdir = paths.project_claude_dir(project_root)
        if claude_pdir:
            dirs.append(claude_pdir / "commands")
    dirs.append(paths.global_subdir("commands"))
    pdir = paths.project_dir(project_root)
    if pdir:
        dirs.append(pdir / "commands")
    return dirs


def load_commands(
    project_root: Path | None = None,
    *,
    project_trusted: bool = True,
    read_claude_dir: bool = True,
) -> dict[str, CustomCommand]:
    """Load custom commands keyed by name.

    Precedence (highest first): project ``.jarn`` > global ``.jarn`` >
    global ``.claude`` > project ``.claude``. Built-in names are never
    shadowed — a conflicting custom file gets a ``-custom`` suffix instead.
    Project-tier ``.claude/commands`` is skipped when ``project_trusted`` is
    ``False``.
    """
    out: dict[str, CustomCommand] = {}
    pdir = paths.project_dir(project_root)
    claude_pdir = paths.project_claude_dir(project_root)

    def _is_project(path: Path) -> bool:
        if pdir and str(path).startswith(str(pdir)):
            return True
        return bool(claude_pdir and str(path).startswith(str(claude_pdir)))

    for path in discover(command_dirs(project_root, read_claude_dir=read_claude_dir)):
        is_proj = _is_project(path)
        if is_proj and not project_trusted:
            continue
        doc = parse(path)
        name = str(doc.meta.get("name") or path.stem)
        if name in BUILTIN_COMMANDS:
            # Don't let a custom file shadow a built-in command.
            name = f"{name}-custom"
        out[name] = CustomCommand(
            name=name,
            description=str(doc.meta.get("description", "")),
            template=doc.body,
            path=path,
        )
    return out


@dataclass(slots=True, frozen=True)
class ParsedInput:
    """Result of parsing a raw input line into a command, a shell escape, or chat.

    Exactly one of ``is_command``, ``is_shell``, or neither is true per instance.
    Shell escapes (``! <cmd>``) bypass the agent entirely — the REPL runs them
    directly via :class:`~jarn.agent.local_backend.CancellableLocalShellBackend`.
    """

    is_command: bool
    name: str = ""
    args: str = ""
    text: str = ""
    is_shell: bool = False
    shell_command: str = ""


def parse_input(line: str) -> ParsedInput:
    """Split a leading ``/command args`` or ``! cmd`` from plain chat text."""
    stripped = line.strip()
    if stripped.startswith("/") and len(stripped) > 1:
        rest = stripped[1:]
        name, _, args = rest.partition(" ")
        return ParsedInput(is_command=True, name=name.strip(), args=args.strip())
    if stripped.startswith("!"):
        # ``!git status`` and ``! git status`` both work; bare ``!`` is a no-op.
        shell_cmd = stripped[1:].lstrip()
        return ParsedInput(is_command=False, is_shell=True, shell_command=shell_cmd)
    return ParsedInput(is_command=False, text=line)
