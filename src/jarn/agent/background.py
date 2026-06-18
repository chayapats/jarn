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
import os
import signal
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class BackgroundProc:
    id: str
    command: str
    popen: subprocess.Popen
    log_path: Path
    cwd: str

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
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(popen.pid), signal.SIGTERM)
        else:  # pragma: no cover - non-posix fallback
            popen.terminate()
        try:
            popen.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(os.getpgid(popen.pid), signal.SIGKILL)
            else:  # pragma: no cover
                popen.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


class ProcessManager:
    """A registry of detached background processes for one J.A.R.N. process."""

    def __init__(self) -> None:
        self._procs: dict[str, BackgroundProc] = {}
        self._counter = 0
        self._lock = threading.Lock()
        self._dir = Path(tempfile.mkdtemp(prefix="jarn-bg-"))

    def start(self, command: str, cwd: str) -> BackgroundProc:
        with self._lock:
            self._counter += 1
            pid = f"bg{self._counter}"
        log_path = self._dir / f"{pid}.log"
        # Own process group so the whole tree is killable; merge stderr into the
        # log; no stdin (a background process must never block on input).
        log_file = log_path.open("wb")
        popen = subprocess.Popen(  # noqa: S602 - LLM-controlled shell, gated upstream
            command,
            shell=True,
            cwd=cwd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        proc = BackgroundProc(id=pid, command=command, popen=popen, log_path=log_path, cwd=cwd)
        with self._lock:
            self._procs[pid] = proc
        return proc

    def status(self, pid: str, *, tail_lines: int = 40) -> dict | None:
        proc = self._procs.get(pid)
        if proc is None:
            return None
        return {
            "id": pid,
            "command": proc.command,
            "running": proc.running(),
            "exit_code": proc.exit_code,
            "tail": _tail(proc.log_path, tail_lines),
        }

    def kill(self, pid: str) -> bool:
        proc = self._procs.get(pid)
        if proc is None:
            return False
        _terminate(proc.popen)
        return True

    def list(self) -> list[dict]:
        with self._lock:
            procs = list(self._procs.values())
        return [
            {"id": p.id, "command": p.command, "running": p.running(), "exit_code": p.exit_code}
            for p in procs
        ]

    def shutdown(self) -> None:
        with self._lock:
            procs = list(self._procs.values())
        for p in procs:
            _terminate(p.popen)


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


def build_background_tools(project_root: Path):
    """LangChain tools for starting / inspecting / killing background processes.

    ``run_in_background`` is gated like ``execute`` (it maps to a SHELL action, so
    the danger-guard inspects the command); the inspect/kill tools are read-only
    controls over processes the agent itself started.
    """
    from langchain_core.tools import tool

    root = str(project_root)
    mgr = manager()

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
        proc = mgr.start(command, cwd=root)
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
        state = "running" if st["running"] else f"exited (code {st['exit_code']})"
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
            state = "running" if p["running"] else f"exited ({p['exit_code']})"
            lines.append(f"{p['id']} [{state}]: {p['command']}")
        return "\n".join(lines)

    return [run_in_background, check_background, kill_background, list_background]
