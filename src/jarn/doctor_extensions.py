"""Extension diagnostics for ``jarn doctor``.

Scans skills, commands, subagents, hooks, MCP servers, and async subagents that
would load at launch — including shadowed files, builtin renames, and entries
skipped because the project is untrusted.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from jarn.config.schema import Config
from jarn.extensibility.commands import BUILTIN_COMMANDS, command_dirs
from jarn.extensibility.frontmatter import discover, parse
from jarn.extensibility.skills import skill_dirs
from jarn.extensibility.subagents import agent_dirs


@dataclass(slots=True)
class MarkdownExtensionRow:
    kind: str
    name: str
    scope: str
    path: str
    status: str  # active | shadowed | skipped_untrusted | invalid | empty_body | renamed_builtin
    detail: str = ""


def collect_extensions(
    project_root: Path | None,
    *,
    project_trusted: bool,
    config: Config,
) -> dict[str, Any]:
    """Return a JSON-serialisable extensions diagnostic block."""
    skills = _scan_markdown_kind("skill", skill_dirs(project_root), project_trusted)
    commands = _scan_commands(project_root, project_trusted)
    subagents = _scan_subagents(project_root, project_trusted)
    hooks = _config_hooks(config, project_trusted)
    mcp = _config_mcp(config, project_trusted)
    async_subagents = _config_async(config, project_trusted)

    warnings: list[str] = []
    if not project_trusted and project_root is not None:
        skipped = sum(
            1
            for row in (*skills, *commands, *subagents)
            if row.status == "skipped_untrusted"
        )
        if skipped:
            warnings.append(
                f"{skipped} project-tier extension file(s) skipped — run "
                f"`jarn trust {project_root}` to load hooks/MCP/skills from this repo."
            )
    for row in (*skills, *commands, *subagents):
        if row.status == "invalid":
            warnings.append(f"Invalid frontmatter in {row.path}")
        if row.status == "empty_body" and row.kind == "subagent":
            warnings.append(f"Subagent {row.name!r} has no body — skipped at load")

    active_counts = {
        "skills": sum(1 for r in skills if r.status == "active"),
        "commands": sum(1 for r in commands if r.status == "active"),
        "subagents": sum(1 for r in subagents if r.status == "active"),
        "hooks": sum(1 for h in hooks if h["status"] == "active"),
        "mcp_servers": sum(1 for s in mcp if s["status"] == "active"),
        "async_subagents": sum(1 for a in async_subagents if a["status"] == "active"),
    }

    return {
        "project_trusted": project_trusted,
        "counts": active_counts,
        "skills": [asdict(r) for r in skills],
        "commands": [asdict(r) for r in commands],
        "subagents": [asdict(r) for r in subagents],
        "hooks": hooks,
        "mcp_servers": mcp,
        "async_subagents": async_subagents,
        "warnings": warnings,
    }


def _scope_for(path: Path, global_dir: Path) -> str:
    return "global" if str(path).startswith(str(global_dir)) else "project"


def _scan_markdown_kind(
    kind: str,
    dirs: list[Path],
    project_trusted: bool,
    *,
    name_key: str = "name",
    require_body: bool = False,
) -> list[MarkdownExtensionRow]:
    """Scan markdown extensions; later paths win on name conflict (project > global)."""
    global_dir = dirs[0] if dirs else Path()
    pending: list[MarkdownExtensionRow] = []

    for path in discover(dirs):
        scope = _scope_for(path, global_dir)
        try:
            doc = parse(path)
        except OSError as exc:
            pending.append(
                MarkdownExtensionRow(
                    kind=kind,
                    name=path.stem,
                    scope=scope,
                    path=str(path),
                    status="invalid",
                    detail=str(exc),
                )
            )
            continue

        yaml_err = _frontmatter_yaml_error(path)
        if yaml_err is not None:
            pending.append(
                MarkdownExtensionRow(
                    kind=kind,
                    name=path.stem,
                    scope=scope,
                    path=str(path),
                    status="invalid",
                    detail=yaml_err,
                )
            )
            continue

        meta = doc.meta
        if not isinstance(meta, dict):
            meta = {}
        name = str(meta.get(name_key) or path.stem)

        if scope == "project" and not project_trusted:
            pending.append(
                MarkdownExtensionRow(
                    kind=kind,
                    name=name,
                    scope=scope,
                    path=str(path),
                    status="skipped_untrusted",
                )
            )
            continue

        if require_body and not doc.body.strip():
            pending.append(
                MarkdownExtensionRow(
                    kind=kind,
                    name=name,
                    scope=scope,
                    path=str(path),
                    status="empty_body",
                )
            )
            continue

        pending.append(
            MarkdownExtensionRow(
                kind=kind,
                name=name,
                scope=scope,
                path=str(path),
                status="pending",
            )
        )

    return _resolve_name_winners(pending)


def _resolve_name_winners(rows: list[MarkdownExtensionRow]) -> list[MarkdownExtensionRow]:
    """Mark the last ``pending`` row per name as active; earlier ones shadowed."""
    terminal = {"invalid", "skipped_untrusted", "empty_body", "renamed_builtin"}
    out: list[MarkdownExtensionRow] = []
    loadable_idx: dict[str, int] = {}

    for i, row in enumerate(rows):
        if row.status in terminal:
            out.append(row)
            continue
        loadable_idx[row.name] = i

    for i, row in enumerate(rows):
        if row.status != "pending":
            continue
        winner_i = loadable_idx[row.name]
        if i == winner_i:
            out.append(
                MarkdownExtensionRow(
                    kind=row.kind,
                    name=row.name,
                    scope=row.scope,
                    path=row.path,
                    status="active",
                )
            )
        else:
            winner = rows[winner_i]
            out.append(
                MarkdownExtensionRow(
                    kind=row.kind,
                    name=row.name,
                    scope=row.scope,
                    path=row.path,
                    status="shadowed",
                    detail=f"shadowed by {winner.scope} at {winner.path}",
                )
            )
    return out


def _scan_commands(project_root: Path | None, project_trusted: bool) -> list[MarkdownExtensionRow]:
    dirs = command_dirs(project_root)
    global_dir = dirs[0] if dirs else Path()
    pending: list[MarkdownExtensionRow] = []
    meta_by_path: dict[str, tuple[str, bool]] = {}  # path -> (declared, renamed)

    for path in discover(dirs):
        scope = _scope_for(path, global_dir)
        doc = parse(path)
        declared = str(doc.meta.get("name") or path.stem)
        load_name = declared
        renamed = declared in BUILTIN_COMMANDS
        if renamed:
            load_name = f"{declared}-custom"
        meta_by_path[str(path)] = (declared, renamed)

        if scope == "project" and not project_trusted:
            pending.append(
                MarkdownExtensionRow(
                    kind="command",
                    name=declared,
                    scope=scope,
                    path=str(path),
                    status="skipped_untrusted",
                    detail=f"would load as /{load_name}" if renamed else "",
                )
            )
            continue

        pending.append(
            MarkdownExtensionRow(
                kind="command",
                name=load_name,
                scope=scope,
                path=str(path),
                status="pending",
            )
        )

    resolved: list[MarkdownExtensionRow] = []
    for row in _resolve_name_winners(pending):
        if row.status not in ("active", "shadowed"):
            resolved.append(row)
            continue
        declared, renamed = meta_by_path[row.path]
        detail = f"loads as /{row.name}" if renamed else ""
        status = "renamed_builtin" if renamed and row.status == "active" else row.status
        resolved.append(
            MarkdownExtensionRow(
                kind="command",
                name=declared,
                scope=row.scope,
                path=row.path,
                status=status,
                detail=detail,
            )
        )
    return resolved


def _scan_subagents(project_root: Path | None, project_trusted: bool) -> list[MarkdownExtensionRow]:
    return _scan_markdown_kind(
        "subagent",
        agent_dirs(project_root),
        project_trusted,
        require_body=True,
    )


def _config_hooks(config: Config, project_trusted: bool) -> list[dict[str, Any]]:
    if not project_trusted and config.hooks:
        # Stripped at load — should not happen when load_config used project_trusted.
        return []
    return [
        {
            "name": h.name or f"{h.event}:{h.command[:40]}",
            "event": h.event,
            "command": h.command,
            "blocking": h.blocking,
            "matcher": h.matcher,
            "status": "active",
        }
        for h in config.hooks
    ]


def _config_mcp(config: Config, project_trusted: bool) -> list[dict[str, Any]]:
    if not project_trusted:
        return []
    return [
        {
            "name": s.name,
            "transport": s.transport,
            "enabled": s.enabled,
            "health": s.health,
            "status": "active" if s.enabled else "disabled",
        }
        for s in config.mcp_servers
    ]


def _config_async(config: Config, project_trusted: bool) -> list[dict[str, Any]]:
    if not project_trusted:
        return []
    return [
        {
            "name": a.name,
            "graph_id": a.graph_id,
            "url": a.url,
            "status": "active",
        }
        for a in config.async_subagents
    ]


def _frontmatter_yaml_error(path: Path) -> str | None:
    """Return an error string when frontmatter YAML is present but invalid."""
    import re

    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return None
    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError as exc:
        return str(exc)
    if meta is not None and not isinstance(meta, dict):
        return "frontmatter must be a YAML mapping"
    return None
