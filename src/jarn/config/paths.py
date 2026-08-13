"""Filesystem locations for J.A.R.N. — the two-tier (global + project) layout.

Global config lives under ``~/.jarn`` (overridable with ``$JARN_HOME``).
Project config lives under ``<project-root>/.jarn`` where the project root is
the nearest ancestor of the current working directory that contains a ``.jarn``
directory, a ``JARN.md`` file, or a ``.git`` directory — in that order.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

import platformdirs

from jarn.util.process_env import external_command_env

#: Name of the per-project config directory committed alongside a repo.
PROJECT_DIR_NAME = ".jarn"
#: Name of the per-project context file whose bounded excerpt enters the prompt.
PROJECT_CONTEXT_FILE = "JARN.md"
#: Config filename used in both tiers.
CONFIG_FILENAME = "config.yaml"


def global_home() -> Path:
    """Return the global J.A.R.N. home directory (``~/.jarn`` by default).

    Overridable via ``$JARN_HOME``. Treat a non-default value as trusted-input
    only — a hijacked env (CI job, shared shell, malicious repo instructions) can
    redirect secrets and the trust store to an attacker-controlled directory.
    See ``SECURITY.md`` and ``jarn doctor`` (which warns when overridden).
    """
    override = os.environ.get("JARN_HOME")
    if override:
        return Path(override).expanduser()
    # Keep it predictable and user-editable: prefer ~/.jarn over an OS app dir.
    return Path.home() / ".jarn"


def default_global_home() -> Path:
    """The canonical home when ``JARN_HOME`` is unset (``~/.jarn``)."""
    return Path.home() / ".jarn"


def _home_dir() -> Path | None:
    """The user's home directory, or ``None`` when the host has no answer."""
    try:
        return Path.home().resolve()
    except (OSError, RuntimeError):
        return None


def _global_jarn_dirs() -> set[Path]:
    """The ``.jarn`` directories that must never be taken for a project marker.

    Resolved best-effort. ``default_global_home()`` goes through ``Path.home()``,
    which raises ``RuntimeError`` when ``$HOME`` is unset and the uid has no
    passwd entry — a real configuration (``action/action.yml`` sets ``JARN_HOME``
    explicitly for exactly that reason), and one where an explicit ``JARN_HOME``
    must keep working rather than take the whole path layer down with it.
    """
    dirs: set[Path] = set()
    for lookup in (global_home, default_global_home):
        try:
            dirs.add(lookup().resolve())
        except (OSError, RuntimeError):
            continue
    return dirs


#: Owner-only. The global tier holds the prompt history, session transcripts, the
#: wiki and memory the agent writes for itself, the trust store, conversation
#: state and the config — all of it readable by every other local account when the
#: directory is created at the default umask (``0755`` on a stock macOS install).
#: That is negligible on a single-user laptop and is not on a shared host or the
#: always-on VPS appliance jarn is meant to run as.
GLOBAL_HOME_MODE = 0o700

#: Set when :func:`ensure_global_home` repairs the mode, and drained by
#: ``setup_logging``. The repair necessarily happens BEFORE logging is configured
#: — configuring it creates ``~/.jarn/logs``, which would itself create the home
#: at the umask first — so a warning emitted at repair time reaches stderr through
#: ``logging.lastResort`` and never the log file. Buffering it gives the operator
#: both: the immediate notice, and a durable record.
_pending_tighten_notice: str | None = None


def take_tighten_notice() -> str | None:
    """Return and clear the pending "tightened permissions" notice, if any."""
    global _pending_tighten_notice
    notice, _pending_tighten_notice = _pending_tighten_notice, None
    return notice


def ensure_global_home() -> Path | None:
    """Create the global home if missing and hold it at owner-only permissions.

    Idempotent, cheap, and safe to call on every start — which it must be, for two
    reasons. ``mkdir(mode=…)`` is masked by the umask AND is a no-op for a
    directory that already exists, so a create-time mode alone would leave every
    install already in the field wide open; only the explicit ``chmod`` repairs
    those. And the home is created implicitly by whichever subsystem touches it
    first — the prompt history, the log file, the session index — so pinning it at
    the two ``mkdir`` call sites that name it explicitly would miss most starts.

    Only ever TIGHTENS, and says so when it does — immediately on stderr, and
    again in the log file once logging is configured (see
    :data:`_pending_tighten_notice` for why it takes two paths).

    Never raises. A missing or read-only home is a real configuration (see
    :func:`global_home`) and must not be what takes the process down. Returns the
    home, or ``None`` when the host could not answer where it is.
    """
    try:
        home = global_home()
    except (OSError, RuntimeError):
        return None
    try:
        home.mkdir(parents=True, exist_ok=True, mode=GLOBAL_HOME_MODE)
    except OSError:
        return home
    if os.name == "nt":  # pragma: no cover - POSIX mode bits are not meaningful
        return home
    try:
        current = stat.S_IMODE(home.stat().st_mode)
    except OSError:
        return home
    if current & 0o077:
        try:
            home.chmod(GLOBAL_HOME_MODE)
        except OSError:
            return home
        global _pending_tighten_notice
        _pending_tighten_notice = (
            f"Tightened permissions on {home} from {current:o} to "
            f"{GLOBAL_HOME_MODE:o} — it held prompt history, transcripts, memory "
            "and the trust store, readable by other local users."
        )
        logging.getLogger("jarn").warning("%s", _pending_tighten_notice)
    return home


