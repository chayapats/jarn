"""Built-in /memory slash-command handlers."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import TYPE_CHECKING

from rich.markup import escape as _escape_markup

from jarn.agent.session import SuggestedMemory
from jarn.controller.core import CommandResult
from jarn.memory import MemoryStore, RecallHit
from jarn.tui import palette

if TYPE_CHECKING:
    from jarn.controller.core import Controller


def cmd_memory(ctrl, args: str) -> CommandResult:
    raw = args.strip()
    if not raw:
        return _memory_list(ctrl)
    try:
        parts = shlex.split(raw)
    except ValueError as exc:
        return CommandResult(f"Could not parse /memory: {exc}")
    if not parts:
        return _memory_list(ctrl)

    subcommand = parts[0].lower()
    if subcommand == "search":
        query = raw[len(parts[0]):].strip()
        return _memory_search(ctrl, query)
    if subcommand == "show":
        return _memory_show(ctrl, parts[1:])
    if subcommand == "add":
        return _memory_add(ctrl, parts[1:])
    if subcommand == "update":
        return _memory_update(ctrl, parts[1:])
    if subcommand == "delete":
        return _memory_delete(ctrl, parts[1:])
    if subcommand in ("dump", "context"):
        return _memory_dump(ctrl)
    return CommandResult(
        "Usage: /memory [search|show|add|update|delete|dump] ...\n"
        "Examples:\n"
        "  /memory dump\n"
        "  /memory search pytest\n"
        "  /memory add project project \"Test style\" \"Use pytest\" \"Prefer parametrized tests.\"\n"
        "  /memory show project test-style\n"
        "  /memory delete global test-style"
    )

def cmd_wiki(ctrl, args: str) -> CommandResult:
    """List or search the wiki knowledge base.

    Usage::

        /wiki              — list all pages in the index
        /wiki list         — same as above
        /wiki search <q>   — grep pages for <q> (case-insensitive)
    """
    from jarn.memory.wiki import WikiStore

    store = WikiStore.build(ctrl.project_root)

    raw = args.strip()
    if not raw or raw.lower() == "list":
        index = store.index_text()
        if not index.strip():
            return CommandResult(
                "No wiki pages yet. "
                "Enable wiki with `wiki: {enabled: true}` in your config, "
                "then let the agent write pages with `wiki_write`."
            )
        return CommandResult(index)

    parts = raw.split(None, 1)
    subcmd = parts[0].lower()
    if subcmd == "search":
        query = parts[1].strip() if len(parts) > 1 else ""
        if not query:
            return CommandResult("Usage: /wiki search <query>")
        results = store.search(query)
        if not results:
            return CommandResult(f"No wiki pages matched {query!r}.")
        lines = [f"[b]Wiki search:[/b] {_escape_markup(query)!r}"]
        for slug, matched in results:
            lines.append(f"\n  [cyan]{_escape_markup(slug)}[/cyan]")
            for line in matched[:5]:
                lines.append(f"    {_escape_markup(line)}")
            if len(matched) > 5:
                lines.append(f"    [dim]… ({len(matched) - 5} more)[/dim]")
        return CommandResult("\n".join(lines))

    return CommandResult("Usage: /wiki [search <q>|list]")

def cmd_map(ctrl, args: str) -> CommandResult:
    """Build and display the ranked repo map.

    Supports an optional focus substring to bias ranking, and the keyword
    ``--refresh`` to bypass the cache and recompute.

    Usage: /map [focus] [--refresh]
    """
    from jarn.agent.repomap import build_repo_map

    raw = args.strip()
    refresh = False
    if "--refresh" in raw:
        raw = raw.replace("--refresh", "").strip()
        refresh = True

    focus = raw.strip()
    root = ctrl.project_root or Path.cwd()
    budget = ctrl.config.context.repo_map_tokens

    if refresh:
        # Bust the cache by removing any matching cache files for this root.
        _bust_repomap_cache(root)

    try:
        text = build_repo_map(root, token_budget=budget, focus=focus)
    except Exception as exc:  # noqa: BLE001
        return CommandResult(f"Error building repo map: {exc}")
    if not text.strip():
        return CommandResult("No source files found in the project.")
    return CommandResult(text)

def _memory_list(ctrl) -> CommandResult:
    stores = _memory_read_stores(ctrl)
    lines = ["[b]Long-term memory[/b] [dim](use /memory search <query>)[/dim]"]
    found = False
    for scope, store in stores:
        memories = store.load_all()
        if not memories:
            continue
        found = True
        lines.append(f"\n[b]{scope}[/b]")
        for memory in memories:
            lines.append(
                f"  [cyan]{_escape_markup(memory.name)}[/cyan] "
                f"([dim]{_escape_markup(memory.type)}[/dim]) — "
                f"{_escape_markup(memory.description)}"
            )
    if not ctrl.project_trusted and ctrl.project_root is not None:
        lines.append("\n[dim]Project memory skipped until this project is trusted (`jarn trust`).[/dim]")
    if not found:
        return CommandResult(
            "No long-term memories yet. Try /memory add project project "
            '"Name" "Description" "Body".'
        )
    return CommandResult("\n".join(lines))

def _memory_search(ctrl, query: str) -> CommandResult:
    if not query:
        return CommandResult("Usage: /memory search <query>")
    hits = _memory_search_hits(ctrl, query, k=5)
    if not hits:
        return CommandResult(f"No memories matched: {query!r}")
    lines = [f"[b]Recall for[/b] {_escape_markup(query)!r}"]
    for scope, hit in hits:
        lines.append(
            f"  [cyan]{_escape_markup(hit.memory.name)}[/cyan] "
            f"[dim]({scope}, {hit.score:.2f})[/dim] — "
            f"{_escape_markup(hit.memory.description)}"
        )
        if hit.memory.body:
            lines.append(_format_memory_body(hit.memory.body))
    return CommandResult("\n".join(lines))

def _memory_dump(ctrl) -> CommandResult:
    """Render everything that gets injected into the system prompt for this project."""
    from jarn.memory import MemoryStore
    from jarn.memory.context import DEFAULT_CONTEXT_FILES, project_context_text

    sep = f"[{palette.C_DIM}]{'─' * 48}[/{palette.C_DIM}]"
    lines: list[str] = ["[b]Memory context dump[/b] [dim](what the agent sees)[/dim]"]

    # 1. Global MEMORY.md index
    lines.append(f"\n{sep}")
    lines.append(f"[b][{palette.C_NOTICE}]Global memory index[/{palette.C_NOTICE}][/b]")
    global_store = MemoryStore.global_store()
    global_index = global_store.index_text().strip()
    if global_index and "—" in global_index:
        lines.append(_escape_markup(global_index))
    else:
        lines.append(f"[{palette.C_DIM}](empty)[/{palette.C_DIM}]")

    # 2. Project MEMORY.md index
    lines.append(f"\n{sep}")
    lines.append(f"[b][{palette.C_NOTICE}]Project memory index[/{palette.C_NOTICE}][/b]")
    if not ctrl.project_trusted and ctrl.project_root is not None:
        lines.append(
            f"[{palette.C_DIM}]Skipped — project untrusted (`jarn trust` to enable).[/{palette.C_DIM}]"
        )
    else:
        project_store = MemoryStore.project_store(ctrl.project_root)
        if project_store:
            project_index = project_store.index_text().strip()
            if project_index and "—" in project_index:
                lines.append(_escape_markup(project_index))
            else:
                lines.append(f"[{palette.C_DIM}](empty)[/{palette.C_DIM}]")
        else:
            lines.append(f"[{palette.C_DIM}](no project root)[/{palette.C_DIM}]")

    # 3. Loaded context file (JARN.md / AGENTS.md / CLAUDE.md)
    lines.append(f"\n{sep}")
    names = (
        ctrl.config.compat.context_files
        if ctrl.config.compat.context_files
        else DEFAULT_CONTEXT_FILES
    )
    ctx_text = project_context_text(ctrl.project_root, context_files=names) if ctrl.project_trusted else None
    # Determine which file was loaded
    ctx_filename: str | None = None
    if ctrl.project_trusted and ctrl.project_root is not None:
        for name in names:
            if (ctrl.project_root / name).is_file():
                ctx_filename = name
                break
    label = f"Context file ({ctx_filename})" if ctx_filename else "Context file"
    lines.append(f"[b][{palette.C_NOTICE}]{_escape_markup(label)}[/{palette.C_NOTICE}][/b]")
    if ctx_text:
        lines.append(_escape_markup(ctx_text.strip()))
    elif not ctrl.project_trusted and ctrl.project_root is not None:
        lines.append(
            f"[{palette.C_DIM}]Skipped — project untrusted.[/{palette.C_DIM}]"
        )
    else:
        lines.append(f"[{palette.C_DIM}](no context file found — run /init to create JARN.md)[/{palette.C_DIM}]")

    # 4. Top-k recall — what recall_block(...) would surface per turn. Real
    # injection is query-dependent (enrich_turn_input), so we recall against a
    # representative query built from the stored memories to show live hits.
    lines.append(f"\n{sep}")
    lines.append(
        f"[b][{palette.C_NOTICE}]Top-k recall (representative)[/{palette.C_NOTICE}][/b]"
    )
    query = " ".join(
        f"{m.name} {m.description}"
        for _scope, store in _memory_read_stores(ctrl)
        for m in store.load_all()
    )
    recall_hits: list[tuple[str, RecallHit]] = (
        _memory_search_hits(ctrl, query, k=3) if query.strip() else []
    )
    if recall_hits:
        for scope, hit in recall_hits:
            lines.append(
                f"  [cyan]{_escape_markup(hit.memory.name)}[/cyan] "
                f"[dim]({scope}, {hit.score:.2f})[/dim] — "
                f"{_escape_markup(hit.memory.description)}"
            )
    else:
        lines.append(f"[{palette.C_DIM}](no memories to recall)[/{palette.C_DIM}]")

    return CommandResult("\n".join(lines))

def _memory_show(ctrl, parts: list[str]) -> CommandResult:
    if not parts:
        return CommandResult("Usage: /memory show [global|project] <name-or-slug>")
    explicit_scope = parts[0].lower() in ("global", "project")
    if explicit_scope:
        scope = parts[0].lower()
        name = " ".join(parts[1:]).strip()
        store, error = _memory_store_for_scope(ctrl, scope, write=False)
        if error or store is None:
            return CommandResult(error or "Memory store unavailable.")
        candidates = [(scope, store)]
    else:
        name = " ".join(parts).strip()
        candidates = list(reversed(_memory_read_stores(ctrl)))
    if not name:
        return CommandResult("Usage: /memory show [global|project] <name-or-slug>")
    for scope, store in candidates:
        memory = store.get(name)
        if memory is None:
            continue
        lines = [
            f"[b]{_escape_markup(memory.name)}[/b] [dim]({scope}, {memory.type})[/dim]",
            _escape_markup(memory.description),
        ]
        if memory.body:
            lines.append(_format_memory_body(memory.body))
        return CommandResult("\n".join(lines))
    return CommandResult(f"No memory found: {name!r}")

def _memory_add(ctrl, parts: list[str]) -> CommandResult:
    from jarn.memory.store import MEMORY_TYPES, Memory

    scope, idx = _memory_scope_from_parts(ctrl, parts)
    remaining = parts[idx:]
    if len(remaining) < 3:
        return CommandResult(
            "Usage: /memory add [global|project] <type> <name> <description> [body]"
        )
    mem_type, name, description = remaining[:3]
    if mem_type not in MEMORY_TYPES:
        return CommandResult(f"Unknown memory type {mem_type!r}; choose one of: {', '.join(MEMORY_TYPES)}")
    store, error = _memory_store_for_scope(ctrl, scope, write=True)
    if error or store is None:
        return CommandResult(error or "Memory store unavailable.")
    body = " ".join(remaining[3:]).strip() or description
    path = store.save(Memory(name=name, description=description, body=body, type=mem_type))
    return CommandResult(f"Saved {scope} memory: {path.name}", rebuilt=False)

def _memory_update(ctrl, parts: list[str]) -> CommandResult:
    scope, idx = _memory_scope_from_parts(ctrl, parts)
    remaining = parts[idx:]
    if len(remaining) < 2:
        return CommandResult("Usage: /memory update [global|project] <name-or-slug> <description> [body]")
    name, description = remaining[:2]
    store, error = _memory_store_for_scope(ctrl, scope, write=True)
    if error or store is None:
        return CommandResult(error or "Memory store unavailable.")
    memory = store.get(name)
    if memory is None:
        return CommandResult(f"No {scope} memory found: {name!r}")
    body = " ".join(remaining[2:]).strip() or memory.body
    memory.description = description
    memory.body = body
    path = store.save(memory)
    return CommandResult(f"Updated {scope} memory: {path.name}")

def _memory_delete(ctrl, parts: list[str]) -> CommandResult:
    scope, idx = _memory_scope_from_parts(ctrl, parts)
    name = " ".join(parts[idx:]).strip()
    if not name:
        return CommandResult("Usage: /memory delete [global|project] <name-or-slug>")
    store, error = _memory_store_for_scope(ctrl, scope, write=True)
    if error or store is None:
        return CommandResult(error or "Memory store unavailable.")
    if not store.delete(name):
        return CommandResult(f"No {scope} memory found: {name!r}")
    return CommandResult(f"Deleted {scope} memory: {name}")

def _memory_read_stores(ctrl) -> list[tuple[str, MemoryStore]]:
    from jarn.memory import MemoryStore

    stores = [("global", MemoryStore.global_store())]
    project = MemoryStore.project_store(ctrl.project_root)
    if project and ctrl.project_trusted:
        stores.append(("project", project))
    return stores

def _memory_search_hits(ctrl, query: str, *, k: int) -> list[tuple[str, RecallHit]]:
    from jarn.memory import VectorIndex
    from jarn.memory.store import slugify

    deduped: dict[str, tuple[str, RecallHit]] = {}
    for scope, store in _memory_read_stores(ctrl):
        if not store.root.is_dir():
            continue
        for hit in VectorIndex(store).search(query, k=k):
            key = slugify(hit.memory.name)
            existing = deduped.get(key)
            if existing is None or hit.score > existing[1].score:
                deduped[key] = (scope, hit)
    return sorted(deduped.values(), key=lambda item: item[1].score, reverse=True)[:k]

def _memory_scope_from_parts(ctrl, parts: list[str]) -> tuple[str, int]:
    if parts and parts[0].lower() in ("global", "project"):
        return parts[0].lower(), 1
    return ("project" if ctrl.project_root is not None else "global"), 0

def _memory_store_for_scope(
    ctrl: Controller,
    scope: str,
    *,
    write: bool,
) -> tuple[MemoryStore | None, str | None]:
    from jarn.memory import MemoryStore

    if scope == "global":
        return MemoryStore.global_store(), None
    if scope != "project":
        return None, "Scope must be 'global' or 'project'."
    if not ctrl.project_trusted:
        return None, "Project memory is disabled until this project is trusted (`jarn trust`)."
    store = MemoryStore.project_store(ctrl.project_root)
    if store is None:
        target = "write" if write else "read"
        return None, f"No project root found; cannot {target} project memory."
    return store, None

def save_suggested_memory(ctrl, suggestion: SuggestedMemory) -> tuple[bool, str]:
    """Persist an agent-suggested (and user-approved) memory via the store.

    Routes through the same scope + trust gating as ``/memory add``: a project
    write is refused on an untrusted project. Returns ``(saved, message)`` so
    the approver can report the outcome to the user without raising."""
    from jarn.memory.store import MEMORY_TYPES, Memory

    name = suggestion.name.strip()
    if not name:
        return False, "Memory has no name; nothing saved."
    mem_type = suggestion.type.strip() or "project"
    if mem_type not in MEMORY_TYPES:
        return False, (
            f"Unknown memory type {mem_type!r}; "
            f"choose one of: {', '.join(MEMORY_TYPES)}."
        )
    scope = suggestion.scope.strip().lower() or "project"
    store, error = _memory_store_for_scope(ctrl, scope, write=True)
    if error or store is None:
        return False, error or "Memory store unavailable."
    description = suggestion.description.strip() or name
    body = suggestion.body.strip() or description
    path = store.save(
        Memory(name=name, description=description, body=body, type=mem_type)
    )
    return True, f"Saved {scope} memory: {path.name}"

def _format_memory_body(body: str) -> str:
    escaped = _escape_markup(body.strip())
    return "\n".join(f"    {line}" for line in escaped.splitlines())


def _bust_repomap_cache(root: Path) -> None:
    """Remove cached repo-map files for *root* (best-effort, never raises).

    Since the cache key embeds both root and the file-set signature, the
    simplest invalidation strategy is to wipe all .json files in the repomap
    cache dir — it's cheap to rebuild and avoids reimplementing the key
    derivation here.
    """
    import contextlib

    from jarn.config import paths as _paths

    cache_dir = _paths.cachedir() / "repomap"
    if not cache_dir.is_dir():
        return
    with contextlib.suppress(Exception):
        for f in cache_dir.iterdir():
            if f.suffix == ".json":
                with contextlib.suppress(Exception):
                    f.unlink()

