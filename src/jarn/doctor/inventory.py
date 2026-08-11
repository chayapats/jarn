"""Bounded, machine-readable host inventory for ``jarn doctor``.

All subprocesses use fixed argv with ``shell=False`` and short timeouts.  The
default inventory is offline; callers can explicitly request provider TCP probes,
which are individually bounded and never abort the rest of doctor.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import re
import shutil
import signal
import site
import socket
import stat
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from jarn.config.migrations import diagnose_config_file
from jarn.config.secrets import keyring_backend_metadata, redact_secrets
from jarn.version import __version__

_COMMAND_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_DISCOVERY_TIMEOUT = 1.0


def _manager_directory(
    executable: str | None,
    args: list[str],
    *,
    manager: str,
    timeout: float,
    checks: list[dict[str, Any]],
) -> Path | None:
    """Ask one package manager for a directory with a fixed, bounded argv."""

    if not executable:
        checks.append({"manager": manager, "checked": False, "reason": "command unavailable"})
        return None
    started = time.monotonic()
    try:
        process = subprocess.Popen(  # noqa: S603 - resolved manager plus fixed arguments
            [executable, *args],
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=os.name == "posix",
        )
    except OSError as exc:
        checks.append(
            {
                "manager": manager,
                "checked": True,
                "ok": False,
                "timed_out": False,
                "error": redact_secrets(str(exc)),
            }
        )
        return None
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            if os.name == "posix":
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            else:  # pragma: no cover - native Windows is not a supported runtime
                process.kill()
        process.communicate()
        checks.append(
            {
                "manager": manager,
                "checked": True,
                "ok": False,
                "timed_out": True,
                "timeout_seconds": timeout,
                "awaited": f"{manager} installation-directory query",
                "action": f"Run `{Path(executable).name} {' '.join(args)}` manually.",
            }
        )
        return None
    except OSError as exc:
        if process.poll() is None:
            process.kill()
            process.communicate()
        checks.append(
            {
                "manager": manager,
                "checked": True,
                "ok": False,
                "timed_out": False,
                "error": redact_secrets(str(exc)),
            }
        )
        return None

    raw = stdout.strip().splitlines()
    candidate = Path(raw[0]).expanduser() if process.returncode == 0 and raw else None
    if candidate is not None and not candidate.is_absolute():
        candidate = None
    checks.append(
        {
            "manager": manager,
            "checked": True,
            "ok": process.returncode == 0 and candidate is not None,
            "timed_out": False,
            "exit_code": process.returncode,
            "directory": str(candidate) if candidate is not None else None,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "error": (
                redact_secrets(stderr.strip().splitlines()[0][:300])
                if process.returncode != 0 and stderr.strip()
                else None
            ),
        }
    )
    return candidate


def _discover_command_inventory(
    name: str,
    *,
    home: Path | None = None,
    timeout: float = _DISCOVERY_TIMEOUT,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Find PATH and off-PATH commands across supported installation methods."""

    if not _COMMAND_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid command name {name!r}")
    home = (home or Path.home()).expanduser()
    names = (name, f"{name}.exe") if os.name == "nt" else (name,)
    candidates: dict[str, dict[str, Any]] = {}
    checks: list[dict[str, Any]] = []

    def record(directory: Path | None, source: str, discovery: str) -> None:
        if directory is None:
            return
        for candidate_name in names:
            candidate = directory.expanduser() / candidate_name
            try:
                present = os.path.lexists(candidate)
                if not present:
                    continue
                absolute = os.path.abspath(candidate)
                usable = candidate.is_file() and os.access(candidate, os.X_OK)
                broken_symlink = candidate.is_symlink() and not candidate.exists()
            except OSError:
                continue
            item = candidates.setdefault(
                absolute,
                {
                    "path": absolute,
                    "sources": [],
                    "discovery": [],
                    "on_path": False,
                    "usable": usable,
                    "broken_symlink": broken_symlink,
                },
            )
            if source not in item["sources"]:
                item["sources"].append(source)
            if discovery not in item["discovery"]:
                item["discovery"].append(discovery)
            item["on_path"] = item["on_path"] or source == "path"
            item["usable"] = item["usable"] or usable
            item["broken_symlink"] = item["broken_symlink"] or broken_symlink

    # Preserve PATH order first: it defines what the current process resolves.
    for path_entry in os.environ.get("PATH", "").split(os.pathsep):
        if path_entry:
            record(Path(path_entry), "path", "current PATH")

    # Common user/system/standalone locations remain useful even when omitted
    # from PATH (the exact failure mode this inventory is meant to diagnose).
    for known_dir, source in (
        (home / ".local" / "bin", "user-command"),
        (home / ".npm-global" / "bin", "npm"),
        (Path("/usr/local/bin"), "system-location"),
        (Path("/usr/bin"), "system-package"),
        (Path("/bin"), "system-package"),
        (Path("/opt/homebrew/bin"), "homebrew"),
        (Path("/home/linuxbrew/.linuxbrew/bin"), "homebrew"),
    ):
        record(known_dir, source, "known installation location")

    # pip --user: include the running interpreter and a separately installed
    # python3/python when available (a frozen build's sys.executable is J.A.R.N.).
    with contextlib.suppress(Exception):
        record(Path(site.getuserbase()) / "bin", "pip-user", "Python user base")
    python = shutil.which("python3") or shutil.which("python")
    pip_user_base = _manager_directory(
        python,
        ["-m", "site", "--user-base"],
        manager="pip-user",
        timeout=timeout,
        checks=checks,
    )
    record(pip_user_base / "bin" if pip_user_base else None, "pip-user", "python -m site")
    for mac_python_dir in (home / "Library" / "Python").glob("*/bin"):
        record(mac_python_dir, "pip-user", "macOS Python user base")

    # pipx and uv may intentionally use non-default tool-bin directories.
    pipx_env = os.environ.get("PIPX_BIN_DIR")
    record(Path(pipx_env) if pipx_env else None, "pipx", "PIPX_BIN_DIR")
    pipx_bin = _manager_directory(
        shutil.which("pipx"),
        ["environment", "--value", "PIPX_BIN_DIR"],
        manager="pipx",
        timeout=timeout,
        checks=checks,
    )
    record(pipx_bin, "pipx", "pipx environment")

    uv_env = os.environ.get("UV_TOOL_BIN_DIR")
    record(Path(uv_env) if uv_env else None, "uv-tool", "UV_TOOL_BIN_DIR")
    uv_bin = _manager_directory(
        shutil.which("uv"),
        ["tool", "dir", "--bin"],
        manager="uv-tool",
        timeout=timeout,
        checks=checks,
    )
    record(uv_bin, "uv-tool", "uv tool dir --bin")

    # npm: query both the global prefix (command location) and root (package
    # ownership evidence), and enumerate nvm versions omitted from PATH.
    npm = shutil.which("npm")
    npm_prefix = _manager_directory(
        npm,
        ["prefix", "-g"],
        manager="npm-prefix",
        timeout=timeout,
        checks=checks,
    )
    npm_root = _manager_directory(
        npm,
        ["root", "-g"],
        manager="npm-root",
        timeout=timeout,
        checks=checks,
    )
    if npm_prefix is not None:
        record(npm_prefix if os.name == "nt" else npm_prefix / "bin", "npm", "npm prefix -g")
    if npm_root is not None:
        package_present = any(
            (npm_root / package / "package.json").is_file() for package in ("jarn-cli", "jarn")
        )
        for check in checks:
            if check.get("manager") == "npm-root":
                check["jarn_package_present"] = package_present
                break
    for executable in (home / ".nvm" / "versions" / "node").glob("*/bin"):
        record(executable, "npm-nvm", "nvm version bin")

    # Homebrew can exist outside PATH on Apple Silicon/Linuxbrew.
    brew = shutil.which("brew")
    if brew is None:
        for known in (Path("/opt/homebrew/bin/brew"), Path("/home/linuxbrew/.linuxbrew/bin/brew")):
            if known.is_file() and os.access(known, os.X_OK):
                brew = str(known)
                break
    brew_prefix = _manager_directory(
        brew,
        ["--prefix"],
        manager="homebrew",
        timeout=timeout,
        checks=checks,
    )
    record(brew_prefix / "bin" if brew_prefix else None, "homebrew", "brew --prefix")

    inventory = list(candidates.values())
    for item in inventory:
        manager_sources = [source for source in item["sources"] if source != "path"]
        item["source"] = (
            manager_sources[0]
            if len(manager_sources) == 1
            else ("multiple-possible-owners" if manager_sources else _candidate_source(item["path"]))
        )
    return inventory, checks


