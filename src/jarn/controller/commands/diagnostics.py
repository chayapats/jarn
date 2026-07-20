"""Built-in /diagnostics slash-command handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.markup import escape as _escape_markup

from jarn.controller.core import CommandResult
from jarn.extensibility.mcp import load_mcp_tools, run_blocking
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
        extra_roots=ctrl.extra_roots,
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
    """Show MCP server health, and list/invoke prompts + list/read resources.

    Subcommands:
      ``/mcp [status] [--refresh|refresh]``  per-server health + last error
      ``/mcp prompts``                       list server prompts and register each
                                             as an invokable ``/mcp__<server>__<p>``
      ``/mcp prompt <server> <name> [k=v …]`` fetch a prompt's text (and register)
      ``/mcp resources``                     list server resources
      ``/mcp read <server> <uri>``           read a resource's content into view
    """
    parts = args.strip().split()
    sub = parts[0].lower() if parts else ""
    rest = parts[1:]
    if sub == "prompts":
        return _mcp_prompts(ctrl)
    if sub == "prompt":
        return _mcp_prompt(ctrl, rest)
    if sub in ("resources", "resource"):
        return _mcp_resources(ctrl)
    if sub == "read":
        return _mcp_read(ctrl, rest)
    if sub not in ("", "status", "refresh") and "--refresh" not in parts:
        return CommandResult(
            "Usage: /mcp [status|refresh|prompts|prompt <server> <name> [k=v …]"
            "|resources|read <server> <uri>]"
        )
    return _mcp_status(ctrl, parts)


def _mcp_status(ctrl, parts: list[str]) -> CommandResult:
    """Per-server health + last error; ``refresh``/``--refresh`` re-probes."""
    sub = parts[0].lower() if parts else ""
    refresh = sub == "refresh" or "--refresh" in parts
    if refresh:
        # The sync command registry is invoked FROM the async REPL's running
        # loop; run_blocking probes on a one-shot worker thread there, and
        # asyncio.run inline when no loop is running (tests / headless).
        net = ctrl.config.permissions.network
        mcp = run_blocking(load_mcp_tools(ctrl.config.mcp_servers, net))
        ctrl.mcp_health = dict(mcp.health)
        ctrl.mcp_errors = dict(mcp.errors)
        # Replace the runtime's MCP cache with this fresh probe so the NEXT
        # rebuild (auto model rotation, mode change, /config set) mirrors the
        # refreshed health/tools instead of reverting to the stale cached values
        # ensure_runtime would otherwise re-apply on a cache hit.
        ctrl._mcp_cache = mcp
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


def _append_mcp_errors(lines: list[str], errors: dict) -> None:
    """Append one dimmed line per per-server discovery error (isolation aware)."""
    for name in sorted(errors):
        lines.append(
            f"  [{palette.C_ERROR}]✗[/{palette.C_ERROR}] {_escape_markup(name)}: "
            f"[{palette.C_DIM}]{_escape_markup(errors[name])}[/{palette.C_DIM}]"
        )


def _register_prompt_commands(ctrl, prompts: dict) -> None:
    """Register discovered MCP prompts into the live runtime's command table.

    The REPL dispatches ``rt.commands[name].render(args)`` into a turn, so adding
    the MCP prompt commands here makes ``/mcp__<server>__<prompt>`` inject the
    prompt text through the EXISTING turn path — no REPL change. The entries are
    wiped on the next runtime rebuild (model/mode change); re-run ``/mcp prompts``
    to refresh. Follow-up: merge these in ``build_runtime`` so they survive
    rebuilds and appear in tab-completion without a manual ``/mcp prompts``."""
    rt = ctrl.runtime
    if rt is None or not prompts:
        return
    rt.commands.update(prompts)


def _mcp_prompts(ctrl) -> CommandResult:
    """List server prompts and register each as an invokable slash command."""
    servers = ctrl.config.mcp_servers
    if not servers:
        return CommandResult("No MCP servers configured.")
    from jarn.extensibility.mcp import load_mcp_prompts

    res = run_blocking(load_mcp_prompts(servers))
    _register_prompt_commands(ctrl, res.prompts)
    lines = ["[b]MCP prompts[/b]"]
    if res.prompts:
        lines.append(
            f"[{palette.C_DIM}]invoke with /<name> — injects the prompt into your "
            f"turn[/{palette.C_DIM}]"
        )
        for name in sorted(res.prompts):
            cmd = res.prompts[name]
            args = (
                f" [{palette.C_DIM}]({', '.join(cmd.argument_names)})[/{palette.C_DIM}]"
                if cmd.argument_names
                else ""
            )
            desc = f" — {_escape_markup(cmd.description)}" if cmd.description else ""
            lines.append(f"  [cyan]/{_escape_markup(name)}[/cyan]{args}{desc}")
    else:
        lines.append(f"[{palette.C_DIM}]No prompts available.[/{palette.C_DIM}]")
    _append_mcp_errors(lines, res.errors)
    if res.prompts and not ctrl.runtime:
        lines.append(
            f"[{palette.C_DIM}]Prompts become invokable after the first turn "
            f"builds the runtime — re-run /mcp prompts then.[/{palette.C_DIM}]"
        )
    return CommandResult("\n".join(lines))


def _mcp_prompt(ctrl, rest: list[str]) -> CommandResult:
    """Fetch a single prompt's text (and register it for direct invocation)."""
    if len(rest) < 2:
        return CommandResult("Usage: /mcp prompt <server> <name> [key=value …]")
    server, pname = rest[0], rest[1]
    arg_str = " ".join(rest[2:])
    if not any(s.name == server and s.enabled for s in ctrl.config.mcp_servers):
        return CommandResult(f"No enabled MCP server named {server!r}.")
    from jarn.config.secrets import redact_secrets
    from jarn.extensibility.mcp import load_mcp_prompts

    res = run_blocking(load_mcp_prompts(ctrl.config.mcp_servers))
    key = f"mcp__{server}__{pname}"
    cmd = res.prompts.get(key)
    if cmd is None:
        available = ", ".join(sorted(res.prompts)) or "(none discovered)"
        return CommandResult(f"No MCP prompt {key!r}. Available: {available}")
    _register_prompt_commands(ctrl, res.prompts)
    try:
        text = cmd.render(arg_str)
    except Exception as exc:  # noqa: BLE001 - surface a clean, redacted message
        return CommandResult(redact_secrets(f"Failed to fetch prompt {key}: {exc}"))
    if not text.strip():
        return CommandResult(f"Prompt {key} returned no text.")
    note = (
        f"[{palette.C_DIM}]{_escape_markup(key)} — invoke /{_escape_markup(key)} "
        f"to inject this into a turn[/{palette.C_DIM}]"
    )
    return CommandResult(f"{note}\n{_escape_markup(text)}")


