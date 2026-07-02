"""Unified slash-command registry shared by Controller and REPL."""

from jarn.commands.registry import (
    COMMAND_SPECS,
    CommandLayer,
    CommandSpec,
    HelpGroup,
    core_command_names,
    route_for_spec,
    spec_by_name,
    ui_command_names,
)

__all__ = [
    "COMMAND_SPECS",
    "CommandLayer",
    "CommandSpec",
    "HelpGroup",
    "core_command_names",
    "route_for_spec",
    "spec_by_name",
    "ui_command_names",
]
