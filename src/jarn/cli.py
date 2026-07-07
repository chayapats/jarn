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
    parser.add_argument(
        "--add-dir",
        dest="add_dir",
        action="append",
        metavar="DIR",
        help=(
            "Add a directory to the session's write scope (repeatable). Each dir "
            "becomes an active root the agent may edit, alongside the project "
            "root. Checkpoint/undo and project context stay primary-root only."
        ),
    )

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
        help=(
            "With -p: emit JSON instead of plain text. On success: "
            "{result, tokens, cost, turns, tool_calls}. On failure: "
            "{error: {kind, message}}."
        ),
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
    parser.add_argument(
        "--max-turns",
        dest="headless_max_turns",
        type=int,
        default=1,
        metavar="N",
        help=(
            "With -p: maximum agent turns on the same thread (default: 1). "
            "Stops early when a turn completes without tool calls."
        ),
    )
    parser.add_argument(
        "--cwd",
        dest="headless_cwd",
        metavar="PATH",
        help="Working directory for this headless run.",
    )
    parser.add_argument(
        "--resume-session",
        dest="headless_resume_session",
        metavar="THREAD",
        help=(
            "With -p: resume a prior headless thread. Pass 'last' for the most "
            "recent session or a thread id from /sessions. An empty prompt "
            "continues without a new user message."
        ),
    )
    parser.add_argument(
        "--output-schema",
        dest="headless_output_schema",
        metavar="FILE",
        help=(
            "With -p: path to a JSON Schema file. Constrains the agent's final "
            "answer to the schema; the parsed object is returned as 'result' in "
            "the --json envelope (exit 1 with kind 'schema' if the agent fails "
            "to produce a conforming response)."
        ),
    )

    parser.epilog = (
        "Headless exit codes (jarn -p): 0 success, "
        "1 generic error or schema validation failure (--output-schema), "
        "2 approval refused, budget hard-stop, or usage error (bad/missing schema file), "
        "124 timeout."
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
    p_keys = sub.add_parser(
        "keys", help="Key inspector — see what your terminal sends for each key"
    )
    p_keys.add_argument(
        "--repl",
        action="store_true",
        help="Use the prompt_toolkit REPL key path (default: Textual inspector)",
    )

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
    sub.add_parser(
        "trust-hooks",
        help="Record a one-time accept to run global lifecycle hooks "
        "(enables `hook_global_require_trust: true`)",
    )

    # --profile was removed in v0.6.0 (deprecated since v0.5.0). Without this
    # guard argparse reports a confusing subcommand "invalid choice" error for
    # `jarn --profile NAME`; fail fast and name the replacement instead.
    raw_args = sys.argv[1:] if argv is None else argv
    if any(a == "--profile" or a.startswith("--profile=") for a in raw_args):
        parser.error(
            "--profile was removed in v0.6.0; use --preset NAME "
            "(same preset names). The policy.profile config key was removed too."
        )

    args = parser.parse_args(argv)

    preset_override = args.preset

    # --output-schema is headless-only: error if given without -p.
    if args.headless_output_schema is not None and args.headless_prompt is None:
        parser.error("--output-schema requires -p / --print")

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
            resume_session=args.headless_resume_session,
            output_schema=args.headless_output_schema,
            add_dirs=args.add_dir,
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
        if args.repl:
            from jarn.repl.key_inspector import run_repl_key_inspector

            run_repl_key_inspector()
        else:
            from jarn.tui.keys import run_key_inspector

            run_key_inspector()
        return 0
    if args.command == "trust":
        return _cmd_trust(path=args.path, remove=args.remove, as_json=args.json)
    if args.command == "trust-hooks":
        return _cmd_trust_hooks()
    return _cmd_launch(
        resume=args.resume,
        profile_override=preset_override,
        add_dirs=args.add_dir,
    )


def _cmd_headless(
    *,
    prompt_arg: str,
    as_json: bool = False,
    model_override: str | None = None,
    permission_mode_override: str | None = None,
    max_turns: int = 1,
    cwd_override: str | None = None,
    profile_override: str | None = None,
    resume_session: str | None = None,
    output_schema: str | None = None,
    add_dirs: list[str] | None = None,
) -> int:
    """Run a single non-interactive agent turn and print the result.

    Reads config from disk (same path as the normal launch), applies any CLI
    overrides, then delegates to :func:`jarn.headless.run_headless`.

    ``--add-dir`` grants (``add_dirs``) are validated with the same
    :func:`_validate_add_dirs` the interactive launch uses and threaded through to
    the Controller's write scope. Like the launch flag, an ``--add-dir`` given at
    start is an explicit operator grant (same trust model as the primary root), so
    it does NOT need the mid-session ``/add-dir`` trust/ask gate.
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
    if not prompt and not resume_session:
        print("error: prompt is empty", file=sys.stderr)
        return 1

    from jarn.config import paths
    from jarn.config.loader import ConfigError, load_config
    from jarn.config.schema import PermissionMode
    from jarn.observability import configure_tracing, setup_logging

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
    setup_logging(cfg.observability.log_level)
    configure_tracing(cfg.observability)

    # Validate --add-dir grants up front (fail fast — don't run with a promised
    # root that isn't there). Same validation as the interactive launch.
    extra_roots, add_dir_err = _validate_add_dirs(add_dirs)
    if add_dir_err is not None:
        print(f"error: {add_dir_err}", file=sys.stderr)
        return 1

    # T-3-3 (item G): in -p mode the diagnostics NOTICE is dropped, but ruff/pyright
    # would still spend up to 30s per edit-turn. Gate the whole feature off so a
    # headless run never pays that latency tax for output nobody consumes.
    cfg.verify.diagnostics = "off"

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

    # Load and parse the JSON schema file if --output-schema was given.
    response_format: Any | None = None
    if output_schema is not None:
        import json as _json

        from jarn.headless import HeadlessFailure, _emit_failure

        schema_path = Path(output_schema)
        try:
            schema_dict = _json.loads(schema_path.read_bytes())
        except (OSError, ValueError) as exc:
            failure = HeadlessFailure(
                "usage",
                f"--output-schema: cannot read/parse {schema_path}: {exc}",
                exit_code=2,
            )
            return _emit_failure(failure, as_json=as_json)
        response_format = {"type": "json_schema", "schema": schema_dict}

    from jarn.headless import run_headless

    return run_headless(
        prompt,
        cfg,
        root,
        project_trusted=trusted,
        as_json=as_json,
        max_turns=max_turns,
        resume_session=resume_session,
        response_format=response_format,
        add_dirs=extra_roots,
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
    """Backward-compatible alias for :func:`jarn.doctor.collect.collect_doctor`."""
    from jarn.doctor.collect import collect_doctor

    return collect_doctor(
        diag,
        config=config,
        project_root=project_root,
        project_trusted=project_trusted,
    )


def _cmd_doctor(*, as_json: bool = False) -> int:
    from rich.console import Console

    from jarn.doctor.collect import collect_doctor
    from jarn.doctor.render import doctor_to_json, render_doctor_console

    diag: dict = {}
    code = collect_doctor(diag)

    if as_json:
        print(doctor_to_json(diag))
        return code

    render_doctor_console(Console(), diag)
    return code


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


def _cmd_trust_hooks() -> int:
    """Record the one-time global-hooks accept marker.

    Enables ``hook_global_require_trust: true``: until this marker exists, the
    controller refuses to build a hook runner (so a compromised global config
    can't auto-run shell on ``session_start``). Removing the marker re-triggers
    the gate. The marker lives in ``JARN_HOME`` (not per-project).
    """
    from jarn.config import paths
    from jarn.config.trust import GLOBAL_HOOKS_TRUST_MARKER, trust_global_hooks

    marker = trust_global_hooks()
    print(
        f"Global lifecycle hooks accepted — marker at {marker}.\n"
        f"`hook_global_require_trust: true` will now run hooks; delete "
        f"{paths.global_home() / GLOBAL_HOOKS_TRUST_MARKER} to re-trigger the gate."
    )
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


def _validate_add_dirs(raw: list[str] | None) -> tuple[list[Path], str | None]:
    """Resolve and validate ``--add-dir`` values (must exist / be a directory).

    Returns ``(roots, error)``. ``error`` is non-None on the first invalid dir;
    the caller surfaces it and aborts (fail fast — don't launch with a promised
    root that isn't there). Roots are resolved + de-duplicated, primary excluded
    later by the engine's own de-dupe.
    """
    roots: list[Path] = []
    for entry in raw or []:
        try:
            path = Path(entry).expanduser().resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            return [], f"--add-dir: cannot resolve {entry!r}: {exc}"
        if not path.exists():
            return [], f"--add-dir: {path} does not exist."
        if not path.is_dir():
            return [], f"--add-dir: {path} is not a directory."
        if path not in roots:
            roots.append(path)
    return roots, None


def _cmd_launch(
    *,
    resume: bool = False,
    profile_override: str | None = None,
    add_dirs: list[str] | None = None,
) -> int:
    from jarn.config import paths
    from jarn.config.loader import ConfigError, load_config
    from jarn.observability import configure_tracing, setup_logging

    extra_roots, add_dir_err = _validate_add_dirs(add_dirs)
    if add_dir_err is not None:
        print(f"error: {add_dir_err}", file=sys.stderr)
        return 1

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
    setup_logging(cfg.observability.log_level)
    configure_tracing(cfg.observability)

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

    return run_inline(
        cfg, root, resume=resume, project_trusted=trusted, add_dirs=extra_roots
    )


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
