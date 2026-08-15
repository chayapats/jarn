"""Explicit slash-command registry for :class:`~jarn.controller.core.Controller`."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from jarn.commands.registry import COMMAND_SPECS, CommandSpec, canonical_name, core_command_names
from jarn.controller.commands import config, diagnostics, memory, meta, session

if TYPE_CHECKING:
    from jarn.controller.core import CommandResult, Controller

CommandHandler = Callable[["Controller", str], "CommandResult"]

_HANDLERS: dict[str, CommandHandler] = {
    # meta
    "help": meta.cmd_help,
    "init": meta.cmd_init,
    "skill": meta.cmd_skill,
    "skills": meta.cmd_skills,
    "login": meta.cmd_login,
    "logout": meta.cmd_logout,
    # config
    "config": config.cmd_config,
    "preset": config.cmd_preset,
    "sandbox": config.cmd_sandbox,
    "model": config.cmd_model,
    "mode": config.cmd_mode,
    "trust": config.cmd_trust,
    # session
    "sessions": session.cmd_sessions,
    "clear": session.cmd_clear,
    "compact": session.cmd_compact,
    "undo": session.cmd_undo,
    "redo": session.cmd_redo,
    "quit": session.cmd_quit,
    # memory
    "memory": memory.cmd_memory,
    "wiki": memory.cmd_wiki,
    "map": memory.cmd_map,
    # diagnostics
    "status": diagnostics.cmd_status,
    "doctor": diagnostics.cmd_doctor,
    "cost": diagnostics.cmd_cost,
    "context": diagnostics.cmd_context,
    "verbose": diagnostics.cmd_verbose,
    "focus": diagnostics.cmd_focus,
    "tools": diagnostics.cmd_tools,
    "title": diagnostics.cmd_title,
    "modules": diagnostics.cmd_modules,
    "module": diagnostics.cmd_module,
    "permissions": diagnostics.cmd_permissions,
    "mcp": diagnostics.cmd_mcp,
    "telemetry": diagnostics.cmd_telemetry,
    "ps": diagnostics.cmd_ps,
    "checkpoints": diagnostics.cmd_checkpoints,
}

REGISTRY: dict[str, CommandHandler] = {}
for spec in COMMAND_SPECS:
    if spec.name not in core_command_names():
        continue
    handler = _HANDLERS.get(canonical_name(spec.name) or spec.name)
    if handler is not None:
        REGISTRY[spec.name] = handler

__all__ = ["CommandHandler", "CommandSpec", "REGISTRY"]
