"""Shared Rich and JSON rendering for ``jarn doctor`` and ``/doctor``."""

from __future__ import annotations

import json

from rich.console import Console
from rich.markup import escape as _escape


def _esc(value: object) -> str:
    """Escape a value for Rich markup; ``None`` becomes empty."""
    if value is None:
        return ""
    return _escape(str(value))


def doctor_to_json(diag: dict) -> str:
    """Serialize doctor diagnostics to a JSON string."""
    return json.dumps(diag)


def doctor_lines(diag: dict) -> list[str]:
    """Return Rich-markup lines for doctor diagnostics."""
    lines: list[str] = ["[b]jarn doctor[/b]"]

    gpath = diag.get("global_config", "")
    present = diag.get("global_config_present", False)
    lines.append(
        f"global config: {_esc(gpath)} "
        f"{'[green]✔[/green]' if present else '[red]missing[/red]'}"
    )
    if diag.get("jarn_home_warning"):
        lines.append(f"[yellow]{_esc(diag['jarn_home_warning'])}[/yellow]")
    root = diag.get("project_root")
    lines.append(f"project root: {_esc(root) if root else '[dim]none[/dim]'}")
    if diag.get("project_trusted") is False and diag.get("project_stripped_keys"):
        keys = ", ".join(diag["project_stripped_keys"])
        lines.append(
            f"[yellow]project untrusted — stripped keys: {_esc(keys)}[/yellow]"
            " [dim](run `jarn trust <root>` to enable)[/dim]"
        )

    if not present:
        lines.append("\n[yellow]No config — run [b]jarn setup[/b].[/yellow]")
        return lines

    lines.append(f"default profile: {_esc(diag.get('default_profile', ''))}")
    lines.append(f"main model: {_esc(diag.get('main_model', ''))}")
    _mode = diag.get("permission_mode", "")
    _eff_mode = diag.get("effective_mode", _mode)
    _mode_str = (
        _mode if _eff_mode == _mode
        else f"{_mode} · effective: {_eff_mode} (after trust clamp)"
    )
    lines.append(f"mode: {_esc(_mode_str)}")
    web_tools_str = "on" if diag.get("web_tools", True) else "off"
    lines.append(f"web tools: {web_tools_str}")

    sbx = diag.get("sandbox") or {}
    sbx_backend = sbx.get("backend") or "none"
    sbx_avail = sbx.get("available", False)
    sbx_mode = sbx.get("mode", "off")
    if sbx_avail:
        sbx_status = f"[green]{_esc(sbx_backend)} available[/green]"
    else:
        sbx_status = "[dim]unavailable[/dim]"
    lines.append(f"sandbox: {sbx_status} · mode {sbx_mode}")

    ex = diag.get("execution") or {}
    ex_backend = ex.get("backend", "local")
    if ex_backend == "docker" or ex.get("docker_image"):
        docker_ok = ex.get("docker_available", False)
        docker_status = "[green]available[/green]" if docker_ok else "[dim]unavailable[/dim]"
        lines.append(
            f"execution backend: {_esc(ex_backend)} · docker: {docker_status}"
            f" · image {_esc(ex.get('docker_image') or '')}"
        )
    else:
        lines.append(f"execution backend: {_esc(ex_backend)}")

    git_diag = diag.get("git") or {}
    autockpt = "on" if git_diag.get("autocheckpoint") else "off"
    lines.append(f"git.autocheckpoint: {autockpt}")

    wiki_diag = diag.get("wiki") or {}
    wiki_enabled = "on" if wiki_diag.get("enabled") else "off"
    lines.append(f"wiki.enabled: {wiki_enabled}")

    obs_diag = diag.get("observability") or {}
    transcript = "on" if obs_diag.get("transcript", True) else "off"
    lines.append(f"observability.transcript: {transcript}")

    ctx_diag = diag.get("context") or {}
    repo_map_mode = ctx_diag.get("repo_map", "tool")
    repo_map_tokens = ctx_diag.get("repo_map_tokens", 1024)
    lines.append(f"context.repo_map: {repo_map_mode} · token_budget {repo_map_tokens}")

    lines.append("\n[b]Providers[/b]")
    for entry in diag.get("providers") or []:
        if entry.get("key_ok"):
            key_state = "[green]key ok[/green]"
        else:
            key_state = f"[yellow]{_esc(entry.get('key_state', ''))}[/yellow]"
        lines.append(
            f"  {_esc(entry.get('name', ''))} "
            f"({_esc(entry.get('type', ''))}): {key_state}"
        )

    lines.append("\n[b]Main model build[/b]")
    if diag.get("main_model_builds"):
        lines.append("  [green]✔ model constructs[/green]")
    else:
        lines.append(
            f"  [red]✗ {_esc(diag.get('main_model_error') or '')}[/red]"
        )

    append_extension_lines(lines, diag.get("extensions") or {})

    ok = diag.get("ok", False)
    lines.append(
        f"\n{'[green]All good.[/green]' if ok else '[yellow]Issues found above.[/yellow]'}"
    )
    return lines


