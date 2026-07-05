"""Background shell processes — run a dev server, watcher, or long build without
blocking the turn.

The agent's ordinary ``execute`` tool is synchronous (it blocks until the command
finishes, with a timeout). This module adds a small process-wide registry of
*detached* processes the agent can start, poll, and kill across turns. Output is
streamed to a per-process log file so ``check_background`` can return a tail
without ever blocking.

The registry is a process singleton (so processes survive a runtime rebuild on a
mode/model switch). An ``atexit`` hook terminates everything still running when
J.A.R.N. exits, so a forgotten dev server doesn't outlive the session.
"""

from __future__ import annotations

import atexit
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from jarn.agent.process_util import terminate_process_group

_log = logging.getLogger("jarn.background")


@dataclass(slots=True)
class BackgroundProc:
    id: str
    command: str
    popen: subprocess.Popen
    log_path: Path
    tmpdir: Path
    cwd: str
    started_at: float = field(default_factory=time.monotonic)
    killed_reason: str | None = None

    def running(self) -> bool:
        return self.popen.poll() is None

    @property
    def exit_code(self) -> int | None:
        return self.popen.poll()


def _tail(path: Path, lines: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    rows = text.splitlines()
    return "\n".join(rows[-lines:])


def _terminate(popen: subprocess.Popen) -> None:
    """Best-effort terminate the whole process group (SIGTERM, then SIGKILL)."""
    if popen.poll() is not None:
        return
    terminate_process_group(popen.pid, grace_secs=3)


def _open_fd_count() -> int | None:
    """Return the number of open file descriptors for this process, if known."""
    if os.name == "posix":
        try:
            return len(os.listdir("/proc/self/fd"))
        except OSError:
            pass
    try:
        import resource  # noqa: PLC0415

        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        # Not a live count — only used when /proc is unavailable.
        return soft
    except Exception:  # noqa: BLE001
        return None


class ProcessManager:
    """A registry of detached background processes for one J.A.R.N. process."""

    def __init__(self) -> None:
        self._procs: dict[str, BackgroundProc] = {}
        self._counter = 0
        self._lock = threading.Lock()
        self.max_concurrent: int | None = None
        self.max_lifetime_secs: float | None = None

    def configure(
        self,
        *,
        max_concurrent: int | None = None,
        max_lifetime_secs: float | None = None,
    ) -> None:
        """Apply optional limits from config (``None`` = unlimited)."""
        self.max_concurrent = max_concurrent
        self.max_lifetime_secs = max_lifetime_secs

    def _prune_exited(self) -> None:
        """Drop registry entries whose processes have already exited; clean up tmpdirs."""
        exited = [pid for pid, proc in self._procs.items() if proc.popen.poll() is not None]
        for pid in exited:
            proc = self._procs.pop(pid)
            shutil.rmtree(proc.tmpdir, ignore_errors=True)

    def _check_limits(self, proc: BackgroundProc) -> None:
        """Kill *proc* if it has exceeded ``max_lifetime_secs``; set ``killed_reason``."""
        if (
            self.max_lifetime_secs is not None
            and proc.killed_reason is None
            and proc.popen.poll() is None
            and time.monotonic() - proc.started_at > self.max_lifetime_secs
        ):
            # Set reason before terminating so a concurrent sweep won't double-kill.
            proc.killed_reason = "killed: exceeded max_lifetime_secs"
            _log.info(
                "background process %s exceeded max_lifetime_secs (%.0fs); terminating",
                proc.id,
                self.max_lifetime_secs,
            )
            _terminate(proc.popen)

    def start(self, command: str, cwd: str) -> BackgroundProc:
        # Sweep lifetime limits outside the lock (terminate_process_group may block 3 s).
        with self._lock:
            to_sweep = list(self._procs.values())
        for p in to_sweep:
            self._check_limits(p)

        with self._lock:
            self._prune_exited()
            alive = sum(1 for p in self._procs.values() if p.running())
            if self.max_concurrent is not None and alive >= self.max_concurrent:
                raise RuntimeError(
                    f"background slots full ({alive}/{self.max_concurrent}) — "
                    "check or kill existing jobs (`list_background`, `kill_background`)"
                )
            self._counter += 1
            pid = f"bg{self._counter}"

        # Create a per-process temp directory for the log file.
        proc_dir = Path(tempfile.mkdtemp(prefix="jarn-bg-"))
        log_path = proc_dir / f"{pid}.log"
        log_file = log_path.open("wb")
        try:
            popen = subprocess.Popen(  # noqa: S602 - LLM-controlled shell, gated upstream
                command,
                shell=True,
                cwd=cwd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            log_file.close()
            shutil.rmtree(proc_dir, ignore_errors=True)
            raise
        # Parent no longer needs the FD — the child retains its copy.
        log_file.close()

        proc = BackgroundProc(
            id=pid,
            command=command,
            popen=popen,
            log_path=log_path,
            tmpdir=proc_dir,
            cwd=cwd,
        )
        with self._lock:
            self._procs[pid] = proc
        return proc

    def status(self, pid: str, *, tail_lines: int = 40) -> dict | None:
        proc = self._procs.get(pid)
        if proc is None:
            return None
        self._check_limits(proc)
        return {
            "id": pid,
            "command": proc.command,
            "running": proc.running(),
            "exit_code": proc.exit_code,
            "tail": _tail(proc.log_path, tail_lines),
            "killed_reason": proc.killed_reason,
        }

    def kill(self, pid: str) -> bool:
        proc = self._procs.get(pid)
        if proc is None:
            return False
        _terminate(proc.popen)
        return True

    def list(self) -> list[dict]:
        with self._lock:
            self._prune_exited()
            procs = list(self._procs.values())
        for p in procs:
            self._check_limits(p)
        return [
            {
                "id": p.id,
                "command": p.command,
                "running": p.running(),
                "exit_code": p.exit_code,
                "killed_reason": p.killed_reason,
            }
            for p in procs
        ]

    def shutdown(self) -> None:
        with self._lock:
            procs = list(self._procs.values())
        for p in procs:
            _terminate(p.popen)
            shutil.rmtree(p.tmpdir, ignore_errors=True)


_MANAGER: ProcessManager | None = None


def manager() -> ProcessManager:
    """The process-wide :class:`ProcessManager` (created on first use)."""
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = ProcessManager()
    return _MANAGER


def shutdown() -> None:
    """Terminate every still-running background process (called at exit)."""
    if _MANAGER is not None:
        _MANAGER.shutdown()


atexit.register(shutdown)


def build_background_tools(
    project_root: Path,
    *,
    max_concurrent: int | None = None,
    max_lifetime_secs: float | None = None,
    _mgr: ProcessManager | None = None,
):
    """LangChain tools for starting / inspecting / killing background processes.

    ``run_in_background`` is gated like ``execute`` (it maps to a SHELL action, so
    the danger-guard inspects the command); the inspect/kill tools are read-only
    controls over processes the agent itself started.

    ``_mgr`` is for testing only — callers should omit it.
    """
    from langchain_core.tools import tool

    root = str(project_root)
    mgr = _mgr if _mgr is not None else manager()
    mgr.configure(
        max_concurrent=max_concurrent,
        max_lifetime_secs=max_lifetime_secs,
    )

    @tool
    def run_in_background(command: str) -> str:  # type: ignore[misc]
        """Start a shell command in the background and return its id immediately.

        Use this for long-running processes — a dev server, a file/test watcher,
        a long build — so you can keep working instead of blocking on output.
        Inspect it later with ``check_background(id)`` and stop it with
        ``kill_background(id)``.

        Args:
            command: The shell command to run in the background.
        """
        try:
            proc = mgr.start(command, cwd=root)
        except RuntimeError as exc:
            return str(exc)
        return (
            f"started {proc.id}: {command}\n"
            f"Use check_background('{proc.id}') to read its output, "
            f"kill_background('{proc.id}') to stop it."
        )

    @tool
    def check_background(id: str) -> str:  # type: ignore[misc]
        """Return a background process's status and most recent output.

        Args:
            id: The id returned by run_in_background (e.g. "bg1").
        """
        st = mgr.status(id)
        if st is None:
            return f"no background process {id!r} (use list_background)."
        if st["running"]:
            state = "running"
        elif st["killed_reason"]:
            state = f"stopped — {st['killed_reason']}"
        else:
            state = f"exited (code {st['exit_code']})"
        tail = st["tail"] or "(no output yet)"
        return f"{id} [{state}]: {st['command']}\n--- recent output ---\n{tail}"

    @tool
    def kill_background(id: str) -> str:  # type: ignore[misc]
        """Terminate a background process started with run_in_background.

        Args:
            id: The id of the process to stop.
        """
        return f"killed {id}." if mgr.kill(id) else f"no background process {id!r}."

    @tool
    def list_background() -> str:  # type: ignore[misc]
        """List the background processes started in this session."""
        procs = mgr.list()
        if not procs:
            return "no background processes."
        lines = []
        for p in procs:
            if p["running"]:
                state = "running"
            elif p["killed_reason"]:
                state = f"stopped — {p['killed_reason']}"
            else:
                state = f"exited ({p['exit_code']})"
            lines.append(f"{p['id']} [{state}]: {p['command']}")
        return "\n".join(lines)

    return [run_in_background, check_background, kill_background, list_background]


__all__ = ["BackgroundProc", "ProcessManager", "build_background_tools", "manager", "shutdown", "_open_fd_count"]
