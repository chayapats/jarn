"""Built-in /meta slash-command handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.markup import escape as _escape_markup

from jarn.controller.core import CommandResult
from jarn.extensibility.commands import format_help
from jarn.memory import write_jarn_md

if TYPE_CHECKING:
    from jarn.controller.core import Controller


def cmd_help(ctrl: Controller, args: str) -> CommandResult:
    custom = ctrl.runtime.commands if ctrl.runtime else None
    return CommandResult(
        format_help(
            custom,
            custom_description=lambda c: getattr(c, "description", ""),
        )
    )


def cmd_init(ctrl: Controller, args: str) -> CommandResult:
    try:
        path = write_jarn_md(ctrl.project_root, overwrite=args.strip() == "--force")
    except FileExistsError as exc:
        return CommandResult(f"{exc} (use /init --force to overwrite)")
    return CommandResult(f"Created {path}. Edit it to give J.A.R.N. project context.")


def cmd_skills(ctrl: Controller, args: str) -> CommandResult:
    if not ctrl.runtime or not ctrl.runtime.skills:
        return CommandResult("No skills loaded.")
    lines = ["[b]Skills[/b]"]
    for s in ctrl.runtime.skills.values():
        trig = "manual" if s.is_manual else "auto"
        lines.append(
            f"  [cyan]{_escape_markup(s.name)}[/cyan] "
            f"([dim]{trig}, {s.scope}[/dim]) — {_escape_markup(s.description)}"
        )
    return CommandResult("\n".join(lines))
