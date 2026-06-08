"""Skills — reusable knowledge/workflows the agent can invoke.

A skill is a ``SKILL.md`` (or ``<name>.md``) file with frontmatter::

    ---
    name: run-migrations
    description: Apply and verify database migrations safely.
    trigger: auto            # auto | manual | "<keyword/glob>"
    ---
    <instructions the agent follows when the skill is active>

Trigger semantics (the "hybrid" model):
* ``auto``     — description is offered to the model, which decides when to use it
* ``manual``   — only runs when invoked explicitly via ``/skill <name>``
* a string     — keyword/glob; auto-eligible and also explicitly invokable

Skills load from up to four tiers (earlier tiers override later on name
conflict):

1. ``<project>/.jarn/skills``  — project-specific, highest priority
2. ``~/.jarn/skills``          — user-global
3. ``<project>/.claude/skills`` — cross-vendor project skills (when ``read_claude_dir``
   is enabled and the project is trusted)
4. ``~/.claude/skills``         — cross-vendor global skills

``.jarn`` always beats ``.claude`` on a name collision; built-in names are
never shadowed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jarn.config import paths
from jarn.extensibility.frontmatter import discover, parse


@dataclass(slots=True)
class Skill:
    name: str
    description: str
    body: str
    trigger: str = "auto"
    path: Path | None = None
    scope: str = "project"  # "global" | "project"

    @property
    def is_manual(self) -> bool:
        return self.trigger.strip().lower() == "manual"

    @property
    def auto_eligible(self) -> bool:
        return not self.is_manual


def skill_dirs(
    project_root: Path | None = None,
    *,
    read_claude_dir: bool = True,
) -> list[Path]:
    """Return the ordered list of skill directories to scan.

    ``.jarn`` directories come before ``.claude`` ones so that local
    customisation always wins on a name conflict.
    """
    dirs = [paths.global_subdir("skills")]
    pdir = paths.project_dir(project_root)
    if pdir:
        dirs.append(pdir / "skills")
    if read_claude_dir:
        dirs.append(paths.global_claude_subdir("skills"))
        claude_pdir = paths.project_claude_dir(project_root)
        if claude_pdir:
            dirs.append(claude_pdir / "skills")
    return dirs


def load_skills(
    project_root: Path | None = None,
    *,
    project_trusted: bool = True,
    read_claude_dir: bool = True,
) -> dict[str, Skill]:
    """Load all skills, keyed by name.

    Precedence (highest first): project ``.jarn`` > global ``.jarn`` >
    global ``.claude`` > project ``.claude``. Because :func:`skill_dirs`
    appends ``.claude`` dirs after ``.jarn`` ones, and :func:`discover` emits
    files in directory order, later entries simply overwrite earlier ones —
    which means ``.jarn`` wins by loading *after* ``.claude`` in the loop.

    Wait — the loop assigns unconditionally, so the *last* write wins. To make
    ``.jarn`` beat ``.claude`` we load ``.claude`` first, then ``.jarn`` on top.
    The directory order in :func:`skill_dirs` is therefore: claude-global,
    claude-project, jarn-global, jarn-project (last write wins).
    """
    # Load order: .claude dirs first (lower priority), then .jarn (higher
    # priority overwrites).  Within each tier global before project so that
    # project-scoped skills can override global ones of the same name.
    global_jarn_dir = paths.global_subdir("skills")
    global_claude_dir = paths.global_claude_subdir("skills")
    pdir = paths.project_dir(project_root)
    claude_pdir = paths.project_claude_dir(project_root)

    # Build two ordered passes: low-priority (.claude) then high-priority (.jarn)
    low_dirs: list[Path] = []
    high_dirs: list[Path] = []

    if read_claude_dir:
        low_dirs.append(global_claude_dir)
        if claude_pdir:
            low_dirs.append(claude_pdir / "skills")

    high_dirs.append(global_jarn_dir)
    if pdir:
        high_dirs.append(pdir / "skills")

    out: dict[str, Skill] = {}

    def _is_project(path: Path) -> bool:
        """True when the skill file lives under a project-scoped directory."""
        if pdir and str(path).startswith(str(pdir)):
            return True
        return bool(claude_pdir and str(path).startswith(str(claude_pdir)))

    for skill_path in discover(low_dirs + high_dirs):
        doc = parse(skill_path)
        name = str(doc.meta.get("name") or skill_path.stem)
        is_proj = _is_project(skill_path)
        if is_proj and not project_trusted:
            continue
        scope = "project" if is_proj else "global"
        out[name] = Skill(
            name=name,
            description=str(doc.meta.get("description", "")),
            body=doc.body,
            trigger=str(doc.meta.get("trigger", "auto")),
            path=skill_path,
            scope=scope,
        )
    return out


def auto_skill_catalog(skills: dict[str, Skill]) -> str:
    """Render the auto-eligible skills as a prompt-injectable catalog.

    Only names + descriptions are injected (cheap); the model reads the full
    skill file on demand. Manual-only skills are excluded.
    """
    eligible = [s for s in skills.values() if s.auto_eligible and s.description]
    if not eligible:
        return ""
    lines = ["# Available skills", ""]
    for s in sorted(eligible, key=lambda s: s.name):
        loc = f" (`{s.path}`)" if s.path else ""
        lines.append(f"- **{s.name}** — {s.description}{loc}")
    lines.append(
        "\nWhen a task matches a skill, read its file for the full instructions "
        "before acting."
    )
    return "\n".join(lines)
