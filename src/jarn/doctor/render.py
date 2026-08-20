"""Shared Rich and JSON rendering for ``jarn doctor`` and ``/doctor``."""

from __future__ import annotations

import json
from types import SimpleNamespace

from rich.console import Console

from jarn.tui import grammar, layout
from jarn.tui.i18n import resolve_locale, t


def _esc(value: object) -> str:
    """Escape a value for Rich markup; ``None`` becomes empty."""
    return layout.escape(_plain(value))


def _plain(value: object) -> str:
    """Redact secrets without Rich escaping (for layout helpers that escape)."""
    if value is None:
        return ""
    from jarn.config.secrets import redact_secrets

    return redact_secrets(str(value))


def _peek_global_ui_locale() -> object | None:
    """Read ``ui.locale`` from global YAML without loading or migrating config."""
    try:
        import yaml

        from jarn.config import paths

        path = paths.global_config_path()
        if not path.is_file():
            return None
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — locale peek must not fail doctor
        return None
    if not isinstance(raw, dict):
        return None
    ui = raw.get("ui")
    if not isinstance(ui, dict):
        return None
    setting = ui.get("locale", "auto")
    if setting not in ("auto", "en", "th"):
        return None
    return SimpleNamespace(ui=SimpleNamespace(locale=setting))


def resolve_doctor_locale(
    locale: str | None = None,
    config: object | None = None,
) -> str:
    """Resolve chrome locale for doctor human output."""
    if locale in ("en", "th"):
        return locale
    if config is not None:
        return resolve_locale(config)
    peeked = _peek_global_ui_locale()
    if peeked is not None:
        return resolve_locale(peeked)
    return resolve_locale()


def _tt(key: str, loc: str, **kwargs: object) -> str:
    return t(key, loc, **kwargs)


def _row(label: str, value: str, extra: str = "") -> str:
    line = layout.kv(label, value)
    return f"{line} {extra}" if extra else line


def doctor_to_json(diag: dict) -> str:
    """Serialize doctor diagnostics to a JSON string."""
    from jarn.config.secrets import redact_structure

    return json.dumps(redact_structure(diag))


