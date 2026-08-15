"""``jarn`` command-line entry point.

Subcommands:
    jarn            launch the TUI (runs setup first if unconfigured)
    jarn login      log in to OpenRouter via OAuth PKCE (browser → keychain)
    jarn auth       manage ChatGPT subscription auth
    jarn codex      compatibility alias for ``jarn auth``
    jarn setup      (re)run the onboarding wizard
    jarn init       create a JARN.md project context file
    jarn doctor     diagnose configuration / providers / keys / extensions
    jarn config     inspect, validate, edit, or reset configuration
    jarn telemetry  inspect or change local telemetry opt-in
    jarn update     check for or transactionally install an update
    jarn rollback   switch to the retained previous executable
    jarn trust      list / trust / untrust project roots
    jarn gateway    set up, inspect, control, or run the Telegram gateway
    jarn --version  print version
"""

from __future__ import annotations

import argparse
import codecs
import contextlib
import math
import signal
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Never

from jarn.tui import grammar, layout, palette
from jarn.util.process_env import external_command_env
from jarn.version import __version__


class JarnArgumentParser(argparse.ArgumentParser):
    """Argparse with the same stable error anatomy as runtime failures."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Abbreviated long options are ambiguous across parser/subparser
        # boundaries and have changed behaviour between Python patch releases.
        # Require the documented spelling so e.g. ``sessions export --output``
        # can never be consumed as top-level ``--output-format`` or
        # ``--output-schema`` before the sessions parser sees it.
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> Never:
        from jarn.errors import ErrorCode, error_detail

        self.print_usage(sys.stderr)
        detail = error_detail(
            ErrorCode.CLI_USAGE,
            "Command usage is invalid.",
            cause=message,
            component="command line parser",
            retryable=False,
            action=f"Run `{self.prog} --help`, correct the command, and retry.",
        )
        self.exit(2, detail.render(stream=sys.stderr) + "\n")

    def format_help(self) -> str:
        """Common commands (epilog) first, then grouped catalog and flags."""
        formatter = self._get_formatter()
        formatter.add_usage(self.usage, self._actions, self._mutually_exclusive_groups)
        formatter.add_text(self.description)
        formatter.add_text(self.epilog)
        formatter.add_text(_format_cli_commands(self))
        skip = {"positional arguments", "Commands"}
        for action_group in self._action_groups:
            if action_group.title in skip:
                continue
            formatter.start_section(action_group.title)
            formatter.add_text(action_group.description)
            formatter.add_arguments(action_group._group_actions)
            formatter.end_section()
        formatter.add_text("See `jarn <command> --help` for subcommand flags.")
        return formatter.format_help()


#: Exhaustive CLI subcommand grouping for ``jarn --help``. Help strings stay on
#: the argparse parsers; this tuple is only membership + scan order.
_CLI_COMMAND_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Start", ("setup", "init", "exec", "sessions")),
    ("Account", ("auth", "login", "codex")),
    ("Install", ("doctor", "config", "update", "rollback", "uninstall")),
    ("Workspace", ("trust", "trust-hooks", "keys")),
    ("Gateway", ("gateway",)),
    ("Support", ("bug", "telemetry", "completions")),
)


def _format_cli_commands(parser: argparse.ArgumentParser) -> str:
    """Grouped command catalog (plain dialect) — replaces argparse's brace dump."""
    sub = next(
        (action for action in parser._actions if isinstance(action, argparse._SubParsersAction)),
        None,
    )
    if sub is None:
        return ""
    helps = {action.dest: (action.help or "") for action in sub._choices_actions}
    lines = [layout.title("Commands", dialect="plain")]
    for group, names in _CLI_COMMAND_GROUPS:
        lines.append(layout.title(group, dialect="plain"))
        for name in names:
            lines.append(layout.row(name, helps.get(name, ""), dialect="plain"))
        lines.append("")
    return "\n".join(lines).rstrip()


def _auth_timeout_arg(value: str) -> float:
    """Argparse validator kept local so ``jarn --help`` stays dependency-light."""

    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number of seconds") from exc
    if not math.isfinite(parsed) or not 1.0 <= parsed <= 900.0:
        raise argparse.ArgumentTypeError("timeout must be between 1 and 900 seconds")
    return parsed


def _configure_safe_stdio() -> None:
    """Prevent non-UTF locales from turning help/errors into tracebacks.

    Python can still start with an ASCII stdout when locale coercion and UTF-8
    mode are explicitly disabled.  Keep the detected encoding, but escape
    characters it cannot represent.  JSON remains valid ASCII/UTF-8-compatible
    output and interactive users get one actionable, ASCII-only warning.
    """

    changed = False
    for stream in (sys.stdout, sys.stderr):
        encoding = getattr(stream, "encoding", None)
        if not encoding:
            continue
        try:
            is_utf = codecs.lookup(encoding).name == "utf-8"
        except LookupError:
            is_utf = False
        if is_utf:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="backslashreplace")
            changed = True
    if changed and getattr(sys.stderr, "isatty", lambda: False)():
        print(
            "warning: JARN-I18N-001: this terminal is not UTF-8; unsupported "
            "characters are escaped. Configure a UTF-8 locale such as C.UTF-8.",
            file=sys.stderr,
        )