def global_secrets_dir() -> Path:
    """Return ``~/.jarn/secrets/`` — J.A.R.N.'s own file-backed secret store.

    The single source of truth for where ``file:<service>/<account>`` secret
    references live (see :mod:`jarn.config.secrets`). The permission engine reads
    it too, to refuse the agent any access to it.
    """
    return global_home() / "secrets"


def secret_store_dirs() -> set[Path]:
    """Every resolved directory that can hold J.A.R.N.'s own stored credentials.

    Covers the active ``JARN_HOME`` *and* the default ``~/.jarn``: setting an
    override does not retract keys an earlier run already wrote under the default
    home, so both must stay off-limits to the agent.

    Resolved best-effort with the same discipline as :func:`_global_jarn_dirs` —
    ``Path.home()`` raises when ``$HOME`` is unset and the uid has no passwd
    entry, and that must never take the permission layer down with it. An empty
    result means "no store directory could be located", which callers treat as
    "fall back to the lexical guard", never as "nothing to protect".
    """
    dirs: set[Path] = set()
    for lookup in (global_home, default_global_home):
        try:
            dirs.add((lookup() / "secrets").resolve())
        except (OSError, RuntimeError):
            continue
    return dirs


def jarn_home_overridden() -> bool:
    """True when ``JARN_HOME`` redirects away from the default ``~/.jarn``."""
    override = os.environ.get("JARN_HOME")
    if not override:
        return False
    try:
        return global_home().resolve() != default_global_home().resolve()
    except (OSError, RuntimeError):
        return True


def global_config_path() -> Path:
    return global_home() / CONFIG_FILENAME


def global_subdir(name: str) -> Path:
    return global_home() / name


def global_logs_dir() -> Path:
    return global_home() / "logs"


def global_memory_dir() -> Path:
    return global_home() / "memory"


def global_wiki_dir() -> Path:
    """Return ``~/.jarn/wiki/`` — the global wiki directory."""
    return global_home() / "wiki"


def personal_root() -> Path:
    """Return ``~/.jarn/personal`` — the gateway's default working root (#51)."""
    return global_home() / "personal"


#: Managed template for ``<root>/.jarn/.gitignore`` (T-OPS-1 / #53).
#: Patterns are relative to ``.jarn/`` so project config/skills/wiki stay
#: trackable while DM transcripts and SQLite state are not pushed.
PROJECT_GITIGNORE_CONTENT = """\
# Managed by jarn — runtime state under .jarn/ (do not commit).
# Project config, skills, and wiki pages remain trackable.
state.sqlite
state.sqlite-*
checkpoints.sqlite*
sessions/
logs/
*.lock
**/*.lock
"""


def ensure_project_gitignore(root: Path | str) -> Path | None:
    """Ensure ``<root>/.jarn/.gitignore`` excludes runtime / DM state (T-OPS-1).

    Idempotent: creates ``.jarn/`` when needed; rewrites only when the file is
    missing or differs from :data:`PROJECT_GITIGNORE_CONTENT`. Best-effort —
    never raises (a read-only root must not take down the gateway bind path).
    Returns the gitignore path, or ``None`` when the write could not complete
    or *root* would collide with the global ``~/.jarn`` tier.
    """
    try:
        root_path = Path(root).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    # Never nest a project marker inside the global tier itself.
    try:
        if root_path in _global_jarn_dirs():
            return None
    except (OSError, RuntimeError):
        return None
    jarn_dir = root_path / PROJECT_DIR_NAME
    path = jarn_dir / ".gitignore"
    try:
        jarn_dir.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.read_text(encoding="utf-8") == PROJECT_GITIGNORE_CONTENT:
            return path
        from jarn.util.atomic import atomic_write_text

        atomic_write_text(path, PROJECT_GITIGNORE_CONTENT)
        return path
    except OSError as exc:
        logging.getLogger("jarn").warning(
            "could not write project .jarn/.gitignore under %s: %s", root_path, exc
        )
        return None