def _mcp_resources(ctrl) -> CommandResult:
    """List resources published by every enabled MCP server."""
    servers = ctrl.config.mcp_servers
    if not servers:
        return CommandResult("No MCP servers configured.")
    from jarn.extensibility.mcp import list_mcp_resources

    res = run_blocking(list_mcp_resources(servers))
    lines = ["[b]MCP resources[/b]"]
    if res.resources:
        lines.append(
            f"[{palette.C_DIM}]read with /mcp read <server> <uri>[/{palette.C_DIM}]"
        )
        for r in res.resources:
            label = _escape_markup(r.name or r.description or "")
            mime = (
                f" [{palette.C_DIM}]{_escape_markup(r.mime_type)}[/{palette.C_DIM}]"
                if r.mime_type
                else ""
            )
            tail = f" — {label}" if label else ""
            lines.append(
                f"  [cyan]{_escape_markup(r.server)}[/cyan] "
                f"{_escape_markup(r.uri)}{mime}{tail}"
            )
    else:
        lines.append(f"[{palette.C_DIM}]No resources available.[/{palette.C_DIM}]")
    _append_mcp_errors(lines, res.errors)
    return CommandResult("\n".join(lines))


def _mcp_read(ctrl, rest: list[str]) -> CommandResult:
    """Read one resource's content into view."""
    if len(rest) < 2:
        return CommandResult("Usage: /mcp read <server> <uri>")
    server, uri = rest[0], rest[1]
    from jarn.config.secrets import redact_secrets
    from jarn.extensibility.mcp import read_mcp_resource

    try:
        content = run_blocking(read_mcp_resource(ctrl.config.mcp_servers, server, uri))
    except Exception as exc:  # noqa: BLE001 - surface a clean, redacted message
        return CommandResult(
            redact_secrets(f"Failed to read {uri} from {server}: {exc}")
        )
    if not content.strip():
        return CommandResult(f"Resource {uri} on {server} returned no content.")
    header = (
        f"[b]{_escape_markup(server)}[/b] "
        f"[{palette.C_DIM}]{_escape_markup(uri)}[/{palette.C_DIM}]"
    )
    return CommandResult(f"{header}\n{_escape_markup(content)}")

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
