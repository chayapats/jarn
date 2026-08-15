"""Built-in /diagnostics slash-command handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from rich.markup import escape as _escape_markup

from jarn.commands.help import usage_error
from jarn.controller.core import CommandResult
from jarn.extensibility.mcp import load_mcp_tools, run_blocking
from jarn.permissions.labels import PERMISSION_MODE_NAMES as _PERMISSION_LABELS
from jarn.tui import grammar, layout

if TYPE_CHECKING:
    from jarn.agent.prompt_modules import PromptModuleScope
    from jarn.controller.core import Controller


def cmd_status(ctrl: Controller, args: str) -> CommandResult:
    """Return a concise, offline session summary for ``/status``."""
    if args.strip():
        return CommandResult(usage_error("status"))
    model = ctrl.config.resolved_main_model() or "not configured"
    provider = ctrl.current_provider() or "not configured"
    provider_config = ctrl.config.providers.get(provider)
    reasoning = "provider default"
    auth = "not configured"
    if provider_config is not None:
        from jarn.config.schema import ProviderType

        raw_effort = provider_config.extra.get("reasoning_effort")
        if raw_effort:
            reasoning = str(raw_effort)
        if provider_config.type is ProviderType.CODEX_SUBSCRIPTION:
            auth = "ChatGPT subscription (Codex-managed; /login to verify)"
        elif provider_config.type in {
            ProviderType.OLLAMA,
            ProviderType.LMSTUDIO,
            ProviderType.OPENAI_COMPATIBLE,
        } and not provider_config.api_key:
            auth = "local endpoint (no cloud key)"
        else:
            auth = "API-key reference"
    mode = ctrl.config.permission_mode.value
    mode_label = _PERMISSION_LABELS.get(mode, mode)
    trust = "trusted" if ctrl.project_trusted else "untrusted (read-only floor)"
    context = ctrl.context_status()
    if context is not None:
        used, window, frac = context
        context_text = layout.context_gauge(frac, used=used, window=window)
    else:
        context_text = "not measured"
    import time as _time

    elapsed = layout.escape("")
    try:
        elapsed = grammar.format_duration(_time.monotonic() - ctrl.session_started_at)
    except Exception:  # pragma: no cover - missing on very old controllers
        elapsed = "—"
    title = ""
    for session in ctrl.sessions.list():
        if session.thread_id == ctrl.thread_id:
            title = session.title or ""
            break
    lines = [
        layout.title("Status"),
        "",
        layout.kv("Directory", str(ctrl.project_root)),
        layout.kv("Model", model),
        layout.kv("Provider", f"{provider}  ·  {auth}"),
        layout.kv("Reasoning", reasoning),
        layout.kv("Permissions", f"{mode_label}  ·  {mode}"),
        layout.kv("Workspace", trust),
        layout.kv("Context", context_text),
        layout.kv(
            "Session",
            f"{ctrl.thread_id}  ·  {elapsed}" + (f"  ·  {title}" if title else ""),
        ),
    ]
    recap = _status_recap(ctrl)
    if recap:
        lines.append("")
        lines.append(layout.title("Recap"))
        lines.extend(recap)
    return CommandResult("\n".join(lines))


def _status_recap(ctrl: Controller) -> list[str]:
    lines: list[str] = []
    top = ctrl.tracker.top_tools()
    if top:
        tools = " · ".join(
            f"{name} {usage.calls}" for name, usage in top[:5]
        )
        lines.append(layout.kv("Tools", tools))
    t = ctrl.tracker.total
    if t.calls:
        lines.append(
            layout.kv("Calls", f"{t.calls}  ·  {t.total_tokens:,} tok  ·  ${t.cost_usd:.4f}")
        )
    return lines


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
        prompt_modules=ctrl.prompt_module_diagnostics(),
    )
    return CommandResult("\n".join(doctor_lines(diag)))


def cmd_cost(ctrl, args: str) -> CommandResult:
    t = ctrl.tracker
    lines = [
        layout.heading("Session usage", t.summary_line()),
        f"status: {t.status().value}",
    ]
    if t.total.cache_read_tokens or t.total.cache_creation_tokens:
        lines.append(
            f"{layout.muted('cache')} "
            f"{t.total.cache_read_tokens:,} read · "
            f"{t.total.cache_creation_tokens:,} write"
        )
    for model, usage in t.per_model.items():
        lines.append(
            f"  {_escape_markup(model)}: ${usage.cost_usd:.4f} · {usage.total_tokens:,} tok"
        )
    top = t.top_tools()
    if top:
        lines.append(layout.muted("top burners (by tool)"))
        for tool, usage in top:
            lines.append(
                f"  {_escape_markup(tool)}: ${usage.cost_usd:.4f} · "
                f"{usage.total_tokens:,} tok · {usage.calls} calls"
            )
    lines.extend(_context_injection_lines(ctrl))
    return CommandResult("\n".join(lines))


def cmd_modules(ctrl: Controller, args: str) -> CommandResult:
    """Show registry state backed by the same assembly metadata the model sees."""
    sub = args.strip().lower()
    if sub not in ("", "active"):
        return CommandResult(usage_error("modules"))
    statuses, assembly = ctrl.prompt_module_statuses()
    lines = [layout.heading("Prompt modules", f"assembled prompt {assembly.token_count:,} tok")]
    for status in statuses:
        if sub == "active" and not status.active:
            continue
        # Keep inactive lazy skill bodies out of the general registry listing;
        # /skills is their discoverable, unbounded catalog. Any active body is
        # still shown here, satisfying actual-content observability.
        if status.kind == "skill" and not status.active:
            continue
        mark = layout.key_mark(status.active)
        if status.configured_budget is None:
            token_text = f"{status.token_count:,} tok"
        else:
            token_text = f"{status.token_count:,}/{status.configured_budget:,} tok"
        truncated = " · truncated" if status.truncated else ""
        lines.append(
            f"  {mark} {layout.accent(status.name)} · "
            f"{status.scope} · {token_text}{truncated}"
        )
        lines.append(
            f"      {layout.muted(status.activation_reason + ' · source: ' + status.source)}"
        )
    if sub != "active":
        lines.append(
            layout.muted(
                "Lazy skill bodies: /skills, then /module on skill.NAME [turn|session]."
            )
        )
    return CommandResult("\n".join(lines))


def cmd_module(ctrl: Controller, args: str) -> CommandResult:
    """Activate/deactivate user-loadable module bodies."""
    parts = args.split()
    if not parts:
        return CommandResult(usage_error("module"))
    action = parts[0].lower()
    if action == "on" and len(parts) in (2, 3):
        scope = parts[2].lower() if len(parts) == 3 else "turn"
        if scope not in ("turn", "session"):
            return CommandResult("Module scope must be 'turn' or 'session'.")
        return ctrl.activate_prompt_module(parts[1], cast("PromptModuleScope", scope))
    if action == "off" and len(parts) == 2:
        return ctrl.deactivate_prompt_module(parts[1])
    return CommandResult(usage_error("module"))


def cmd_permissions(ctrl, args: str) -> CommandResult:
    r = ctrl.config.permissions
    session_allow = ctrl.engine._all_allow()[len(r.allow) :]
    mode = ctrl.config.permission_mode.value
    label = _PERMISSION_LABELS.get(mode, mode)
    lines = [
        layout.kv("Mode", f"{label}  ·  ({mode})"),
        layout.kv("Allow", ", ".join(r.allow) or "(none)"),
        layout.kv("Deny", ", ".join(r.deny) or "(none)"),
        layout.kv("Session-allow", ", ".join(session_allow) or "(none)"),
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
        # Split off only ``prompt <server> <name>`` and hand the RAW remainder
        # to the parser: str.split()+" ".join() would collapse the quoting/
        # whitespace of ``key="multi word"`` values before they reach shlex.
        return _mcp_prompt(ctrl, args.strip().split(maxsplit=3)[1:])
    if sub in ("resources", "resource"):
        return _mcp_resources(ctrl)
    if sub == "read":
        return _mcp_read(ctrl, rest)
    if sub not in ("", "status", "refresh") and "--refresh" not in parts:
        return CommandResult(usage_error("mcp"))
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
        # This synchronous command may run its probe on a one-shot worker loop,
        # which cannot own sessions used later by the controller's main loop.
        # Probe ephemerally, then force the next runtime build to establish fresh
        # persistent sessions on the correct loop.
        mcp = run_blocking(load_mcp_tools(ctrl.config.mcp_servers, net, persistent=False))
        ctrl.mcp_health = dict(mcp.health)
        ctrl.mcp_errors = dict(mcp.errors)
        ctrl._invalidate_runtime(drop_mcp_cache=True)
        for server in ctrl.config.mcp_servers:
            if server.name in ctrl.mcp_health:
                server.health = ctrl.mcp_health[server.name]
    servers = ctrl.config.mcp_servers
    if not servers:
        return CommandResult("No MCP servers configured.")
    mark = {
        "ok": layout.key_mark(True),
        "error": layout.err(grammar.GLYPH_FAIL),
    }
    lines = [layout.title("MCP servers")]
    for server in servers:
        health = ctrl.mcp_health.get(server.name, server.health or "unknown")
        glyph = mark.get(health, layout.key_mark(False))
        transport = getattr(server, "transport", "") or ""
        detail = f" {layout.muted(f'({transport})')}" if transport else ""
        line = f"  {glyph} {layout.accent(server.name)}{detail} — {health}"
        err = ctrl.mcp_errors.get(server.name)
        if err:
            line += f"\n      {layout.muted('last error: ' + err)}"
        lines.append(line)
    if not ctrl.runtime:
        lines.append(layout.muted("Health is populated after the first turn loads the servers."))
    return CommandResult("\n".join(lines))


def _append_mcp_errors(lines: list[str], errors: dict) -> None:
    """Append one dimmed line per per-server discovery error (isolation aware)."""
    for name in sorted(errors):
        lines.append(
            f"  {layout.err(grammar.GLYPH_FAIL)} {_escape_markup(name)}: "
            f"{layout.muted(errors[name])}"
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

    res = run_blocking(load_mcp_prompts(servers, ctrl.config.permissions.network))
    _register_prompt_commands(ctrl, res.prompts)
    lines = [layout.title("MCP prompts")]
    if res.prompts:
        lines.append(layout.muted("invoke with /<name> — injects the prompt into your turn"))
        for name in sorted(res.prompts):
            cmd = res.prompts[name]
            args = (
                f" {layout.muted('(' + ', '.join(cmd.argument_names) + ')')}"
                if cmd.argument_names
                else ""
            )
            desc = f" — {_escape_markup(cmd.description)}" if cmd.description else ""
            lines.append(f"  {layout.accent('/' + name)}{args}{desc}")
    else:
        lines.append(layout.muted("No prompts available."))
    _append_mcp_errors(lines, res.errors)
    if res.prompts and not ctrl.runtime:
        lines.append(
            layout.muted(
                "Prompts become invokable after the first turn "
                "builds the runtime — re-run /mcp prompts then."
            )
        )
    return CommandResult("\n".join(lines))


def _mcp_prompt(ctrl, rest: list[str]) -> CommandResult:
    """Fetch a single prompt's text (and register it for direct invocation)."""
    if len(rest) < 2:
        return CommandResult(usage_error("mcp"))
    server, pname = rest[0], rest[1]
    arg_str = " ".join(rest[2:])
    if not any(s.name == server and s.enabled for s in ctrl.config.mcp_servers):
        return CommandResult(f"No enabled MCP server named {server!r}.")
    from jarn.config.secrets import redact_secrets
    from jarn.extensibility.mcp import load_mcp_prompts

    res = run_blocking(load_mcp_prompts(ctrl.config.mcp_servers, ctrl.config.permissions.network))
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
    note = layout.muted(f"{key} — invoke /{key} to inject this into a turn")
    return CommandResult(f"{note}\n{_escape_markup(text)}")