def ensure_personal_root() -> Path:
    """Create ``~/.jarn/personal`` and ``git init`` it when missing.

    Idempotent. The Telegram gateway uses this as the default ``(chat_id, root)``
    root when the user has not ``/repo``-switched to an allowlisted project.
    Also ensures ``<personal>/.jarn/.gitignore`` so transcripts/state stay local.
    """
    import subprocess

    ensure_global_home()
    root = personal_root()
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        subprocess.run(  # noqa: S603 - fixed argv, no user input
            ["git", "init"],
            cwd=root,
            check=True,
            capture_output=True,
            env=external_command_env(),
            text=True,
        )
    ensure_project_gitignore(root)
    return root


def project_wiki_dir(root: Path | None = None) -> Path | None:
    """Return ``<root>/.jarn/wiki/`` for the discovered (or given) project root."""
    pdir = project_dir(root)
    return pdir / "wiki" if pdir else None


def find_project_root(start: Path | None = None) -> Path | None:
    """Walk upward from ``start`` (cwd) looking for a project marker.

    Returns the directory containing the marker, or ``None`` if none is found
    before the filesystem root.
    """
    current = (start or Path.cwd()).resolve()
    global_jarn_dirs = _global_jarn_dirs()
    home = _home_dir()
    for directory in (current, *current.parents):
        marker = directory / PROJECT_DIR_NAME
        if marker.is_dir() and marker.resolve() not in global_jarn_dirs:
            return directory
        if (directory / PROJECT_CONTEXT_FILE).is_file():
            return directory
        if (directory / ".git").exists():
            return directory
        if home is not None and directory == home:
            # Stop here — the home directory is checked (a dotfiles repo at
            # ~/.git is legitimate) but never crossed. Ignoring the global
            # ~/.jarn marker above removed what used to terminate this walk;
            # without a stop the search escapes to /, and the root it returns
            # becomes an in-scope write root for the permission engine, so a
            # stray .jarn or .git anywhere above $HOME would put the whole
            # machine in scope.
            break
    return None


def project_dir(root: Path | None = None) -> Path | None:
    """Return ``<root>/.jarn`` for the discovered (or given) project root."""
    root = root or find_project_root()
    if root is None:
        return None
    directory = root / PROJECT_DIR_NAME
    if directory.resolve() in _global_jarn_dirs():
        return None
    return directory


def project_config_path(root: Path | None = None) -> Path | None:
    pdir = project_dir(root)
    return pdir / CONFIG_FILENAME if pdir else None


def project_context_path(root: Path | None = None) -> Path | None:
    root = root or find_project_root()
    if root is None:
        return None
    return root / PROJECT_CONTEXT_FILE


def project_state_db(root: Path | None = None) -> Path | None:
    """SQLite checkpointer DB for resumable sessions (gitignored)."""
    pdir = project_dir(root)
    return pdir / "state.sqlite" if pdir else None


def project_sessions_dir(root: Path | None = None) -> Path | None:
    """Directory for per-session JSONL transcript files (gitignored).

    Returns ``<root>/.jarn/sessions/`` when a project root is discoverable,
    else ``None`` (the transcript writer falls back to the global home).
    """
    pdir = project_dir(root)
    return pdir / "sessions" if pdir else None


def global_sessions_dir() -> Path:
    """Fallback transcript directory under the global J.A.R.N. home."""
    return global_home() / "sessions"


def cachedir() -> Path:
    """Per-user cache dir for non-essential, regenerable data."""
    return Path(platformdirs.user_cache_dir("jarn"))


# ── Cross-vendor (.claude) helpers ───────────────────────────────────────────
#: Name of the Claude Code config/extension directory (cross-vendor standard).
CLAUDE_DIR_NAME = ".claude"


def global_claude_home() -> Path:
    """Return ``~/.claude`` — the global Claude Code extension directory."""
    return Path.home() / CLAUDE_DIR_NAME


def global_claude_subdir(name: str) -> Path:
    """Return ``~/.claude/<name>``."""
    return global_claude_home() / name


def project_claude_dir(root: Path | None = None) -> Path | None:
    """Return ``<root>/.claude`` for the discovered (or given) project root."""
    root = root or find_project_root()
    if root is None:
        return None
    directory = root / CLAUDE_DIR_NAME
    try:
        global_claude = global_claude_home().resolve()
    except (OSError, RuntimeError):
        return directory  # no home directory to collide with
    if directory.resolve() == global_claude:
        return None
    return directory
