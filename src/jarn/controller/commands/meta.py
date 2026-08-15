"""Built-in /meta slash-command handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.markup import escape as _escape_markup

from jarn.commands.help import format_help, format_help_detail, usage_error
from jarn.controller.core import CommandResult
from jarn.extensibility.skills import find_skill, render_skill_invocation
from jarn.memory import write_jarn_md
from jarn.tui import layout

if TYPE_CHECKING:
    from jarn.controller.core import Controller


def cmd_help(ctrl: Controller, args: str) -> CommandResult:
    custom = ctrl.runtime.commands if ctrl.runtime else None
    topic = args.strip()
    if topic:
        return CommandResult(
            format_help_detail(
                topic,
                custom,
                custom_description=lambda c: getattr(c, "description", ""),
            )
        )
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


def cmd_login(ctrl: Controller, args: str) -> CommandResult:
    """Map the in-session spelling to the verified terminal auth ceremony."""
    if args.strip():
        return CommandResult(usage_error("login"))
    return CommandResult(
        f"Run {layout.strong('jarn auth login')} in a terminal. It will show the browser URL "
        "or device code and will report success only after the account is verified."
    )


def cmd_logout(ctrl: Controller, args: str) -> CommandResult:
    """Map the in-session spelling to scoped Codex-managed logout."""
    if args.strip():
        return CommandResult(usage_error("logout"))
    return CommandResult(
        f"Run {layout.strong('jarn auth logout')} in a terminal. This removes only "
        "Codex-managed ChatGPT credentials; provider API keys are preserved."
    )


def cmd_skill(ctrl: Controller, args: str) -> CommandResult:
    """`/skill <name>`: invoke a skill by name, injecting its body into the turn.

    ``manual``-trigger skills are kept out of the auto catalog (see
    ``skills.py``), so this is the ONLY entry point that can run them; auto/string
    skills resolve here too. The resolved body is returned as the turn's injected
    instructions. Missing/unknown names fail cleanly with a pointer to /skills.
    """
    name = args.strip()
    if not name:
        return CommandResult(usage_error("skill"))
    if not ctrl.runtime or not ctrl.runtime.skills:
        return CommandResult("No skills loaded. Run /skills to see what's available.")
    skill = find_skill(ctrl.runtime.skills, name)
    if skill is None:
        available = ", ".join(sorted(ctrl.runtime.skills))
        return CommandResult(
            f"Unknown skill: {name!r}. Available: {available or 'none'}. "
            "Run /skills to list them."
        )
    activated = ctrl.activate_prompt_module(skill.name, "turn")
    if not activated.text.startswith("Activated "):
        return activated
    return CommandResult(
        render_skill_invocation(skill),
        seed_turn=True,
        seed_input=(
            f"Apply the activated `{skill.name}` skill to this turn. "
            f"Goal: {skill.description}"
        ),
    )


def cmd_skills(ctrl: Controller, args: str) -> CommandResult:
    if not ctrl.runtime or not ctrl.runtime.skills:
        return CommandResult("No skills loaded.")
    lines = [layout.title("Skills")]
    for s in ctrl.runtime.skills.values():
        trig = "manual" if s.is_manual else "auto"
        lines.append(
            f"  {layout.accent(s.name)} "
            f"({layout.muted(f'{trig}, {s.scope}')}) — {_escape_markup(s.description)}"
        )
    return CommandResult("\n".join(lines))