def _mcp_resources(ctrl) -> CommandResult:
    """List resources published by every enabled MCP server."""
    servers = ctrl.config.mcp_servers
    if not servers:
        return CommandResult("No MCP servers configured.")
    from jarn.extensibility.mcp import list_mcp_resources

    res = run_blocking(list_mcp_resources(servers, ctrl.config.permissions.network))
    lines = [layout.title("MCP resources")]
    if res.resources:
        lines.append(layout.muted("read with /mcp read <server> <uri>"))
        for r in res.resources:
            label = _escape_markup(r.name or r.description or "")
            mime = f" {layout.muted(r.mime_type)}" if r.mime_type else ""
            tail = f" — {label}" if label else ""
            lines.append(
                f"  {layout.accent(r.server)} {_escape_markup(r.uri)}{mime}{tail}"
            )
    else:
        lines.append(layout.muted("No resources available."))
    _append_mcp_errors(lines, res.errors)
    return CommandResult("\n".join(lines))


def _mcp_read(ctrl, rest: list[str]) -> CommandResult:
    """Read one resource's content into view."""
    if len(rest) < 2:
        return CommandResult(usage_error("mcp"))
    server, uri = rest[0], rest[1]
    from jarn.config.secrets import redact_secrets
    from jarn.extensibility.mcp import read_mcp_resource

    try:
        content = run_blocking(
            read_mcp_resource(ctrl.config.mcp_servers, server, uri, ctrl.config.permissions.network)
        )
    except Exception as exc:  # noqa: BLE001 - surface a clean, redacted message
        return CommandResult(redact_secrets(f"Failed to read {uri} from {server}: {exc}"))
    if not content.strip():
        return CommandResult(f"Resource {uri} on {server} returned no content.")
    header = f"{layout.title(server)} {layout.muted(uri)}"
    return CommandResult(f"{header}\n{_escape_markup(content)}")


