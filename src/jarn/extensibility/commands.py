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


@dataclass(frozen=True, slots=True)
class BuiltinCommand:
    name: str
    description: str
    route: CommandRoute
    usage: str = ""


BUILTINS: tuple[BuiltinCommand, ...] = (
    BuiltinCommand("help", "Show available commands and shortcuts.", "controller"),
    BuiltinCommand("init", "Create a JARN.md project context file.", "controller"),
    BuiltinCommand(
        "model",
        "Show or switch the active model.",
        "repl",
        usage="[/ref]",
    ),
    BuiltinCommand(
        "mode",
        "Show or switch the permission mode (plan/ask/auto-edit/yolo).",
        "repl",
        usage="[plan|ask|auto-edit|yolo]",
    ),
    BuiltinCommand(
        "sandbox",
        "Show or toggle the execution backend (local/sandbox).",
        "controller",
        usage="[on|off]",
    ),
    BuiltinCommand("cost", "Show session token usage and cost.", "controller"),
    BuiltinCommand(
        "compact",
        "Summarize and compact the conversation context.",
        "repl",
    ),
    BuiltinCommand(
        "expand",
        "Open the last turn's full tool output in the pager (same as Ctrl+O).",
        "repl",
    ),
    BuiltinCommand("clear", "Clear the conversation and start a fresh thread.", "controller"),
    BuiltinCommand("sessions", "List and resume previous sessions.", "controller"),
    BuiltinCommand("resume", "Pick a previous session to resume.", "repl"),
    BuiltinCommand("skills", "List available skills.", "controller"),
    BuiltinCommand(
        "memory",
        "List, search, show, add, update, or delete long-term memory.",
        "controller",
        usage="[search|show|add|update|delete] ...",
    ),
    BuiltinCommand("permissions", "Show current permission rules and allowlist.", "controller"),
    BuiltinCommand(
        "queue",
        "Show or manage queued input lines (while a turn is running).",
        "repl",
        usage="[clear|cancel <n>|move <from> <to>]",
    ),
    BuiltinCommand("quit", "Exit J.A.R.N.", "controller"),
)

# Backward-compatible name → description map.
BUILTIN_COMMANDS: dict[str, str] = {cmd.name: cmd.description for cmd in BUILTINS}

_BUILTIN_BY_NAME: dict[str, BuiltinCommand] = {cmd.name: cmd for cmd in BUILTINS}

HELP_SHORTCUTS = (
    "Tab complete · ↑/↓ history · Shift+Tab mode · "
    "Ctrl+O or /expand last output · Esc cancel turn · Ctrl+C twice to quit"
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


def format_help(
    custom: dict[str, Any] | None = None,
    *,
    custom_description: Callable[[Any], str] | None = None,
) -> str:
    """Build ``/help`` body (Rich markup)."""
    lines = ["[b]Built-in commands[/b]"]
    for cmd in BUILTINS:
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
    lines.append(f"\n[dim]Shortcuts: {HELP_SHORTCUTS}[/dim]")
    lines.append(f"[dim]{HELP_COPY_HINT}[/dim]")
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


def command_dirs(project_root: Path | None = None) -> list[Path]:
    dirs = [paths.global_subdir("commands")]
    pdir = paths.project_dir(project_root)
    if pdir:
        dirs.append(pdir / "commands")
    return dirs


def load_commands(
    project_root: Path | None = None,
    *,
    project_trusted: bool = True,
) -> dict[str, CustomCommand]:
    """Load custom commands keyed by name (project overrides global)."""
    out: dict[str, CustomCommand] = {}
    global_dir = paths.global_subdir("commands")
    for path in discover(command_dirs(project_root)):
        scope = "global" if str(path).startswith(str(global_dir)) else "project"
        if scope == "project" and not project_trusted:
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
    """Result of parsing a raw input line into a command or a chat message."""

    is_command: bool
    name: str = ""
    args: str = ""
    text: str = ""


def parse_input(line: str) -> ParsedInput:
    """Split a leading ``/command args`` from plain chat text."""
    stripped = line.strip()
    if stripped.startswith("/") and len(stripped) > 1:
        rest = stripped[1:]
        name, _, args = rest.partition(" ")
        return ParsedInput(is_command=True, name=name.strip(), args=args.strip())
    return ParsedInput(is_command=False, text=line)