def doctor_lines(
    diag: dict,
    *,
    locale: str | None = None,
    config: object | None = None,
) -> list[str]:
    """Return Rich-markup lines for doctor diagnostics."""
    loc = resolve_doctor_locale(locale, config)
    lines: list[str] = [layout.heading(_tt("doctor.title", loc))]

    append_inventory_lines(lines, diag, locale=loc)

    gpath = diag.get("global_config", "")
    present = diag.get("global_config_present", False)
    cfg_status = (
        layout.ok(_tt("doctor.status.ok", loc))
        if present
        else layout.err(_tt("doctor.status.missing", loc))
    )
    lines.append(_row(_tt("doctor.label.config", loc), _plain(gpath), cfg_status))
    if diag.get("jarn_home_warning"):
        lines.append(layout.warn(diag["jarn_home_warning"]))
    if diag.get("jarn_home_mode_warning"):
        lines.append(layout.warn(diag["jarn_home_mode_warning"]))
    root = diag.get("project_root")
    project_value = _plain(root) if root else _tt("doctor.status.none", loc)
    lines.append(_row(_tt("doctor.label.project", loc), project_value))
    added_roots = [r for r in diag.get("roots", []) if r != (str(root) if root else None)]
    if added_roots:
        lines.append(
            _row(
                _tt("doctor.label.extra_roots", loc),
                ", ".join(_plain(r) for r in added_roots),
                layout.muted(_tt("doctor.extra_roots.hint", loc)),
            )
        )
    if diag.get("project_trusted") is False and diag.get("project_stripped_keys"):
        keys = ", ".join(str(k) for k in diag["project_stripped_keys"])
        lines.append(
            f"{layout.warn(_tt('doctor.untrusted', loc, keys=keys))}"
            f" {layout.muted(_tt('doctor.untrusted.hint', loc))}"
        )

    if not present:
        lines.append("")
        lines.append(layout.banner_warn(_tt("doctor.no_config", loc)))
        append_error_lines(lines, diag.get("errors") or [], locale=loc)
        return lines

    lines.append(_row(_tt("doctor.label.profile", loc), _plain(diag.get("default_profile", ""))))
    model = _plain(diag.get("main_model", ""))
    if diag.get("main_model_builds"):
        model_extra = layout.ok(_tt("doctor.status.ok", loc))
        lines.append(_row(_tt("doctor.label.model", loc), model, model_extra))
    else:
        err_txt = _plain(diag.get("main_model_error") or "")
        model_extra = layout.err(_tt("doctor.status.fail", loc))
        combined = f"{model} {err_txt}".strip()
        lines.append(_row(_tt("doctor.label.model", loc), combined, model_extra))

    _mode = diag.get("permission_mode", "")
    _eff_mode = diag.get("effective_mode", _mode)
    if _eff_mode == _mode:
        mode_str = _plain(_mode)
    else:
        mode_str = _tt(
            "doctor.mode.clamped",
            loc,
            mode=_plain(_mode),
            effective=_plain(_eff_mode),
        )
    lines.append(_row(_tt("doctor.label.mode", loc), mode_str))
    web_tools_str = (
        _tt("doctor.status.on", loc)
        if diag.get("web_tools", True)
        else _tt("doctor.status.off", loc)
    )
    lines.append(_row(_tt("doctor.label.web", loc), web_tools_str))

    sbx = diag.get("sandbox") or {}
    sbx_backend = sbx.get("backend") or "none"
    sbx_avail = sbx.get("available", False)
    sbx_mode = sbx.get("mode", "off")
    if sbx_avail:
        sbx_extra = layout.ok(f"{sbx_backend} {_tt('doctor.status.available', loc)}")
    else:
        sbx_extra = layout.muted(_tt("doctor.status.unavailable", loc))
    lines.append(_row(_tt("doctor.label.sandbox", loc), str(sbx_mode), sbx_extra))

    ex = diag.get("execution") or {}
    ex_backend = ex.get("backend", "local")
    if ex_backend == "docker" or ex.get("docker_image"):
        docker_ok = ex.get("docker_available", False)
        docker_extra = (
            layout.ok(_tt("doctor.status.available", loc))
            if docker_ok
            else layout.muted(_tt("doctor.status.unavailable", loc))
        )
        image = _plain(ex.get("docker_image") or "")
        lines.append(
            _row(
                _tt("doctor.label.execution", loc), f"{_plain(ex_backend)} · {image}", docker_extra
            )
        )
    else:
        lines.append(_row(_tt("doctor.label.execution", loc), _plain(ex_backend)))

    git_diag = diag.get("git") or {}
    autockpt = (
        _tt("doctor.status.on", loc)
        if git_diag.get("autocheckpoint")
        else _tt("doctor.status.off", loc)
    )
    lines.append(_row(_tt("doctor.label.git", loc), autockpt))

    wiki_diag = diag.get("wiki") or {}
    wiki_enabled = (
        _tt("doctor.status.on", loc) if wiki_diag.get("enabled") else _tt("doctor.status.off", loc)
    )
    lines.append(_row(_tt("doctor.label.wiki", loc), wiki_enabled))

    obs_diag = diag.get("observability") or {}
    transcript = (
        _tt("doctor.status.on", loc)
        if obs_diag.get("transcript", True)
        else _tt("doctor.status.off", loc)
    )
    lines.append(_row(_tt("doctor.label.transcript", loc), transcript))

    ctx_diag = diag.get("context") or {}
    repo_map_mode = ctx_diag.get("repo_map", "tool")
    repo_map_tokens = ctx_diag.get("repo_map_tokens", 1024)
    lines.append(
        _row(
            _tt("doctor.label.repo_map", loc),
            f"{repo_map_mode} · {repo_map_tokens}",
        )
    )

    module_diag = diag.get("prompt_modules") or {}
    module_rows = module_diag.get("modules") or []
    active_modules = [row for row in module_rows if row.get("active")]
    prompt_tokens = module_diag.get("prompt_tokens")
    if prompt_tokens is not None:
        lines.append(
            _row(
                _tt("doctor.label.modules", loc),
                _tt(
                    "doctor.modules.summary",
                    loc,
                    n=len(active_modules),
                    tokens=f"{prompt_tokens:,}",
                ),
            )
        )
    if module_diag.get("error"):
        lines.append(
            layout.warn(f"{_tt('doctor.modules.unavailable', loc)}: {_plain(module_diag['error'])}")
        )

    lines.append(layout.section(_tt("doctor.section.providers", loc)))
    for entry in diag.get("providers") or []:
        name = _plain(entry.get("name", ""))
        kind = _plain(entry.get("type", ""))
        if entry.get("key_ok"):
            key_state = layout.ok(_tt("doctor.status.key_ok", loc))
        else:
            key_state = layout.warn(_plain(entry.get("key_state", "")))
        lines.append(f"  {layout.item(name, meta=kind)}  {key_state}")

    append_extension_lines(lines, diag.get("extensions") or {}, locale=loc)

    append_error_lines(lines, diag.get("errors") or [], locale=loc)

    for warning in diag.get("warnings") or []:
        lines.append(layout.warn(f"{grammar.GLYPH_WARN} {_plain(warning)}"))

    ok = diag.get("ok", False)
    issue_n = len(diag.get("errors") or []) + len(diag.get("warnings") or [])
    if ok:
        cta = layout.banner_ok(_tt("doctor.cta.ok", loc))
    else:
        n = issue_n or 1
        cta = layout.banner_warn(_tt("doctor.cta.issues", loc, n=n))
    lines.append(f"\n{cta}")
    return lines