def cmd_telemetry(ctrl, args: str) -> CommandResult:
    """Show telemetry opt-in status and local sink stats."""
    sub = args.strip().lower()
    if sub and sub != "status":
        return CommandResult(usage_error("telemetry"))
    summary = ctrl.telemetry.status_summary()
    enabled = "enabled" if summary["enabled"] else "disabled"
    install = "present" if summary["install_id_present"] else "absent"
    size_kb = summary["size_bytes"] / 1024
    lines = [
        layout.title("Telemetry"),
        layout.kv("status", enabled),
        layout.kv("file", summary["path"] or "(none)"),
        layout.kv("size", f"{size_kb:.1f} KB ({summary['size_bytes']:,} bytes)"),
        layout.kv("events on disk", f"{summary['event_count']:,}"),
        layout.kv("install id", install),
    ]
    if not summary["enabled"]:
        lines.append(layout.muted("Opt in with observability.telemetry: true in config."))
    return CommandResult("\n".join(lines))


def cmd_ps(ctrl, args: str) -> CommandResult:
    """List background processes, or ``/ps kill <id>`` to stop one."""
    from jarn.agent.background import manager

    mgr = manager()
    parts = args.split()
    if parts and parts[0] == "kill":
        if len(parts) < 2:
            return CommandResult(usage_error("ps"))
        ok = mgr.kill(parts[1])
        return CommandResult(
            f"Killed {parts[1]}." if ok else f"No background process {parts[1]!r}."
        )
    procs = mgr.list()
    if not procs:
        return CommandResult("No background processes.")
    lines = [layout.title("Background processes")]
    for p in procs:
        state = "running" if p["running"] else f"exited ({p['exit_code']})"
        lines.append(
            f"  {layout.accent(str(p['id']))} {layout.muted(state)} "
            f"{_escape_markup(p['command'])}"
        )
    lines.append(layout.muted("/ps kill <id> to stop one"))
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
    lines = [layout.heading("Checkpoints", "most recent first")]
    for i, entry in enumerate(entries):
        marker = "→ " if i == 0 else "  "
        lines.append(f"{marker}{layout.muted(entry.sha[:12])} {_escape_markup(entry.label)}")
    return CommandResult("\n".join(lines))


