"""Shared Rich and JSON rendering for ``jarn doctor`` and ``/doctor``."""

from __future__ import annotations

import json

from rich.console import Console
from rich.markup import escape as _escape

from jarn.tui import grammar, layout


def _esc(value: object) -> str:
    """Escape a value for Rich markup; ``None`` becomes empty."""
    if value is None:
        return ""
    from jarn.config.secrets import redact_secrets

    return _escape(redact_secrets(str(value)))


def doctor_to_json(diag: dict) -> str:
    """Serialize doctor diagnostics to a JSON string."""
    from jarn.config.secrets import redact_structure

    return json.dumps(redact_structure(diag))


def doctor_lines(diag: dict) -> list[str]:
    """Return Rich-markup lines for doctor diagnostics."""
    lines: list[str] = ["[b]jarn doctor[/b]"]

    append_inventory_lines(lines, diag)

    gpath = diag.get("global_config", "")
    present = diag.get("global_config_present", False)
    lines.append(
        f"global config: {_esc(gpath)} "
        f"{layout.ok(grammar.GLYPH_OK) if present else layout.err('missing')}"
    )
    if diag.get("jarn_home_warning"):
        lines.append(layout.warn(diag['jarn_home_warning']))
    if diag.get("jarn_home_mode_warning"):
        lines.append(layout.warn(diag['jarn_home_mode_warning']))
    root = diag.get("project_root")
    lines.append(f"project root: {_esc(root) if root else '[dim]none[/dim]'}")
    # Added (--add-dir / /add-dir) roots beyond the primary, if any.
    added_roots = [r for r in diag.get("roots", []) if r != (str(root) if root else None)]
    if added_roots:
        lines.append(
            "added roots: " + ", ".join(_esc(r) for r in added_roots)
            + " [dim](write scope only — checkpoint/context stay primary)[/dim]"
        )
    if diag.get("project_trusted") is False and diag.get("project_stripped_keys"):
        keys = ", ".join(diag["project_stripped_keys"])
        lines.append(
            f"{layout.warn('project untrusted — stripped keys: ' + _esc(keys))}"
            " [dim](run `jarn trust <root>` to enable)[/dim]"
        )

    if not present:
        lines.append("\n" + layout.warn("No config — run jarn setup."))
        append_error_lines(lines, diag.get("errors") or [])
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
        sbx_status = f"{layout.ok(_esc(sbx_backend) + ' available')}"
    else:
        sbx_status = "[dim]unavailable[/dim]"
    lines.append(f"sandbox: {sbx_status} · mode {sbx_mode}")

    ex = diag.get("execution") or {}
    ex_backend = ex.get("backend", "local")
    if ex_backend == "docker" or ex.get("docker_image"):
        docker_ok = ex.get("docker_available", False)
        docker_status = layout.ok("available") if docker_ok else layout.muted("unavailable")
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

    module_diag = diag.get("prompt_modules") or {}
    module_rows = module_diag.get("modules") or []
    active_modules = [row for row in module_rows if row.get("active")]
    prompt_tokens = module_diag.get("prompt_tokens")
    if prompt_tokens is not None:
        lines.append(
            f"prompt modules: {len(active_modules)} active · {prompt_tokens:,} tok assembled"
        )
    if module_diag.get("error"):
        lines.append(
            f"{layout.warn('prompt module diagnostics unavailable: ' + _esc(module_diag['error']))}"
        )

    lines.append("\n[b]Providers[/b]")
    for entry in diag.get("providers") or []:
        if entry.get("key_ok"):
            key_state = layout.ok("key ok")
        else:
            key_state = layout.warn(entry.get("key_state", ""))
        lines.append(
            f"  {_esc(entry.get('name', ''))} "
            f"({_esc(entry.get('type', ''))}): {key_state}"
        )

    lines.append("\n[b]Main model build[/b]")
    if diag.get("main_model_builds"):
        lines.append(f"  {layout.ok(grammar.GLYPH_OK + ' model constructs')}")
    else:
        lines.append(
            f"  {layout.err(grammar.GLYPH_FAIL + ' ' + _esc(diag.get('main_model_error') or ''))}"
        )

    append_extension_lines(lines, diag.get("extensions") or {})

    append_error_lines(lines, diag.get("errors") or [])

    for warning in diag.get("warnings") or []:
        lines.append(layout.warn(f"{grammar.GLYPH_WARN} {_esc(warning)}"))

    ok = diag.get("ok", False)
    lines.append(
        f"\n{layout.banner_ok('All good.') if ok else layout.banner_warn('Issues found above. Run jarn doctor --fix --dry-run to preview repairs.')}"
    )
    return lines


