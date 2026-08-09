"""Skills — reusable knowledge/workflows the agent can invoke.

A skill is a ``SKILL.md`` (or ``<name>.md``) file with frontmatter::

    ---
    name: run-migrations
    description: Apply and verify database migrations safely.
    trigger: auto            # auto | manual | "<keyword/glob>"
    ---
    <instructions the agent follows when the skill is active>

Layouts (both are discovered):

* Flat: ``skills/<name>.md``
* Nested (Agent Skills / Claude): ``skills/<name>/SKILL.md``

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

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from jarn.config import paths
from jarn.extensibility.frontmatter import discover, parse
from jarn.memory.tokens import truncate_to_token_budget
from jarn.util.atomic import atomic_write_text

# Flat ``*.md`` plus nested ``<name>/SKILL.md`` (Agent Skills layout). Nested
# is listed after flat so a same-name nested skill wins within one directory.
_SKILL_GLOBS: tuple[str, ...] = ("*.md", "*/SKILL.md")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


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


def _skill_dirs_ordered(
    project_root: Path | None = None,
    *,
    read_claude_dir: bool = True,
) -> list[Path]:
    """Return skill directories in discovery order (lowest priority first).

    ``load_skills`` is the source of truth: ``.claude`` tiers load first,
    then ``.jarn`` tiers overwrite on name conflict (``.jarn`` wins).
    """
    global_jarn_dir = paths.global_subdir("skills")
    global_claude_dir = paths.global_claude_subdir("skills")
    pdir = paths.project_dir(project_root)
    claude_pdir = paths.project_claude_dir(project_root)

    low_dirs: list[Path] = []
    high_dirs: list[Path] = []

    if read_claude_dir:
        low_dirs.append(global_claude_dir)
        if claude_pdir:
            low_dirs.append(claude_pdir / "skills")

    high_dirs.append(global_jarn_dir)
    if pdir:
        high_dirs.append(pdir / "skills")

    return low_dirs + high_dirs


def _default_skill_name(path: Path) -> str:
    """Fallback name when frontmatter omits ``name``.

    Nested ``…/<name>/SKILL.md`` uses the parent directory; flat ``<name>.md``
    uses the file stem.
    """
    if path.name == "SKILL.md":
        return path.parent.name
    return path.stem


def load_skills(
    project_root: Path | None = None,
    *,
    project_trusted: bool = True,
    read_claude_dir: bool = True,
) -> dict[str, Skill]:
    """Load all skills, keyed by name.

    Precedence (highest first): project ``.jarn`` > global ``.jarn`` >
    project ``.claude`` > global ``.claude``.  Directories are scanned in
    low-to-high priority order so later entries overwrite earlier ones.
    """
    pdir = paths.project_dir(project_root)
    claude_pdir = paths.project_claude_dir(project_root)

    out: dict[str, Skill] = {}

    def _is_project(path: Path) -> bool:
        """True when the skill file lives under a project-scoped directory."""
        if pdir and str(path).startswith(str(pdir)):
            return True
        return bool(claude_pdir and str(path).startswith(str(claude_pdir)))

    for skill_path in discover(
        _skill_dirs_ordered(project_root, read_claude_dir=read_claude_dir),
        _SKILL_GLOBS,
    ):
        doc = parse(skill_path)
        name = str(doc.meta.get("name") or _default_skill_name(skill_path))
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


def slugify_skill_name(text: str) -> str:
    """Filesystem-safe directory slug for a nested ``skills/<slug>/SKILL.md``."""
    return _SLUG_RE.sub("-", text.strip().lower()).strip("-")[:60] or "skill"


def skill_to_markdown(
    name: str,
    description: str,
    body: str,
    trigger: str = "auto",
) -> str:
    """Render a skill as YAML-frontmatter markdown (Agent Skills shape)."""
    front = yaml.safe_dump(
        {
            "name": name,
            "description": description,
            "trigger": trigger,
        },
        sort_keys=False,
        allow_unicode=True,
    ).strip()
    return f"---\n{front}\n---\n\n{body.strip()}\n"


def write_skill(
    project_root: Path,
    *,
    name: str,
    description: str,
    body: str,
    trigger: str = "auto",
) -> Path:
    """Write a nested skill under ``<root>/.jarn/skills/<slug>/SKILL.md``.

    Always uses the nested Agent Skills layout (discovery already supports it).
    Returns the path written. Raises ``ValueError`` when the project has no
    ``.jarn`` directory or the name is unsafe.
    """
    if "/" in name or "\\" in name or name.strip() in (".", ".."):
        raise ValueError("Skill name must not contain path separators.")
    pdir = paths.project_dir(project_root)
    if pdir is None:
        raise ValueError("No project .jarn directory; cannot write a skill.")
    slug = slugify_skill_name(name)
    if not slug or slug in (".", ".."):
        raise ValueError(f"Skill name {name!r} produces an empty slug.")
    skill_dir = pdir / "skills" / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    atomic_write_text(
        path,
        skill_to_markdown(
            name=name.strip(),
            description=description.strip() or name.strip(),
            body=body.strip() or description.strip() or name.strip(),
            trigger=(trigger.strip() or "auto"),
        ),
    )
    return path


def find_skill(skills: dict[str, Skill], name: str) -> Skill | None:
    """Resolve a skill by name for explicit ``/skill <name>`` invocation.

    Tries an exact match first, then a trimmed case-insensitive one so a user
    typing ``/skill Deploy`` still finds a skill keyed ``deploy``. Returns
    ``None`` when nothing matches (the caller renders the "unknown skill" error).
    """
    if name in skills:
        return skills[name]
    wanted = name.strip().lower()
    for key, skill in skills.items():
        if key.lower() == wanted:
            return skill
    return None


def render_skill_invocation(skill: Skill) -> str:
    """Render a skill's full body as an injectable, follow-me instruction block.

    ``manual``-trigger skills are excluded from :func:`auto_skill_catalog`, so an
    explicit ``/skill <name>`` is the ONLY way to run them; string/auto skills
    can be invoked this way too. Unlike the catalog (names + descriptions only,
    read-on-demand), the whole body is injected here so the model follows it
    immediately for this turn without a tool round-trip.
    """
    lines = [f"# Skill: {skill.name}"]
    if skill.description:
        lines.append(skill.description)
    lines.append("")
    lines.append("Follow these skill instructions for this turn:")
    lines.append("")
    lines.append(skill.body.strip())
    return "\n".join(lines)


def auto_skill_catalog(
    skills: dict[str, Skill],
    *,
    token_budget: int | None = 512,
) -> str:
    """Render the auto-eligible skills as a prompt-injectable catalog.

    Only names + descriptions are injected (cheap); the model reads the full
    skill file on demand. Manual-only skills are excluded. The whole catalog is
    budget-capped so adding extensions cannot silently make every turn expensive.
    """
    eligible = [s for s in skills.values() if s.auto_eligible and s.description]
    if not eligible:
        return ""
    header = [
        "# Available skills",
        "",
        "Read a matching skill file for its full instructions before using it.",
        "",
    ]
    entries: list[str] = []
    for s in sorted(eligible, key=lambda s: s.name):
        loc = f" (`{s.path}`)" if s.path else ""
        entries.append(f"- **{s.name}** — {s.description}{loc}")
    catalog = "\n".join([*header, *entries])
    if token_budget is not None:
        return truncate_skill_catalog_to_budget(catalog, token_budget)
    return catalog


def truncate_skill_catalog_to_budget(catalog: str, budget: int) -> str:
    """Fit a rendered skill catalog by whole entries with an omitted count.

    An alphabetical character cut can hide every skill after the first long
    entry and can leave a half-readable path.  This keeps only complete bullet
    entries and points to ``/skills`` for the complete lazy listing.  Tiny
    budgets still obey the strict ceiling through the generic fallback.
    """
    from jarn.memory.tokens import count_tokens

    if budget <= 0 or not catalog:
        return ""
    if count_tokens(catalog) <= budget:
        return catalog

    lines = catalog.splitlines()
    first_entry = next(
        (index for index, line in enumerate(lines) if line.startswith("- **")),
        len(lines),
    )
    header = lines[:first_entry]
    entries = lines[first_entry:]
    total = len(entries)
    for kept in range(total - 1, -1, -1):
        omitted = total - kept
        notice = (
            f"- … truncated: {omitted} more "
            f"skill{'s' if omitted != 1 else ''} omitted; run /skills."
        )
        candidate = "\n".join([*header, *entries[:kept], notice])
        if count_tokens(candidate) <= budget:
            return candidate

    fallback = "# Available skills\n\nRun /skills to list all skills."
    return truncate_to_token_budget(fallback, budget)