def append_inventory_lines(
    lines: list[str],
    diag: dict,
    *,
    locale: str | None = None,
    config: object | None = None,
) -> None:
    """Append concise human output for the machine-readable host inventory."""
    loc = resolve_doctor_locale(locale, config)
    jarn = diag.get("jarn") or {}
    if jarn:
        install = jarn.get("install") or {}
        active = jarn.get("active_executable")
        version = _plain(jarn.get("version") or _tt("doctor.status.unknown", loc))
        method = _plain(install.get("method") or _tt("doctor.status.unknown", loc))
        lines.append(_row(_tt("doctor.label.version", loc), f"{version} · {method}"))
        if active:
            lines.append(
                _row(
                    _tt("doctor.label.executable", loc),
                    _plain(active),
                    layout.ok(_tt("doctor.status.found", loc)),
                )
            )
        else:
            lines.append(
                _row(
                    _tt("doctor.label.executable", loc),
                    "",
                    layout.err(_tt("doctor.status.missing", loc)),
                )
            )
        for shadow in jarn.get("shadowed") or []:
            lines.append(f"  {layout.warn(_tt('doctor.shadowing', loc))} {_esc(shadow)}")
        for candidate in jarn.get("candidate_inventory") or []:
            if (
                not isinstance(candidate, dict)
                or candidate.get("active")
                or candidate.get("on_path")
            ):
                continue
            sources = ", ".join(str(value) for value in candidate.get("sources") or [])
            owner = sources or _tt("doctor.status.unknown", loc)
            lines.append(
                f"  {layout.warn(_tt('doctor.off_path', loc))} "
                f"{_esc(candidate.get('path'))} · {_esc(owner)}"
            )
        if install.get("canonical_record_error"):
            lines.append(
                f"  {layout.err(_tt('doctor.install_invalid', loc))} "
                f"{_esc(install['canonical_record_error'])}"
            )
        if install.get("active_matches_record") is False:
            lines.append(
                f"  {layout.err(_tt('doctor.activation_mismatch', loc))} "
                f"{_plain(_tt('doctor.activation_mismatch.detail', loc))}"
            )

    host = diag.get("platform") or {}
    if host:
        libc = host.get("libc") or {}
        python = host.get("python") or {}
        unknown = _tt("doctor.status.unknown", loc)
        lines.append(
            _row(
                _tt("doctor.label.host", loc),
                f"{_plain(host.get('system'))} {_plain(host.get('release'))} · "
                f"{_plain(host.get('architecture'))} · "
                f"{_plain(libc.get('name'))} {_plain(libc.get('version') or unknown)} · "
                f"Python {_plain(python.get('version') or unknown)}",
            )
        )

    shell = diag.get("shell") or {}
    if shell:
        profile_state = (
            _tt("doctor.status.present", loc)
            if shell.get("profile_present")
            else _tt("doctor.status.missing", loc)
        )
        lines.append(
            _row(
                _tt("doctor.label.shell", loc),
                f"{_plain(shell.get('name') or _tt('doctor.status.unknown', loc))} · "
                f"{profile_state} {_plain(shell.get('profile') or '')}".rstrip(),
            )
        )

    installation = diag.get("installation") or {}
    if installation:
        writable = installation.get("writable")
        write_state = (
            _tt("doctor.status.writable", loc)
            if writable
            else _tt("doctor.status.not_writable", loc)
        )
        free = installation.get("free_bytes")
        free_text = (
            _tt("doctor.free_bytes", loc, n=f"{int(free) // (1024 * 1024 * 1024):,}")
            if free is not None
            else _tt("doctor.free_unknown", loc)
        )
        mode = _plain(installation.get("directory_mode") or _tt("doctor.status.unknown", loc))
        lines.append(
            _row(_tt("doctor.label.install_dir", loc), f"{write_state} · {mode} · {free_text}")
        )

    dependencies = diag.get("dependencies") or {}
    if dependencies:
        for name in ("uv", "codex"):
            item = dependencies.get(name) or {}
            state = (
                layout.ok(_tt("doctor.status.ok", loc))
                if item.get("ok")
                else layout.err(_tt("doctor.status.unavailable", loc))
            )
            version = _plain(item.get("version") or _tt("doctor.status.version_unknown", loc))
            path = _plain(item.get("path") or _tt("doctor.status.not_on_path", loc))
            lines.append(_row(name, f"{version} · {path}", state))
        protocol = (dependencies.get("codex") or {}).get("protocol") or {}
        compatible = protocol.get("compatible")
        proto_state = (
            _tt("doctor.status.compatible", loc)
            if compatible
            else _tt("doctor.status.incompatible", loc)
        )
        lines.append(_row(_tt("doctor.label.protocol", loc), proto_state))

    configuration = diag.get("configuration") or {}
    if configuration:
        for tier, key in (
            ("global", "doctor.label.global_schema"),
            ("project", "doctor.label.project_schema"),
        ):
            item = configuration.get(tier)
            if item:
                lines.append(
                    _row(
                        _tt(key, loc),
                        f"{_plain(item.get('status') or _tt('doctor.status.unknown', loc))} "
                        f"({_plain(item.get('source_version'))} → "
                        f"{_plain(item.get('target_version'))})",
                    )
                )

    store = diag.get("secrets") or {}
    if store:
        issue_count = len(store.get("permission_issues") or [])
        state = (
            _tt("doctor.status.secure", loc)
            if issue_count == 0
            else _tt("doctor.secrets.issues", loc, n=issue_count)
        )
        lines.append(_row(_tt("doctor.label.secrets", loc), state))

    catalog = diag.get("catalog") or {}
    if catalog:
        unknown = _tt("doctor.status.unknown", loc)
        lines.append(
            _row(
                _tt("doctor.label.catalog", loc),
                _tt(
                    "doctor.catalog.summary",
                    loc,
                    source=_plain(catalog.get("source") or unknown),
                    freshness=_plain(catalog.get("freshness") or unknown),
                ),
            )
        )

    update = diag.get("update") or {}
    if update:
        enabled = (
            _tt("doctor.status.enabled", loc)
            if update.get("checks_enabled")
            else _tt("doctor.status.disabled", loc)
        )
        lines.append(
            _row(
                _tt("doctor.label.updates", loc),
                _tt(
                    "doctor.updates.summary",
                    loc,
                    channel=_plain(update.get("channel") or _tt("doctor.status.unknown", loc)),
                    checks=enabled,
                ),
            )
        )

    network = diag.get("network") or {}
    if network.get("checked"):
        checks = network.get("checks") or []
        reachable = sum(bool(item.get("reachable")) for item in checks)
        lines.append(
            _row(
                _tt("doctor.label.network", loc),
                _tt("doctor.network.summary", loc, total=len(checks), reachable=reachable),
            )
        )