def build_parser() -> argparse.ArgumentParser:
    """Construct and return the top-level jarn ArgumentParser.

    Factored out of ``main()`` so the same parser object can be introspected by
    ``jarn completions`` and by the anti-drift test without duplicating the
    subcommand/flag list.
    """
    parser = JarnArgumentParser(
        prog="jarn",
        description="J.A.R.N. — Just A Reliable Nerd (coding agent TUI)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"jarn {__version__}")

    start = parser.add_argument_group("Start")
    start.add_argument(
        "--resume", action="store_true", help="Pick a previous session to resume on launch"
    )
    start.add_argument(
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

    oneshot = parser.add_argument_group("One-shot")
    oneshot.add_argument(
        "-p",
        "--print",
        dest="headless_prompt",
        metavar="PROMPT",
        help=(
            "Run a single non-interactive turn and print the result. "
            "Pass '-' to read the prompt from stdin."
        ),
    )
    oneshot.add_argument(
        "--output-format",
        dest="headless_output_format",
        choices=["text", "json", "stream-json"],
        default=None,
        metavar="FORMAT",
        help=(
            "With -p: output format (text|json|stream-json; default text). "
            "'json' emits one buffered final object; 'stream-json' emits NDJSON — "
            "one JSON object per event as the turn runs, then a terminal "
            '{"type":"result",...} line (with thread_id) — mirroring '
            "`claude -p --output-format stream-json`."
        ),
    )
    oneshot.add_argument(
        "--json",
        action="store_true",
        help=(
            "With -p: legacy alias for --output-format json. On success: "
            "{result, tokens, cost, turns, tool_calls, verification}. On failure: "
            "{error: {kind, message}}."
        ),
    )
    oneshot.add_argument(
        "--model",
        dest="headless_model",
        metavar="REF",
        help="Override the active model for this headless run.",
    )
    oneshot.add_argument(
        "--mode",
        dest="headless_permission_mode",
        choices=["plan", "ask", "auto-edit", "yolo"],
        metavar="MODE",
        help=(
            "With -p: override the permission mode (plan|ask|auto-edit|yolo). "
            "Note: --preset overrides this for trust-relevant knobs if both are given."
        ),
    )
    # Deprecated alias of --mode (hidden); still honoured.
    oneshot.add_argument(
        "--permission-mode",
        dest="headless_permission_mode",
        choices=["plan", "ask", "auto-edit", "yolo"],
        help=argparse.SUPPRESS,
    )
    oneshot.add_argument(
        "--preset",
        dest="preset",
        metavar="NAME",
        help=(
            "Apply a preset — a launch-time shortcut that sets mode + sandbox "
            "(trusted-repo|review-only|sandbox-required|ci|offline)."
        ),
    )
    oneshot.add_argument(
        "--max-turns",
        dest="headless_max_turns",
        type=int,
        default=1,
        metavar="N",
        help=(
            "With -p: must be 1 (the default); values >1 are rejected. A headless "
            "invocation always runs exactly one complete model/tool graph turn."
        ),
    )
    oneshot.add_argument(
        "--cwd",
        dest="headless_cwd",
        metavar="PATH",
        help="Working directory for this headless run.",
    )
    oneshot.add_argument(
        "--ignore-project-config",
        dest="headless_ignore_project_config",
        action="store_true",
        help=(
            "Ignore <cwd>/.jarn/config.yaml while still operating on the project "
            "files (safe for automation on untrusted checkouts)."
        ),
    )
    oneshot.add_argument(
        "--resume-session",
        dest="headless_resume_session",
        metavar="THREAD",
        help=(
            "With -p: resume a prior headless thread. Pass 'last' for the most "
            "recent session or a thread id from /sessions. An empty prompt "
            "continues without a new user message."
        ),
    )
    oneshot.add_argument(
        "--output-schema",
        dest="headless_output_schema",
        metavar="FILE",
        help=(
            "With -p: path to a JSON Schema file. Constrains the agent's final "
            "answer to the schema; the parsed object is returned as 'result' in "
            "the --json envelope (exit 9 with kind 'schema' if the agent fails "
            "to produce a conforming response)."
        ),
    )

    parser.epilog = """Start and common commands:
  jarn setup                       verified, resumable first-run setup
  jarn gateway setup               verify a Telegram bot, discover your user ID,
                                   store its token safely, and offer auto-start
  jarn                             start interactive coding in the current directory
  jarn exec "TASK" --mode ask      run one automation-safe, non-interactive turn
  jarn sessions                    list saved sessions; add --help for export/delete

Installation and configuration (no browser required):
  jarn doctor --json               show resolved executable path, install method/record,
                                   setup state, dependency versions, and PATH conflicts
  jarn config path                 print the active global config path
  jarn config path --project       print .jarn/config.yaml for this project
  jarn config validate             validate configuration without changing it

Authentication:
  jarn auth login [--device]       sign in with a ChatGPT subscription (device for SSH)
  jarn auth status                 verify Codex dependency, auth mode, and account
  jarn auth repair                 recheck dependency and refresh ChatGPT auth
  jarn auth logout                 remove only Codex-managed credentials
  jarn login                       OpenRouter OAuth login (separate from ChatGPT auth)

Models and reasoning:
  In interactive J.A.R.N., /model lists verified models and then offers only
  reasoning efforts supported by the chosen model; /model refresh forces a refresh.
  Use /status to inspect the active model/reasoning or --model PROFILE/MODEL with exec.

Permissions and safety:
  /mode plan|ask|auto-edit|yolo changes the interactive mode. With exec, use --mode.
  plan = review only; ask = confirm changes (safe default); auto-edit = workspace edits;
  yolo = broad access, but hard catastrophic-action and credential guards remain active.

Diagnosis, repair, and support:
  jarn doctor                      offline, non-mutating diagnosis (add --network to opt in)
  jarn doctor --fix --dry-run      preview allowlisted, recoverable repairs
  jarn doctor --fix                apply the shown plan with backup/rollback protection
  jarn doctor --report FILE        write a redacted support report (owner-only mode 0600)
  jarn bug --dry-run               prepare local support material without opening a browser

Update, rollback, and removal:
  jarn update --check              check only; jarn update --dry-run previews activation
  jarn rollback                    activate the retained previous working version
  jarn uninstall                   choose components; config/data/credentials are kept by default
  jarn uninstall --help            show explicit data-removal category flags

Stable exit codes:
  0 success; 1 internal/diagnostic issue; 2 usage or configuration;
  3 auth; 4 model unavailable; 5 permission denied; 6 network/provider;
  7 update/rollback failed; 8 budget exceeded; 9 verification failed;
  10 updated executable requires a fresh shell; 124 timeout; 130 cancelled.
"""

    sub = parser.add_subparsers(dest="command", metavar="COMMAND", title="Commands")

    # Stable, discoverable spelling for automation. The historical ``-p`` /
    # ``--print`` flags remain supported; both routes dispatch through exactly
    # the same headless implementation and output contract.
    p_exec = sub.add_parser("exec", help="Run one non-interactive agent turn")
    p_exec.add_argument("headless_prompt", metavar="PROMPT", help="Task text, or '-' for stdin")
    p_exec.add_argument(
        "--output-format",
        dest="headless_output_format",
        choices=["text", "json", "stream-json"],
        default=None,
        metavar="FORMAT",
        help="Output format: text, one JSON result, or streaming NDJSON",
    )
    p_exec.add_argument(
        "--json",
        action="store_true",
        help="Alias for --output-format json",
    )
    p_exec.add_argument("--model", dest="headless_model", metavar="REF")
    p_exec.add_argument(
        "--mode",
        dest="headless_permission_mode",
        choices=["plan", "ask", "auto-edit", "yolo"],
        metavar="MODE",
    )
    p_exec.add_argument("--preset", dest="preset", metavar="NAME")
    p_exec.add_argument("--max-turns", dest="headless_max_turns", type=int, default=1)
    p_exec.add_argument("--cwd", dest="headless_cwd", metavar="PATH")
    p_exec.add_argument(
        "--ignore-project-config",
        dest="headless_ignore_project_config",
        action="store_true",
    )
    p_exec.add_argument("--resume-session", dest="headless_resume_session", metavar="THREAD")
    p_exec.add_argument("--output-schema", dest="headless_output_schema", metavar="FILE")
    p_exec.add_argument(
        "--add-dir",
        dest="add_dir",
        action="append",
        metavar="DIR",
        help="Add a directory to this run's write scope (repeatable)",
    )

    p_setup = sub.add_parser("setup", help="Run the onboarding wizard")
    p_setup.add_argument(
        "--force",
        action="store_true",
        help="Overwrite ~/.jarn/config.yaml without prompting",
    )
    p_init = sub.add_parser("init", help="Create a JARN.md project context file")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing JARN.md")
    p_doctor = sub.add_parser("doctor", help="Diagnose configuration and providers")
    p_doctor.add_argument("--json", action="store_true", help="Emit diagnostics as JSON")
    p_doctor.add_argument(
        "--fix",
        action="store_true",
        help="Apply the allowlisted repair plan (backups and rollback are automatic)",
    )
    p_doctor.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the repair plan without changing files (implies --fix)",
    )
    p_doctor.add_argument(
        "--report",
        nargs="?",
        const="jarn-support-report.json",
        metavar="FILE",
        help="Write a privacy-scanned support report (default: ./jarn-support-report.json)",
    )
    p_doctor.add_argument(
        "--network",
        action="store_true",
        help="Opt in to bounded provider reachability checks (doctor is offline by default)",
    )

    p_config = sub.add_parser("config", help="Inspect or safely manage configuration")
    config_actions = p_config.add_subparsers(dest="config_action", required=True)

    def add_config_scope(command: argparse.ArgumentParser, *, json_output: bool = True) -> None:
        command.add_argument(
            "--project",
            action="store_true",
            help="Use the current project's .jarn/config.yaml instead of the global file",
        )
        if json_output:
            command.add_argument("--json", action="store_true", help="Emit stable JSON")

    p_config_show = config_actions.add_parser("show", help="Show redacted configuration YAML")
    add_config_scope(p_config_show)
    p_config_path = config_actions.add_parser("path", help="Print the configuration path")
    add_config_scope(p_config_path)
    p_config_validate = config_actions.add_parser(
        "validate", help="Validate syntax, schema version, and typed settings"
    )
    add_config_scope(p_config_validate)
    p_config_edit = config_actions.add_parser(
        "edit", help="Edit a temporary copy, validate it, then commit atomically"
    )
    add_config_scope(p_config_edit, json_output=False)
    p_config_edit.add_argument(
        "--editor",
        metavar="COMMAND",
        help="Editor command (default: $VISUAL, then $EDITOR)",
    )
    p_config_reset = config_actions.add_parser(
        "reset", help="Back up and replace configuration with the shipped template"
    )
    add_config_scope(p_config_reset)
    p_config_reset.add_argument(
        "--yes", action="store_true", help="Confirm replacement without prompting"
    )

    p_telemetry = sub.add_parser("telemetry", help="Inspect or change opt-in local-only telemetry")
    telemetry_actions = p_telemetry.add_subparsers(dest="telemetry_action", required=True)
    for telemetry_action, telemetry_help in (
        ("status", "Show opt-in state and local sink health"),
        ("on", "Opt in to numeric local telemetry"),
        ("off", "Opt out without deleting existing local data"),
    ):
        telemetry_parser = telemetry_actions.add_parser(telemetry_action, help=telemetry_help)
        telemetry_parser.add_argument("--json", action="store_true", help="Emit stable JSON")

    p_update = sub.add_parser("update", help="Check for or transactionally install an update")
    p_update.add_argument("--check", action="store_true", help="Check only; do not install")
    p_update.add_argument("--json", action="store_true", help="Emit stable JSON")
    p_update.add_argument(
        "--channel",
        choices=["stable", "beta"],
        default="stable",
        help="Release channel (default: stable)",
    )
    p_update.add_argument(
        "--dry-run", action="store_true", help="Download and preview the installer plan only"
    )
    p_update.add_argument(
        "--version", dest="update_version", metavar="VERSION", help="Install this exact version"
    )

    p_rollback = sub.add_parser("rollback", help="Activate the retained previous version")
    p_rollback.add_argument("--json", action="store_true", help="Emit stable JSON")

    p_sessions = sub.add_parser("sessions", help="List, export, or delete saved sessions")
    p_sessions.add_argument(
        "sessions_action",
        nargs="?",
        choices=["list", "export", "delete"],
        default="list",
    )
    p_sessions.add_argument("thread_id", nargs="?", metavar="THREAD")
    p_sessions.add_argument("--output", metavar="FILE", help="Export destination")
    p_sessions.add_argument("--json", action="store_true", help="Emit a JSON session list")
    p_sessions.add_argument(
        "--yes", action="store_true", help="Confirm deletion without an interactive prompt"
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
    p_trust.add_argument("--json", action="store_true", help="Emit the trust list as JSON")
    sub.add_parser(
        "trust-hooks",
        help="Record a one-time accept to run global lifecycle hooks "
        "(enables `hook_global_require_trust: true`)",
    )
    sub.add_parser(
        "login",
        help="Log in to OpenRouter via OAuth PKCE — opens your browser, "
        "catches the callback, and stores the API key in the OS keychain",
    )

    def add_auth_actions(parent: argparse.ArgumentParser, *, dest: str) -> None:
        actions = parent.add_subparsers(dest=dest, required=True)

        def add_timeout(command: argparse.ArgumentParser) -> None:
            command.add_argument(
                "--timeout",
                type=_auth_timeout_arg,
                metavar="SECONDS",
                help=(
                    "Bound each Codex auth wait to 1-900 seconds "
                    "(default: $JARN_AUTH_TIMEOUT_SECONDS or 120)"
                ),
            )

        login = actions.add_parser("login", help="Sign in with your ChatGPT subscription")
        add_timeout(login)
        method = login.add_mutually_exclusive_group()
        method.add_argument(
            "--device",
            action="store_true",
            help="Use a device code (best for SSH/headless hosts; selected automatically there)",
        )
        method.add_argument(
            "--browser",
            action="store_true",
            help="Force browser/loopback login on this host",
        )
        login.add_argument("--json", action="store_true", help="Emit JSONL challenge + status")
        login.add_argument(
            "--yes",
            action="store_true",
            help="Install/update the official standalone Codex dependency without prompting",
        )
        status = actions.add_parser("status", help="Verify auth mode, account, and dependency")
        add_timeout(status)
        status.add_argument("--json", action="store_true", help="Emit the stable JSON status")
        status.add_argument(
            "--refresh",
            action="store_true",
            help="Force Codex to refresh the managed ChatGPT token",
        )
        logout = actions.add_parser("logout", help="Remove only Codex-managed credentials")
        add_timeout(logout)
        logout.add_argument("--json", action="store_true", help="Emit the stable JSON status")
        repair = actions.add_parser(
            "repair", help="Recheck the Codex dependency and refresh ChatGPT auth"
        )
        add_timeout(repair)
        repair.add_argument("--json", action="store_true", help="Emit the stable JSON status")
        repair.add_argument(
            "--yes",
            action="store_true",
            help="Install/update the official standalone Codex dependency without prompting",
        )

    p_auth = sub.add_parser("auth", help="Sign in, verify, repair, or sign out of ChatGPT")
    add_auth_actions(p_auth, dest="auth_action")

    p_codex = sub.add_parser(
        "codex",
        help="Compatibility alias for `jarn auth`",
    )
    add_auth_actions(p_codex, dest="codex_action")

    p_uninstall = sub.add_parser(
        "uninstall",
        help="Remove selected J.A.R.N. components while retaining user data by default",
    )
    p_uninstall.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation; without category flags, remove only the executable",
    )
    for category, description in (
        ("executable", "managed command, rollback binary, and install record"),
        ("dependencies", "isolated runtimes owned exclusively by J.A.R.N."),
        ("config", "global configuration, extensions, and trust data"),
        ("sessions", "sessions, transcripts, memory, wiki, and history"),
        ("cache", "regenerable caches, logs, runtime files, and telemetry"),
        ("credentials", "J.A.R.N. secret files and keychain entries"),
    ):
        p_uninstall.add_argument(
            f"--{category}",
            dest=f"uninstall_{category}",
            action="store_true",
            help=f"Remove only the {description}",
        )

    p_bug = sub.add_parser(
        "bug",
        help=(
            "Write a privacy-scanned local report and, with consent, open a "
            "content-free GitHub issue template"
        ),
    )
    p_bug.add_argument(
        "--dry-run",
        action="store_true",
        help="Write the report file without opening the browser",
    )

    p_completions = sub.add_parser(
        "completions",
        help="Emit a shell completion script for bash, zsh, or fish",
    )
    p_completions.add_argument(
        "shell",
        choices=["bash", "zsh", "fish"],
        help="Target shell",
    )

    p_gateway = sub.add_parser(
        "gateway",
        help=("Set up, inspect, or run the Telegram gateway"),
    )
    p_gateway.add_argument(
        "gateway_action",
        nargs="?",
        choices=[
            "run",
            "setup",
            "status",
            "install-service",
            "start",
            "stop",
            "restart",
        ],
        default="run",
        help="Action to perform (default: run)",
    )
    p_gateway.add_argument(
        "--fake-backend",
        action="store_true",
        help=(
            "Dry-run with InMemoryGatewayBackend (no daemon workers). "
            "Also set by JARN_TELEGRAM_FAKE_BACKEND=1."
        ),
    )
    p_gateway.add_argument(
        "--token-stdin",
        action="store_true",
        help="Read the bot token from stdin (setup only; never place it in argv)",
    )
    p_gateway.add_argument(
        "--allowed-user",
        type=int,
        action="append",
        default=[],
        metavar="ID",
        help="Allow a numeric Telegram user ID (setup only; repeatable)",
    )
    p_gateway.add_argument(
        "--no-service",
        action="store_true",
        help="Configure Telegram without offering a systemd user service",
    )
    p_gateway.add_argument(
        "--yes",
        action="store_true",
        help="Accept setup save/service confirmations",
    )
    p_gateway.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing Telegram bot/allowlist during setup",
    )
    p_gateway.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        metavar="SECONDS",
        help="Bounded wait for a new /start message during setup (default: 120)",
    )

    return parser


@contextlib.contextmanager
def _shutdown_background_on_sigterm() -> Iterator[None]:
    """Clean up detached jobs before the default SIGTERM process exit."""
    previous = signal.getsignal(signal.SIGTERM)

    def _handle_sigterm(signum, _frame) -> None:
        from jarn.agent.background import shutdown

        shutdown()
        raise SystemExit(128 + signum)

    try:
        signal.signal(signal.SIGTERM, _handle_sigterm)
    except (OSError, ValueError):
        # Signal handlers can only be installed from the main thread.
        yield
        return
    try:
        yield
    finally:
        with contextlib.suppress(OSError, ValueError):
            signal.signal(signal.SIGTERM, previous)


def main(argv: list[str] | None = None) -> int:
    _configure_safe_stdio()
    with _shutdown_background_on_sigterm():
        try:
            return _main(argv)
        except KeyboardInterrupt:
            from jarn.errors import ErrorCode, error_detail
            from jarn.exit_codes import EXIT_CANCELLED

            print(
                error_detail(
                    ErrorCode.CANCELLED,
                    "Operation cancelled.",
                    cause="The user interrupted the active command.",
                    component="command line",
                    retryable=True,
                    action="Run the command again when ready; no success was recorded.",
                ).render(),
                file=sys.stderr,
            )
            return EXIT_CANCELLED
        except Exception as exc:  # noqa: BLE001 - final user-facing CLI boundary
            return _render_unhandled_cli_error(exc)


