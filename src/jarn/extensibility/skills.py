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

Skills load from two tiers (project overrides global on name conflict):
``~/.jarn/skills`` and ``<project>/.jarn/skills``.
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


def skill_dirs(project_root: Path | None = None) -> list[Path]:
    dirs = [paths.global_subdir("skills")]
    pdir = paths.project_dir(project_root)
    if pdir:
        dirs.append(pdir / "skills")
    return dirs


def load_skills(
    project_root: Path | None = None,
    *,
    project_trusted: bool = True,
) -> dict[str, Skill]:
    """Load all skills, keyed by name. Project tier overrides global."""
    out: dict[str, Skill] = {}
    global_dir = paths.global_subdir("skills")
    for path in discover(skill_dirs(project_root)):
        doc = parse(path)
        name = str(doc.meta.get("name") or path.stem)
        scope = "global" if str(path).startswith(str(global_dir)) else "project"
        if scope == "project" and not project_trusted:
            continue
        out[name] = Skill(
            name=name,
            description=str(doc.meta.get("description", "")),
            body=doc.body,
            trigger=str(doc.meta.get("trigger", "auto")),
            path=path,
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