def append_inventory_lines(lines: list[str], diag: dict) -> None:
    """Append concise human output for the machine-readable host inventory."""
    jarn = diag.get("jarn") or {}
    if jarn:
        install = jarn.get("install") or {}
        active = jarn.get("active_executable")
        lines.append(
            f"version/install: {_esc(jarn.get('version') or 'unknown')} · "
            f"{_esc(install.get('method') or 'unknown')}"
        )
        lines.append(
            "active executable: "
            + (f"{layout.ok('found')} {_esc(active)}" if active else layout.err("missing"))
        )
        for shadow in jarn.get("shadowed") or []:
            lines.append(f"  {layout.warn('shadowing candidate:')} {_esc(shadow)}")
        for candidate in jarn.get("candidate_inventory") or []:
            if not isinstance(candidate, dict) or candidate.get("active") or candidate.get("on_path"):
                continue
            sources = ", ".join(str(value) for value in candidate.get("sources") or [])
            lines.append(
                f"  {layout.warn('other installation (off PATH):')} "
                f"{_esc(candidate.get('path'))} · {_esc(sources or 'unknown owner')}"
            )
        if install.get("canonical_record_error"):
            lines.append(
                f"  {layout.err('install record invalid:')} "
                f"{_esc(install['canonical_record_error'])}"
            )
        if install.get("active_matches_record") is False:
            lines.append(
                f"  {layout.err('activation mismatch:')} active executable differs from metadata"
            )

    host = diag.get("platform") or {}
    if host:
        libc = host.get("libc") or {}
        python = host.get("python") or {}
        lines.append(
            "host: "
            f"{_esc(host.get('system'))} {_esc(host.get('release'))} · "
            f"{_esc(host.get('architecture'))} · "
            f"{_esc(libc.get('name'))} {_esc(libc.get('version') or 'unknown')} · "
            f"Python {_esc(python.get('version') or 'unknown')}"
        )

    shell = diag.get("shell") or {}
    if shell:
        profile_state = "present" if shell.get("profile_present") else "missing"
        lines.append(
            f"shell/profile: {_esc(shell.get('name') or 'unknown')} · "
            f"{profile_state} {_esc(shell.get('profile') or '')}"
        )

    installation = diag.get("installation") or {}
    if installation:
        writable = installation.get("writable")
        write_state = "writable" if writable else "not writable"
        free = installation.get("free_bytes")
        free_text = (
            f"{int(free) // (1024 * 1024 * 1024):,} GiB free"
            if free is not None
            else "free space unknown"
        )
        lines.append(
            f"install directory: {write_state} · mode "
            f"{_esc(installation.get('directory_mode') or 'unknown')} · {free_text}"
        )

    dependencies = diag.get("dependencies") or {}
    if dependencies:
        for name in ("uv", "codex"):
            item = dependencies.get(name) or {}
            state = layout.ok("ok") if item.get("ok") else layout.err("unavailable")
            lines.append(
                f"{name}: {state} · {_esc(item.get('version') or 'version unknown')} · "
                f"{_esc(item.get('path') or 'not on PATH')}"
            )
        protocol = (dependencies.get("codex") or {}).get("protocol") or {}
        compatible = protocol.get("compatible")
        state = "compatible" if compatible else "incompatible or unavailable"
        lines.append(f"Codex app-server protocol: {state}")

    configuration = diag.get("configuration") or {}
    if configuration:
        for tier in ("global", "project"):
            item = configuration.get(tier)
            if item:
                lines.append(
                    f"{tier} config schema: {_esc(item.get('status') or 'unknown')} "
                    f"({_esc(item.get('source_version'))} → "
                    f"{_esc(item.get('target_version'))})"
                )

    store = diag.get("secrets") or {}
    if store:
        issue_count = len(store.get("permission_issues") or [])
        state = "secure" if issue_count == 0 else f"{issue_count} permission issue(s)"
        lines.append(f"local secret store: {state}")

    catalog = diag.get("catalog") or {}
    if catalog:
        lines.append(
            f"model catalog: {_esc(catalog.get('source') or 'unknown')} · "
            f"{_esc(catalog.get('freshness') or 'unknown')}"
        )

    update = diag.get("update") or {}
    if update:
        enabled = "enabled" if update.get("checks_enabled") else "disabled"
        lines.append(
            f"updates: {_esc(update.get('channel') or 'unknown')} channel · checks {enabled}"
        )

    network = diag.get("network") or {}
    if network.get("checked"):
        checks = network.get("checks") or []
        reachable = sum(bool(item.get("reachable")) for item in checks)
        lines.append(f"provider reachability: checked {len(checks)} · reachable {reachable}")