def _render_unhandled_cli_error(exc: Exception) -> int:
    """Render a controlled top-level failure and keep tracebacks in the log."""

    import logging
    import traceback

    from jarn.config.loader import ConfigError
    from jarn.config.secrets import redact_secrets
    from jarn.errors import ErrorCode, JarnUserError, error_detail
    from jarn.exit_codes import (
        EXIT_AUTH,
        EXIT_BUDGET_EXCEEDED,
        EXIT_INTERNAL,
        EXIT_MODEL_UNAVAILABLE,
        EXIT_NETWORK_PROVIDER,
        EXIT_PERMISSION_DENIED,
        EXIT_TIMEOUT,
        EXIT_UPDATE_FAILED,
        EXIT_USAGE_CONFIG,
        EXIT_VERIFICATION_FAILED,
    )

    if isinstance(exc, JarnUserError):
        detail = exc.detail
    elif isinstance(exc, ConfigError):
        detail = error_detail(
            ErrorCode.CONFIG_INVALID_SCHEMA,
            "Configuration could not be loaded.",
            cause=str(exc),
            component="configuration",
            retryable=False,
            action="Run `jarn config validate`, then `jarn doctor --fix --dry-run`.",
        )
    elif isinstance(exc, TimeoutError):
        detail = error_detail(
            ErrorCode.NETWORK_FAILED,
            "The operation timed out.",
            cause=str(exc) or "A bounded operation exceeded its deadline.",
            component="command line",
            retryable=True,
            action="Check network/provider status and retry; use `jarn doctor --network`.",
        )
    else:
        # A fresh process has no handler yet; ``logger.exception`` would invoke
        # logging.lastResort and dump a traceback to the terminal before the
        # controlled error below. Configure the file-only sink best-effort. Log
        # a pre-redacted traceback as text because formatter-level ``exc_info``
        # is appended after ordinary record filters run.
        logger = logging.getLogger("jarn")
        try:
            from jarn.observability.logging import setup_logging

            logger = setup_logging()
        except (OSError, RuntimeError):
            pass
        if logger.handlers:
            rendered_trace = redact_secrets(
                "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            )
            # Logging is diagnostic, never a second failure boundary. A disk
            # filling up or a delayed handler error must not replace the stable
            # terminal anatomy with logging's own traceback.
            previous_raise_exceptions = logging.raiseExceptions
            logging.raiseExceptions = False
            try:
                with contextlib.suppress(Exception):
                    logger.error("Unhandled CLI failure\n%s", rendered_trace)
            finally:
                logging.raiseExceptions = previous_raise_exceptions
        detail = error_detail(
            ErrorCode.INTERNAL,
            "J.A.R.N. could not complete the command.",
            cause=str(exc) or type(exc).__name__,
            component="command line",
            retryable=False,
            action="Run `jarn doctor --report jarn-support-report.json` and inspect the log.",
        )

    print(detail.render(), file=sys.stderr)
    prefix_exit = {
        "JARN-CONFIG-": EXIT_USAGE_CONFIG,
        "JARN-CLI-": EXIT_USAGE_CONFIG,
        "JARN-AUTH-": EXIT_AUTH,
        "JARN-MODEL-": EXIT_MODEL_UNAVAILABLE,
        "JARN-SAFE-": EXIT_PERMISSION_DENIED,
        "JARN-NET-": EXIT_NETWORK_PROVIDER,
        "JARN-UPDATE-": EXIT_UPDATE_FAILED,
        "JARN-BUDGET-": EXIT_BUDGET_EXCEEDED,
        "JARN-VERIFY-": EXIT_VERIFICATION_FAILED,
    }
    if isinstance(exc, TimeoutError):
        return EXIT_TIMEOUT
    return next(
        (exit_code for prefix, exit_code in prefix_exit.items() if detail.code.startswith(prefix)),
        EXIT_INTERNAL,
    )


def _main(argv: list[str] | None = None) -> int:
    parser = build_parser()

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

    # Before anything touches the global tier. Whichever subsystem gets there
    # first (prompt history, the log file, the session index) would otherwise
    # create ~/.jarn at the process umask; this also repairs installs that were
    # created that way before the mode was pinned. Best-effort and never raises.
    # Imported here, like every other `paths` use in this module, to keep it off
    # the cold-start path for `jarn --version`.
    from jarn.config import paths as _paths

    # Doctor is explicitly a read-only diagnostic unless its allowlisted repair
    # executor is asked to apply a plan. Its collector must be able to report a
    # permissive/missing home without repairing or creating it before the plan is
    # shown. ``doctor --fix`` performs that chmod through the transactional repair
    # service, so the universal startup hardening is skipped for every doctor mode.
    if args.command != "doctor":
        _paths.ensure_global_home()

    preset_override = args.preset

    # --output-schema is headless-only: error if given without -p.
    if args.headless_output_schema is not None and args.headless_prompt is None:
        parser.error("--output-schema requires -p / --print")

    # Resolve the effective output format. --json is a legacy alias for
    # --output-format json; the two may co-occur only when they agree, otherwise
    # it is a usage error (explicit conflicting intent).
    output_format = args.headless_output_format
    if args.json:
        if output_format is not None and output_format != "json":
            parser.error(
                f"--json conflicts with --output-format {output_format} "
                "(--json is an alias for --output-format json)"
            )
        output_format = "json"
    if output_format is None:
        output_format = "text"

    # Headless one-shot: dispatch before any TUI setup.
    if args.headless_prompt is not None:
        return _cmd_headless(
            prompt_arg=args.headless_prompt,
            output_format=output_format,
            model_override=args.headless_model,
            permission_mode_override=args.headless_permission_mode,
            max_turns=args.headless_max_turns,
            cwd_override=args.headless_cwd,
            profile_override=preset_override,
            resume_session=args.headless_resume_session,
            output_schema=args.headless_output_schema,
            add_dirs=args.add_dir,
            ignore_project_config=args.headless_ignore_project_config,
        )

    # Gateway is a long-running daemon — skip TUI key-protocol fixes.
    if args.command == "gateway":
        return _cmd_gateway(
            action=str(args.gateway_action),
            fake_backend=bool(args.fake_backend),
            token_stdin=bool(args.token_stdin),
            allowed_users=list(args.allowed_user),
            no_service=bool(args.no_service),
            assume_yes=bool(args.yes),
            force=bool(args.force),
            timeout_seconds=float(args.timeout),
        )

    # ChatGPT auth is a terminal/browser ceremony and does not need TUI key-protocol
    # setup. Dispatch it early like the gateway daemon.
    if args.command in ("auth", "codex"):
        action = args.auth_action if args.command == "auth" else args.codex_action
        return _cmd_auth(
            action=action,
            device=bool(getattr(args, "device", False)),
            browser=bool(getattr(args, "browser", False)),
            as_json=bool(getattr(args, "json", False)),
            refresh=bool(getattr(args, "refresh", False)),
            yes=bool(getattr(args, "yes", False)),
            timeout_seconds=getattr(args, "timeout", None),
        )

    # Administrative commands do not initialise terminal key protocols or any
    # model/TUI stack. This keeps automation fast and safe on headless hosts.
    if args.command == "config":
        return _cmd_config(
            action=args.config_action,
            project=bool(args.project),
            as_json=bool(args.json),
            yes=bool(getattr(args, "yes", False)),
            editor=getattr(args, "editor", None),
        )
    if args.command == "telemetry":
        return _cmd_telemetry(action=args.telemetry_action, as_json=bool(args.json))
    if args.command == "update":
        return _cmd_update(
            check_only=bool(args.check),
            as_json=bool(args.json),
            channel=args.channel,
            dry_run=bool(args.dry_run),
            version=args.update_version,
        )
    if args.command == "rollback":
        return _cmd_rollback(as_json=bool(args.json))
    if args.command == "doctor":
        if args.fix or args.dry_run or args.report is not None or args.network:
            return _cmd_doctor(
                as_json=bool(args.json),
                fix=bool(args.fix),
                dry_run=bool(args.dry_run),
                report=args.report,
                network=bool(args.network),
            )
        # Keep the historical call shape for embedders that monkeypatch or call
        # the one-argument helper directly.
        return _cmd_doctor(as_json=bool(args.json))
    if args.command == "uninstall":
        selected = {
            category
            for category in (
                "executable",
                "dependencies",
                "config",
                "sessions",
                "cache",
                "credentials",
            )
            if bool(getattr(args, f"uninstall_{category}", False))
        }
        return _cmd_uninstall(yes=bool(args.yes), categories=selected or None)

    # Fix the macOS Caps Lock language-switch stray-character bug before any TUI
    # (app / wizard / key inspector) starts its terminal driver.
    from jarn.tui.keyfix import apply_kitty_keyfix

    apply_kitty_keyfix()

    if args.command == "setup":
        return _cmd_setup(force=args.force)
    if args.command == "init":
        return _cmd_init(force=args.force)
    if args.command == "sessions":
        return _cmd_sessions(
            action=args.sessions_action,
            thread_id=args.thread_id,
            output=args.output,
            as_json=args.json,
            yes=args.yes,
        )
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
    if args.command == "login":
        return _cmd_login()
    if args.command == "bug":
        return _cmd_bug(dry_run=args.dry_run)
    if args.command == "completions":
        return _cmd_completions(shell=args.shell, parser=parser)
    return _cmd_launch(
        resume=args.resume,
        profile_override=preset_override,
        add_dirs=args.add_dir,
        ignore_project_config=args.headless_ignore_project_config,
    )


def _cmd_headless(
    *,
    prompt_arg: str,
    output_format: str = "text",
    model_override: str | None = None,
    permission_mode_override: str | None = None,
    max_turns: int = 1,
    cwd_override: str | None = None,
    profile_override: str | None = None,
    resume_session: str | None = None,
    output_schema: str | None = None,
    add_dirs: list[str] | None = None,
    ignore_project_config: bool = False,
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

    def _fail(kind: str, message: str) -> int:
        """Emit a pre-run failure in the caller's output format.

        A failure before the agent starts still owes the wire its terminal
        record. A bare ``return 1`` leaves ``--output-format stream-json`` with
        zero bytes on stdout and ``--json`` with no envelope, so a consumer
        written against the documented contract hangs or raises on an empty
        parse instead of reading a ``run_error``. Text output is unchanged.
        """
        from jarn.exit_codes import (
            EXIT_AUTH,
            EXIT_INTERNAL,
            EXIT_MODEL_UNAVAILABLE,
            EXIT_NETWORK_PROVIDER,
            EXIT_PERMISSION_DENIED,
            EXIT_TIMEOUT,
            EXIT_USAGE_CONFIG,
        )
        from jarn.headless import HeadlessFailure, _emit_headless_failure

        exit_code = {
            "usage": EXIT_USAGE_CONFIG,
            "config": EXIT_USAGE_CONFIG,
            "auth": EXIT_AUTH,
            "model": EXIT_MODEL_UNAVAILABLE,
            "permission": EXIT_PERMISSION_DENIED,
            "network": EXIT_NETWORK_PROVIDER,
            "timeout": EXIT_TIMEOUT,
        }.get(kind, EXIT_INTERNAL)
        return _emit_headless_failure(
            HeadlessFailure(kind, message, exit_code=exit_code),
            output_format=output_format,
        )

    # Resolve working directory (used as the project root).
    root = Path(cwd_override).expanduser().resolve() if cwd_override else Path.cwd()

    # Read the prompt (a literal '-' means stdin).
    if prompt_arg == "-":
        try:
            prompt = sys.stdin.read()
        except (EOFError, KeyboardInterrupt):
            return _fail("usage", "could not read prompt from stdin")
    else:
        prompt = prompt_arg

    prompt = prompt.strip()
    if not prompt and not resume_session:
        return _fail("usage", "prompt is empty")

    from jarn.config import paths
    from jarn.config.loader import ConfigError, load_config
    from jarn.config.schema import PermissionMode
    from jarn.observability import configure_tracing, setup_logging

    if not paths.global_config_path().is_file():
        return _fail("config", "no configuration found — run `jarn setup` first.")

    # Use the same trust logic as the interactive launch. The project tier is
    # read once and passed forward so the fingerprinted content is exactly what
    # gets loaded (no TOCTOU between the trust decision and the load).
    try:
        if ignore_project_config:
            # CI can work on an untrusted checkout without either auto-trusting its
            # hooks/MCP/providers or being clamped to plan mode by the trust prompt.
            #
            # The project tier is dropped by passing an explicit empty dict. Do NOT
            # express this as project_root=None: None means "discover the root", not
            # "no project", so load_config re-finds this very checkout and reads its
            # .jarn/config.yaml — and with project_trusted=True it merged the repo's
            # hooks / mcp_servers / providers unsanitised, the exact opposite of what
            # this flag promises. project_trusted=False is belt-and-braces: should the
            # tier ever become non-empty again, the dangerous keys are still stripped.
            project_raw: dict[str, Any] = {}
            cfg = load_config(project_raw=project_raw, project_trusted=False)
            # Nothing untrusted was loaded, so there is nothing to clamp to plan mode.
            trusted = True
        else:
            trusted, project_raw, trust_err = _resolve_project_trust(root)
            if trust_err is not None:
                return _fail("config", str(trust_err))
            cfg = load_config(
                project_root=root,
                project_trusted=trusted,
                project_raw=project_raw,
            )
    except ConfigError as exc:
        return _fail("config", str(exc))
    setup_logging(cfg.observability.log_level)
    configure_tracing(cfg.observability)

    # Validate --add-dir grants up front (fail fast — don't run with a promised
    # root that isn't there). Same validation as the interactive launch.
    extra_roots, add_dir_err = _validate_add_dirs(add_dirs)
    if add_dir_err is not None:
        return _fail("usage", str(add_dir_err))

    # T-3-3 (item G): in -p mode the diagnostics NOTICE is dropped, but ruff/pyright
    # would still spend up to 30s per edit-turn. Gate the whole feature off so a
    # headless run never pays that latency tax for output nobody consumes.
    cfg.verify.diagnostics = "off"

    # Apply CLI overrides.
    if model_override:
        cfg.routing.main = model_override
        cfg.default_model = model_override

    # Expand the effective preset (CLI > config) and clamp untrusted. A preset
    # sets the trust-relevant knobs including the mode, but an EXPLICIT
    # --permission-mode must win over the preset's default mode (explicit >
    # preset > config default) — so it is threaded into the resolver rather than
    # set before and stomped by apply_profile. The preset still governs the other
    # knobs (sandbox/network/web).
    from jarn.config.profiles import resolve_effective_profile

    try:
        resolve_effective_profile(
            cfg,
            project_trusted=trusted,
            cli_profile=profile_override,
            cli_permission_mode=permission_mode_override,
        )
    except ConfigError as exc:
        return _fail("usage", str(exc))

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

        from jarn.headless import HeadlessFailure, _emit_headless_failure

        schema_path = Path(output_schema)
        try:
            schema_dict = _json.loads(schema_path.read_bytes())
        except (OSError, ValueError) as exc:
            failure = HeadlessFailure(
                "usage",
                f"--output-schema: cannot read/parse {schema_path}: {exc}",
                exit_code=2,
            )
            return _emit_headless_failure(failure, output_format=output_format)
        response_format = {"type": "json_schema", "schema": schema_dict}

    from jarn.headless import run_headless

    return run_headless(
        prompt,
        cfg,
        root,
        project_trusted=trusted,
        output_format=output_format,
        max_turns=max_turns,
        resume_session=resume_session,
        response_format=response_format,
        add_dirs=extra_roots,
    )


def _cmd_setup(*, force: bool = False) -> int:
    result, failure_exit = _run_setup_checked(force=force)
    if failure_exit is not None:
        return failure_exit
    assert result is not None
    _warm_tokenizer_for_setup()
    return 0


def _run_setup_checked(*, force: bool = False) -> tuple[Path | None, int | None]:
    """Run setup while preserving cancellation/auth/model/config exit taxonomy."""

    from jarn.onboarding import run_setup_tui
    from jarn.onboarding.outcome import SetupCommandError, SetupFailureKind

    try:
        result = run_setup_tui(force=force, propagate_errors=True)
    except SetupCommandError as exc:
        print(exc.detail.render(), file=sys.stderr)
        return None, exc.exit_code
    if result is None:
        # Compatibility for embedders or older wizard implementations that
        # still return ``None`` instead of propagating the typed cancellation.
        fallback_error = SetupCommandError(
            "Setup exited before readiness was verified.",
            kind=SetupFailureKind.CANCELLED,
        )
        print(fallback_error.detail.render(), file=sys.stderr)
        return None, fallback_error.exit_code
    return result, None


def _warm_tokenizer_for_setup() -> None:
    """Populate tiktoken's persistent cache while network use is expected."""
    from jarn.memory.tokens import warm_tokenizer_cache

    print("Caching tokenizer data…", end=" ", flush=True)
    if warm_tokenizer_cache():
        print("done.")
    else:
        print("skipped (unavailable; token estimates will be used).")


def _cmd_init(*, force: bool) -> int:
    from jarn.memory import write_jarn_md

    try:
        path = write_jarn_md(overwrite=force)
    except FileExistsError as exc:
        return _emit_cli_failure(
            "JARN-CLI-001",
            "Project context already exists.",
            cause=str(exc),
            component="project initialization",
            action="Review the existing file; use `jarn init --force` only to replace it.",
            exit_code=2,
        )
    print(f"Created {path}")
    return 0


def _emit_admin_error(exc: Exception, *, as_json: bool, exit_code: int) -> int:
    """Render one stable user-facing error in human or machine form."""
    import json

    from jarn.config.secrets import redact_secrets
    from jarn.errors import ErrorCode, JarnUserError, error_detail

    if isinstance(exc, JarnUserError):
        detail = exc.detail
    else:
        detail = error_detail(
            ErrorCode.INTERNAL,
            "The command could not be completed.",
            cause=redact_secrets(str(exc)),
            component="command line",
            retryable=False,
            action="Run `jarn doctor --report` and retry after reviewing the report.",
        )
    if as_json:
        print(
            json.dumps(
                {"schemaVersion": 1, "ok": False, "error": detail.to_dict()},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        print(detail.render(), file=sys.stderr)
    return exit_code


def _emit_cli_failure(
    code: str,
    summary: str,
    *,
    cause: str,
    component: str,
    action: str,
    exit_code: int,
    retryable: bool = False,
    as_json: bool = False,
) -> int:
    """Build and render the complete stable anatomy at legacy CLI boundaries."""

    from jarn.errors import JarnUserError, error_detail

    return _emit_admin_error(
        JarnUserError(
            error_detail(
                code,
                summary,
                cause=cause,
                component=component,
                retryable=retryable,
                action=action,
            )
        ),
        as_json=as_json,
        exit_code=exit_code,
    )


def _error_detail_from_mapping(
    value: dict[str, Any],
    *,
    fallback_code: str,
    fallback_summary: str,
    fallback_cause: str,
    fallback_component: str,
    fallback_action: str,
) -> Any:
    """Normalize a service error mapping through the central redaction boundary."""
    from jarn.errors import error_detail

    return error_detail(
        str(value.get("code") or fallback_code),
        str(value.get("summary") or fallback_summary),
        cause=str(value.get("cause") or fallback_cause),
        component=str(value.get("component") or fallback_component),
        retryable=bool(value.get("retryable", False)),
        action=str(value.get("action") or fallback_action),
        log_path=value.get("log_path"),
        report_path=value.get("report_path"),
        details=value.get("details"),
    )


def _config_user_error(
    summary: str,
    *,
    cause: str,
    action: str,
    path: Path | None = None,
    write: bool = False,
) -> Exception:
    from jarn.errors import ErrorCode, JarnUserError, error_detail

    return JarnUserError(
        error_detail(
            ErrorCode.CONFIG_WRITE_FAILED if write else ErrorCode.CONFIG_INVALID_SCHEMA,
            summary,
            cause=cause,
            component="configuration",
            retryable=write,
            action=action,
            details={"path": str(path)} if path is not None else None,
        )
    )


def _config_target(*, project: bool) -> Path:
    from jarn.config import paths

    if not project:
        return paths.global_config_path()
    root = paths.find_project_root() or Path.cwd()
    return root / paths.PROJECT_DIR_NAME / paths.CONFIG_FILENAME


def _read_config_mapping(path: Path) -> dict[str, Any]:
    from ruamel.yaml import YAML
    from ruamel.yaml.error import YAMLError

    try:
        loaded = YAML().load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, YAMLError) as exc:
        raise _config_user_error(
            "Configuration could not be parsed.",
            cause=str(exc),
            action="Keep the file unchanged; fix its YAML or restore a .bak file.",
            path=path,
        ) from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise _config_user_error(
            "Configuration root must be a mapping.",
            cause=f"The top-level value is {type(loaded).__name__}.",
            action="Use YAML key/value settings at the top level.",
            path=path,
        )
    return loaded


def _validated_config_mapping(data: dict[str, Any], path: Path) -> dict[str, Any]:
    import warnings

    from pydantic import ValidationError

    from jarn.config.pydantic_schema import (
        ConfigValidationError,
        migrate_config,
        parse_config_model,
        safe_config_validation_message,
    )

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            migrated = migrate_config(data)
            parse_config_model(migrated)
    except (ConfigValidationError, ValidationError) as exc:
        raise _config_user_error(
            "Configuration failed schema validation.",
            cause=safe_config_validation_message(exc, raw=data),
            action="Correct the reported setting; the active file was not changed.",
            path=path,
        ) from exc
    return migrated


def _render_config_mapping(data: dict[str, Any]) -> str:
    import io

    from ruamel.yaml import YAML

    buffer = io.StringIO()
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.dump(data, buffer)
    return buffer.getvalue()


def _commit_config_mapping(
    path: Path,
    data: dict[str, Any],
    *,
    expected_source: bytes | None,
) -> Path | None:
    """Commit a validated mapping with an exact-source race guard and backup."""
    from jarn.config.yaml_store import rotate_backup
    from jarn.util.atomic import atomic_write_text, file_lock

    if path.is_symlink() or path.parent.is_symlink():
        raise _config_user_error(
            "Refusing to write configuration through a symbolic link.",
            cause="The destination identity could change during activation.",
            action="Use a regular config file in a directory you control.",
            path=path,
            write=True,
        )
    text = _render_config_mapping(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path) as locked:
        if not locked:
            raise _config_user_error(
                "Configuration write lock is unavailable.",
                cause="Another filesystem policy prevented safe serialization.",
                action="Check directory ownership and retry; no file was changed.",
                path=path,
                write=True,
            )
        current = path.read_bytes() if path.is_file() else None
        if current != expected_source:
            raise _config_user_error(
                "Configuration changed while the command was running.",
                cause="The source bytes no longer match the version that was reviewed.",
                action="Re-run the command against the current file; no overwrite occurred.",
                path=path,
                write=True,
            )
        backup = rotate_backup(path)
        try:
            atomic_write_text(path, text, mode=0o600)
            installed = _read_config_mapping(path)
            _validated_config_mapping(installed, path)
            if path.read_text(encoding="utf-8") != text:
                raise OSError("installed configuration bytes differ from the candidate")
        except Exception as exc:
            rollback_error: Exception | None = None
            try:
                active = path.read_bytes() if path.is_file() else None
                if active != expected_source:
                    if expected_source is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic_write_text(
                            path,
                            expected_source.decode("utf-8"),
                            mode=0o600,
                        )
            except Exception as rollback_exc:  # noqa: BLE001 - integrity boundary
                rollback_error = rollback_exc
            if rollback_error is not None:
                raise _config_user_error(
                    "Configuration activation and automatic rollback both failed.",
                    cause=f"activation: {exc}; rollback: {rollback_error}",
                    action=(f"Stop and restore {backup or path} before using this configuration."),
                    path=path,
                    write=True,
                ) from rollback_error
            raise _config_user_error(
                "Configuration could not be activated.",
                cause=str(exc),
                action="The previous file was restored; inspect its .bak and retry.",
                path=path,
                write=True,
            ) from exc
    return backup


def _template_config_mapping(*, project: bool, path: Path) -> dict[str, Any]:
    from ruamel.yaml import YAML

    from jarn.config.defaults import global_config_template, project_config_template

    template = project_config_template() if project else global_config_template()
    loaded = YAML().load(template) or {}
    if not isinstance(loaded, dict):  # pragma: no cover - shipped template invariant
        raise _config_user_error(
            "The shipped configuration template is invalid.",
            cause="The embedded YAML root is not a mapping.",
            action="Reinstall J.A.R.N. and run `jarn doctor --report`.",
            path=path,
        )
    return _validated_config_mapping(loaded, path)


def _config_reset_preview(
    path: Path,
    *,
    source: bytes | None,
    project: bool,
) -> dict[str, Any]:
    """Describe reset impact by category without rendering config values."""

    groups = (
        (
            "provider profiles, credential references, and model routing",
            {"default_profile", "default_model", "providers", "routing"},
        ),
        (
            "permissions, trust-related policy, and execution settings",
            {"permission_mode", "permissions", "policy", "execution"},
        ),
        (
            "budgets, context, verification, pricing, and search settings",
            {"budget", "context", "verify", "pricing", "search"},
        ),
        (
            "hooks, MCP servers, async subagents, gateway, and extensions",
            {"hooks", "mcp_servers", "async_subagents", "gateway", "extensions"},
        ),
        (
            "UI, observability, updates, git, wiki, plan, and compatibility settings",
            {"ui", "observability", "updates", "git", "wiki", "plan", "compat"},
        ),
    )
    sections: list[str] = []
    if source is None:
        sections.append("new default configuration (no existing file)")
    else:
        try:
            keys = set(_read_config_mapping(path))
        except Exception:  # noqa: BLE001 - corrupt YAML still needs a safe preview
            sections.append("all bytes in the existing unparseable configuration")
        else:
            covered: set[str] = {"config_version", "strict_secrets"}
            for label, members in groups:
                covered.update(members)
                if keys & members:
                    sections.append(label)
            unknown_count = len(keys - covered)
            if unknown_count:
                sections.append(f"custom or unknown top-level settings ({unknown_count})")
            if not sections:
                sections.append("the existing empty/default configuration")
    return {
        "scope": "project" if project else "global",
        "target": str(path),
        "operation": "replace" if source is not None else "create",
        "backup": source is not None,
        "replacedCategories": sections,
        "preservedCategories": [
            "credentials stored outside config",
            "sessions and transcripts",
            "memory, skills, commands, and caches",
        ],
    }


def _print_config_reset_preview(preview: dict[str, Any]) -> None:
    print("Configuration reset preview:")
    print(f"  Target: {preview['target']} ({preview['scope']})")
    print(f"  Operation: {preview['operation']} default template")
    for category in preview["replacedCategories"]:
        print(f"  Replace: {category}")
    for category in preview["preservedCategories"]:
        print(f"  Preserve: {category}")
    if preview["backup"]:
        print("  Recovery: create a byte-for-byte backup before activation")


def _config_diagnostic_payload(path: Path) -> dict[str, Any]:
    from jarn.config.migrations import diagnose_config_file
    from jarn.errors import ErrorCode, error_detail

    diagnostic = diagnose_config_file(path)
    valid = diagnostic.status in {"current", "migration-required"}
    error = diagnostic.error
    if not valid and error is None:
        error = error_detail(
            ErrorCode.CONFIG_INVALID_SCHEMA,
            "Configuration file does not exist.",
            cause=f"No regular file was found at {path}.",
            component="configuration",
            retryable=False,
            action="Run `jarn setup` or `jarn config reset`.",
            details={"path": str(path)},
        ).to_dict()
    return {
        "schemaVersion": 1,
        "ok": valid,
        "path": str(path),
        "status": diagnostic.status,
        "sourceVersion": diagnostic.source_version,
        "targetVersion": diagnostic.target_version,
        "message": diagnostic.message,
        "recoveryActions": list(diagnostic.recovery_actions),
        "backups": [str(item) for item in diagnostic.backup_paths],
        "error": error,
    }


def _config_provenance_payload(
    path: Path,
    data: dict[str, Any],
    *,
    project: bool,
) -> dict[str, Any]:
    """Describe the source layer of displayed values without resolving secrets.

    ``config show`` intentionally renders one selected source file, not a silently
    merged runtime config.  This companion makes that boundary explicit and gives
    automation a per-field source map while documenting the later layers that can
    affect an invocation.
    """

    source = "project_config" if project else "global_config"
    values: dict[str, str] = {}

    def visit(value: Any, prefix: str) -> None:
        if isinstance(value, dict):
            if not value and prefix:
                values[prefix] = source
            for key, item in value.items():
                child = f"{prefix}.{key}" if prefix else str(key)
                visit(item, child)
            return
        if isinstance(value, list):
            values[prefix] = source
            return
        values[prefix] = source

    visit(data, "")
    values.pop("", None)
    return {
        "schemaVersion": 1,
        "selectedScope": "project" if project else "global",
        "selectedPath": str(path),
        "displayedValues": dict(sorted(values.items())),
        "runtimeLayers": [
            {
                "source": "built_in_default",
                "effect": "supplies typed defaults for values absent from configuration",
            },
            {
                "source": "global_config",
                "effect": "machine/user defaults loaded before a trusted project tier",
            },
            {
                "source": "project_config",
                "effect": "trusted project values merge after global values; unsafe keys are trust-gated",
            },
            {
                "source": "environment",
                "effect": "resolves credential references at use time; the reference remains displayed",
            },
            {
                "source": "cli_flag",
                "effect": "per-command model, mode, preset, cwd, and related overrides apply after loading",
            },
            {
                "source": "managed_policy",
                "effect": "preset and workspace-trust policy may clamp the effective permission mode",
            },
        ],
        "note": (
            "This command shows one source file. Use `jarn doctor --json` and "
            "interactive `/status` for effective runtime readiness."
        ),
    }


def _print_config_provenance(payload: dict[str, Any]) -> None:
    print("\n# Provenance")
    print(f"# Displayed values: {payload['selectedScope']} config ({payload['selectedPath']})")
    for layer in payload["runtimeLayers"]:
        print(f"# {layer['source']}: {layer['effect']}")
    print(f"# {payload['note']}")


def _cmd_config(
    *,
    action: str,
    project: bool = False,
    as_json: bool = False,
    yes: bool = False,
    editor: str | None = None,
) -> int:
    """Implement safe top-level configuration inspection and editing."""
    import json
    import os
    import shlex
    import subprocess
    import tempfile

    from jarn.config.secrets import redact_structure
    from jarn.errors import JarnUserError
    from jarn.exit_codes import EXIT_CANCELLED, EXIT_SUCCESS, EXIT_USAGE_CONFIG

    path = _config_target(project=project)
    try:
        if action == "path":
            if as_json:
                print(
                    json.dumps(
                        {
                            "schemaVersion": 1,
                            "ok": True,
                            "scope": "project" if project else "global",
                            "path": str(path),
                            "exists": path.is_file(),
                        },
                        sort_keys=True,
                    )
                )
            else:
                print(path)
            return EXIT_SUCCESS

        if action == "validate":
            payload = _config_diagnostic_payload(path)
            if as_json:
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            elif payload["ok"]:
                print(f"Valid configuration: {path} ({payload['message']}).")
                if payload["status"] == "migration-required":
                    print("Preview migration with `jarn doctor --fix --dry-run`.")
            else:
                error = payload.get("error") or {}
                if error:
                    detail = _error_detail_from_mapping(
                        error,
                        fallback_code="JARN-CONFIG-002",
                        fallback_summary=str(payload["message"]),
                        fallback_cause="Configuration validation failed.",
                        fallback_component="configuration",
                        fallback_action="Restore a valid backup.",
                    )
                    print(detail.render(), file=sys.stderr)
                else:
                    raise AssertionError("invalid configuration diagnostic omitted its error")
            return EXIT_SUCCESS if payload["ok"] else EXIT_USAGE_CONFIG

        if action == "show":
            if not path.is_file():
                raise _config_user_error(
                    "Configuration file does not exist.",
                    cause=f"No regular file was found at {path}.",
                    action="Run `jarn setup` or `jarn config reset` first.",
                    path=path,
                )
            raw = _read_config_mapping(path)
            safe = redact_structure(raw)
            provenance = _config_provenance_payload(path, raw, project=project)
            if as_json:
                print(
                    json.dumps(
                        {
                            "schemaVersion": 1,
                            "ok": True,
                            "path": str(path),
                            "config": safe,
                            "provenance": provenance,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
            else:
                print(_render_config_mapping(safe), end="")
                _print_config_provenance(provenance)
            return EXIT_SUCCESS

        source = path.read_bytes() if path.is_file() else None
        if action == "reset":
            preview = _config_reset_preview(path, source=source, project=project)
            if not as_json:
                _print_config_reset_preview(preview)
            if source is not None and not yes:
                if as_json:
                    raise _config_user_error(
                        "Reset requires explicit confirmation in JSON mode.",
                        cause="Interactive prompts would corrupt machine-readable output.",
                        action="Re-run with `jarn config reset --yes --json`.",
                        path=path,
                    )
                try:
                    answer = input(f"Back up and reset {path}? [y/N]: ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    from jarn.errors import ErrorCode, error_detail

                    print(
                        error_detail(
                            ErrorCode.CANCELLED,
                            "Configuration reset was cancelled.",
                            cause=(
                                "Confirmation input was interrupted or unavailable; "
                                "the active configuration was not changed."
                            ),
                            component="configuration reset",
                            retryable=True,
                            action=(
                                "Re-run `jarn config reset` when an interactive "
                                "confirmation is available."
                            ),
                        ).render(),
                        file=sys.stderr,
                    )
                    return EXIT_CANCELLED
                if answer not in {"y", "yes"}:
                    from jarn.errors import ErrorCode, error_detail

                    print(
                        error_detail(
                            ErrorCode.CANCELLED,
                            "Configuration reset was cancelled.",
                            cause="The user declined the itemized reset preview.",
                            component="configuration reset",
                            retryable=True,
                            action=(
                                "Re-run `jarn config reset` only when the listed "
                                "replacement is intended."
                            ),
                        ).render(),
                        file=sys.stderr,
                    )
                    return EXIT_CANCELLED
            data = _template_config_mapping(project=project, path=path)
            backup = _commit_config_mapping(path, data, expected_source=source)
            payload = {
                "schemaVersion": 1,
                "ok": True,
                "changed": True,
                "path": str(path),
                "backup": str(backup) if backup else None,
                "preview": preview,
            }
            if as_json:
                print(json.dumps(payload, sort_keys=True))
            else:
                suffix = f" Backup: {backup}." if backup else ""
                print(f"Reset configuration at {path}.{suffix}")
            return EXIT_SUCCESS

        if action != "edit":  # pragma: no cover - argparse constrains choices
            raise _config_user_error(
                "Unknown configuration action.",
                cause=action,
                action="Run `jarn config --help`.",
                path=path,
            )

        command = editor or os.environ.get("VISUAL") or os.environ.get("EDITOR")
        if not command:
            raise _config_user_error(
                "No editor is configured.",
                cause="Neither --editor, $VISUAL, nor $EDITOR supplied a command.",
                action="Set EDITOR (for example `export EDITOR='nano'`) and retry.",
                path=path,
            )
        try:
            editor_argv = shlex.split(command, posix=os.name != "nt")
        except ValueError as exc:
            raise _config_user_error(
                "Editor command could not be parsed.",
                cause=str(exc),
                action="Pass a valid executable and arguments with --editor.",
                path=path,
            ) from exc
        if not editor_argv:
            raise _config_user_error(
                "Editor command is empty.",
                cause="No executable remained after parsing the command.",
                action="Set EDITOR to an executable name and retry.",
                path=path,
            )

        path.parent.mkdir(parents=True, exist_ok=True)
        seed = (
            source
            if source is not None
            else _render_config_mapping(
                _template_config_mapping(project=project, path=path)
            ).encode("utf-8")
        )
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.edit.", dir=path.parent)
        temp_path = Path(temp_name)
        try:
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(seed)
                handle.flush()
                os.fsync(handle.fileno())
            completed = subprocess.run(  # noqa: S603 - explicit user-selected editor argv
                [*editor_argv, str(temp_path)],
                check=False,
                env=external_command_env(),
            )
            if completed.returncode != 0:
                raise _config_user_error(
                    "Editor exited without saving configuration.",
                    cause=f"The editor returned exit code {completed.returncode}.",
                    action="Fix the editor command and retry; the active file was not changed.",
                    path=path,
                    write=True,
                )
            edited_bytes = temp_path.read_bytes()
            if source is not None and edited_bytes == source:
                print(f"No configuration changes: {path}")
                return EXIT_SUCCESS
            edited = _validated_config_mapping(_read_config_mapping(temp_path), temp_path)
            backup = _commit_config_mapping(path, edited, expected_source=source)
        finally:
            temp_path.unlink(missing_ok=True)
        suffix = f" Backup: {backup}." if backup else ""
        print(f"Updated configuration at {path}.{suffix}")
        return EXIT_SUCCESS
    except JarnUserError as exc:
        return _emit_admin_error(exc, as_json=as_json, exit_code=EXIT_USAGE_CONFIG)
    except (OSError, ValueError) as exc:
        wrapped = _config_user_error(
            "Configuration command failed safely.",
            cause=str(exc),
            action="The active file was not intentionally overwritten; inspect permissions and retry.",
            path=path,
            write=action in {"edit", "reset"},
        )
        return _emit_admin_error(wrapped, as_json=as_json, exit_code=EXIT_USAGE_CONFIG)


def _set_config_boolean(path: Path, dotted_key: str, value: bool) -> tuple[bool, Path | None]:
    if not path.is_file():
        raise _config_user_error(
            "Global configuration does not exist.",
            cause=f"No regular file was found at {path}.",
            action="Run `jarn setup` before changing telemetry.",
            path=path,
        )
    source = path.read_bytes()
    data = _validated_config_mapping(_read_config_mapping(path), path)
    node: dict[str, Any] = data
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    if node.get(parts[-1]) is value:
        return False, None
    node[parts[-1]] = value
    validated = _validated_config_mapping(data, path)
    return True, _commit_config_mapping(path, validated, expected_source=source)


def _cmd_telemetry(*, action: str, as_json: bool = False) -> int:
    """Inspect or change the explicit local-telemetry opt-in."""
    import json
    import stat

    from jarn.config import paths
    from jarn.errors import JarnUserError, error_detail
    from jarn.exit_codes import EXIT_INTERNAL, EXIT_SUCCESS, EXIT_USAGE_CONFIG
    from jarn.observability.telemetry import Telemetry

    config_path = paths.global_config_path()
    try:
        changed = False
        backup: Path | None = None
        if action in {"on", "off"}:
            changed, backup = _set_config_boolean(
                config_path,
                "observability.telemetry",
                action == "on",
            )

        enabled = False
        if config_path.is_file():
            data = _read_config_mapping(config_path)
            observability = data.get("observability")
            if isinstance(observability, dict):
                enabled = observability.get("telemetry") is True
        sink_path = paths.global_home() / "telemetry.jsonl"
        telemetry = Telemetry(
            enabled=enabled,
            sink_path=sink_path,
        )
        # ``Path.is_file()`` deliberately returns False for some inaccessible
        # paths. The recorder's summary treats a genuinely missing sink as
        # healthy, so distinguish that from a path whose identity cannot be
        # inspected (or is a symlink/non-regular entry) before reading it or
        # declaring success.
        sink_problem = ""
        try:
            sink_entry = sink_path.lstat()
        except FileNotFoundError:
            sink_entry = None
        except OSError as exc:
            sink_problem = str(exc) or "telemetry sink metadata is unreadable"
        else:
            assert sink_entry is not None
            if stat.S_ISLNK(sink_entry.st_mode):
                sink_problem = "refusing to inspect a symbolic-link telemetry sink"
            elif not stat.S_ISREG(sink_entry.st_mode):
                sink_problem = "the telemetry sink is not a regular file"
        if sink_problem:
            # Build the ordinary empty-sink shape without touching the unsafe
            # entry, then carry its real path and failure into the public status.
            summary = Telemetry(enabled=enabled, sink_path=None).status_summary()
            summary["path"] = str(sink_path)
            summary["health"] = "degraded"
            summary["last_error"] = sink_problem
        else:
            summary = telemetry.status_summary()
        health = str(summary.get("health", "degraded"))
        failure = None
        warning: dict[str, Any] | None = None
        if health == "corrupt":
            count = int(summary.get("corrupt_record_count", 0) or 0)
            lines = summary.get("corrupt_record_lines")
            line_hint = (
                f" at line(s) {', '.join(str(line) for line in lines)}"
                if isinstance(lines, list) and lines
                else ""
            )
            repairability = (
                "The malformed final record is eligible for automatic repair on "
                "a later telemetry append."
                if summary.get("repairable_final_record")
                else "Non-final corruption is never removed automatically."
            )
            failure = error_detail(
                "JARN-TELEMETRY-001",
                "The local telemetry sink is corrupt.",
                cause=f"Detected {count} malformed record(s){line_hint}. {repairability}",
                component="local telemetry sink",
                retryable=True,
                action=(
                    "Run `jarn telemetry off`, preserve the reported sink if it is needed "
                    "for inspection, move or delete only that sink, then run "
                    "`jarn telemetry on` when ready."
                ),
            )
        elif health == "degraded":
            failure = error_detail(
                "JARN-TELEMETRY-001",
                "The local telemetry sink could not be verified.",
                cause=str(summary.get("last_error") or "The sink could not be read safely."),
                component="local telemetry sink",
                retryable=True,
                action=(
                    "Correct ownership/read permissions on the reported sink, or run "
                    "`jarn telemetry off`; then retry `jarn telemetry status`."
                ),
            )
        elif health == "recovered" or summary.get("recovery_performed"):
            warning = {
                "code": "JARN-TELEMETRY-002",
                "summary": "The local telemetry sink recovered from a partial final record.",
                "detail": str(
                    summary.get("recovery_message")
                    or "The repair completed and no corruption remains."
                ),
            }
        payload = {
            "schemaVersion": 1,
            "ok": failure is None,
            "changed": changed,
            "backup": str(backup) if backup else None,
            **summary,
        }
        if failure is not None:
            payload["error"] = failure.to_dict()
        if warning is not None:
            payload["warning"] = warning
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            state = "enabled" if enabled else "disabled"
            print(f"Telemetry: {state} (local only; no network upload).")
            print(f"Sink: {summary['path']}")
            print(f"Events: {summary['valid_event_count']} valid; health: {summary['health']}")
            if failure is not None:
                print(failure.render(), file=sys.stderr)
            elif warning is not None:
                print(
                    f"{warning['code']}: {warning['summary']} {warning['detail']}",
                    file=sys.stderr,
                )
            if changed:
                print(f"Configuration updated at {config_path}.")
                if backup:
                    print(f"Backup: {backup}")
            elif action in {"on", "off"}:
                print("No change; telemetry already had the requested setting.")
        return EXIT_INTERNAL if failure is not None else EXIT_SUCCESS
    except JarnUserError as exc:
        return _emit_admin_error(exc, as_json=as_json, exit_code=EXIT_USAGE_CONFIG)


def _cmd_update(
    *,
    check_only: bool = False,
    as_json: bool = False,
    channel: str = "stable",
    dry_run: bool = False,
    version: str | None = None,
) -> int:
    """Dispatch the transactional updater with stable CLI exit mapping."""
    import io
    import json
    from contextlib import redirect_stderr, redirect_stdout

    from jarn.config.secrets import redact_secrets
    from jarn.errors import ErrorCode, JarnUserError, error_detail
    from jarn.exit_codes import EXIT_SUCCESS, EXIT_UPDATE_FAILED, EXIT_USAGE_CONFIG
    from jarn.update import run_update

    if check_only and (dry_run or version is not None):
        exc = JarnUserError(
            error_detail(
                ErrorCode.CLI_USAGE,
                "Update options conflict.",
                cause="--check cannot be combined with --dry-run or --version.",
                component="updater",
                retryable=False,
                action="Use --check alone, or remove it to preview/install a version.",
            )
        )
        return _emit_admin_error(exc, as_json=as_json, exit_code=EXIT_USAGE_CONFIG)
    captured_stdout = io.StringIO()
    captured_stderr = io.StringIO()
    try:
        if as_json:
            with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
                result = run_update(
                    channel=channel,
                    check_only=check_only,
                    as_json=True,
                    dry_run=dry_run,
                    version=version,
                )
        else:
            # Keep progress on stdout live, but replace legacy one-line errors
            # with the stable blocking-error anatomy below.
            with redirect_stderr(captured_stderr):
                result = run_update(
                    channel=channel,
                    check_only=check_only,
                    as_json=False,
                    dry_run=dry_run,
                    version=version,
                )
    except Exception as exc:  # noqa: BLE001 - updater boundary
        detail = JarnUserError(
            error_detail(
                ErrorCode.UPDATE_FAILED,
                "Update failed before activation.",
                cause=str(exc),
                component="updater",
                retryable=True,
                action="The active version was preserved; run `jarn doctor --report`.",
            )
        )
        return _emit_admin_error(detail, as_json=as_json, exit_code=EXIT_UPDATE_FAILED)
    if result in {EXIT_SUCCESS, 10}:
        if as_json:
            print(captured_stdout.getvalue(), end="")
        elif captured_stderr.getvalue():
            print(captured_stderr.getvalue(), end="", file=sys.stderr)
        return int(result)
    raw_cause = captured_stderr.getvalue().strip()
    updater_payload: dict[str, Any] | None = None
    if as_json:
        raw_json = captured_stdout.getvalue().strip()
        try:
            parsed = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError):
            raw_cause = raw_cause or raw_json
        else:
            updater_payload = parsed if isinstance(parsed, dict) else None
            error = parsed.get("error") if isinstance(parsed, dict) else None
            if isinstance(error, dict):
                raw_cause = str(error.get("message") or error.get("summary") or raw_cause)
            elif error:
                raw_cause = str(error)
    detail = JarnUserError(
        error_detail(
            ErrorCode.UPDATE_FAILED,
            "Update did not complete.",
            cause=redact_secrets(raw_cause or f"The updater exited with status {result}."),
            component="updater",
            retryable=True,
            action="The active version was preserved; run `jarn doctor --report` and retry.",
        )
    )
    preview = updater_payload.get("preview") if updater_payload is not None else None
    if as_json and isinstance(preview, dict):
        # Preserve the updater's bounded, redacted ownership/release/config
        # preview while retaining the CLI's stable public error anatomy.
        print(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "ok": False,
                    "changed": False,
                    "error": detail.detail.to_dict(),
                    "preview": preview,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return EXIT_UPDATE_FAILED
    return _emit_admin_error(detail, as_json=as_json, exit_code=EXIT_UPDATE_FAILED)


def _cmd_rollback(*, as_json: bool = False) -> int:
    """Dispatch explicit rollback with the update-failure exit class."""
    import io
    import json
    from contextlib import redirect_stderr, redirect_stdout

    from jarn.config.secrets import redact_secrets
    from jarn.errors import ErrorCode, JarnUserError, error_detail
    from jarn.exit_codes import EXIT_SUCCESS, EXIT_UPDATE_FAILED
    from jarn.update import run_rollback

    captured_stdout = io.StringIO()
    captured_stderr = io.StringIO()
    try:
        if as_json:
            with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
                result = run_rollback(as_json=True)
        else:
            with redirect_stderr(captured_stderr):
                result = run_rollback(as_json=False)
    except Exception as exc:  # noqa: BLE001 - rollback boundary
        detail = JarnUserError(
            error_detail(
                ErrorCode.UPDATE_FAILED,
                "Rollback failed safely.",
                cause=str(exc),
                component="updater",
                retryable=False,
                action="The active version was preserved; inspect `jarn doctor --report`.",
            )
        )
        return _emit_admin_error(detail, as_json=as_json, exit_code=EXIT_UPDATE_FAILED)
    if result == 0:
        if as_json:
            print(captured_stdout.getvalue(), end="")
        elif captured_stderr.getvalue():
            print(captured_stderr.getvalue(), end="", file=sys.stderr)
        return EXIT_SUCCESS

    raw_cause = captured_stderr.getvalue().strip()
    if as_json:
        raw_json = captured_stdout.getvalue().strip()
        try:
            parsed = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError):
            raw_cause = raw_cause or raw_json
        else:
            error = parsed.get("error") if isinstance(parsed, dict) else None
            if isinstance(error, dict):
                raw_cause = str(error.get("message") or error.get("summary") or raw_cause)
            elif error:
                raw_cause = str(error)
    detail = JarnUserError(
        error_detail(
            ErrorCode.UPDATE_FAILED,
            "Rollback did not complete.",
            cause=redact_secrets(raw_cause or f"The rollback command exited with status {result}."),
            component="updater",
            retryable=False,
            action="The active version was preserved; inspect `jarn doctor --report`.",
        )
    )
    return _emit_admin_error(detail, as_json=as_json, exit_code=EXIT_UPDATE_FAILED)


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


def _cmd_doctor(
    *,
    as_json: bool = False,
    fix: bool = False,
    dry_run: bool = False,
    report: str | None = None,
    network: bool = False,
) -> int:
    """Run the shared offline-first doctor, optional repairs, and support report."""
    import json

    from rich.console import Console

    from jarn.doctor.render import render_doctor_console
    from jarn.doctor.service import run_doctor_service
    from jarn.errors import JarnUserError
    from jarn.exit_codes import EXIT_INTERNAL

    report_path = Path(report).expanduser() if report is not None else None
    try:
        result = run_doctor_service(
            network=network,
            fix=fix or dry_run,
            dry_run=dry_run,
            report_path=report_path,
        )
    except JarnUserError as exc:
        return _emit_admin_error(exc, as_json=as_json, exit_code=EXIT_INTERNAL)
    except Exception as exc:  # noqa: BLE001 - terminal boundary needs stable anatomy
        return _emit_admin_error(exc, as_json=as_json, exit_code=EXIT_INTERNAL)

    if as_json:
        # Preserve historical top-level diagnostic keys for automation while
        # adding the versioned repair/report outcome alongside them.
        payload = {
            **result.diagnostics,
            "schemaVersion": 1,
            "ok": result.ok,
            "exit_code": result.exit_code,
            "repair_plan": result.repair_plan.to_dict(),
            "repair_result": (
                result.repair_result.to_dict() if result.repair_result is not None else None
            ),
            "report_path": str(result.report_path) if result.report_path else None,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return result.exit_code

    console = Console()
    render_doctor_console(console, result.diagnostics)
    if result.repair_result is not None:
        mode = "preview" if result.repair_result.dry_run else "result"
        console.print(f"\n{layout.strong(f'Repair {mode}')}")
        for item in result.repair_result.applied:
            console.print(
                f"  {item.get('status', 'planned')}: "
                f"{item.get('description') or item.get('id') or 'repair'}",
                markup=False,
            )
        for reason in result.repair_result.skipped:
            console.print(f"  skipped: {reason}", style=palette.C_WARN, markup=False)
        if result.repair_result.error:
            detail = _error_detail_from_mapping(
                result.repair_result.error,
                fallback_code="JARN-DOCTOR-001",
                fallback_summary="Doctor repair failed.",
                fallback_cause="The repair batch was rolled back.",
                fallback_component="doctor repair",
                fallback_action="Review the doctor report and retry.",
            )
            console.print(detail.render(), markup=False)
    if result.report_path is not None:
        console.print(f"\nSupport report: {result.report_path}", markup=False)
    return result.exit_code


def _cmd_sessions(
    *,
    action: str,
    thread_id: str | None,
    output: str | None,
    as_json: bool,
    yes: bool,
) -> int:
    """Manage session metadata and redacted transcript exports."""
    import json

    from jarn.memory.sessions import SessionIndex, UnsafeSessionExportError

    index = SessionIndex()
    if action == "list":
        sessions = index.list()
        if as_json:
            print(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "sessions": [
                            {
                                "threadId": item.thread_id,
                                "title": item.title,
                                "updatedAt": item.updated_at,
                                "project": item.project_root or None,
                                "model": item.model or None,
                                "state": item.state,
                            }
                            for item in sessions
                        ],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            return 0
        if not sessions:
            print("No saved sessions.")
            return 0
        for item in sessions:
            print(
                f"{item.thread_id}  {item.updated_human}  {item.state}  "
                f"{item.project_root or '-'}  {item.model or '-'}  {item.title}"
            )
        return 0

    if not thread_id:
        return _emit_cli_failure(
            "JARN-CLI-001",
            "A session identifier is required.",
            cause=f"`jarn sessions {action}` was called without THREAD.",
            component="session lifecycle",
            action="Run `jarn sessions list`, then pass the exact thread id.",
            exit_code=2,
            as_json=as_json,
        )

    if action == "export":
        target = (
            Path(output).expanduser() if output else Path.cwd() / f"jarn-session-{thread_id}.jsonl"
        )
        try:
            exported = index.export(thread_id, target)
        except UnsafeSessionExportError as exc:
            return _emit_cli_failure(
                "JARN-SAFE-001",
                "The session export path is unsafe.",
                cause=str(exc),
                component="session export",
                action=(
                    "Choose a new regular file whose parent directories are not "
                    "symbolic links, then retry. No linked target was changed."
                ),
                exit_code=5,
                as_json=as_json,
            )
        except (KeyError, FileNotFoundError) as exc:
            return _emit_cli_failure(
                "JARN-CLI-001",
                "The requested session could not be found.",
                cause=str(exc),
                component="session export",
                action="Run `jarn sessions list` and retry with an existing thread id.",
                exit_code=2,
                as_json=as_json,
            )
        except OSError as exc:
            return _emit_cli_failure(
                "JARN-INTERNAL-001",
                "The session export could not be written.",
                cause=str(exc),
                component="session export",
                action="Check destination permissions/free space, then retry the export.",
                exit_code=1,
                retryable=True,
                as_json=as_json,
            )
        print(f"Exported redacted session to {exported}")
        return 0

    session = index.get(thread_id)
    if session is None:
        return _emit_cli_failure(
            "JARN-CLI-001",
            "The requested session could not be found.",
            cause=f"No indexed session matches {thread_id!r}.",
            component="session lifecycle",
            action="Run `jarn sessions list` and retry with an existing thread id.",
            exit_code=2,
            as_json=as_json,
        )
    if not yes:
        try:
            answer = (
                input(
                    f"Delete session {thread_id} ({session.title!r}), its checkpoints, "
                    "and transcript? [y/N]: "
                )
                .strip()
                .lower()
            )
        except (EOFError, KeyboardInterrupt):
            return _emit_cli_failure(
                "JARN-CLI-002",
                "Session deletion was cancelled.",
                cause="The confirmation prompt was interrupted.",
                component="session lifecycle",
                action="No session was deleted; rerun only when ready.",
                exit_code=130,
                retryable=True,
                as_json=as_json,
            )
        if answer not in ("y", "yes"):
            return _emit_cli_failure(
                "JARN-CLI-002",
                "Session deletion was cancelled.",
                cause="The destructive confirmation was declined.",
                component="session lifecycle",
                action="No session was deleted; rerun only when intended.",
                exit_code=130,
                retryable=True,
                as_json=as_json,
            )
    if not index.delete(thread_id):
        return _emit_cli_failure(
            "JARN-INTERNAL-001",
            "The session could not be removed.",
            cause=f"Session storage refused deletion of {thread_id!r}.",
            component="session lifecycle",
            action="Run `jarn doctor --report`, check data permissions, and retry.",
            exit_code=1,
            retryable=True,
            as_json=as_json,
        )
    print(f"Deleted session {thread_id}. Other sessions and configuration were preserved.")
    return 0


def _cmd_trust(*, path: str | None, remove: bool, as_json: bool = False) -> int:
    """List, trust, or untrust project roots in the shared trust store."""
    from jarn.config.trust import (
        TrustStore,
        commit_trust_if_unchanged,
        parse_project_config,
        project_config_bytes,
    )

    try:
        store = TrustStore.load()
    except (OSError, ValueError) as exc:
        return _emit_cli_failure(
            "JARN-CONFIG-001",
            "The trust store could not be loaded.",
            cause=str(exc),
            component="project trust",
            action="Run `jarn doctor --report`, repair the trust store, then retry.",
            exit_code=2,
            as_json=as_json,
        )

    if path is None:
        return _trust_list(store, as_json=as_json)

    try:
        root = Path(path).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        return _emit_cli_failure(
            "JARN-CLI-001",
            "The project path could not be resolved.",
            cause=str(exc),
            component="project trust",
            action="Pass an existing project directory and retry.",
            exit_code=2,
            as_json=as_json,
        )

    if remove:
        removed = store.untrust(root)
        if not removed:
            return _emit_cli_failure(
                "JARN-CLI-001",
                "The project is not in the trust store.",
                cause=f"No trust record exists for {root}.",
                component="project trust",
                action="Run `jarn trust` to list recorded project roots.",
                exit_code=2,
                as_json=as_json,
            )
        try:
            store.save()
        except OSError as exc:
            return _emit_cli_failure(
                "JARN-CONFIG-005",
                "The trust store could not be updated.",
                cause=str(exc),
                component="project trust",
                action="Check trust-store ownership/free space, then retry.",
                exit_code=2,
                retryable=True,
                as_json=as_json,
            )
        print(f"Untrusted {root}")
        return 0

    # Read the project config once: fingerprint the exact on-disk bytes, then
    # re-verify they haven't changed before recording trust (TOCTOU guard).
    raw_bytes = project_config_bytes(root)
    if raw_bytes is None:
        return _emit_cli_failure(
            "JARN-CLI-001",
            "No project configuration is available to trust.",
            cause=f"{root}/.jarn/config.yaml does not exist or is unreadable.",
            component="project trust",
            action="Create/review the project config, then retry `jarn trust`.",
            exit_code=2,
            as_json=as_json,
        )
    try:
        parsed = parse_project_config(raw_bytes, root)
    except (OSError, ValueError) as exc:
        return _emit_cli_failure(
            "JARN-CONFIG-002",
            "The project configuration cannot be trusted yet.",
            cause=str(exc),
            component="project trust",
            action="Validate and review .jarn/config.yaml before recording trust.",
            exit_code=2,
            as_json=as_json,
        )
    err = commit_trust_if_unchanged(store, root, raw_bytes, parsed)
    if err is not None:
        return _emit_cli_failure(
            "JARN-CONFIG-005",
            "Project trust was not recorded.",
            cause=err,
            component="project trust",
            action="Re-read the changed project config and retry only after review.",
            exit_code=2,
            retryable=True,
            as_json=as_json,
        )
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


def _cmd_bug(*, dry_run: bool = False) -> int:
    """Write a scanned local report and open a content-free issue only with consent."""
    from jarn.bug_report import run_bug_report

    return run_bug_report(dry_run=dry_run)


def _cmd_login() -> int:
    """Run the OpenRouter OAuth PKCE login flow.

    Opens the browser, waits for the OAuth callback on a loopback listener,
    exchanges the code for an API key, and stores it in the OS keychain.
    The raw key is never printed; only the masked tail and the reference are shown.
    Falls back gracefully when the browser cannot be opened (SSH / headless).
    """
    from rich.console import Console

    from jarn.config.secrets import redact_secrets
    from jarn.onboarding.oauth import LoginResult, login_openrouter

    console = Console()
    console.print(
        f"\n{layout.accent('OpenRouter login', bold=True)}  "
        f"{layout.muted('— Opens your browser; close it here with Ctrl+C to abort.')}\n"
    )

    try:
        result: LoginResult = login_openrouter()
    except TimeoutError as exc:
        return _emit_cli_failure(
            "JARN-NET-001",
            "OpenRouter login timed out.",
            cause=str(exc),
            component="OpenRouter authentication",
            action="Retry `jarn login`, or configure an environment/keychain reference in setup.",
            exit_code=124,
            retryable=True,
        )
    except KeyboardInterrupt:
        return _emit_cli_failure(
            "JARN-CLI-002",
            "OpenRouter login was cancelled.",
            cause="The browser login ceremony was interrupted.",
            component="OpenRouter authentication",
            action="No successful login was recorded; rerun `jarn login` when ready.",
            exit_code=130,
            retryable=True,
        )
    except Exception as exc:  # noqa: BLE001
        return _emit_cli_failure(
            "JARN-AUTH-010",
            "OpenRouter login failed.",
            cause=redact_secrets(str(exc)),
            component="OpenRouter authentication",
            action="Check browser/network access, then retry or use `jarn setup` with a key reference.",
            exit_code=3,
            retryable=True,
        )

    if not result.changed:
        # Existing key kept — nothing to persist; don't rewrite the config.
        console.print(f"{layout.ok(grammar.GLYPH_OK)}  Keeping your existing key ({layout.strong(result.reference)}).")
        console.print(f"   Key tail: {layout.muted(result.masked_key)}")
        return 0

    console.print(f"{layout.ok(grammar.GLYPH_OK)}  Logged in — key stored as {layout.strong(result.reference)}")
    console.print(f"   Key tail: {layout.muted(result.masked_key)}")

    # Write the reference into the OpenRouter provider in the global config.
    if _write_openrouter_key_ref(result.reference):
        console.print(f"\n{layout.ok(grammar.GLYPH_OK)}  Config updated.  Launch {layout.strong('jarn')} to start coding.")
        return 0
    console.print(
        f"\n{layout.warn('!')}  The key is stored ({layout.strong(result.reference)}) but the "
        "config was left untouched — set `providers.openrouter.api_key` manually."
    )
    return _emit_cli_failure(
        "JARN-CONFIG-005",
        "OpenRouter login succeeded but configuration activation did not.",
        cause="The credential reference was stored, but the active config was not safely updated.",
        component="OpenRouter configuration",
        action="Set providers.openrouter.api_key to the shown reference or run `jarn config edit`.",
        exit_code=2,
        retryable=True,
    )


def _cmd_auth(
    *,
    action: str,
    device: bool = False,
    browser: bool = False,
    as_json: bool = False,
    refresh: bool = False,
    yes: bool = False,
    timeout_seconds: float | None = None,
) -> int:
    """Manage ChatGPT auth through the direct Codex app-server protocol."""
    import json

    from rich.console import Console
    from rich.prompt import Confirm

    from jarn.auth import (
        CODEX_OFFICIAL_INSTALL_COMMAND,
        AuthServiceError,
        AuthState,
        CodexAuthService,
        CodexDependencyInstaller,
        CodexDependencyInstallError,
        DependencyState,
        detect_login_method,
        login_interactive,
    )
    from jarn.config.secrets import redact_secrets
    from jarn.errors import ErrorCode, error_detail
    from jarn.exit_codes import EXIT_AUTH, EXIT_CANCELLED

    console = Console()
    service = (
        CodexAuthService()
        if timeout_seconds is None
        else CodexAuthService(timeout_seconds=timeout_seconds)
    )

    def announce_wait(message: str) -> None:
        if not as_json:
            deadline = getattr(service, "timeout_seconds", timeout_seconds or 120.0)
            console.print(layout.muted(f"{message} (timeout {deadline:g}s)…"))

    def emit(status) -> None:
        if as_json:
            print(json.dumps(status.to_dict(), ensure_ascii=False))
            return
        if status.ready:
            console.print(
                f"{layout.ok(grammar.GLYPH_OK)} ChatGPT connected"
                f" ({layout.strong(status.plan_type or 'plan unknown')}); account verified."
            )
            return
        if status.error is not None:
            detail = _error_detail_from_mapping(
                status.error.to_dict(),
                fallback_code=ErrorCode.AUTH_FAILED.value,
                fallback_summary="ChatGPT authentication is not ready.",
                fallback_cause=status.state.value,
                fallback_component="authentication",
                fallback_action="Run `jarn auth repair`, then retry.",
            )
            console.print(detail.render(), markup=False)
            return
        detail = error_detail(
            ErrorCode.AUTH_SIGNED_OUT,
            "ChatGPT authentication is not ready.",
            cause=f"Authentication state is {status.state.value}.",
            component="authentication",
            retryable=status.state is AuthState.SIGNED_OUT,
            action="Run `jarn auth login`, then verify with `jarn auth status`.",
        )
        console.print(detail.render(), markup=False)

    def ensure_dependency(status):
        """Offer the reviewed standalone dependency and return a service using it."""

        nonlocal service
        if status.dependency.state not in (
            DependencyState.MISSING,
            DependencyState.INCOMPATIBLE,
        ):
            return True
        installer = CodexDependencyInstaller()
        plan = installer.resolve_plan()
        if as_json:
            print(
                json.dumps(
                    {
                        "type": "dependency_install_offer",
                        "required": True,
                        "accepted": bool(yes),
                        "plan": plan.to_dict(),
                        "manual_command": CODEX_OFFICIAL_INSTALL_COMMAND,
                    },
                    ensure_ascii=False,
                )
            )
        else:
            reason = (
                "not installed"
                if status.dependency.state is DependencyState.MISSING
                else f"incompatible ({status.dependency.version or 'version unknown'})"
            )
            console.print(f"\n{layout.warn('!')} OpenAI Codex CLI is {reason}.")
            console.print(f"{layout.field('Purpose')} ChatGPT subscription authentication and model access")
            console.print(f"{layout.field('Version/channel')} {plan.version} ({plan.channel})")
            console.print(f"{layout.field('Source')} {plan.source} — {plan.metadata_url}")
            console.print(f"{layout.field('Destination')} {plan.destination}")
            console.print(f"{layout.field('Verification')} official metadata + SHA-256 manifest")

        accepted = yes
        if not accepted and not as_json and sys.stdin.isatty():
            accepted = Confirm.ask("Install the official standalone Codex CLI now?", default=True)
        if not accepted:
            if as_json:
                print(
                    json.dumps(
                        {
                            "type": "dependency_install_declined",
                            "ok": False,
                            "manual_command": CODEX_OFFICIAL_INSTALL_COMMAND,
                        }
                    )
                )
            else:
                console.print(f"{layout.warn('Setup incomplete:')} Codex CLI was not changed.")
                console.print(f"Manual official command: {layout.strong(CODEX_OFFICIAL_INSTALL_COMMAND)}")
            return False

        try:
            result = installer.install(
                plan,
                on_progress=(
                    None
                    if as_json
                    else lambda stage: console.print(layout.muted(f"Codex dependency: {stage}…"))
                ),
            )
        except CodexDependencyInstallError as exc:
            if as_json:
                print(
                    json.dumps(
                        {
                            "type": "dependency_install_error",
                            "ok": False,
                            "stage": exc.stage,
                            "error": exc.detail,
                            "manual_command": CODEX_OFFICIAL_INSTALL_COMMAND,
                        },
                        ensure_ascii=False,
                    )
                )
            else:
                console.print(f"{layout.err('Setup incomplete:')} {exc}")
                console.print(f"Manual official command: {layout.strong(CODEX_OFFICIAL_INSTALL_COMMAND)}")
            return False
        if as_json:
            print(
                json.dumps(
                    {"type": "dependency_install_result", **result.to_dict()},
                    ensure_ascii=False,
                )
            )
        else:
            action = "Installed" if result.changed else "Verified"
            console.print(
                f"{layout.ok(grammar.GLYPH_OK)} {action} Codex CLI {result.smoke_version} at "
                f"{layout.strong(result.executable)}"
            )
        service = (
            CodexAuthService(command=result.executable)
            if timeout_seconds is None
            else CodexAuthService(
                command=result.executable,
                timeout_seconds=timeout_seconds,
            )
        )
        return True

    try:
        if action == "login":
            announce_wait("Checking the Codex dependency and current account")
            dependency_check = service.status(refresh=False)
            if not ensure_dependency(dependency_check):
                emit(dependency_check)
                return EXIT_AUTH
            if not as_json:
                console.print(
                    f"\n{layout.accent('ChatGPT subscription login', bold=True)}  "
                    + layout.muted(
                        "— credentials remain managed by Codex; J.A.R.N. never reads them."
                    )
                )
            method = detect_login_method(force_device=device, force_browser=browser)
            status = login_interactive(
                service,
                method=method,
                console=console,
                as_json=as_json,
            )
            emit(status)
            return 0 if status.ready else EXIT_AUTH

        if action == "logout":
            announce_wait("Signing out and verifying the Codex account")
            status = service.logout()
            if status.state is AuthState.SIGNED_OUT:
                if as_json:
                    emit(status)
                else:
                    console.print(f"{layout.ok(grammar.GLYPH_OK)} Codex-managed credentials removed.")
                return 0
            emit(status)
            return EXIT_AUTH

        announce_wait("Checking the Codex dependency and ChatGPT account")
        status = service.status(refresh=refresh or action == "repair")
        if action == "repair" and not ensure_dependency(status):
            emit(status)
            return EXIT_AUTH
        if action == "repair" and status.dependency.state in (
            DependencyState.MISSING,
            DependencyState.INCOMPATIBLE,
        ):
            status = service.status(refresh=True)
        emit(status)
        return 0 if status.ready else EXIT_AUTH
    except KeyboardInterrupt:
        detail = error_detail(
            ErrorCode.CANCELLED,
            "ChatGPT sign-in was cancelled.",
            cause="The login ceremony was interrupted before account verification.",
            component="authentication",
            retryable=True,
            action="Run `jarn auth login` again when ready.",
        )
        if as_json:
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "type": "auth_cancelled",
                        "cancelled": True,
                        "error": detail.to_dict(),
                    }
                )
            )
        else:
            console.print("\n" + detail.render(), style=palette.C_WARN, markup=False)
        return EXIT_CANCELLED
    except AuthServiceError as exc:
        emit(exc.status)
        return EXIT_AUTH
    except Exception as exc:  # noqa: BLE001 - terminal boundary must fail with a usable message
        detail = error_detail(
            ErrorCode.AUTH_PROTOCOL_ERROR,
            "ChatGPT authentication could not be completed.",
            cause=redact_secrets(str(exc)),
            component="authentication",
            retryable=True,
            action="Run `jarn auth status`, then `jarn auth repair` and retry.",
        )
        if as_json:
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "type": "auth_error",
                        "error": detail.to_dict(),
                    },
                    ensure_ascii=False,
                )
            )
        else:
            console.print(detail.render(), markup=False)
        return EXIT_AUTH


def _cmd_codex(
    *,
    action: str,
    device: bool = False,
    browser: bool = False,
    as_json: bool = False,
    refresh: bool = False,
    yes: bool = False,
    timeout_seconds: float | None = None,
) -> int:
    """Backward-compatible Python API for the historical ``jarn codex`` path."""

    return _cmd_auth(
        action=action,
        device=device,
        browser=browser,
        as_json=as_json,
        refresh=refresh,
        yes=yes,
        timeout_seconds=timeout_seconds,
    )


def _cmd_uninstall(*, yes: bool = False, categories: set[str] | None = None) -> int:
    """Remove global ~/.jarn state and OS keychain entries."""
    from jarn.uninstall import run_uninstall

    return run_uninstall(yes=yes, categories=categories)


def _write_openrouter_key_ref(reference: str) -> bool:
    """Set ``providers.openrouter.api_key`` in the global config (non-destructively).

    If no global config exists yet, creates a minimal one.  Existing keys for
    other providers are preserved.  Returns True when the config was written;
    returns False (after a stderr warning) when the existing config cannot be
    parsed — refusing to replace a whole config with a single key.
    """
    from jarn.config import paths
    from jarn.errors import JarnUserError

    config_path = paths.global_config_path()
    try:
        expected_source = config_path.read_bytes() if config_path.is_file() else None
        data = _read_config_mapping(config_path) if expected_source is not None else {}
        data = _validated_config_mapping(data, config_path)
        providers = data.setdefault("providers", {})
        if not isinstance(providers, dict):
            raise _config_user_error(
                "OpenRouter credential reference could not be added.",
                cause="providers must be a YAML mapping.",
                action="Correct the providers setting with `jarn config edit`, then retry.",
                path=config_path,
            )
        entry = providers.setdefault("openrouter", {"type": "openrouter"})
        if not isinstance(entry, dict):
            raise _config_user_error(
                "OpenRouter credential reference could not be added.",
                cause="providers.openrouter must be a YAML mapping.",
                action="Correct the OpenRouter provider with `jarn config edit`, then retry.",
                path=config_path,
            )
        entry["type"] = "openrouter"
        entry["api_key"] = reference
        data = _validated_config_mapping(data, config_path)
        _commit_config_mapping(
            config_path,
            data,
            expected_source=expected_source,
        )
    except (JarnUserError, OSError, UnicodeError) as exc:
        detail = exc.detail.summary if isinstance(exc, JarnUserError) else str(exc)
        print(
            "warning: could not safely update ~/.jarn/config.yaml; refusing to "
            f"overwrite it: {detail}",
            file=sys.stderr,
        )
        return False
    return True


def _trust_list(store: Any, *, as_json: bool) -> int:
    entries = store.entries()

    if as_json:
        import json

        print(json.dumps([{"root": root, "fingerprint": fp} for root, fp in entries.items()]))
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


def _cmd_completions(*, shell: str, parser: argparse.ArgumentParser) -> int:
    """Emit a shell completion script for the given shell."""
    from jarn.completions import emit_completions

    print(emit_completions(shell, parser))
    return 0


def _cmd_gateway(
    *,
    action: str = "run",
    fake_backend: bool = False,
    token_stdin: bool = False,
    allowed_users: list[int] | None = None,
    no_service: bool = False,
    assume_yes: bool = False,
    force: bool = False,
    timeout_seconds: float = 120.0,
) -> int:
    """Set up, inspect, control, or run the Telegram gateway."""

    setup_options_used = bool(
        token_stdin
        or allowed_users
        or no_service
        or assume_yes
        or force
        or timeout_seconds != 120.0
    )
    if action != "setup" and setup_options_used:
        return _emit_cli_failure(
            "JARN-CLI-001",
            "Telegram setup options require the setup action.",
            cause=f"One or more setup-only flags were used with `jarn gateway {action}`.",
            component="CLI arguments",
            action="Use `jarn gateway setup --help`, or remove the setup-only flags.",
            exit_code=2,
        )
    if action != "run" and fake_backend:
        return _emit_cli_failure(
            "JARN-CLI-001",
            "--fake-backend is available only when running the gateway.",
            cause=f"The flag was used with `jarn gateway {action}`.",
            component="CLI arguments",
            action="Run `jarn gateway --fake-backend`, or remove that flag.",
            exit_code=2,
        )

    if action == "setup":
        from jarn.telegram.setup import run_gateway_setup

        return run_gateway_setup(
            token_stdin=token_stdin,
            allowed_users=allowed_users,
            no_service=no_service,
            assume_yes=assume_yes,
            force=force,
            timeout_seconds=timeout_seconds,
        )
    if action == "status":
        from jarn.telegram.setup import run_gateway_status

        return run_gateway_status()
    if action == "install-service":
        from jarn.telegram.setup import run_gateway_service_install

        return run_gateway_service_install()
    if action in {"start", "stop", "restart"}:
        from jarn.telegram.setup import run_gateway_service_action

        return run_gateway_service_action(action)

    from jarn.telegram.cli import run_gateway_cli

    return run_gateway_cli(fake_backend=fake_backend)


def _cmd_launch(
    *,
    resume: bool = False,
    profile_override: str | None = None,
    add_dirs: list[str] | None = None,
    ignore_project_config: bool = False,
) -> int:
    from jarn.config import paths
    from jarn.config.loader import ConfigError, load_config
    from jarn.observability import configure_tracing, setup_logging

    extra_roots, add_dir_err = _validate_add_dirs(add_dirs)
    if add_dir_err is not None:
        return _emit_cli_failure(
            "JARN-CLI-001",
            "An additional workspace directory is invalid.",
            cause=add_dir_err,
            component="workspace launch",
            action="Pass an existing directory with `--add-dir` and retry.",
            exit_code=2,
        )

    if not paths.global_config_path().is_file():
        print("No configuration found. Running first-time setup...\n")
        _result, setup_exit = _run_setup_checked()
        if setup_exit is not None:
            return setup_exit
        if not paths.global_config_path().is_file():
            from jarn.onboarding.outcome import SetupCommandError, SetupFailureKind

            failure = SetupCommandError(
                "Setup returned without activating the verified configuration.",
                kind=SetupFailureKind.VERIFICATION,
            )
            print(failure.detail.render(), file=sys.stderr)
            return failure.exit_code
        _warm_tokenizer_for_setup()

    root = paths.find_project_root() or Path.cwd()

    try:
        if ignore_project_config:
            # Drop the project tier entirely — see the identical branch in
            # _cmd_headless for why this must not be spelled project_root=None.
            cfg = load_config(project_raw={}, project_trusted=False)
            # Nothing untrusted was loaded, so there is nothing to clamp to plan mode.
            trusted = True
        else:
            # Trust boundary: a project's .jarn/config.yaml can run hooks / spawn MCP
            # servers / override providers (secret exfil). Don't honour those keys from
            # an untrusted project until the user explicitly approves them. The project
            # tier is read once and passed forward so the fingerprinted content is
            # exactly what gets loaded (no TOCTOU between the trust decision and load).
            trusted, project_raw, trust_err = _resolve_project_trust(root)
            if trust_err is not None:
                return _emit_cli_failure(
                    "JARN-CONFIG-002",
                    "Project trust could not be established.",
                    cause=trust_err,
                    component="workspace launch",
                    action="Review the project config, then run `jarn trust` or use `--ignore-project-config`.",
                    exit_code=2,
                )

            cfg = load_config(
                project_root=root,
                project_trusted=trusted,
                project_raw=project_raw,
            )
    except ConfigError as exc:
        return _emit_cli_failure(
            "JARN-CONFIG-002",
            "Configuration could not be loaded.",
            cause=str(exc),
            component="workspace launch",
            action="Run `jarn config validate` and `jarn doctor`, then retry.",
            exit_code=2,
        )
    setup_logging(cfg.observability.log_level)
    configure_tracing(cfg.observability)

    # Apply the effective policy profile (CLI > config) and clamp untrusted.
    from jarn.config.profiles import resolve_effective_profile

    try:
        effective_preset = resolve_effective_profile(
            cfg, project_trusted=trusted, cli_profile=profile_override
        )
    except ConfigError as exc:
        return _emit_cli_failure(
            "JARN-CONFIG-002",
            "The selected permission/policy profile is invalid.",
            cause=str(exc),
            component="workspace launch",
            action="Correct the profile or permission mode, then retry `jarn`.",
            exit_code=2,
        )

    # A turn is marked incomplete before provider/tool work and complete only
    # after its terminal event. On the next interactive start, make a crash or
    # forced shutdown recoverable without requiring the user to remember
    # ``--resume``. Declining is a harmless one-time choice for this launch.
    if not resume and sys.stdin.isatty() and sys.stdout.isatty():
        from jarn.memory.sessions import SessionIndex, default_db_path

        interrupted = SessionIndex(default_db_path(root)).latest_incomplete()
        if interrupted is not None:
            try:
                answer = (
                    input(
                        f"Resume interrupted session {interrupted.thread_id[:8]} "
                        f"({interrupted.title!r})? [Y/n]: "
                    )
                    .strip()
                    .lower()
                )
            except (EOFError, KeyboardInterrupt):
                answer = "n"
            resume = answer not in ("n", "no")

    # The terminal front-end (native scrollback) is the only chat UI.
    from jarn.repl import run_inline

    return run_inline(
        cfg,
        root,
        resume=resume,
        project_trusted=trusted,
        add_dirs=extra_roots,
        preset_name=effective_preset,
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
    import sys

    from rich.console import Console

    console = Console(stderr=True)
    console.print(
        "\n"
        + layout.warn(f"{grammar.GLYPH_WARN} This project's config")
        + f" ({layout.muted(str(root) + '/.jarn/config.yaml')}) "
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
        console.print(layout.warn("These changed since you last trusted this project."))
    console.print(
        layout.muted(
            "Trust only repositories you would run code from. If you decline, "
            "these settings are ignored and the session continues safely."
        )
    )
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(
            "Project config was not trusted because no interactive terminal is "
            "available; use --ignore-project-config to skip it explicitly.",
            file=sys.stderr,
        )
        return False
    try:
        answer = input("Trust this project's config? [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


if __name__ == "__main__":
    raise SystemExit(main())