def _context_injection_lines(ctrl) -> list[str]:
    """Exact active-module token sizes from the assembly source of truth."""
    statuses, assembly = ctrl.prompt_module_statuses()
    lines = [
        "",
        layout.heading("Prompt modules", f"{assembly.token_count:,} tok assembled"),
    ]
    for status in statuses:
        if not status.active:
            continue
        budget = f"/{status.configured_budget:,}" if status.configured_budget is not None else ""
        truncated = " truncated" if status.truncated else ""
        lines.append(
            f"  {status.name}: {status.token_count:,}{budget} tok · {status.scope}{truncated}"
        )
    return lines


def cmd_context(ctrl: Controller, args: str) -> CommandResult:
    show_all = args.strip().lower() in ("all", "--all")
    if args.strip() and not show_all:
        return CommandResult(usage_error("context"))
    ctx = ctrl.context_status()
    lines = [layout.title("Context"), ""]
    if ctx is not None:
        used, window, frac = ctx
        lines.append(f"  {layout.context_gauge(frac, used=used, window=window)}")
        lines.append(
            layout.muted(
                f"  {used:,} / {window:,} tokens · compact at {ctrl.config.context.compact_at_pct}%"
            )
        )
    else:
        lines.append(layout.muted("  Context window not measured yet."))
    statuses, assembly = ctrl.prompt_module_statuses()
    lines.append("")
    lines.append(
        layout.title("Prompt modules")
        + f"  {layout.muted(f'{assembly.token_count:,} tok assembled')}"
    )
    for status in statuses:
        if not show_all and not status.active:
            continue
        budget = f"/{status.configured_budget:,}" if status.configured_budget is not None else ""
        mark = grammar.GLYPH_KEY_OK if status.active else grammar.GLYPH_KEY_OFF
        truncated = " truncated" if status.truncated else ""
        lines.append(
            f"  {mark} {layout.accent(status.name)}  "
            f"{status.token_count:,}{budget} tok · {status.scope}{truncated}"
        )
    return CommandResult("\n".join(lines))


