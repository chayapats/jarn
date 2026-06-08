"""Project context assembly — the ``JARN.md`` file plus memory indices that get
folded into the agent's system prompt at session start.
"""

from __future__ import annotations

from pathlib import Path

from jarn.config import paths
from jarn.memory.store import MemoryStore

JARN_MD_TEMPLATE = """\
# {project_name}

> Project context for J.A.R.N. This file is auto-loaded into the agent's system
> prompt. Keep it short and high-signal — it costs tokens on every turn.

## What this project is

<one or two sentences describing the project and its purpose>

## Stack & layout

- Language / framework:
- Entry point:
- Key directories:

## Conventions

- <coding conventions, naming, formatting the agent must follow>

## How to run / test

```bash
# build:
# test:
# run:
```

## Things the agent should know

- <non-obvious constraints, gotchas, "don't touch X">
"""


def project_context_text(project_root: Path | None = None) -> str | None:
    """Return the contents of ``JARN.md`` for the project, if present."""
    path = paths.project_context_path(project_root)
    if path and path.is_file():
        return path.read_text(encoding="utf-8")
    return None


def init_template(project_root: Path | None = None) -> str:
    root = project_root or paths.find_project_root() or Path.cwd()
    return JARN_MD_TEMPLATE.format(project_name=root.name or "Project")


def write_jarn_md(project_root: Path | None = None, *, overwrite: bool = False) -> Path:
    """Create ``JARN.md`` from the template (the ``/init`` command)."""
    root = project_root or paths.find_project_root() or Path.cwd()
    path = root / paths.PROJECT_CONTEXT_FILE
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} already exists (pass overwrite=True to replace)")
    path.write_text(init_template(root), encoding="utf-8")
    return path


def assemble_system_context(
    project_root: Path | None = None,
    *,
    project_trusted: bool = True,
) -> str:
    """Build the context block appended to the agent's base system prompt.

    Combines (in order): project ``JARN.md``, global memory index, project
    memory index. Empty sections are omitted. Returns ``""`` when nothing is
    available so the caller can skip appending.

    When ``project_trusted`` is ``False``, project-tier context (``JARN.md`` and
    project memory) is omitted — the same trust boundary that strips dangerous
    config keys also keeps hostile repo content out of the system prompt.
    """
    sections: list[str] = []

    if project_trusted:
        ctx = project_context_text(project_root)
        if ctx:
            sections.append("# Project context (JARN.md)\n\n" + ctx.strip())

    global_index = MemoryStore.global_store().index_text().strip()
    if global_index and "—" in global_index:  # has at least one entry
        sections.append("# Long-term memory (global)\n\n" + global_index)

    if project_trusted:
        project_store = MemoryStore.project_store(project_root)
        if project_store:
            project_index = project_store.index_text().strip()
            if project_index and "—" in project_index:
                sections.append("# Long-term memory (project)\n\n" + project_index)

    return "\n\n---\n\n".join(sections)
