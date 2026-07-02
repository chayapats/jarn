"""Built-in agent tools wired during runtime assembly."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jarn.config.schema import Config


def _exit_plan_mode_tool():
    """The ``exit_plan_mode`` tool — present a plan and request approval to act.

    The tool body runs only *after* the user approves (the session driver gates
    it behind a plan-approval interrupt), so its return value is simply the
    "go" signal the model reads to start executing.
    """
    from langchain_core.tools import tool

    @tool
    def exit_plan_mode(plan: str) -> str:
        """Present your implementation plan and ask to leave read-only plan mode.

        Call this ONLY when the session is in plan mode and you have finished
        researching and have a concrete, step-by-step plan. The user reviews the
        plan and, on approval, the session switches to an editing mode — then
        carry the plan out, verifying as you go. Do not call this in other modes
        or just to display text.

        Args:
            plan: The proposed plan as concise markdown (numbered steps).
        """
        return (
            "Plan approved by the user. You are now in an editing mode — "
            "implement the plan step by step and verify as you go."
        )

    return exit_plan_mode


def _suggest_memory_tool():
    """The ``suggest_memory`` tool — propose a long-term memory for the user to keep.

    The tool body runs only *after* the user approves (the session driver gates it
    behind a memory-approval interrupt and the approver does the actual write via
    the memory store, respecting the global/project tier and trust gating), so its
    return value is simply the confirmation the model reads.
    """
    from langchain_core.tools import tool

    @tool
    def suggest_memory(
        name: str,
        description: str,
        body: str,
        type: str = "project",
        scope: str = "project",
    ) -> str:
        """Suggest a durable memory for the user to approve, edit, or decline.

        Call this when you learn something worth remembering across sessions — a
        stable user preference, a project convention, or a hard-won fact — and want
        it persisted. The user reviews your suggestion and chooses to save it (as is
        or edited) or decline; nothing is written unless they approve. Do not use
        this for transient, turn-local details.

        Args:
            name: Short title for the memory (used as its filename/slug).
            description: One-line summary shown in the memory index.
            body: The memory content in concise markdown.
            type: Memory category — one of user, feedback, project, reference.
            scope: Where to store it — "global" (all projects) or "project".
        """
        return (
            "Memory saved with the user's approval. Continue with the task; you can "
            "rely on this being remembered in future sessions."
        )

    return suggest_memory


def _add_wiki_tools(
    tools: list[Any],
    system_prompt: str,
    root: Path | None,
    *,
    project_trusted: bool,
    wiki_index_tokens: int | None = None,
) -> tuple[list[Any], str]:
    """Register the four wiki tools and optionally inject the wiki index.

    Returns ``(updated_tools, updated_system_prompt)``.

    Trust gate mirrors project JARN.md / skills: project-tier wiki content is
    only injected into the system prompt when ``project_trusted`` is ``True``
    (an untrusted repo's wiki could carry prompt-injection payloads).  Global
    wiki is always available.
    """
    from langchain_core.tools import tool

    from jarn.memory.wiki import WikiStore

    # When the project is untrusted, the tool-facing store is global-only so
    # that wiki_read / wiki_search cannot expose project wiki pages (which could
    # carry prompt-injection payloads).  Mirrors the index-injection gate below.
    full_store = WikiStore.build(root)
    store = full_store if project_trusted else WikiStore(global_wiki_dir=full_store.global_wiki_dir)

    @tool
    def wiki_search(query: str) -> str:  # type: ignore[misc]
        """Search the project wiki knowledge base for pages matching a query.

        Performs a case-insensitive substring search over all wiki pages and
        returns the matching lines from each page.  Use this to find relevant
        notes, decisions, or documentation before writing new ones.

        Args:
            query: Substring to search for (case-insensitive).
        """
        results = store.search(query)
        if not results:
            return f"No wiki pages matched {query!r}."
        lines = [f"wiki_search results for {query!r}:\n"]
        for slug, matched in results:
            lines.append(f"## {slug}")
            for line in matched[:10]:
                lines.append(f"  {line}")
            if len(matched) > 10:
                lines.append(f"  … ({len(matched) - 10} more lines)")
        return "\n".join(lines)

    @tool
    def wiki_read(page: str) -> str:  # type: ignore[misc]
        """Read the full contents of a wiki page by its name/slug.

        Args:
            page: The slug or name of the wiki page to read.
        """
        try:
            return store.read(page)
        except (FileNotFoundError, ValueError) as exc:
            return f"wiki_read error: {exc}"

    @tool
    def wiki_write(page: str, content: str) -> str:  # type: ignore[misc]
        """Create or overwrite a wiki page with the given content.

        Writes to the project tier if available, otherwise to the global tier.
        The page name is sanitized to a safe slug — no path traversal allowed.
        Requires approval in ask mode (same policy as file writes).

        Args:
            page: Name for the wiki page (becomes the slug/filename).
            content: Full markdown content to write.
        """
        try:
            ref = store.write(page, content)
            return f"wiki page written: {ref}"
        except ValueError as exc:
            return f"wiki_write error: {exc}"

    @tool
    def wiki_append(page: str, text: str) -> str:  # type: ignore[misc]
        """Append text to an existing wiki page (creates the page if absent).

        Requires approval in ask mode (same policy as file writes).

        Args:
            page: Name/slug of the wiki page to append to.
            text: Markdown text to append.
        """
        try:
            ref = store.append(page, text)
            return f"wiki page updated: {ref}"
        except ValueError as exc:
            return f"wiki_append error: {exc}"

    new_tools = [*tools, wiki_search, wiki_read, wiki_write, wiki_append]

    # Inject wiki index into the system prompt when pages exist.
    # Global tier always loads; project tier only when project is trusted.
    index_parts: list[str] = []

    if project_trusted:
        # Full combined index (project + global).
        index_text = store.index_text(token_budget=wiki_index_tokens)
    else:
        # Untrusted project: index uses the same global-only store as the wiki
        # tools (see trust gate above) so project pages are excluded.
        from jarn.memory.wiki import WikiStore as _WS

        global_only = _WS(global_wiki_dir=store.global_wiki_dir)
        index_text = global_only.index_text(token_budget=wiki_index_tokens)

    if index_text.strip():
        index_parts.append(index_text.strip())

    if index_parts:
        block = "\n\n<wiki_index>\n" + "\n\n".join(index_parts) + "\n</wiki_index>\n\n"
        system_prompt = block + system_prompt

    return new_tools, system_prompt


def _build_repo_map_tool(root: Path | None, *, token_budget: int):
    """Return a LangChain ``@tool``-decorated function for the agent to call.

    The tool is read-only (no side effects) and delegates to
    :func:`jarn.agent.repomap.build_repo_map`.  ``root`` is captured in a
    closure so the model just passes an optional ``focus`` string.
    """
    from langchain_core.tools import tool

    from jarn.agent.repomap import build_repo_map

    # Capture root at build time so the tool closure works even if root is None
    # (falls back to cwd) — same pattern as other built-in tools.
    _root = root or Path.cwd()
    _budget = token_budget

    @tool
    def repo_map(focus: str = "") -> str:
        """Return a ranked, token-budgeted map of the current repository.

        Provides file paths and their top-level symbols (classes, functions,
        types) ordered by importance so you can orient in a large codebase
        without reading every file.

        Args:
            focus: Optional substring to bias ranking toward matching paths.
        """
        try:
            return build_repo_map(_root, token_budget=_budget, focus=focus)
        except Exception as exc:  # noqa: BLE001
            return f"repo_map failed: {exc}"

    return repo_map


def _inject_repo_map(system_prompt: str, root: Path, *, token_budget: int) -> str:
    """Prepend a repo map block to *system_prompt* (for ``context.repo_map: auto``).

    Failures are silently swallowed — a missing map should never prevent the
    agent from starting.
    """
    from jarn.agent.repomap import build_repo_map

    try:
        map_text = build_repo_map(root, token_budget=token_budget)
        if not map_text.strip():
            return system_prompt
        block = (
            "<repo_map>\n"
            + map_text
            + "\n</repo_map>\n\n"
        )
        return block + system_prompt
    except Exception:  # noqa: BLE001
        return system_prompt


def _wire_builtin_tools(
    tools: list[Any],
    system_prompt: str,
    config: Config,
    root: Path | None,
    *,
    project_trusted: bool,
) -> tuple[list[Any], str]:
    if config.wiki.enabled:
        tools, system_prompt = _add_wiki_tools(
            tools,
            system_prompt,
            root,
            project_trusted=project_trusted,
            wiki_index_tokens=config.context.wiki_index_tokens,
        )

    repo_map_mode = config.context.repo_map
    if repo_map_mode in ("tool", "auto"):
        repo_map_tool = _build_repo_map_tool(
            root, token_budget=config.context.repo_map_tokens
        )
        tools = [*tools, repo_map_tool]

    if repo_map_mode == "auto" and root is not None:
        system_prompt = _inject_repo_map(
            system_prompt, root, token_budget=config.context.repo_map_tokens
        )

    if config.execution.backend == "local" and config.execution.background:
        from jarn.agent.background import build_background_tools

        tools = [*tools, *build_background_tools(
            root or Path.cwd(),
            max_concurrent=config.execution.background_max_concurrent,
            max_lifetime_secs=config.execution.background_max_lifetime_secs,
        )]

    tools = [*tools, _exit_plan_mode_tool(), _suggest_memory_tool()]
    return tools, system_prompt
