"""Filesystem locations for J.A.R.N. — the two-tier (global + project) layout.

Global config lives under ``~/.jarn`` (overridable with ``$JARN_HOME``).
Project config lives under ``<project-root>/.jarn`` where the project root is
the nearest ancestor of the current working directory that contains a ``.jarn``
directory, a ``JARN.md`` file, or a ``.git`` directory — in that order.
"""

from __future__ import annotations

import os
from pathlib import Path

import platformdirs

#: Name of the per-project config directory committed alongside a repo.
PROJECT_DIR_NAME = ".jarn"
#: Name of the per-project context file auto-loaded into the system prompt.
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
