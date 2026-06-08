"""A local shell backend whose commands can be killed when a turn is cancelled.

deepagents' :class:`LocalShellBackend` runs each command with a blocking
``subprocess.run`` inside a worker thread (``asyncio.to_thread``). Cancelling the
turn's asyncio task cancels the *future* but the thread — and the OS process it
spawned — keep running to completion. So pressing Esc / Ctrl+C left ``sleep 30``
(and anything it wrote) running on the host.

This subclass spawns each command in its **own process session**
(``start_new_session=True``) and tracks the live process, so :meth:`terminate_all`
can kill the whole process group (not just the top-level shell) when the user
cancels. Behaviour (output combining, ``[stderr]`` prefixing, truncation, exit
code) matches the base class.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading

from deepagents.backends import LocalShellBackend
from deepagents.backends.protocol import ExecuteResponse


class CancellableLocalShellBackend(LocalShellBackend):
    """LocalShellBackend whose spawned process tree can be terminated on cancel."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._live: set[subprocess.Popen] = set()
        self._live_lock = threading.Lock()

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        if not command or not isinstance(command, str):
            return ExecuteResponse(
                output="Error: Command must be a non-empty string.",
                exit_code=1, truncated=False,
            )
        effective_timeout = timeout if timeout is not None else self._default_timeout
        if effective_timeout <= 0:
            raise ValueError(f"timeout must be positive, got {effective_timeout}")

        proc = subprocess.Popen(  # noqa: S602
            command,
            shell=True,  # parity with the base backend (LLM-controlled shell)
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            env=self._env,
            cwd=str(self.cwd),
            start_new_session=True,  # own process group → whole tree is killable
        )
        with self._live_lock:
            self._live.add(proc)
        try:
            try:
                stdout, stderr = proc.communicate(timeout=effective_timeout)
            except subprocess.TimeoutExpired:
                self._kill(proc)
                proc.communicate()  # reap
                return ExecuteResponse(
                    output=f"Error: Command timed out after {effective_timeout} seconds.",
                    exit_code=124, truncated=False,
                )
        finally:
            with self._live_lock:
                self._live.discard(proc)

        parts: list[str] = []
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.extend(f"[stderr] {line}" for line in stderr.strip().split("\n"))
        output = "\n".join(parts) if parts else "<no output>"

        truncated = False
        if len(output) > self._max_output_bytes:
            output = output[: self._max_output_bytes]
            output += f"\n\n... Output truncated at {self._max_output_bytes} bytes."
            truncated = True

        code = proc.returncode or 0
        if code != 0:
            output = f"{output.rstrip()}\n\nExit code: {code}"
        return ExecuteResponse(output=output, exit_code=code, truncated=truncated)

    def _kill(self, proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:  # pragma: no cover - non-posix fallback
                proc.kill()
        except (ProcessLookupError, OSError):
            pass

    def terminate_all(self) -> int:
        """Kill every still-running command's process group. Returns the count."""
        with self._live_lock:
            procs = list(self._live)
        for proc in procs:
            self._kill(proc)
        return len(procs)
