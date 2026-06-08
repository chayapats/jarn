"""Extensibility subsystem — skills, commands, custom subagents, hooks, MCP."""

from jarn.extensibility.commands import (
    BUILTIN_COMMANDS,
    BUILTINS,
    BuiltinCommand,
    CustomCommand,
    ParsedInput,
    builtin_names,
    format_help,
    load_commands,
    parse_input,
    readme_command_rows,
)
from jarn.extensibility.hooks import HookEvent, HookResult, HookRunner
from jarn.extensibility.skills import Skill, auto_skill_catalog, load_skills
from jarn.extensibility.subagents import CustomSubagent, load_subagents

__all__ = [
    "BUILTIN_COMMANDS",
    "BUILTINS",
    "BuiltinCommand",
    "CustomCommand",
    "builtin_names",
    "format_help",
    "readme_command_rows",
    "CustomSubagent",
    "HookEvent",
    "HookResult",
    "HookRunner",
    "ParsedInput",
    "Skill",
    "auto_skill_catalog",
    "load_commands",
    "load_skills",
    "load_subagents",
    "parse_input",
]