def append_error_lines(
    lines: list[str],
    errors: list[dict],
    *,
    locale: str | None = None,
    config: object | None = None,
) -> None:
    """Render stable error anatomy without exposing a Python traceback."""
    if not errors:
        return
    loc = resolve_doctor_locale(locale, config)
    lines.append(layout.section(_tt("doctor.section.errors", loc)))
    for item in errors:
        code = _plain(item.get("code") or "JARN-INTERNAL-001")
        summary = _plain(item.get("summary") or _tt("doctor.errors.check_failed", loc))
        lines.append(f"  {layout.err(f'{grammar.GLYPH_FAIL} {code}:')} {_esc(summary)}")
        lines.append(
            _row(
                _tt("error.cause", loc),
                _plain(item.get("cause") or _tt("doctor.status.unknown", loc)),
            )
        )
        retry = (
            _tt("error.retryable.yes", loc)
            if item.get("retryable")
            else _tt("error.retryable.no", loc)
        )
        component = _plain(item.get("component") or _tt("doctor.status.unknown", loc))
        lines.append(_row(_tt("error.component", loc), f"{component} · {retry}"))
        lines.append(
            _row(
                _tt("error.next", loc),
                _plain(item.get("action") or _tt("doctor.errors.next_default", loc)),
            )
        )
        lines.append(
            _row(
                _tt("error.log", loc),
                _plain(item.get("report_path") or item.get("log_path") or ""),
            )
        )


