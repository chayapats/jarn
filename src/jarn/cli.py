"""``jarn`` command-line entry point.

Subcommands:
    jarn            launch the TUI (runs setup first if unconfigured)
    jarn setup      (re)run the onboarding wizard
    jarn init       create a JARN.md project context file
    jarn doctor     diagnose configuration / providers / keys / extensions
    jarn trust      list / trust / untrust project roots
    jarn --version  print version
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from jarn.version import __version__

#: Fire the policy.profile deprecation notice at most once per process.
_warned_policy_profile = False


def _warn_policy_profile_deprecated(cfg: Any) -> None:
    """One-time notice when the deprecated ``policy.profile`` config key is set.

    ``profile`` is now a launch-time ``preset``; warn at the launch boundary and
    name what it expands to, so users can set mode/sandbox directly instead.
    """
    global _warned_policy_profile
    if _warned_policy_profile or not cfg.policy.profile:
        return
    _warned_policy_profile = True
    from jarn.config.profiles import PROFILES

    eff = PROFILES.get(cfg.policy.profile, {})
    print(
        f"warning: policy.profile is deprecated; '{cfg.policy.profile}' sets "
        f"mode={eff.get('permission_mode', '?')}, sandbox={eff.get('local_sandbox', '?')}. "
        "Set those directly or use --preset.",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jarn", description="J.A.R.N. — Just A Reliable Nerd (coding agent TUI)"
    )
    parser.add_argument("--version", action="version", version=f"jarn {__version__}")
    parser.add_argument("--resume", action="store_true", help="Pick a previous session to resume on launch")

    # Headless one-shot flags (top-level, not a subcommand).
    parser.add_argument(
        "-p", "--print",
        dest="headless_prompt",
        metavar="PROMPT",
        help=(
            "Run a single non-interactive turn and print the result. "
            "Pass '-' to read the prompt from stdin."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="With -p: emit a JSON object {result, tokens, cost, turns} instead of plain text.",
    )
    parser.add_argument(
        "--model",
        dest="headless_model",
        metavar="REF",
        help="Override the active model for this headless run.",
    )
    parser.add_argument(
        "--mode",
        dest="headless_permission_mode",
        choices=["plan", "ask", "auto-edit", "yolo"],
        metavar="MODE",
        help=(
            "Override the permission mode for this run (plan|ask|auto-edit|yolo). "
            "Note: --preset overrides this for trust-relevant knobs if both are given."
        ),
    )
    # Deprecated alias of --mode (hidden); still honoured.
    parser.add_argument(
        "--permission-mode",
        dest="headless_permission_mode",
        choices=["plan", "ask", "auto-edit", "yolo"],
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--preset",
        dest="preset",
        metavar="NAME",
        help=(
            "Apply a preset — a launch-time shortcut that sets mode + sandbox "
            "(trusted-repo|review-only|sandbox-required|ci|offline)."
        ),
    )
    # Deprecated alias of --preset (hidden); still honoured, warns when used.
    parser.add_argument(
        "--profile",
        dest="legacy_profile",
        metavar="NAME",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--max-turns",
        dest="headless_max_turns",
        type=int,
        default=1,
        metavar="N",
        help="Maximum agent turns (default: 1).",
    )
    parser.add_argument(
        "--cwd",
        dest="headless_cwd",
        metavar="PATH",
        help="Working directory for this headless run.",
    )

    sub = parser.add_subparsers(dest="command")

    p_setup = sub.add_parser("setup", help="Run the onboarding wizard")
    p_setup.add_argument(
        "--force",
        action="store_true",
        help="Overwrite ~/.jarn/config.yaml without prompting",
    )
    p_init = sub.add_parser("init", help="Create a JARN.md project context file")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing JARN.md")
    p_doctor = sub.add_parser("doctor", help="Diagnose configuration and providers")
    p_doctor.add_argument(
        "--json", action="store_true", help="Emit diagnostics as JSON"
    )
    sub.add_parser("keys", help="Key inspector — see what your terminal sends for each key")

    p_trust = sub.add_parser(
        "trust", help="List, trust, or untrust project roots (capability gate)"
    )
    p_trust.add_argument(
        "path",
        nargs="?",
        help="Project root to trust (defaults to listing the trust store)",
    )
    p_trust.add_argument(
        "--remove",
        action="store_true",
        help="Remove PATH from the trust store instead of adding it",
    )
    p_trust.add_argument(
        "--json", action="store_true", help="Emit the trust list as JSON"
    )

    args = parser.parse_args(argv)

    # Unify the permission surface: --preset is canonical, --profile is a
    # deprecated alias. Merge + warn once here so headless and launch both see
    # one resolved value. (--mode/--permission-mode already share a dest.)
    preset_override = args.preset or args.legacy_profile
    if args.legacy_profile:
        print(
            "warning: --profile is deprecated; use --preset (same names).",
            file=sys.stderr,
        )

    # Headless one-shot: dispatch before any TUI setup.
    if args.headless_prompt is not None:
        return _cmd_headless(
            prompt_arg=args.headless_prompt,
            as_json=args.json,
            model_override=args.headless_model,
            permission_mode_override=args.headless_permission_mode,
            max_turns=args.headless_max_turns,
            cwd_override=args.headless_cwd,
            profile_override=preset_override,
        )

    # Fix the macOS Caps Lock language-switch stray-character bug before any TUI
    # (app / wizard / key inspector) starts its terminal driver.
    from jarn.tui.keyfix import apply_kitty_keyfix

    apply_kitty_keyfix()

    if args.command == "setup":
        return _cmd_setup(force=args.force)
    if args.command == "init":
        return _cmd_init(force=args.force)
    if args.command == "doctor":
        return _cmd_doctor(as_json=args.json)
    if args.command == "keys":
        from jarn.tui.keys import run_key_inspector

        run_key_inspector()
        return 0
    if args.command == "trust":
        return _cmd_trust(path=args.path, remove=args.remove, as_json=args.json)
    return _cmd_launch(resume=args.resume, profile_override=preset_override)


def _cmd_headless(
    *,
    prompt_arg: str,
    as_json: bool = False,
    model_override: str | None = None,
    permission_mode_override: str | None = None,
    max_turns: int = 1,
    cwd_override: str | None = None,
    profile_override: str | None = None,
) -> int:
    """Run a single non-interactive agent turn and print the result.

    Reads config from disk (same path as the normal launch), applies any CLI
    overrides, then delegates to :func:`jarn.headless.run_headless`.
    """
    import sys

    # Resolve working directory (used as the project root).
    root = Path(cwd_override).expanduser().resolve() if cwd_override else Path.cwd()

    # Read the prompt (a literal '-' means stdin).
    if prompt_arg == "-":
        try:
            prompt = sys.stdin.read()
        except (EOFError, KeyboardInterrupt):
            print("error: could not read prompt from stdin", file=sys.stderr)
            return 1
    else:
        prompt = prompt_arg

    prompt = prompt.strip()
    if not prompt:
        print("error: prompt is empty", file=sys.stderr)
        return 1

    from jarn.config import paths
    from jarn.config.loader import ConfigError, load_config
    from jarn.config.schema import PermissionMode
    from jarn.observability import configure_langsmith, setup_logging

    if not paths.global_config_path().is_file():
        print(
            "error: no configuration found — run `jarn setup` first.",
            file=sys.stderr,
        )
        return 1

    # Use the same trust logic as the interactive launch. The project tier is
    # read once and passed forward so the fingerprinted content is exactly what
    # gets loaded (no TOCTOU between the trust decision and the load).
    trusted, project_raw, trust_err = _resolve_project_trust(root)
    if trust_err is not None:
        print(f"error: {trust_err}", file=sys.stderr)
        return 1
    cfg = load_config(
        project_root=root, project_trusted=trusted, project_raw=project_raw
    )
    _warn_policy_profile_deprecated(cfg)
    setup_logging(cfg.observability.log_level)
    configure_langsmith(cfg.observability.langsmith)

    # Apply CLI overrides.
    if model_override:
        cfg.routing.main = model_override
        cfg.default_model = model_override
    if permission_mode_override:
        cfg.permission_mode = PermissionMode(permission_mode_override)

    # Expand the effective preset (CLI > config) and clamp untrusted. A preset
    # sets the trust-relevant knobs (incl. the mode), so warn when both were
    # supplied rather than silently dropping --mode.
    if profile_override and permission_mode_override:
        print(
            f"warning: --preset {profile_override} overrides "
            f"--mode {permission_mode_override} for trust-relevant settings.",
            file=sys.stderr,
        )
    from jarn.config.profiles import resolve_effective_profile

    try:
        resolve_effective_profile(
            cfg, project_trusted=trusted, cli_profile=profile_override
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Warn about yolo only when it actually survives the untrusted clamp, so an
    # untrusted run (pinned to plan) is never mislabelled as "no approval prompts".
    if cfg.permission_mode == PermissionMode.YOLO:
        print(
            "warning: running in yolo mode — no approval prompts"
            " (danger-guard still blocks catastrophic actions).",
            file=sys.stderr,
        )

    from jarn.headless import run_headless

    return run_headless(
        prompt,
        cfg,
        root,
        project_trusted=trusted,
        as_json=as_json,
        max_turns=max_turns,
    )


def _cmd_setup(*, force: bool = False) -> int:
    from jarn.onboarding import run_setup_tui

    result = run_setup_tui(force=force)
    if result is None:
        print("Setup cancelled.")
        return 1
    return 0


def _cmd_init(*, force: bool) -> int:
    from jarn.memory import write_jarn_md

    try:
        path = write_jarn_md(overwrite=force)
    except FileExistsError as exc:
        print(f"{exc}\nUse `jarn init --force` to overwrite.", file=sys.stderr)
        return 1
    print(f"Created {path}")
    return 0


def _collect_doctor(
    diag: dict,
    *,
    config: Any = None,
    project_root: Any = None,
    project_trusted: bool | None = None,
) -> int:
    """Populate ``diag`` with doctor diagnostics and return the exit code.

    Pure data collection — no rendering — so the same diagnostics back both the
    Rich and the ``--json`` output paths.

    When ``config`` is provided (e.g. from the REPL controller), the function
    uses it directly instead of loading from disk.  ``project_root`` and
    ``project_trusted`` are also accepted so the caller can pass its live
    session state.
    """
    from jarn.config import paths
    from jarn.config.secrets import SecretResolutionError, resolve
    from jarn.providers import ModelFactory, ModelResolutionError

    gpath = paths.global_config_path()
    diag["global_config"] = str(gpath)
    diag["global_config_present"] = gpath.is_file()

    if config is None:
        # CLI path: auto-discover root from the filesystem.
        from jarn.config.loader import load_config

        root = paths.find_project_root() if project_root is None else project_root
        diag["project_root"] = str(root) if root else None

        if not gpath.is_file():
            diag["ok"] = False
            return 1

        from jarn.config.trust import is_project_trusted

        if project_trusted is None:
            project_trusted = is_project_trusted(root) if root is not None else True
        diag["project_trusted"] = project_trusted
        cfg = load_config(project_root=root, project_trusted=project_trusted)
    else:
        # REPL path: use the live config that was already loaded at session start.
        # The session is running, so the config was already loaded successfully;
        # mark it present regardless of the on-disk state to show all diagnostics.
        cfg = config
        root = project_root
        diag["project_root"] = str(root) if root else None
        diag["global_config_present"] = True
        if project_trusted is None:
            project_trusted = True
        diag["project_trusted"] = project_trusted

    diag["default_profile"] = cfg.default_profile
    diag["main_model"] = cfg.resolved_main_model()
    diag["permission_mode"] = cfg.permission_mode.value
    diag["policy_profile"] = cfg.policy.profile or "none"
    diag["web_tools"] = cfg.policy.web_tools
    # Effective mode after the trust clamp: an untrusted project is pinned to
    # plan regardless of the configured mode/preset, so surface that here.
    from jarn.config.profiles import UNTRUSTED_FLOOR_PROFILE
    from jarn.config.schema import PermissionMode

    diag["effective_mode"] = (
        PermissionMode.PLAN.value if not project_trusted else cfg.permission_mode.value
    )
    diag["effective_profile"] = (
        UNTRUSTED_FLOOR_PROFILE if not project_trusted else (cfg.policy.profile or "none")
    )

    # Transparency: name the project-tier keys an untrusted load dropped so the
    # user knows what `jarn trust` would enable. Empty when trusted or no project.
    stripped: list[str] = []
    if not project_trusted and root is not None:
        from jarn.config.paths import project_config_path
        from jarn.config.trust import stripped_project_keys

        ppath = project_config_path(root)
        if ppath is not None and ppath.is_file():
            from jarn.config.loader import _read_yaml

            stripped = stripped_project_keys(_read_yaml(ppath) or {})
    diag["project_stripped_keys"] = stripped

    factory = ModelFactory(cfg)
    ok = True
    providers: list[dict] = []
    for name, prov in cfg.providers.items():
        entry: dict[str, Any] = {"name": name, "type": prov.type.value}
        try:
            resolve(prov.api_key)
            entry["key_state"] = "key ok"
            entry["key_ok"] = True
        except SecretResolutionError as exc:
            entry["key_state"] = str(exc)
            entry["key_ok"] = False
            if name == cfg.default_profile:
                ok = False
        providers.append(entry)
    diag["providers"] = providers

    try:
        factory.build_main()
        diag["main_model_builds"] = True
        diag["main_model_error"] = None
    except ModelResolutionError as exc:
        diag["main_model_builds"] = False
        diag["main_model_error"] = str(exc)
        ok = False

    from jarn.agent.docker_backend import docker_available as _docker_available
    from jarn.agent.os_sandbox import available as _sbx_available
    from jarn.agent.os_sandbox import backend_name as _sbx_name

    diag["sandbox"] = {
        "backend": _sbx_name(),
        "available": _sbx_available(),
        "mode": cfg.execution.local_sandbox,
    }
    diag["execution"] = {
        "backend": cfg.execution.backend,
        "docker_image": cfg.execution.docker_image,
        "docker_available": _docker_available(),
    }

    # Surface newer feature flags so operators can see them in doctor output.
    diag["git"] = {
        "autocheckpoint": cfg.git.autocheckpoint,
    }
    diag["wiki"] = {
        "enabled": cfg.wiki.enabled,
    }
    diag["observability"] = {
        "transcript": cfg.observability.transcript,
    }
    diag["context"] = {
        "repo_map": cfg.context.repo_map,
        "repo_map_tokens": cfg.context.repo_map_tokens,
    }

    from jarn.doctor_extensions import collect_extensions

    diag["extensions"] = collect_extensions(
        root, project_trusted=project_trusted, config=cfg
    )

    diag["ok"] = ok
    return 0 if ok else 1


def _cmd_doctor(*, as_json: bool = False) -> int:
    diag: dict = {}
    code = _collect_doctor(diag)

    if as_json:
        import json

        print(json.dumps(diag))
        return code

    from rich.console import Console

    console = Console()
    console.rule("[b]jarn doctor[/b]")

    gpath = diag["global_config"]
    present = diag["global_config_present"]
    console.print(f"global config: {gpath} {'[green]✔[/green]' if present else '[red]missing[/red]'}")
    console.print(f"project root: {diag['project_root'] or '[dim]none[/dim]'}")
    if diag.get("project_trusted") is False and diag.get("project_stripped_keys"):
        keys = ", ".join(diag["project_stripped_keys"])
        console.print(
            f"[yellow]project untrusted — stripped keys: {keys}[/yellow]"
            " [dim](run `jarn trust <root>` to enable)[/dim]"
        )

    if not present:
        console.print("\n[yellow]No config — run [b]jarn setup[/b].[/yellow]")
        return code

    console.print(f"default profile: {diag['default_profile']}")
    console.print(f"main model: {diag['main_model']}")
    _mode = diag["permission_mode"]
    _eff_mode = diag.get("effective_mode", _mode)
    _mode_str = _mode if _eff_mode == _mode else f"{_mode} · effective: {_eff_mode} (after trust clamp)"
    console.print(f"mode: {_mode_str}")
    console.print(
        f"preset (deprecated): {diag.get('policy_profile', 'none')}"
        f" · web tools: {'on' if diag.get('web_tools', True) else 'off'}"
    )

    sbx = diag.get("sandbox") or {}
    sbx_backend = sbx.get("backend") or "none"
    sbx_avail = sbx.get("available", False)
    sbx_mode = sbx.get("mode", "off")
    if sbx_avail:
        sbx_status = f"[green]{sbx_backend} available[/green]"
    else:
        sbx_status = "[dim]unavailable[/dim]"
    console.print(f"sandbox: {sbx_status} · mode {sbx_mode}")

    ex = diag.get("execution") or {}
    ex_backend = ex.get("backend", "local")
    if ex_backend == "docker" or ex.get("docker_image"):
        docker_ok = ex.get("docker_available", False)
        docker_status = (
            "[green]available[/green]" if docker_ok else "[dim]unavailable[/dim]"
        )
        console.print(
            f"execution backend: {ex_backend} · docker: {docker_status}"
            f" · image {ex.get('docker_image')}"
        )
    else:
        console.print(f"execution backend: {ex_backend}")

    git_diag = diag.get("git") or {}
    autockpt = "on" if git_diag.get("autocheckpoint") else "off"
    console.print(f"git.autocheckpoint: {autockpt}")

    wiki_diag = diag.get("wiki") or {}
    wiki_enabled = "on" if wiki_diag.get("enabled") else "off"
    console.print(f"wiki.enabled: {wiki_enabled}")

    obs_diag = diag.get("observability") or {}
    transcript = "on" if obs_diag.get("transcript", True) else "off"
    console.print(f"observability.transcript: {transcript}")

    ctx_diag = diag.get("context") or {}
    repo_map_mode = ctx_diag.get("repo_map", "tool")
    repo_map_tokens = ctx_diag.get("repo_map_tokens", 1024)
    console.print(f"context.repo_map: {repo_map_mode} · token_budget {repo_map_tokens}")

    console.print("\n[b]Providers[/b]")
    for entry in diag["providers"]:
        if entry["key_ok"]:
            key_state = "[green]key ok[/green]"
        else:
            key_state = f"[yellow]{entry['key_state']}[/yellow]"
        console.print(f"  {entry['name']} ({entry['type']}): {key_state}")

    console.print("\n[b]Main model build[/b]")
    if diag["main_model_builds"]:
        console.print("  [green]✔ model constructs[/green]")
    else:
        console.print(f"  [red]✗ {diag['main_model_error']}[/red]")

    _print_extensions(console, diag.get("extensions") or {})

    ok = diag["ok"]
    console.print(f"\n{'[green]All good.[/green]' if ok else '[yellow]Issues found above.[/yellow]'}")
    return code


def _print_extensions(console: Any, ext: dict) -> None:
    counts = ext.get("counts") or {}
    console.print("\n[b]Extensions[/b]")
    if ext.get("project_trusted") is False:
        console.print("  [yellow]project untrusted — project-tier files/config skipped[/yellow]")
    console.print(
        "  "
        f"skills {counts.get('skills', 0)} · "
        f"commands {counts.get('commands', 0)} · "
        f"subagents {counts.get('subagents', 0)} · "
        f"hooks {counts.get('hooks', 0)} · "
        f"mcp {counts.get('mcp_servers', 0)} · "
        f"async {counts.get('async_subagents', 0)}"
    )

    for warning in ext.get("warnings") or []:
        console.print(f"  [yellow]⚠ {warning}[/yellow]")

    for kind, label in (
        ("skills", "Skills"),
        ("commands", "Commands"),
        ("subagents", "Subagents"),
    ):
        rows = ext.get(kind) or []
        active = [r for r in rows if r.get("status") in ("active", "renamed_builtin")]
        if not active:
            continue
        console.print(f"\n  [b]{label}[/b]")
        for row in active:
            scope = row.get("scope", "")
            name = row.get("name", "")
            detail = row.get("detail", "")
            suffix = f" — {detail}" if detail else ""
            console.print(f"    [green]✔[/green] {name} ({scope}){suffix}")

    hooks = [h for h in ext.get("hooks") or [] if h.get("status") == "active"]
    if hooks:
        console.print("\n  [b]Hooks[/b]")
        for hook in hooks:
            blocking = "blocking" if hook.get("blocking") else "non-blocking"
            console.print(
                f"    [green]✔[/green] {hook.get('event')} ({blocking}): "
                f"{hook.get('command')}"
            )

    servers = [s for s in ext.get("mcp_servers") or [] if s.get("status") == "active"]
    if servers:
        console.print("\n  [b]MCP servers[/b]")
        for server in servers:
            health = server.get("health") or "unknown"
            console.print(
                f"    [green]✔[/green] {server.get('name')} "
                f"({server.get('transport')}, health={health})"
            )

    shadowed = [
        r
        for kind in ("skills", "commands", "subagents")
        for r in (ext.get(kind) or [])
        if r.get("status") == "shadowed"
    ]
    if shadowed:
        console.print("\n  [dim]Shadowed (not loaded):[/dim]")
        for row in shadowed:
            console.print(
                f"    [dim]{row.get('name')} ({row.get('scope')}) — "
                f"{row.get('detail', '')}[/dim]"
            )


def _cmd_trust(*, path: str | None, remove: bool, as_json: bool = False) -> int:
    """List, trust, or untrust project roots in the shared trust store."""
    from jarn.config.trust import (
        TrustStore,
        commit_trust_if_unchanged,
        parse_project_config,
        project_config_bytes,
    )

    store = TrustStore.load()

    if path is None:
        return _trust_list(store, as_json=as_json)

    root = Path(path).expanduser().resolve()

    if remove:
        removed = store.untrust(root)
        if not removed:
            print(f"{root} is not in the trust store.", file=sys.stderr)
            return 1
        store.save()
        print(f"Untrusted {root}")
        return 0

    # Read the project config once: fingerprint the exact on-disk bytes, then
    # re-verify they haven't changed before recording trust (TOCTOU guard).
    raw_bytes = project_config_bytes(root)
    if raw_bytes is None:
        print(
            f"No project config at {root}/.jarn/config.yaml — nothing to trust.",
            file=sys.stderr,
        )
        return 1
    parsed = parse_project_config(raw_bytes, root)
    err = commit_trust_if_unchanged(store, root, raw_bytes, parsed)
    if err is not None:
        print(f"error: {err}", file=sys.stderr)
        return 1
    print(f"Trusted {root}")
    return 0


def _trust_list(store: Any, *, as_json: bool) -> int:
    entries = store.entries()

    if as_json:
        import json

        print(
            json.dumps(
                [{"root": root, "fingerprint": fp} for root, fp in entries.items()]
            )
        )
        return 0

    if not entries:
        print("No trusted projects.")
        return 0

    for root, fp in entries.items():
        print(f"{fp[:12]}  {root}")
    return 0


def _cmd_launch(*, resume: bool = False, profile_override: str | None = None) -> int:
    from jarn.config import paths
    from jarn.config.loader import ConfigError, load_config
    from jarn.observability import configure_langsmith, setup_logging

    if not paths.global_config_path().is_file():
        print("No configuration found. Running first-time setup...\n")
        from jarn.onboarding import run_setup_tui

        if run_setup_tui() is None and not paths.global_config_path().is_file():
            print("Setup cancelled.")
            return 1

    root = paths.find_project_root() or Path.cwd()

    # Trust boundary: a project's .jarn/config.yaml can run hooks / spawn MCP
    # servers / override providers (secret exfil). Don't honour those keys from an
    # untrusted project until the user explicitly approves them. The project tier
    # is read once and passed forward so the fingerprinted content is exactly what
    # gets loaded (no TOCTOU between the trust decision and the load).
    trusted, project_raw, trust_err = _resolve_project_trust(root)
    if trust_err is not None:
        print(f"error: {trust_err}", file=sys.stderr)
        return 1

    cfg = load_config(
        project_root=root, project_trusted=trusted, project_raw=project_raw
    )
    _warn_policy_profile_deprecated(cfg)
    setup_logging(cfg.observability.log_level)
    configure_langsmith(cfg.observability.langsmith)

    # Apply the effective policy profile (CLI > config) and clamp untrusted.
    from jarn.config.profiles import resolve_effective_profile

    try:
        resolve_effective_profile(
            cfg, project_trusted=trusted, cli_profile=profile_override
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # The terminal front-end (native scrollback) is the only chat UI.
    from jarn.repl import run_inline

    return run_inline(cfg, root, resume=resume, project_trusted=trusted)


def _resolve_project_trust(root: Path) -> tuple[bool, dict[str, Any], str | None]:
    """Decide whether to honour the project's capability-granting config keys.

    Returns ``(trusted, project_raw, error)``. ``project_raw`` is the project
    tier dict read from the **same bytes** used to fingerprint it, so the caller
    can pass it straight into ``load_config(project_raw=...)`` with no second
    read (TOCTOU). ``error`` is non-None only when the config changed between the
    fingerprint and the commit — the caller should surface it and abort.

    Returns ``True`` when the project is already trusted (at its current
    fingerprint) or the user approves the prompt; ``False`` otherwise (the
    dangerous keys are then stripped at load time). No-op (trusted) when the
    project declares nothing dangerous.
    """
    from jarn.config.trust import (
        TrustStore,
        commit_trust_if_unchanged,
        fingerprint,
        parse_project_config,
        project_config_bytes,
        project_dangerous,
    )

    raw_bytes = project_config_bytes(root)
    if raw_bytes is None:
        return True, {}, None  # no project config → trusted, empty tier
    project_raw = parse_project_config(raw_bytes, root)

    danger = project_dangerous(project_raw)
    if not danger:
        return True, project_raw, None

    store = TrustStore.load()
    fp = fingerprint(danger)
    status = store.status(root, fp)
    if status == "trusted":
        return True, project_raw, None

    granted = _prompt_project_trust(root, danger, status)
    if not granted:
        return False, project_raw, None
    # The user took time to answer; re-verify the file hasn't changed since we
    # fingerprinted it. If it has, refuse — the stored fingerprint would not
    # match what we'd actually load.
    err = commit_trust_if_unchanged(store, root, raw_bytes, project_raw)
    if err is not None:
        return False, project_raw, err
    return True, project_raw, None


def _prompt_project_trust(root: Path, danger: dict, status: str) -> bool:
    from rich.console import Console

    console = Console()
    console.print(
        f"\n[yellow]⚠ This project's config[/yellow] ([dim]{root}/.jarn/config.yaml[/dim]) "
        "declares settings that can run code or access secrets:"
    )
    labels = {
        "hooks": "hooks (shell commands run automatically)",
        "mcp_servers": "MCP servers (spawned at startup)",
        "async_subagents": "async subagents (remote graphs)",
        "providers": "providers (model endpoints / API keys)",
        "execution": "execution backend",
        "permission_mode": "permission mode",
        "policy": "policy profile (permission mode / sandbox / web tools)",
        "observability": "observability (telemetry / LangSmith tracing)",
        "permissions.allow": "pre-approved (allow) commands",
    }
    for key in danger:
        console.print(f"  • {labels.get(key, key)}")
    if status == "changed":
        console.print("[yellow]These changed since you last trusted this project.[/yellow]")
    console.print(
        "[dim]Trust only repositories you would run code from. If you decline, "
        "these settings are ignored and the session continues safely.[/dim]"
    )
    try:
        answer = input("Trust this project's config? [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


if __name__ == "__main__":
    raise SystemExit(main())
