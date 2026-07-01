"""Project context assembly — the ``JARN.md`` file plus memory indices that get
folded into the agent's system prompt at session start.

The project context file is resolved from an ordered list (default:
``["JARN.md", "AGENTS.md", "CLAUDE.md"]``). The first file present in the
project root wins, so users coming from other agents (Cursor, Claude Code, …)
work out of the box without renaming their existing context file.
"""

from __future__ import annotations

from pathlib import Path

from jarn.config import paths
from jarn.memory.store import MemoryStore
from jarn.memory.tokens import truncate_to_token_budget

#: Default ordered list of context filenames tried in the project root.
#: Mirrors :attr:`jarn.config.schema.CompatConfig.context_files`.
DEFAULT_CONTEXT_FILES: list[str] = ["JARN.md", "AGENTS.md", "CLAUDE.md"]

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


def resolve_context_file(
    project_root: Path | None = None,
    *,
    context_files: list[str] | None = None,
) -> Path | None:
    """Return the :class:`~pathlib.Path` of the first present context file.

    Same resolution order as :func:`project_context_text` — this is the
    companion that returns the *path* rather than the content, used by the
    startup notice to name which file was loaded.
    """
    root = project_root or paths.find_project_root()
    if root is None:
        legacy = paths.project_context_path(project_root)
        if legacy and legacy.is_file():
            return legacy
        return None

    names = context_files if context_files is not None else DEFAULT_CONTEXT_FILES
    for name in names:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def project_context_text(
    project_root: Path | None = None,
    *,
    context_files: list[str] | None = None,
    token_budget: int | None = None,
) -> str | None:
    """Return the contents of the first present context file for the project.

    ``context_files`` is an ordered list of filenames to check (default
    :data:`DEFAULT_CONTEXT_FILES`). The first file found in the project root
    wins. This lets users coming from Claude Code (``CLAUDE.md``) or OpenAI
    Codex (``AGENTS.md``) have their context loaded without renaming anything.

    Falls back to the legacy :func:`jarn.config.paths.project_context_path`
    when the project root cannot be determined.

    When ``token_budget`` is set, the returned text is truncated to fit with a
    visible ``(truncated N tokens)`` notice.
    """
    path = resolve_context_file(project_root, context_files=context_files)
    if path is None:
        return None
    text = path.read_text(encoding="utf-8")
    if token_budget is not None:
        return truncate_to_token_budget(text, token_budget)
    return text


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
    context_files: list[str] | None = None,
    memory_tokens: int | None = None,
    project_context_tokens: int | None = None,
) -> str:
    """Build the context block appended to the agent's base system prompt.

    Combines (in order): project context file, global memory index, project
    memory index. Empty sections are omitted. Returns ``""`` when nothing is
    available so the caller can skip appending.

    ``context_files`` is forwarded to :func:`project_context_text` to control
    the ordered candidate list (defaults to :data:`DEFAULT_CONTEXT_FILES`).

    When ``project_trusted`` is ``False``, project-tier context and project
    memory are omitted — the same trust boundary that strips dangerous config
    keys also keeps hostile repo content out of the system prompt.
    """
    sections: list[str] = []

    if project_trusted:
        ctx = project_context_text(
            project_root,
            context_files=context_files,
            token_budget=project_context_tokens,
        )
        if ctx:
            sections.append("# Project context (JARN.md)\n\n" + ctx.strip())

    global_index = MemoryStore.global_store().index_text(
        token_budget=memory_tokens
    ).strip()
    if global_index and "—" in global_index:  # has at least one entry
        sections.append("# Long-term memory (global)\n\n" + global_index)

    if project_trusted:
        project_store = MemoryStore.project_store(project_root)
        if project_store:
            project_index = project_store.index_text(token_budget=memory_tokens).strip()
            if project_index and "—" in project_index:
                sections.append("# Long-term memory (project)\n\n" + project_index)

    return "\n\n---\n\n".join(sections)