def append_extension_lines(
    lines: list[str],
    ext: dict,
    *,
    locale: str | None = None,
    config: object | None = None,
) -> None:
    """Append the doctor Extensions block as Rich-markup lines."""
    loc = resolve_doctor_locale(locale, config)
    counts = ext.get("counts") or {}
    lines.append(layout.section(_tt("doctor.section.extensions", loc)))
    if ext.get("project_trusted") is False:
        lines.append(f"  {layout.warn(_tt('doctor.ext.untrusted', loc))}")
    lines.append(
        "  "
        + _tt(
            "doctor.ext.counts",
            loc,
            skills=counts.get("skills", 0),
            commands=counts.get("commands", 0),
            subagents=counts.get("subagents", 0),
            hooks=counts.get("hooks", 0),
            mcp=counts.get("mcp_servers", 0),
            async_n=counts.get("async_subagents", 0),
        )
    )

    for warning in ext.get("warnings") or []:
        lines.append(f"  {layout.warn(grammar.GLYPH_WARN + ' ' + _plain(warning))}")

    for kind, key in (
        ("skills", "doctor.section.skills"),
        ("commands", "doctor.section.commands"),
        ("subagents", "doctor.section.subagents"),
    ):
        rows = ext.get(kind) or []
        active = [r for r in rows if r.get("status") in ("active", "renamed_builtin")]
        if not active:
            continue
        lines.append("")
        lines.append(f"  {layout.heading(_tt(key, loc))}")
        for row in active:
            scope = _esc(row.get("scope", ""))
            name = _esc(row.get("name", ""))
            detail = _esc(row.get("detail", ""))
            suffix = f" — {detail}" if detail else ""
            lines.append(f"    {layout.ok(grammar.GLYPH_OK)} {name} ({scope}){suffix}")

    hooks = [h for h in ext.get("hooks") or [] if h.get("status") == "active"]
    if hooks:
        lines.append("")
        lines.append(f"  {layout.heading(_tt('doctor.section.hooks', loc))}")
        for hook in hooks:
            blocking = (
                _tt("doctor.hook.blocking", loc)
                if hook.get("blocking")
                else _tt("doctor.hook.nonblocking", loc)
            )
            lines.append(
                f"    {layout.ok(grammar.GLYPH_OK)} "
                f"{_esc(hook.get('event') or '')} ({blocking}): "
                f"{_esc(hook.get('command') or '')}"
            )

    servers = [s for s in ext.get("mcp_servers") or [] if s.get("status") == "active"]
    if servers:
        lines.append("")
        lines.append(f"  {layout.heading(_tt('doctor.section.mcp', loc))}")
        for server in servers:
            health = server.get("health") or _tt("doctor.status.unknown", loc)
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
        lines.append("")
        lines.append(f"  {layout.muted(_tt('doctor.section.shadowed', loc))}")
        for row in shadowed:
            lines.append(
                "    "
                + layout.muted(
                    f"{_plain(row.get('name') or '')} "
                    f"({_plain(row.get('scope') or '')}) — "
                    f"{_plain(row.get('detail', ''))}"
                )
            )


def render_doctor_console(
    console: Console,
    diag: dict,
    *,
    locale: str | None = None,
    config: object | None = None,
) -> None:
    """Print doctor diagnostics to a Rich console."""
    loc = resolve_doctor_locale(locale, config)
    console.rule(layout.heading(_tt("doctor.title", loc)))
    for line in doctor_lines(diag, locale=loc)[1:]:
        console.print(line)
