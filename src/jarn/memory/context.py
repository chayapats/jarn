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

> The beginning of this file is auto-loaded into J.A.R.N.'s prompt. Put universal,
> high-signal rules first. Move long or task-specific workflows into `.jarn/skills/`
> so the agent reads them only when needed.

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


def memory_index_context(
    project_root: Path | None = None,
    *,
    project_trusted: bool = True,
    token_budget: int | None = None,
) -> str:
    """Build the lazy memory catalog under one shared token ceiling."""
    memory_sections: list[str] = []
    global_index = MemoryStore.global_store().index_text().strip()
    if global_index and "—" in global_index:  # has at least one entry
        memory_sections.append("# Long-term memory (global)\n\n" + global_index)

    if project_trusted:
        project_store = MemoryStore.project_store(project_root)
        if project_store:
            project_index = project_store.index_text().strip()
            if project_index and "—" in project_index:
                memory_sections.append("# Long-term memory (project)\n\n" + project_index)

    if not memory_sections:
        return ""
    if token_budget is None:
        return "\n\n---\n\n".join(memory_sections)

    return truncate_memory_catalog_to_budget(
        "\n\n---\n\n".join(memory_sections), token_budget
    )


def truncate_memory_catalog_to_budget(catalog: str, budget: int) -> str:
    """Fit global/project memory sections under one redistributable ceiling."""
    from jarn.memory.tokens import count_tokens

    if budget <= 0 or not catalog:
        return ""
    if count_tokens(catalog) <= budget:
        return catalog

    separator = "\n\n---\n\n"
    sections = catalog.split(separator)
    if len(sections) == 1:
        return truncate_to_token_budget(catalog, budget)

    # Start with a fair share, but immediately hand unused capacity from a short
    # tier to tiers that still have content.  Reserve the joined separator, then
    # enforce the exact final count because tokenizer boundaries are non-additive.
    separator_cost = count_tokens(separator) * (len(sections) - 1)
    available = max(0, budget - separator_cost)
    raw_costs = [count_tokens(section) for section in sections]
    allocations = [min(cost, available // len(sections)) for cost in raw_costs]
    remaining = max(0, available - sum(allocations))
    while remaining:
        needy = [i for i, cost in enumerate(raw_costs) if allocations[i] < cost]
        if not needy:
            break
        share = max(1, remaining // len(needy))
        progressed = False
        for index in needy:
            grant = min(share, remaining, raw_costs[index] - allocations[index])
            if grant:
                allocations[index] += grant
                remaining -= grant
                progressed = True
            if remaining == 0:
                break
        if not progressed:
            break

    bounded = [
        truncate_to_token_budget(section, allocation)
        for section, allocation in zip(sections, allocations, strict=True)
    ]
    result = separator.join(section for section in bounded if section)
    return truncate_to_token_budget(result, budget)


def project_context_block(
    project_root: Path | None = None,
    *,
    context_files: list[str] | None = None,
    token_budget: int | None = None,
) -> str:
    """Render the trusted project-guidance module with read-more routing."""
    context_path = resolve_context_file(
        project_root,
        context_files=context_files,
    )
    ctx = project_context_text(
        project_root,
        context_files=context_files,
        token_budget=None,
    )
    if not ctx:
        return ""

    display_path = "project file"
    if context_path is not None:
        root = project_root or paths.find_project_root(project_root)
        if root is not None:
            try:
                display_path = context_path.relative_to(root).as_posix()
            except ValueError:
                display_path = context_path.name
        else:
            display_path = context_path.name
    heading = f"# Project context ({display_path})"
    full = f"{heading}\n\n{ctx.strip()}"
    if token_budget is None:
        return full

    from jarn.memory.tokens import count_tokens

    if count_tokens(full) <= token_budget:
        return full

    hint = (
        f"More guidance exists in `{display_path}`. Read that file when "
        "the task needs details beyond this excerpt."
    )
    fixed = f"{heading}\n\n{hint}"
    if count_tokens(fixed) > token_budget:
        return truncate_to_token_budget(full, token_budget)

    # Reserve the discoverable read-more route first, then fit as much complete
    # guidance as possible between the heading and hint.  Count the final joined
    # text because tokenizer boundaries are not additive.
    body_budget = max(0, token_budget - count_tokens(fixed))
    while body_budget >= 0:
        excerpt = truncate_to_token_budget(ctx.strip(), body_budget).strip()
        block = f"{heading}\n\n{excerpt}\n\n{hint}" if excerpt else fixed
        if count_tokens(block) <= token_budget:
            return block
        body_budget -= 1
    return fixed


def assemble_system_context(
    project_root: Path | None = None,
    *,
    project_trusted: bool = True,
    context_files: list[str] | None = None,
    memory_tokens: int | None = None,
    project_context_tokens: int | None = None,
) -> str:
    """Build the context block appended to the agent's base system prompt.

    Combines (in order): a bounded project-context excerpt, then bounded global
    and project memory indices. Empty sections are omitted. ``memory_tokens`` is
    one shared budget across both memory tiers, not a per-tier allowance.

    ``context_files`` is forwarded to :func:`project_context_text` to control
    the ordered candidate list (defaults to :data:`DEFAULT_CONTEXT_FILES`).

    When ``project_trusted`` is ``False``, project-tier context and project
    memory are omitted — the same trust boundary that strips dangerous config
    keys also keeps hostile repo content out of the system prompt.
    """
    sections: list[str] = []

    if project_trusted:
        block = project_context_block(
            project_root,
            context_files=context_files,
            token_budget=project_context_tokens,
        )
        if block:
            sections.append(block)

    memory_block = memory_index_context(
        project_root,
        project_trusted=project_trusted,
        token_budget=memory_tokens,
    )
    if memory_block:
        sections.append(memory_block)

    return "\n\n---\n\n".join(sections)
