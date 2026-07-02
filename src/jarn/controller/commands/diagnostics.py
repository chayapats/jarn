"""Built-in /diagnostics slash-command handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.markup import escape as _escape_markup

from jarn.controller.core import CommandResult
from jarn.extensibility.mcp import load_mcp_tools
from jarn.tui import palette

if TYPE_CHECKING:
    from jarn.controller.core import Controller


def cmd_doctor(ctrl: Controller, args: str) -> CommandResult:
    """Run the same diagnostics as ``jarn doctor`` and return them inline."""
    from jarn.doctor.collect import collect_doctor
    from jarn.doctor.render import doctor_lines

    diag: dict = {}
    collect_doctor(
        diag,
        config=ctrl.config,
        project_root=ctrl.project_root,
        project_trusted=ctrl.project_trusted,
    )
    return CommandResult("\n".join(doctor_lines(diag)))

def cmd_cost(ctrl, args: str) -> CommandResult:
    t = ctrl.tracker
    lines = [f"[b]Session usage[/b] — {t.summary_line()}", f"status: {t.status().value}"]
    if t.total.cache_read_tokens or t.total.cache_creation_tokens:
        lines.append(
            f"[{palette.C_DIM}]cache[/{palette.C_DIM}] "
            f"{t.total.cache_read_tokens:,} read · "
            f"{t.total.cache_creation_tokens:,} write"
        )
    for model, usage in t.per_model.items():
        lines.append(
            f"  {_escape_markup(model)}: ${usage.cost_usd:.4f} · {usage.total_tokens:,} tok"
        )
    top = t.top_tools()
    if top:
        lines.append(f"[{palette.C_DIM}]top burners (by tool)[/{palette.C_DIM}]")
        for tool, usage in top:
            lines.append(
                f"  {_escape_markup(tool)}: ${usage.cost_usd:.4f} · "
                f"{usage.total_tokens:,} tok · {usage.calls} calls"
            )
    lines.extend(_context_injection_lines(ctrl))
    return CommandResult("\n".join(lines))

def cmd_permissions(ctrl, args: str) -> CommandResult:
    r = ctrl.config.permissions
    session_allow = ctrl.engine._all_allow()[len(r.allow) :]
    lines = [
        f"[b]Mode[/b]: {ctrl.config.permission_mode.value}",
        f"[b]Allow[/b]: {', '.join(_escape_markup(p) for p in r.allow) or '(none)'}",
        f"[b]Deny[/b]: {', '.join(_escape_markup(p) for p in r.deny) or '(none)'}",
        f"[b]Session-allow[/b]: "
        f"{', '.join(_escape_markup(p) for p in session_allow) or '(none)'}",
    ]
    return CommandResult("\n".join(lines))

def cmd_mcp(ctrl, args: str) -> CommandResult:
    """Show configured MCP servers with per-server health + last error.

    Usage: ``/mcp``, ``/mcp status``, ``/mcp refresh``, or ``/mcp status --refresh``
    to re-probe servers and refresh health maps."""
    import asyncio

    parts = args.strip().split()
    sub = parts[0].lower() if parts else ""
    if sub and sub not in ("status", "refresh"):
        return CommandResult("Usage: /mcp [status] [--refresh|refresh]")
    refresh = sub == "refresh" or "--refresh" in parts
    if refresh:
        mcp = asyncio.run(load_mcp_tools(ctrl.config.mcp_servers))
        ctrl.mcp_health = dict(mcp.health)
        ctrl.mcp_errors = dict(mcp.errors)
        for server in ctrl.config.mcp_servers:
            if server.name in ctrl.mcp_health:
                server.health = ctrl.mcp_health[server.name]
    servers = ctrl.config.mcp_servers
    if not servers:
        return CommandResult("No MCP servers configured.")
    glyph = {
        "ok": f"[{palette.C_SUCCESS}]●[/{palette.C_SUCCESS}]",
        "error": f"[{palette.C_ERROR}]✗[/{palette.C_ERROR}]",
    }
    lines = ["[b]MCP servers[/b]"]
    for server in servers:
        health = ctrl.mcp_health.get(server.name, server.health or "unknown")
        mark = glyph.get(health, f"[{palette.C_DIM}]○[/{palette.C_DIM}]")
        transport = getattr(server, "transport", "") or ""
        detail = f" [dim]({_escape_markup(transport)})[/dim]" if transport else ""
        line = f"  {mark} [cyan]{_escape_markup(server.name)}[/cyan]{detail} — {health}"
        err = ctrl.mcp_errors.get(server.name)
        if err:
            line += f"\n      [dim]last error: {_escape_markup(err)}[/dim]"
        lines.append(line)
    if not ctrl.runtime:
        lines.append(
            f"[{palette.C_DIM}]Health is populated after the first turn "
            f"loads the servers.[/{palette.C_DIM}]"
        )
    return CommandResult("\n".join(lines))

def cmd_telemetry(ctrl, args: str) -> CommandResult:
    """Show telemetry opt-in status and local sink stats."""
    sub = args.strip().lower()
    if sub and sub != "status":
        return CommandResult("Usage: /telemetry status")
    summary = ctrl.telemetry.status_summary()
    enabled = "enabled" if summary["enabled"] else "disabled"
    install = "present" if summary["install_id_present"] else "absent"
    size_kb = summary["size_bytes"] / 1024
    lines = [
        "[b]Telemetry[/b]",
        f"  status: {enabled}",
        f"  file: {_escape_markup(summary['path']) or '(none)'}",
        f"  size: {size_kb:.1f} KB ({summary['size_bytes']:,} bytes)",
        f"  events on disk: {summary['event_count']:,}",
        f"  install id: {install}",
    ]
    if not summary["enabled"]:
        lines.append(
            f"[{palette.C_DIM}]Opt in with observability.telemetry: true "
            f"in config.[/{palette.C_DIM}]"
        )
    return CommandResult("\n".join(lines))

def cmd_ps(ctrl, args: str) -> CommandResult:
    """List background processes, or ``/ps kill <id>`` to stop one."""
    from jarn.agent.background import manager

    mgr = manager()
    parts = args.split()
    if parts and parts[0] == "kill":
        if len(parts) < 2:
            return CommandResult("Usage: /ps kill <id>")
        ok = mgr.kill(parts[1])
        return CommandResult(
            f"Killed {parts[1]}." if ok else f"No background process {parts[1]!r}."
        )
    procs = mgr.list()
    if not procs:
        return CommandResult("No background processes.")
    lines = ["[b]Background processes[/b]"]
    for p in procs:
        state = "running" if p["running"] else f"exited ({p['exit_code']})"
        lines.append(f"  [cyan]{p['id']}[/cyan] [dim][{state}][/dim] {_escape_markup(p['command'])}")
    lines.append("[dim]/ps kill <id> to stop one[/dim]")
    return CommandResult("\n".join(lines))

def cmd_checkpoints(ctrl, args: str) -> CommandResult:
    """List recent auto-checkpoints available for /undo."""
    if not ctrl.checkpoint_manager.enabled:
        return CommandResult(
            "Autocheckpoint is disabled. "
            "Set git.autocheckpoint: true in your config to enable /undo."
        )
    if not ctrl.checkpoint_manager.is_repo:
        return CommandResult("Not a git repository — checkpoints are unavailable.")
    entries = ctrl.checkpoint_manager.list()
    if not entries:
        return CommandResult(
            "No checkpoints yet. "
            "Checkpoints are taken automatically at the start of each agent turn."
        )
    lines = ["[b]Checkpoints[/b] [dim](most recent first)[/dim]"]
    for i, entry in enumerate(entries):
        marker = "→ " if i == 0 else "  "
        lines.append(
            f"{marker}[dim]{entry.sha[:12]}[/dim] {_escape_markup(entry.label)}"
        )
    return CommandResult("\n".join(lines))

def _context_injection_lines(ctrl) -> list[str]:
    """Token sizes for blocks injected into the system prompt."""
    from jarn.memory.context import DEFAULT_CONTEXT_FILES, project_context_text
    from jarn.memory.store import MemoryStore
    from jarn.memory.tokens import count_tokens
    from jarn.memory.wiki import WikiStore

    ctx = ctrl.config.context
    lines = [
        "",
        "[b]Context injection[/b] [dim](/memory dump for full text)[/dim]",
    ]

    names = ctrl.config.compat.context_files or DEFAULT_CONTEXT_FILES
    proj_text = (
        project_context_text(
            ctrl.project_root,
            context_files=names,
            token_budget=ctx.project_context_tokens,
        )
        if ctrl.project_trusted
        else None
    )
    proj_tok = count_tokens(proj_text) if proj_text else 0
    lines.append(
        f"  project context: {proj_tok:,} / {ctx.project_context_tokens:,} tok"
    )

    global_index = MemoryStore.global_store().index_text(token_budget=ctx.memory_tokens)
    global_tok = count_tokens(global_index) if global_index.strip() else 0
    lines.append(f"  memory (global): {global_tok:,} / {ctx.memory_tokens:,} tok")

    project_tok = 0
    if ctrl.project_trusted:
        project_store = MemoryStore.project_store(ctrl.project_root)
        if project_store:
            project_index = project_store.index_text(token_budget=ctx.memory_tokens)
            project_tok = count_tokens(project_index) if project_index.strip() else 0
    lines.append(f"  memory (project): {project_tok:,} / {ctx.memory_tokens:,} tok")

    wiki_store = WikiStore.build(ctrl.project_root)
    if ctrl.project_trusted:
        wiki_index = wiki_store.index_text(token_budget=ctx.wiki_index_tokens)
    else:
        from jarn.memory.wiki import WikiStore as _WS

        wiki_index = _WS(global_wiki_dir=wiki_store.global_wiki_dir).index_text(
            token_budget=ctx.wiki_index_tokens
        )
    wiki_tok = count_tokens(wiki_index) if wiki_index.strip() else 0
    lines.append(f"  wiki index: {wiki_tok:,} / {ctx.wiki_index_tokens:,} tok")
    return lines