def append_error_lines(lines: list[str], errors: list[dict]) -> None:
    """Render stable error anatomy without exposing a Python traceback."""
    if not errors:
        return
    lines.append("\n[b]Actionable errors[/b]")
    for item in errors:
        lines.append(
            f"  {layout.err(grammar.GLYPH_FAIL + ' ' + _esc(item.get('code') or 'JARN-INTERNAL-001') + ':')} "
            f"{_esc(item.get('summary') or 'Check failed.')}"
        )
        lines.append(f"    cause: {_esc(item.get('cause') or 'unknown')}")
        lines.append(
            f"    component: {_esc(item.get('component') or 'unknown')} · "
            f"retryable: {'yes' if item.get('retryable') else 'no'}"
        )
        lines.append(f"    next: {_esc(item.get('action') or 'Run jarn doctor again.')}")
        lines.append(
            f"    log/report: {_esc(item.get('report_path') or item.get('log_path') or '')}"
        )


def append_extension_lines(lines: list[str], ext: dict) -> None:
    """Append the doctor Extensions block as Rich-markup lines."""
    counts = ext.get("counts") or {}
    lines.append("\n[b]Extensions[/b]")
    if ext.get("project_trusted") is False:
        lines.append(
            f"  {layout.warn('project untrusted — project-tier files/config skipped')}"
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
        lines.append(f"  {layout.warn(grammar.GLYPH_WARN + ' ' + _esc(warning))}")

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
            lines.append(f"    {layout.ok(grammar.GLYPH_OK)} {name} ({scope}){suffix}")

    hooks = [h for h in ext.get("hooks") or [] if h.get("status") == "active"]
    if hooks:
        lines.append("\n  [b]Hooks[/b]")
        for hook in hooks:
            blocking = "blocking" if hook.get("blocking") else "non-blocking"
            lines.append(
                f"    {layout.ok(grammar.GLYPH_OK)} "
                f"{_esc(hook.get('event') or '')} ({blocking}): "
                f"{_esc(hook.get('command') or '')}"
            )

    servers = [s for s in ext.get("mcp_servers") or [] if s.get("status") == "active"]
    if servers:
        lines.append("\n  [b]MCP servers[/b]")
        for server in servers:
            health = server.get("health") or "unknown"
            lines.append(
                f"    {layout.ok(grammar.GLYPH_OK)} "
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
