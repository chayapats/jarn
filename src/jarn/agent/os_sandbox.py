"""OS-level execution sandbox for the local shell backend.

The danger-guard is a regex-level heuristic; this module adds an optional
*kernel-enforced* layer that restricts what the shell backend can actually do,
regardless of what the LLM tries to run.

Two mechanisms are supported:
- **macOS** (``sandbox-exec`` / Seatbelt): generates an SBPL profile that
  denies file writes outside the allow-set and optionally denies all network.
- **Linux** (``bwrap`` / Bubblewrap): builds a minimal bwrap argv that bind-
  mounts the filesystem read-only everywhere except an explicit writable set.

This module is a *pure* builder — all functions return data structures and
never call ``exec``/``subprocess``. The backend wires the result into Popen.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def available() -> bool:
    """Return True if a sandbox mechanism exists on this host.

    Checks whether ``sandbox-exec`` (macOS) or ``bwrap`` (Linux) is on PATH.
    """
    return backend_name() is not None


def backend_name() -> str | None:
    """Return the name of the available sandbox backend, or None.

    Returns ``"sandbox-exec"`` on macOS when ``sandbox-exec`` is on PATH,
    ``"bwrap"`` on Linux when ``bwrap`` is on PATH, and ``None`` when no
    supported tool is present (including on Windows or other platforms).
    """
    if sys.platform == "darwin":
        if shutil.which("sandbox-exec") is not None:
            return "sandbox-exec"
        return None
    if sys.platform.startswith("linux"):
        if shutil.which("bwrap") is not None:
            return "bwrap"
        return None
    return None


# ---------------------------------------------------------------------------
# Default writable paths
# ---------------------------------------------------------------------------


def default_writable(project_root: Path) -> list[Path]:
    """Return a sensible default write-allow list for a project root.

    Reads are always allowed by both backends; only *write* targets matter
    here. The set is intentionally generous so normal dev tooling works:
    - The system temp directory (``$TMPDIR`` / ``/tmp``).
    - ``~/.cache`` — pip, uv, cargo, and most build tools write there.
    - ``~/.npm`` — npm global cache.
    - ``~/.cargo`` — Cargo registry / build artefacts.
    - ``~/.local/share`` — many tools use XDG data home.

    These directories only appear in the writable list if they exist;
    non-existent paths do not block the sandbox from starting.
    """
    home = Path.home()
    candidates: list[Path] = [
        Path(os.environ.get("TMPDIR", "/tmp")),
        home / ".cache",
        home / ".npm",
        home / ".cargo",
        home / ".local" / "share",
    ]
    # Only include paths that exist — avoids bwrap --bind failures on absent dirs.
    return [p for p in candidates if p.exists()]


# ---------------------------------------------------------------------------
# Profile / argv builders
# ---------------------------------------------------------------------------


def _macos_profile(
    *,
    project_root: Path,
    allow_network: bool,
    writable: list[Path],
) -> str:
    """Generate an SBPL (Seatbelt Policy Language) profile string.

    The profile:
    - Allows everything by default (so reading, executing, IPC, and signals
      work without an exhaustive allowlist).
    - Denies ``file-write*`` everywhere, then re-allows it for the project
      root, the system temp dir, and each caller-supplied writable path.
    - Optionally denies all network access when ``allow_network`` is False.
    """
    tmpdir = str(Path(os.environ.get("TMPDIR", "/tmp")).resolve())

    # Collect the paths that should be writable (dedup, resolve to str).
    # Paths MUST be canonicalized: Seatbelt matches a write against the target's
    # *resolved* path, but on macOS `/var` and `/tmp` (and any temp dir beneath
    # them) are symlinks into `/private/...`. An unresolved `(subpath "/var/...")`
    # would never match, silently denying writes inside the project itself.
    write_allow: list[str] = [str(Path(project_root).resolve()), tmpdir]
    for p in writable:
        s = str(Path(p).resolve())
        if s not in write_allow:
            write_allow.append(s)

    allow_clauses = "\n".join(
        f'  (subpath "{p}")' for p in write_allow
    )

    deny_network = "" if allow_network else "\n(deny network*)"

    profile = (
        "(version 1)\n"
        "(allow default)\n"
        "(deny file-write*\n"
        "  (subpath \"/\"))\n"
        f"(allow file-write*\n"
        f"{allow_clauses})\n"
        f"{deny_network}"
    ).strip()

    return profile


def _macos_argv(
    command: str,
    *,
    project_root: Path,
    allow_network: bool,
    writable: list[Path],
) -> list[str]:
    """Return argv list for sandbox-exec on macOS."""
    profile = _macos_profile(
        project_root=project_root,
        allow_network=allow_network,
        writable=writable,
    )
    return ["sandbox-exec", "-p", profile, "/bin/sh", "-c", command]


def _linux_argv(
    command: str,
    *,
    project_root: Path,
    allow_network: bool,
    writable: list[Path],
) -> list[str]:
    """Return argv list for bwrap (Bubblewrap) on Linux.

    Layout:
    - ``--ro-bind / /`` makes the entire filesystem available read-only.
    - ``--bind <project_root> <project_root>`` overlays the project read-write.
    - Each writable path is bind-mounted read-write on top of the read-only base.
    - ``--tmpfs /tmp`` provides a fresh, writable temp directory.
    - ``--dev /dev`` and ``--proc /proc`` are needed for most real commands.
    - ``--die-with-parent`` ensures the sandbox dies if the parent process exits.
    - ``--unshare-net`` (optional) removes network namespace when denied.
    """
    # Canonicalize bind sources/targets so symlinked paths resolve consistently.
    proj = str(Path(project_root).resolve())
    argv: list[str] = [
        "bwrap",
        "--ro-bind", "/", "/",
        "--bind", proj, proj,
    ]

    for p in writable:
        rp = str(Path(p).resolve())
        argv.extend(["--bind", rp, rp])

    argv.extend([
        "--tmpfs", "/tmp",
        "--dev", "/dev",
        "--proc", "/proc",
        "--die-with-parent",
    ])

    if not allow_network:
        argv.append("--unshare-net")

    argv.extend(["/bin/sh", "-c", command])
    return argv


def wrap(
    command: str,
    *,
    project_root: Path,
    allow_network: bool,
    writable: list[Path],
) -> list[str]:
    """Return an argv list that runs ``command`` under the OS sandbox.

    The command is executed via ``/bin/sh -c <command>`` inside the jail.
    This is a pure function: it builds the argv/profile string but never
    execs or spawns a process itself.

    ``project_root`` is always writable inside the sandbox.  ``writable`` is
    the caller-supplied additional write allow-list (use
    :func:`default_writable` for a sensible starting set).  ``allow_network``
    controls whether outbound network access is permitted.

    On macOS: delegates to ``sandbox-exec -p <profile> /bin/sh -c <command>``.
    On Linux: delegates to a ``bwrap`` invocation.

    Raises :class:`RuntimeError` if no sandbox backend is available on this
    host; callers should check :func:`available` first.
    """
    name = backend_name()
    if name == "sandbox-exec":
        return _macos_argv(
            command,
            project_root=project_root,
            allow_network=allow_network,
            writable=writable,
        )
    if name == "bwrap":
        return _linux_argv(
            command,
            project_root=project_root,
            allow_network=allow_network,
            writable=writable,
        )
    raise RuntimeError(
        "No OS sandbox backend available on this host. "
        "Install sandbox-exec (macOS) or bwrap (Linux), or disable the sandbox."
    )