def append_extension_lines(lines: list[str], ext: dict) -> None:
    """Append the doctor Extensions block as Rich-markup lines."""
    counts = ext.get("counts") or {}
    lines.append("\n[b]Extensions[/b]")
    if ext.get("project_trusted") is False:
        lines.append(
            "  [yellow]project untrusted — project-tier files/config skipped[/yellow]"
        )
    lines.append(
        "  "
        f"skills {counts.get('skills', 0)} · "
        f"commands {counts.get('commands', 0)} · "
        f"subagents {counts.get('subagents', 0)} · "
        f"hooks {counts.get('hooks', 0)} · "
        f"mcp {counts.get('mcp_servers', 0)} · "
        f"async {counts.get('async_subagents', 0)}"
    )

    for warning in ext.get("warnings") or []:
        lines.append(f"  [yellow]⚠ {_esc(warning)}[/yellow]")

    for kind, label in (
        ("skills", "Skills"),
        ("commands", "Commands"),
        ("subagents", "Subagents"),
    ):
        rows = ext.get(kind) or []
        active = [r for r in rows if r.get("status") in ("active", "renamed_builtin")]
        if not active:
            continue
        lines.append(f"\n  [b]{label}[/b]")
        for row in active:
            scope = _esc(row.get("scope", ""))
            name = _esc(row.get("name", ""))
            detail = _esc(row.get("detail", ""))
            suffix = f" — {detail}" if detail else ""
            lines.append(f"    [green]✔[/green] {name} ({scope}){suffix}")

    hooks = [h for h in ext.get("hooks") or [] if h.get("status") == "active"]
    if hooks:
        lines.append("\n  [b]Hooks[/b]")
        for hook in hooks:
            blocking = "blocking" if hook.get("blocking") else "non-blocking"
            lines.append(
                f"    [green]✔[/green] "
                f"{_esc(hook.get('event') or '')} ({blocking}): "
                f"{_esc(hook.get('command') or '')}"
            )

    servers = [s for s in ext.get("mcp_servers") or [] if s.get("status") == "active"]
    if servers:
        lines.append("\n  [b]MCP servers[/b]")
        for server in servers:
            health = server.get("health") or "unknown"
            lines.append(
                f"    [green]✔[/green] "
                f"{_esc(server.get('name') or '')} "
                f"({_esc(server.get('transport') or '')}, health={_esc(health)})"
            )

    shadowed = [
        r
        for kind in ("skills", "commands", "subagents")
        for r in (ext.get(kind) or [])
        if r.get("status") == "shadowed"
    ]
    if shadowed:
        lines.append("\n  [dim]Shadowed (not loaded):[/dim]")
        for row in shadowed:
            lines.append(
                f"    [dim]{_esc(row.get('name') or '')} "
                f"({_esc(row.get('scope') or '')}) — "
                f"{_esc(row.get('detail', ''))}[/dim]"
            )


def render_doctor_console(console: Console, diag: dict) -> None:
    """Print doctor diagnostics to a Rich console."""
    console.rule("[b]jarn doctor[/b]")
    for line in doctor_lines(diag)[1:]:
        console.print(line)