def cmd_verbose(ctrl: Controller, args: str) -> CommandResult:
    from jarn.tui.grammar import TOOL_PROGRESS_VALUES, next_tool_progress

    wanted = args.strip().lower()
    if wanted:
        if wanted not in TOOL_PROGRESS_VALUES:
            return CommandResult(usage_error("verbose"))
        ctrl.tool_progress = wanted
    else:
        ctrl.tool_progress = next_tool_progress(ctrl.tool_progress)
    ctrl.focus_mode = False
    ctrl._focus_saved_progress = None
    return CommandResult(
        f"Tool progress: {layout.accent(ctrl.tool_progress)}. "
        f"{layout.muted('Session only — persist with /config set ui.tool_progress.')}"
    )


def cmd_focus(ctrl: Controller, args: str) -> CommandResult:
    wanted = args.strip().lower()
    if wanted in ("status",):
        state = "on" if ctrl.focus_mode else "off"
        return CommandResult(f"Focus is {layout.accent(state)}.")
    turn_on = wanted in ("on",) or (wanted == "" and not ctrl.focus_mode)
    turn_off = wanted in ("off",) or (wanted == "" and ctrl.focus_mode)
    if turn_on:
        if not ctrl.focus_mode:
            ctrl._focus_saved_progress = ctrl.tool_progress
            ctrl.tool_progress = "off"
            ctrl.focus_mode = True
        return CommandResult(
            f"Focus {layout.accent('on')} — tool lines hidden. "
            f"{layout.muted('/focus off to restore · /expand for full output.')}"
        )
    if turn_off:
        if ctrl.focus_mode:
            ctrl.tool_progress = ctrl._focus_saved_progress or "new"
            ctrl._focus_saved_progress = None
            ctrl.focus_mode = False
        return CommandResult(
            f"Focus {layout.accent('off')} — tool progress {ctrl.tool_progress}."
        )
    return CommandResult(usage_error("focus"))


def cmd_tools(ctrl: Controller, args: str) -> CommandResult:
    if args.strip():
        return CommandResult(usage_error("tools"))
    from jarn.agent.permissions_bridge import (
        ASYNC_SUBAGENT_TOOLS,
        BACKGROUND_CONTROL_TOOLS,
        BACKGROUND_START_TOOL,
        INTERNAL_TOOLS,
        MUTATING_TOOLS,
        READONLY_TOOLS,
        WIKI_MUTATING_TOOLS,
        WIKI_READONLY_TOOLS,
    )

    groups = [
        ("Read", READONLY_TOOLS),
        ("Edit / shell", MUTATING_TOOLS),
        ("Internal", INTERNAL_TOOLS),
    ]
    if ctrl.config.policy.web_tools:
        groups.append(("Web", ("web_search", "web_fetch")))
    if ctrl.config.wiki.enabled:
        groups.append(("Wiki", WIKI_READONLY_TOOLS + WIKI_MUTATING_TOOLS))
    if ctrl.config.execution.background:
        groups.append(("Background", (BACKGROUND_START_TOOL, *BACKGROUND_CONTROL_TOOLS)))
    if ctrl.config.async_subagents:
        groups.append(("Async subagents", ASYNC_SUBAGENT_TOOLS))
    lines = [layout.title("Tools")]
    for label, names in groups:
        lines.append(layout.section(label))
        for name in names:
            lines.append(f"  {layout.accent(name)}")
    return CommandResult("\n".join(lines))


def cmd_title(ctrl: Controller, args: str) -> CommandResult:
    text = args.strip()
    current = ""
    for session in ctrl.sessions.list():
        if session.thread_id == ctrl.thread_id:
            current = session.title or ""
            break
    if not text:
        shown = current or "(untitled)"
        return CommandResult(layout.kv("Title", shown))
    import time as _time

    ctrl.record_session_title(text, when=_time.time())
    return CommandResult(f"Session titled {layout.accent(text)}.")