def _command_candidates(name: str) -> list[str]:
    inventory, _checks = _discover_command_inventory(name)
    return [str(item["path"]) for item in inventory]


def _candidate_source(path: str) -> str:
    lowered = Path(path).as_posix().lower()
    if "/.nvm/" in lowered or "/node_modules/" in lowered or "/.npm/" in lowered:
        return "npm"
    if "/pipx/venvs/" in lowered:
        return "pipx"
    if "/uv/tools/" in lowered or "/uv/tool/" in lowered:
        return "uv-tool"
    if "/homebrew/" in lowered or "/linuxbrew/" in lowered or "/cellar/" in lowered:
        return "homebrew"
    if Path(path).parent.as_posix() in {"/usr/bin", "/bin", "/usr/sbin", "/sbin"}:
        return "system-package"
    if "/.local/bin/" in lowered:
        return "user-command"
    return "path-command"


def _shell_resolution(shell: str) -> dict[str, Any]:
    """Inspect the user's login/interactive resolution with a fixed command."""

    if not shell or not Path(shell).is_file():
        return {
            "checked": False,
            "reason": "configured shell executable is unavailable",
            "action": "Run `type -a jarn` and `command -V jarn` in the current shell.",
        }
    command = (
        "type -a jarn 2>/dev/null; command -V jarn 2>/dev/null; "
        "alias jarn 2>/dev/null || true; hash -t jarn 2>/dev/null || true"
    )
    started = time.monotonic()
    try:
        result = subprocess.run(
            [shell, "-lic", command],
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except subprocess.TimeoutExpired:
        return {
            "checked": True,
            "timed_out": True,
            "timeout_seconds": 2,
            "awaited": "login/interactive shell command resolution",
            "action": "Run `type -a jarn`; clear a stale cache with `hash -r` or `rehash`.",
        }
    except OSError as exc:
        return {
            "checked": True,
            "timed_out": False,
            "error": redact_secrets(str(exc)),
            "action": "Run `type -a jarn` in the current shell.",
        }
    lines = [redact_secrets(line)[:500] for line in result.stdout.splitlines() if line.strip()]
    return {
        "checked": True,
        "timed_out": False,
        "exit_code": result.returncode,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        "lines": lines,
        "alias_detected": any("alias" in line.lower() for line in lines),
        "function_detected": any("function" in line.lower() for line in lines),
        "hash_cache_action": "Run `hash -r` (bash) or `rehash` (zsh) after activation.",
    }


def _keyring_inventory(*, timeout: float = 2.0) -> dict[str, Any]:
    """Return backend metadata without reading credentials or risking a hang."""

    try:
        return keyring_backend_metadata(timeout=timeout)
    except TimeoutError:
        return {
            "available": False,
            "backend": None,
            "priority": None,
            "credentials_read": False,
            "timed_out": True,
            "timeout_seconds": timeout,
            "awaited": "OS keychain backend metadata",
            "action": "Start/unlock the OS keychain service, then retry `jarn doctor`.",
        }
    except Exception as exc:  # noqa: BLE001 - diagnostics must survive backend import failure
        return {
            "available": False,
            "backend": None,
            "priority": None,
            "credentials_read": False,
            "error": redact_secrets(str(exc)),
            "action": "Install or repair the OS keychain backend, then retry doctor.",
        }


def _sandbox_inventory() -> dict[str, Any]:
    system = platform.system().lower()
    bwrap = shutil.which("bwrap")
    sandbox_exec = shutil.which("sandbox-exec")
    docker = shutil.which("docker")
    if system == "linux":
        native = bwrap
        expected = "bwrap"
    elif system == "darwin":
        native = sandbox_exec
        expected = "sandbox-exec"
    else:
        native = None
        expected = "none (native Windows is unsupported; use WSL2)"
    return {
        "platform": system,
        "native_expected": expected,
        "native_path": native,
        "native_available": native is not None,
        "bwrap_path": bwrap,
        "sandbox_exec_path": sandbox_exec,
        "docker_path": docker,
        "docker_available": docker is not None,
    }


def _update_inventory() -> dict[str, Any]:
    from jarn.config import paths

    cache = paths.global_home() / "update-check.json"
    result: dict[str, Any] = {
        "cache_path": str(cache),
        "cache_present": cache.is_file(),
        "cache_mode": _mode(cache),
    }
    if cache.is_file():
        with contextlib.suppress(OSError, ValueError, TypeError):
            payload = json.loads(cache.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                result.update(
                    {
                        "checked_at": payload.get("checked_at") or payload.get("ts"),
                        "latest_version": payload.get("latest_version")
                        or payload.get("latest"),
                        "channel": payload.get("channel") or "stable",
                    }
                )
    return result


def _run_version(argv: list[str], *, timeout: float = 2.0) -> dict[str, Any]:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "timed_out": True,
            "timeout_seconds": timeout,
            "awaited": "dependency version command",
            "action": "Run the dependency directly or increase the doctor timeout.",
            "version": None,
        }
    except OSError as exc:
        return {
            "ok": False,
            "timed_out": False,
            "version": None,
            "error": redact_secrets(str(exc)),
            "action": "Check the executable permissions and PATH, then retry doctor.",
        }
    output = (proc.stdout or proc.stderr).strip().splitlines()
    return {
        "ok": proc.returncode == 0,
        "timed_out": False,
        "exit_code": proc.returncode,
        "version": redact_secrets(output[0][:300]) if output else None,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
    }


def _profile_for_shell(shell: str) -> Path | None:
    home = Path.home()
    name = Path(shell).name.lower()
    if name == "zsh":
        return home / ".zshrc"
    if name == "bash":
        return home / ".bashrc"
    if name == "fish":
        return home / ".config" / "fish" / "config.fish"
    if os.name != "nt":
        return home / ".profile"
    return None


def _mode(path: Path) -> str | None:
    if os.name == "nt":
        return None
    try:
        return f"{stat.S_IMODE(path.stat().st_mode):04o}"
    except OSError:
        return None


def _install_method(active: Path | None) -> dict[str, Any]:
    from jarn.install_state import (
        InstallStateError,
        default_manifest_path,
        load_actionable_install_record,
    )

    canonical_manifest = default_manifest_path()
    record_error: str | None = None
    try:
        record = load_actionable_install_record(canonical_manifest)
    except InstallStateError as exc:
        if canonical_manifest.exists():
            record_error = redact_secrets(str(exc))
    else:
        active_matches = None
        if active is not None:
            with contextlib.suppress(OSError):
                active_matches = active.resolve() == record.active_path.resolve()
        return {
            "method": record.method,
            "metadata_present": True,
            "metadata_source": "canonical-install-record",
            "metadata_path": str(canonical_manifest),
            "version": record.version,
            "channel": record.channel,
            "activation_status": record.activation.get("status"),
            "setup_status": record.setup_status,
            "active_matches_record": active_matches,
        }

    # Compatibility only: pre-GA installers wrote either a one-line marker next
    # to the executable or a manifest under JARN_HOME.  New writes must use the
    # shared install_state record above.
    marker_candidates: list[Path] = []
    if active is not None:
        marker_candidates.append(active.parent / ".jarn-install-method")
    try:
        from jarn.config.paths import global_home

        marker_candidates.append(global_home() / "install.json")
    except Exception:
        pass
    for marker in marker_candidates:
        try:
            text = marker.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not text:
            continue
        if marker.suffix == ".json":
            try:
                metadata = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(metadata, dict) and metadata.get("method"):
                activation = metadata.get("activation")
                return {
                    "method": str(metadata["method"]),
                    "metadata_present": True,
                    "metadata_source": "legacy",
                    "version": metadata.get("version"),
                    "channel": metadata.get("channel"),
                    "activation_status": (
                        activation.get("status") if isinstance(activation, dict) else None
                    ),
                    "setup_status": metadata.get("setup_status"),
                    "canonical_record_error": record_error,
                }
            continue
        return {
            "method": text.split()[0],
            "metadata_present": True,
            "metadata_source": "legacy",
            "canonical_record_error": record_error,
        }
    if getattr(sys, "frozen", False):
        return {
            "method": "standalone-binary",
            "metadata_present": False,
            "canonical_record_error": record_error,
        }
    if active is not None:
        lowered = active.as_posix().lower()
        if "/uv/tools/" in lowered or "/uv/tool/" in lowered:
            inferred = "uv-tool"
        elif "/pipx/venvs/" in lowered:
            inferred = "pipx"
        elif "/node_modules/" in lowered or "/.npm/" in lowered:
            inferred = "npm"
        elif "/homebrew/" in lowered or "/linuxbrew/" in lowered or "/cellar/" in lowered:
            inferred = "homebrew"
        elif active.parent.as_posix() in {"/usr/bin", "/bin", "/usr/sbin", "/sbin"}:
            inferred = "system-package"
        else:
            inferred = "python-package"
        return {
            "method": inferred,
            "metadata_present": False,
            "inferred": True,
            "canonical_record_error": record_error,
        }
    return {
        "method": "python-package",
        "metadata_present": False,
        "canonical_record_error": record_error,
    }


def _secret_permissions() -> dict[str, Any]:
    from jarn.config.paths import global_secrets_dir

    root = global_secrets_dir()
    issues: list[dict[str, str]] = []
    files = 0
    if root.is_dir():
        for path in root.rglob("*"):
            try:
                if path.is_file():
                    files += 1
                    mode = stat.S_IMODE(path.stat().st_mode) if os.name != "nt" else 0
                    if os.name != "nt" and mode & 0o077:
                        issues.append({"path": str(path), "mode": f"{mode:04o}"})
            except OSError:
                continue
    root_mode = _mode(root)
    if root_mode is not None and int(root_mode, 8) & 0o077:
        issues.insert(0, {"path": str(root), "mode": root_mode})
    return {
        "store_present": root.is_dir(),
        "file_count": files,
        "root_mode": root_mode,
        "permission_issues": issues,
    }


def _config_diag_dict(value: Any) -> dict[str, Any]:
    data = asdict(value)
    data["path"] = str(data["path"])
    data["backup_paths"] = [str(path) for path in data.get("backup_paths") or []]
    return data


def collect_host_inventory(
    *, project_root: Path | None = None, project_trusted: bool | None = None
) -> dict[str, Any]:
    """Collect the non-network portion of the GA doctor inventory."""
    from jarn.config import paths

    candidate_inventory, discovery_checks = _discover_command_inventory("jarn")
    active_candidates = [str(item["path"]) for item in candidate_inventory]
    active_which = shutil.which("jarn")
    active_which = os.path.abspath(active_which) if active_which else None
    active = Path(active_which) if active_which else None
    for candidate in candidate_inventory:
        candidate["active"] = candidate["path"] == active_which
    shadowed = [
        str(candidate["path"])
        for candidate in candidate_inventory
        if candidate["on_path"] and not candidate["active"]
    ]
    shell = os.environ.get("SHELL", "")
    profile = _profile_for_shell(shell) if shell else None
    install_dir = active.parent if active is not None else None
    free_bytes: int | None = None
    install_mode: str | None = None
    if install_dir is not None:
        with contextlib.suppress(OSError):
            free_bytes = shutil.disk_usage(install_dir).free
        install_mode = _mode(install_dir)

    libc_name, libc_version = platform.libc_ver()
    global_diag = diagnose_config_file(paths.global_config_path())
    project_config = paths.project_config_path(project_root)
    project_diag = diagnose_config_file(project_config) if project_config else None

    uv_path = shutil.which("uv")
    from jarn.auth import DependencyState, inspect_codex_dependency

    codex_dependency = inspect_codex_dependency(timeout_seconds=2)
    codex_path = codex_dependency.executable
    uv_info = _run_version([uv_path, "--version"]) if uv_path else {"ok": False}
    codex_info = {
        "ok": codex_dependency.state
        not in {DependencyState.MISSING, DependencyState.INCOMPATIBLE},
        "version": codex_dependency.version,
        "state": codex_dependency.state.value,
        "minimum_version": codex_dependency.minimum_version,
        "error": codex_dependency.detail,
    }
    protocol: dict[str, Any]
    if codex_path:
        help_info = _run_version([codex_path, "app-server", "--help"])
        protocol = {
            "compatible": bool(help_info.get("ok")),
            "check": "codex app-server --help",
            "error": help_info.get("error")
            or ("command timed out" if help_info.get("timed_out") else None),
        }
    else:
        protocol = {"compatible": False, "check": "codex executable missing"}

    # Unified catalog cache (Codex/local/provider snapshots), not the historical
    # pricing-only OpenRouter cache.
    cache_root = paths.cachedir() / "model-catalog"
    cache_files = list(cache_root.glob("*.json")) if cache_root.is_dir() else []
    cache_age: float | None = None
    cache_mtimes: list[tuple[float, Path]] = []
    for cache_file in cache_files:
        with contextlib.suppress(OSError):
            cache_mtimes.append((cache_file.stat().st_mtime, cache_file))
    newest_cache = max(cache_mtimes, key=lambda item: item[0])[1] if cache_mtimes else None
    cache_source = "none"
    cache_freshness = "missing"
    if newest_cache is not None:
        with contextlib.suppress(OSError):
            cache_age = max(0.0, time.time() - newest_cache.stat().st_mtime)
        with contextlib.suppress(OSError, ValueError, TypeError):
            import json

            payload = json.loads(newest_cache.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                cache_source = str(payload.get("source") or "unknown")
                expires_at = str(payload.get("expires_at") or "")
                if expires_at:
                    from datetime import datetime

                    expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                    cache_freshness = (
                        "fresh"
                        if expires.timestamp() > time.time()
                        else "stale"
                    )

    return {
        "jarn": {
            "version": __version__,
            "active_executable": str(active) if active else None,
            "resolved_executable": str(active.resolve()) if active else None,
            "path_candidates": active_candidates,
            "candidate_inventory": candidate_inventory,
            "discovery_checks": discovery_checks,
            "shadowed": shadowed,
            "other_installations": [
                str(candidate["path"])
                for candidate in candidate_inventory
                if not candidate["active"]
            ],
            "install": _install_method(active),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "architecture": platform.machine(),
            "libc": {"name": libc_name or "unknown", "version": libc_version or None},
            "python": {
                "version": platform.python_version(),
                "executable": sys.executable,
                "managed": "uv" in Path(sys.executable).parts,
            },
        },
        "shell": {
            "name": Path(shell).name if shell else None,
            "executable": shell or None,
            "profile": str(profile) if profile else None,
            "profile_present": profile.is_file() if profile else None,
            "path_value": os.environ.get("PATH", ""),
            "resolution": _shell_resolution(shell),
            "parent_shell_limitation": (
                "A child process cannot inspect ephemeral aliases, functions, or hash-table "
                "state in the already-running parent shell. The login/interactive probe above "
                "checks reproducible shell startup state; run `type -a jarn`, `command -V jarn`, "
                "and `hash -t jarn` in the current shell for the remaining live state."
            ),
        },
        "installation": {
            "directory": str(install_dir) if install_dir else None,
            "directory_mode": install_mode,
            "writable": os.access(install_dir, os.W_OK) if install_dir else None,
            "free_bytes": free_bytes,
        },
        "dependencies": {
            "uv": {"path": uv_path, **uv_info},
            "codex": {"path": codex_path, **codex_info, "protocol": protocol},
        },
        "configuration": {
            "schema_current": global_diag.target_version,
            "global": _config_diag_dict(global_diag),
            "project": _config_diag_dict(project_diag) if project_diag else None,
        },
        "secrets": {**_secret_permissions(), "keyring": _keyring_inventory()},
        "sandbox": _sandbox_inventory(),
        "workspace": {
            "root": str(project_root) if project_root else None,
            "trusted": project_trusted,
        },
        "catalog": {
            "cache_present": bool(cache_files),
            "cache_count": len(cache_files),
            "cache_age_seconds": round(cache_age, 1) if cache_age is not None else None,
            "source": cache_source,
            "freshness": cache_freshness,
        },
        "network": {"checked": False, "checks": []},
        "updates": _update_inventory(),
    }


def collect_provider_reachability(config: Any, *, timeout: float = 0.75) -> dict[str, Any]:
    """Boundedly probe configured provider endpoints without issuing API requests."""
    checks: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for name, provider in getattr(config, "providers", {}).items():
        raw_url = getattr(provider, "base_url", None)
        if not raw_url:
            continue
        parsed = urlparse(str(raw_url))
        host = parsed.hostname
        if not host:
            continue
        port = parsed.port or (443 if parsed.scheme in {"https", "wss"} else 80)
        key = (host, port)
        if key in seen:
            continue
        seen.add(key)
        started = time.monotonic()
        error: str | None = None
        timed_out = False
        try:
            with socket.create_connection(key, timeout=timeout):
                reachable = True
        except OSError as exc:
            reachable = False
            error = redact_secrets(str(exc))
            timed_out = isinstance(exc, TimeoutError)
        checks.append(
            {
                "provider": str(name),
                "host": host,
                "port": port,
                "reachable": reachable,
                "timed_out": timed_out,
                "timeout_seconds": timeout,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                "error": error,
                "awaited": "TCP connection to the configured provider endpoint",
                "action": (
                    "Check DNS, proxy/firewall, and provider availability, then retry."
                    if not reachable
                    else None
                ),
            }
        )
    return {"checked": True, "checks": checks}


__all__ = ["collect_host_inventory", "collect_provider_reachability"]
