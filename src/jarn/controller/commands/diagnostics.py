"""Built-in /diagnostics slash-command handlers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from jarn.commands.help import usage_error
from jarn.controller.core import CommandResult
from jarn.extensibility.mcp import load_mcp_tools, run_blocking
from jarn.permissions.labels import PERMISSION_MODE_NAMES as _PERMISSION_LABELS
from jarn.permissions.labels import permission_mode_name
from jarn.tui import grammar, layout
from jarn.tui.i18n import resolve_locale, t
from jarn.tui.layout import Dialect

if TYPE_CHECKING:
    from jarn.agent.prompt_modules import PromptModuleScope
    from jarn.controller.core import Controller

#: Cheap recap scan: last 256 KiB of a transcript is enough for Files / last lines.
_TRANSCRIPT_RECAP_MAX_BYTES = 256 * 1024
_EDIT_TOOL_NAMES = frozenset({"write_file", "edit_file"})
_SKIP_USER_PREFIXES = ("(steered)", "(verification repair)")
_RESPONSE_TOOL = "(response)"
_RECAP_SNIPPET_WIDTH = 72


def _page_locale(ctrl: Controller, locale: str | None = None) -> str:
    if locale in ("en", "th"):
        return locale
    return resolve_locale(ctrl.config)


def _status_auth_label(provider_config: Any, loc: str) -> str:
    from jarn.config.schema import ProviderType

    if provider_config is None:
        return t("status.not_configured", loc)
    if provider_config.type is ProviderType.CODEX_SUBSCRIPTION:
        return t("status.auth.chatgpt", loc)
    if (
        provider_config.type
        in {
            ProviderType.OLLAMA,
            ProviderType.LMSTUDIO,
            ProviderType.OPENAI_COMPATIBLE,
        }
        and not provider_config.api_key
    ):
        return t("status.auth.local", loc)
    return t("status.auth.api_key", loc)


def _status_turn_label(turns: int, loc: str) -> str:
    if turns == 1:
        return t("status.turn_one", loc)
    return t("status.turn_many", loc, n=turns)


def format_status_page(
    ctrl: Controller,
    *,
    dialect: Dialect = "rich",
    locale: str | None = None,
) -> str:
    """``/status`` body. ``dialect='html'`` is Telegram ``parse_mode=HTML``."""
    loc = _page_locale(ctrl, locale)
    model = ctrl.config.resolved_main_model() or t("status.not_configured", loc)
    provider_id = ctrl.current_provider()
    provider = provider_id or t("status.not_configured", loc)
    provider_config = ctrl.config.providers.get(provider_id) if provider_id else None
    reasoning = t("status.provider_default", loc)
    if provider_config is not None:
        raw_effort = provider_config.extra.get("reasoning_effort")
        if raw_effort:
            reasoning = str(raw_effort)
    auth = _status_auth_label(provider_config, loc)
    mode = ctrl.config.permission_mode.value
    mode_label = permission_mode_name(mode, loc)
    trust = t("status.trusted", loc) if ctrl.project_trusted else t("status.untrusted_floor", loc)
    context = ctrl.context_status()
    if context is not None:
        used, window, frac = context
        context_text = layout.context_gauge(frac, used=used, window=window, dialect=dialect)
    else:
        context_text = t("status.not_measured", loc)
    import time as _time

    elapsed = ""
    try:
        elapsed = grammar.format_duration(_time.monotonic() - ctrl.session_started_at)
    except Exception:  # pragma: no cover - missing on very old controllers
        elapsed = "—"
    title = ""
    for session in ctrl.sessions.list():
        if session.thread_id == ctrl.thread_id:
            title = session.title or ""
            break
    recap_meta = _transcript_recap(ctrl.sessions.transcript_path(ctrl.thread_id))
    turns = int(recap_meta.get("turns") or 0)
    session_bits = [str(ctrl.thread_id), elapsed]
    if turns > 0:
        session_bits.append(_status_turn_label(turns, loc))
    if title:
        session_bits.append(title)
    lines = [
        layout.heading(t("status.title", loc), dialect=dialect),
        "",
        layout.kv(t("status.directory", loc), str(ctrl.project_root), dialect=dialect),
        layout.kv(t("status.model", loc), model, dialect=dialect),
        layout.kv(t("status.provider", loc), f"{provider}  ·  {auth}", dialect=dialect),
        layout.kv(t("status.reasoning", loc), reasoning, dialect=dialect),
        layout.kv(t("status.permissions", loc), f"{mode_label}  ·  {mode}", dialect=dialect),
        layout.kv(t("status.workspace", loc), trust, dialect=dialect),
        layout.kv(t("status.context", loc), context_text, dialect=dialect),
        layout.kv(t("status.session", loc), "  ·  ".join(session_bits), dialect=dialect),
    ]
    compact_n = int(getattr(ctrl, "compact_count", 0) or 0)
    if compact_n:
        lines.append(
            layout.kv(
                t("status.compact", loc),
                t("status.compact_value", loc, n=compact_n),
                dialect=dialect,
            )
        )
    recap = _status_recap(ctrl, recap_meta, dialect=dialect, locale=loc)
    if recap:
        lines.append(layout.section(t("status.recap", loc), dialect=dialect))
        lines.extend(recap)
    return "\n".join(lines)


def cmd_status(ctrl: Controller, args: str) -> CommandResult:
    """Return a concise, offline session summary for ``/status``."""
    loc = resolve_locale(ctrl.config)
    if args.strip():
        return CommandResult(usage_error("status", locale=loc))
    return CommandResult(format_status_page(ctrl, locale=loc))


def format_resume_recap(
    ctrl: Controller,
    *,
    dialect: Dialect = "rich",
    locale: str | None = None,
) -> str:
    """Local directory/model/mode + last-turn recap after ``/resume``.

    Reuses :func:`_transcript_recap` / :func:`_status_recap` — no model call.
    """
    loc = _page_locale(ctrl, locale)
    model = ctrl.config.resolved_main_model() or t("status.not_configured", loc)
    mode = ctrl.config.permission_mode.value
    mode_label = permission_mode_name(mode, loc)
    recap_meta = _transcript_recap(ctrl.sessions.transcript_path(ctrl.thread_id))
    title = ""
    for session in ctrl.sessions.list():
        if session.thread_id == ctrl.thread_id:
            title = session.title or ""
            break
    session_bits = [str(ctrl.thread_id)]
    turns = int(recap_meta.get("turns") or 0)
    if turns > 0:
        session_bits.append(_status_turn_label(turns, loc))
    if title:
        session_bits.append(title)
    lines = [
        layout.heading(t("status.resume_title", loc), dialect=dialect),
        "",
        layout.kv(t("status.directory", loc), str(ctrl.project_root), dialect=dialect),
        layout.kv(t("status.model", loc), model, dialect=dialect),
        layout.kv(t("status.permissions", loc), f"{mode_label}  ·  {mode}", dialect=dialect),
        layout.kv(t("status.session", loc), "  ·  ".join(session_bits), dialect=dialect),
    ]
    recap = _status_recap(ctrl, recap_meta, dialect=dialect, locale=loc)
    if recap:
        lines.append(layout.section(t("status.recap", loc), dialect=dialect))
        lines.extend(recap)
    return "\n".join(lines)


def _recap_snippet(text: str, width: int = _RECAP_SNIPPET_WIDTH) -> str:
    """First line, collapsed whitespace, then ``layout.truncate`` (no pre-escape)."""
    first = text.split("\n", 1)[0]
    collapsed = " ".join(first.split())
    if not collapsed:
        return ""
    return layout.truncate(collapsed, width)


def _empty_transcript_recap() -> dict[str, Any]:
    return {"turns": 0, "last_user": "", "last_assistant": "", "files": []}


def _transcript_recap(path: Path) -> dict[str, Any]:
    """Scan a session transcript JSONL for /status Recap fields. No model call.

    Returns ``{turns, last_user, last_assistant, files}``. Missing/unreadable
    files yield zeros/empties. Malformed JSONL lines are skipped.
    """
    recap = _empty_transcript_recap()
    try:
        if not path.is_file():
            return recap
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > _TRANSCRIPT_RECAP_MAX_BYTES:
                fh.seek(size - _TRANSCRIPT_RECAP_MAX_BYTES)
                data = fh.read()
                newline = data.find(b"\n")
                if newline >= 0:
                    data = data[newline + 1 :]
            else:
                data = fh.read()
        text = data.decode("utf-8", errors="replace")
    except OSError:
        return recap

    turns = 0
    last_user = ""
    last_assistant = ""
    file_order: dict[str, None] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        kind = record.get("type")
        if kind == "user":
            body = record.get("text")
            if not isinstance(body, str):
                body = "" if body is None else str(body)
            if body.startswith(_SKIP_USER_PREFIXES):
                continue
            turns += 1
            snippet = _recap_snippet(body)
            if snippet:
                last_user = snippet
        elif kind == "assistant":
            body = record.get("text")
            if not isinstance(body, str):
                body = "" if body is None else str(body)
            snippet = _recap_snippet(body)
            if snippet:
                last_assistant = snippet
        elif kind == "tool":
            name = record.get("name")
            if name not in _EDIT_TOOL_NAMES:
                continue
            args = record.get("args")
            if not isinstance(args, dict):
                continue
            file_path = args.get("file_path") or args.get("path")
            if file_path is None:
                continue
            path_text = str(file_path).strip()
            if not path_text:
                continue
            file_order.pop(path_text, None)
            file_order[path_text] = None
    recap["turns"] = turns
    recap["last_user"] = last_user
    recap["last_assistant"] = last_assistant
    recap["files"] = list(reversed(file_order))[:3]
    return recap


def _status_recap(
    ctrl: Controller,
    recap: dict[str, Any] | None = None,
    *,
    dialect: Dialect = "rich",
    locale: str | None = None,
) -> list[str]:
    loc = _page_locale(ctrl, locale)
    if recap is None:
        recap = _transcript_recap(ctrl.sessions.transcript_path(ctrl.thread_id))
    lines: list[str] = []
    top = [
        (name, usage) for name, usage in ctrl.tracker.top_tools(limit=6) if name != _RESPONSE_TOOL
    ][:5]
    if top:
        tools = " · ".join(f"{name} {usage.calls}" for name, usage in top)
        lines.append(layout.kv(t("status.tools", loc), tools, dialect=dialect))
    total = ctrl.tracker.total
    if total.calls:
        lines.append(
            layout.kv(
                t("status.calls", loc),
                f"{total.calls}  ·  {total.total_tokens:,} tok  ·  ${total.cost_usd:.4f}",
                dialect=dialect,
            )
        )
    files = recap.get("files") or []
    if files:
        lines.append(
            layout.kv(t("status.files", loc), " · ".join(str(p) for p in files), dialect=dialect)
        )
    last_user = recap.get("last_user") or ""
    if last_user:
        lines.append(layout.kv(t("status.last_you", loc), str(last_user), dialect=dialect))
    last_assistant = recap.get("last_assistant") or ""
    if last_assistant:
        lines.append(layout.kv(t("status.last_jarn", loc), str(last_assistant), dialect=dialect))
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
    return CommandResult("\n".join(doctor_lines(diag, locale=resolve_locale(ctrl.config))))


def cmd_cost(ctrl, args: str) -> CommandResult:
    t = ctrl.tracker
    lines = [
        layout.heading("Session usage", t.summary_line()),
        layout.kv("status", t.status().value),
    ]
    if t.total.cache_read_tokens or t.total.cache_creation_tokens:
        lines.append(
            f"{layout.muted('cache')} "
            f"{t.total.cache_read_tokens:,} read · "
            f"{t.total.cache_creation_tokens:,} write"
        )
    for model, usage in t.per_model.items():
        lines.append(
            f"  {layout.escape(model)}: ${usage.cost_usd:.4f} · {usage.total_tokens:,} tok"
        )
    top = t.top_tools()
    if top:
        lines.append(layout.muted("top burners (by tool)"))
        for tool, usage in top:
            lines.append(
                f"  {layout.escape(tool)}: ${usage.cost_usd:.4f} · "
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
            f"  {mark} {layout.accent(status.name)} · {status.scope} · {token_text}{truncated}"
        )
        lines.append(
            f"      {layout.muted(status.activation_reason + ' · source: ' + status.source)}"
        )
    if sub != "active":
        lines.append(
            layout.muted("Lazy skill bodies: /skills, then /module on skill.NAME [turn|session].")
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
    lines = [layout.heading("MCP servers")]
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
            f"  {layout.err(grammar.GLYPH_FAIL)} {layout.escape(name)}: "
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
    lines = [layout.heading("MCP prompts")]
    if res.prompts:
        lines.append(layout.muted("invoke with /<name> — injects the prompt into your turn"))
        for name in sorted(res.prompts):
            cmd = res.prompts[name]
            args = (
                f" {layout.muted('(' + ', '.join(cmd.argument_names) + ')')}"
                if cmd.argument_names
                else ""
            )
            desc = f" — {layout.escape(cmd.description)}" if cmd.description else ""
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
    return CommandResult(f"{note}\n{layout.escape(text)}")


def _mcp_resources(ctrl) -> CommandResult:
    """List resources published by every enabled MCP server."""
    servers = ctrl.config.mcp_servers
    if not servers:
        return CommandResult("No MCP servers configured.")
    from jarn.extensibility.mcp import list_mcp_resources

    res = run_blocking(list_mcp_resources(servers, ctrl.config.permissions.network))
    lines = [layout.heading("MCP resources")]
    if res.resources:
        lines.append(layout.muted("read with /mcp read <server> <uri>"))
        for r in res.resources:
            label = layout.escape(r.name or r.description or "")
            mime = f" {layout.muted(r.mime_type)}" if r.mime_type else ""
            tail = f" — {label}" if label else ""
            lines.append(f"  {layout.accent(r.server)} {layout.escape(r.uri)}{mime}{tail}")
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
    header = layout.heading(server, uri)
    return CommandResult(f"{header}\n{layout.escape(content)}")


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
        layout.heading("Telemetry"),
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
    lines = [layout.heading("Background processes")]
    for p in procs:
        state = "running" if p["running"] else f"exited ({p['exit_code']})"
        lines.append(f"  {layout.item(str(p['id']), p['command'], meta=state)}")
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
        lines.append(f"{marker}{layout.muted(entry.sha[:12])} {layout.escape(entry.label)}")
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
    lines = [layout.heading("Context"), ""]
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
    lines.append(layout.heading("Prompt modules", f"{assembly.token_count:,} tok assembled"))
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
        "\n".join(
            [
                layout.kv("Tool progress", ctrl.tool_progress),
                layout.muted("Session only — persist with /config set ui.tool_progress."),
            ]
        )
    )


def cmd_busy(ctrl: Controller, args: str) -> CommandResult:
    from jarn.config.schema import BUSY_INPUT_MODES

    wanted = args.strip().lower()
    current = getattr(ctrl, "busy_input_mode", None) or getattr(
        ctrl.config.ui, "busy_input_mode", "queue"
    )
    if wanted in ("", "status"):
        return CommandResult(
            "\n".join(
                [
                    layout.kv("Busy input", str(current)),
                    layout.muted("Session only — persist with /config set ui.busy_input_mode."),
                ]
            )
        )
    if wanted not in BUSY_INPUT_MODES:
        return CommandResult(usage_error("busy"))
    if wanted == "steer" and not ctrl.config.ui.steering:
        return CommandResult(
            layout.err("Steering is off.")
            + " "
            + layout.muted("Enable ui.steering or use /busy queue.")
        )
    ctrl.busy_input_mode = wanted
    return CommandResult(
        "\n".join(
            [
                layout.kv("Busy input", ctrl.busy_input_mode),
                layout.muted("Session only — persist with /config set ui.busy_input_mode."),
            ]
        )
    )


def cmd_focus(ctrl: Controller, args: str) -> CommandResult:
    wanted = args.strip().lower()
    if wanted in ("status",):
        state = "on" if ctrl.focus_mode else "off"
        return CommandResult(layout.kv("Focus", state))
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
        return CommandResult(f"Focus {layout.accent('off')} — tool progress {ctrl.tool_progress}.")
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
    lines = [layout.heading("Tools")]
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
