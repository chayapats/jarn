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
        "--permission-mode",
        dest="headless_permission_mode",
        choices=["plan", "ask", "auto-edit", "yolo"],
        metavar="MODE",
        help="Override the permission mode for this headless run (plan|ask|auto-edit|yolo).",
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

    # Headless one-shot: dispatch before any TUI setup.
    if args.headless_prompt is not None:
        return _cmd_headless(
            prompt_arg=args.headless_prompt,
            as_json=args.json,
            model_override=args.headless_model,
            permission_mode_override=args.headless_permission_mode,
            max_turns=args.headless_max_turns,
            cwd_override=args.headless_cwd,
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
    return _cmd_launch(resume=args.resume)


def _cmd_headless(
    *,
    prompt_arg: str,
    as_json: bool = False,
    model_override: str | None = None,
    permission_mode_override: str | None = None,
    max_turns: int = 1,
    cwd_override: str | None = None,
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
    from jarn.config.loader import load_config
    from jarn.config.schema import PermissionMode
    from jarn.observability import configure_langsmith, setup_logging

    if not paths.global_config_path().is_file():
        print(
            "error: no configuration found — run `jarn setup` first.",
            file=sys.stderr,
        )
        return 1

    # Use the same trust logic as the interactive launch.
    trusted = _resolve_project_trust(root)
    cfg = load_config(project_root=root, project_trusted=trusted)
    setup_logging(cfg.observability.log_level)
    configure_langsmith(cfg.observability.langsmith)

    # Apply CLI overrides.
    if model_override:
        cfg.routing.main = model_override
        cfg.default_model = model_override
    if permission_mode_override:
        cfg.permission_mode = PermissionMode(permission_mode_override)

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


def _collect_doctor(diag: dict) -> int:
    """Populate ``diag`` with doctor diagnostics and return the exit code.

    Pure data collection — no rendering — so the same diagnostics back both the
    Rich and the ``--json`` output paths.
    """
    from jarn.config import paths
    from jarn.config.loader import load_config
    from jarn.config.secrets import SecretResolutionError, resolve
    from jarn.providers import ModelFactory, ModelResolutionError

    gpath = paths.global_config_path()
    diag["global_config"] = str(gpath)
    diag["global_config_present"] = gpath.is_file()
    root = paths.find_project_root()
    diag["project_root"] = str(root) if root else None

    if not gpath.is_file():
        diag["ok"] = False
        return 1

    from jarn.config.trust import is_project_trusted

    project_trusted = is_project_trusted(root) if root is not None else True
    diag["project_trusted"] = project_trusted
    cfg = load_config(project_root=root, project_trusted=project_trusted)
    diag["default_profile"] = cfg.default_profile
    diag["main_model"] = cfg.resolved_main_model()
    diag["permission_mode"] = cfg.permission_mode.value

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

    if not present:
        console.print("\n[yellow]No config — run [b]jarn setup[/b].[/yellow]")
        return code

    console.print(f"default profile: {diag['default_profile']}")
    console.print(f"main model: {diag['main_model']}")
    console.print(f"permission mode: {diag['permission_mode']}")

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
    from jarn.config import paths
    from jarn.config.loader import _read_yaml
    from jarn.config.trust import TrustStore, fingerprint, project_dangerous

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

    ppath = paths.project_config_path(root)
    if ppath is None or not ppath.is_file():
        print(
            f"No project config at {root}/.jarn/config.yaml — nothing to trust.",
            file=sys.stderr,
        )
        return 1

    danger = project_dangerous(_read_yaml(ppath))
    store.trust(root, fingerprint(danger))
    store.save()
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


def _cmd_launch(*, resume: bool = False) -> int:
    from jarn.config import paths
    from jarn.config.loader import load_config
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
    # untrusted project until the user explicitly approves them.
    trusted = _resolve_project_trust(root)

    cfg = load_config(project_root=root, project_trusted=trusted)
    setup_logging(cfg.observability.log_level)
    configure_langsmith(cfg.observability.langsmith)

    # The terminal front-end (native scrollback) is the only chat UI.
    from jarn.repl import run_inline

    return run_inline(cfg, root, resume=resume, project_trusted=trusted)


def _resolve_project_trust(root: Path) -> bool:
    """Decide whether to honour the project's capability-granting config keys.

    Returns ``True`` when the project is already trusted (at its current
    fingerprint) or the user approves the prompt; ``False`` otherwise (the
    dangerous keys are then stripped at load time). No-op (trusted) when the
    project declares nothing dangerous.
    """
    from jarn.config import paths
    from jarn.config.loader import _read_yaml
    from jarn.config.trust import (
        TrustStore,
        fingerprint,
        project_dangerous,
    )

    ppath = paths.project_config_path(root)
    danger = project_dangerous(_read_yaml(ppath))
    if not danger:
        return True

    store = TrustStore.load()
    fp = fingerprint(danger)
    status = store.status(root, fp)
    if status == "trusted":
        return True

    granted = _prompt_project_trust(root, danger, status)
    if granted:
        store.trust(root, fp)
        store.save()
    return granted


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
