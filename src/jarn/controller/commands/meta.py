"""Built-in /meta slash-command handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.markup import escape as _escape_markup

from jarn.controller.core import CommandResult
from jarn.extensibility.commands import format_help
from jarn.extensibility.skills import find_skill, render_skill_invocation
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


def cmd_skill(ctrl: Controller, args: str) -> CommandResult:
    """`/skill <name>`: invoke a skill by name, injecting its body into the turn.

    ``manual``-trigger skills are kept out of the auto catalog (see
    ``skills.py``), so this is the ONLY entry point that can run them; auto/string
    skills resolve here too. The resolved body is returned as the turn's injected
    instructions. Missing/unknown names fail cleanly with a pointer to /skills.
    """
    name = args.strip()
    if not name:
        return CommandResult(
            "Usage: /skill <name> — invoke a skill by name. Run /skills to list them."
        )
    if not ctrl.runtime or not ctrl.runtime.skills:
        return CommandResult("No skills loaded. Run /skills to see what's available.")
    skill = find_skill(ctrl.runtime.skills, name)
    if skill is None:
        available = ", ".join(sorted(ctrl.runtime.skills))
        return CommandResult(
            f"Unknown skill: {name!r}. Available: {available or 'none'}. "
            "Run /skills to list them."
        )
    return CommandResult(render_skill_invocation(skill), seed_turn=True)


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
