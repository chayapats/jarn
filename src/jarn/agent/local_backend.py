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

When ``sandbox_mode`` is ``"auto"`` or ``"require"`` (from
:attr:`jarn.config.schema.ExecutionConfig.local_sandbox`), each command is wrapped
by :mod:`jarn.agent.os_sandbox` before being handed to Popen so the kernel, not a
regex, enforces write isolation. ``"off"`` (the default) preserves the original
``shell=True`` behaviour exactly.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path

from deepagents.backends import LocalShellBackend
from deepagents.backends.protocol import ExecuteResponse

from jarn.agent.process_util import terminate_process_group

logger = logging.getLogger("jarn.agent.local_backend")

#: One-time warning state: emitted at most once per process per backend instance
#: when ``sandbox_mode="auto"`` and no sandbox is available.
_WARNED_SANDBOX_UNAVAILABLE: set[int] = set()


class CancellableLocalShellBackend(LocalShellBackend):
    """LocalShellBackend whose spawned process tree can be terminated on cancel.

    Parameters
    ----------
    sandbox_mode:
        Controls kernel-enforced OS sandbox behaviour.
        ``"off"``     — disabled; behaves exactly as the original backend.
        ``"auto"``    — sandbox when available; degrade with a one-time warning.
        ``"require"`` — sandbox or fail closed (execute returns exit_code 126).
    project_root:
        Project root exposed to the sandbox as a writable path.  Ignored when
        ``sandbox_mode="off"``.
    sandbox_allow_network:
        When the OS sandbox is active, permit outbound network access.
    sandbox_extra_writable:
        Extra filesystem paths the sandbox may write to (in addition to
        project_root and the default cache/temp set from
        :func:`jarn.agent.os_sandbox.default_writable`).
    """

    def __init__(
        self,
        *args,
        sandbox_mode: str = "off",
        project_root: Path | None = None,
        sandbox_allow_network: bool = True,
        sandbox_extra_writable: list[Path] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._live: set[subprocess.Popen] = set()
        self._live_lock = threading.Lock()
        self._sandbox_mode = sandbox_mode
        self._sandbox_project_root = project_root or Path(self.cwd)
        self._sandbox_allow_network = sandbox_allow_network
        self._sandbox_extra_writable: list[Path] = sandbox_extra_writable or []

    def _build_sandbox_argv(self, command: str) -> list[str] | None:
        """Return a sandboxed argv list, or None to fall back to shell=True.

        Returns ``None`` when the sandbox is off or when ``mode="auto"`` and
        no backend is available (after emitting a warning).  Raises
        :class:`RuntimeError` when ``mode="require"`` and no backend exists.
        """
        if self._sandbox_mode == "off":
            return None

        from jarn.agent import os_sandbox

        if not os_sandbox.available():
            if self._sandbox_mode == "require":
                # Fail closed: caller converts this to a safe error response.
                raise RuntimeError(
                    "OS sandbox backend (sandbox-exec / bwrap) is not available "
                    "on this host but execution.local_sandbox is set to 'require'. "
                    "Install the sandbox tool or set local_sandbox: auto / off."
                )
            # mode == "auto" — degrade with a single warning per backend instance.
            obj_id = id(self)
            if obj_id not in _WARNED_SANDBOX_UNAVAILABLE:
                _WARNED_SANDBOX_UNAVAILABLE.add(obj_id)
                logger.warning(
                    "jarn: OS sandbox requested (local_sandbox: auto) but no "
                    "sandbox backend (sandbox-exec/bwrap) is available on this "
                    "host — running WITHOUT kernel-enforced isolation."
                )
            return None

        from jarn.agent.os_sandbox import default_writable

        writable = [
            *default_writable(self._sandbox_project_root),
            *self._sandbox_extra_writable,
        ]
        return os_sandbox.wrap(
            command,
            project_root=self._sandbox_project_root,
            allow_network=self._sandbox_allow_network,
            writable=writable,
        )

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        if not command or not isinstance(command, str):
            return ExecuteResponse(
                output="Error: Command must be a non-empty string.",
                exit_code=1, truncated=False,
            )
        effective_timeout = timeout if timeout is not None else self._default_timeout
        if effective_timeout <= 0:
            raise ValueError(f"timeout must be positive, got {effective_timeout}")

        # Decide whether to sandbox and build the Popen arguments accordingly.
        try:
            sandbox_argv = self._build_sandbox_argv(command)
        except RuntimeError as exc:
            # mode="require" with no sandbox backend: fail closed.
            return ExecuteResponse(
                output=f"Error: {exc}",
                exit_code=126,
                truncated=False,
            )

        if sandbox_argv is not None:
            # Sandboxed: argv is fully constructed; do NOT use shell=True.
            proc = subprocess.Popen(  # noqa: S603
                sandbox_argv,
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                env=self._env,
                cwd=str(self.cwd),
                start_new_session=True,  # own process group → whole tree is killable
            )
        else:
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
        terminate_process_group(proc.pid)

    def terminate_all(self) -> int:
        """Kill every still-running command's process group. Returns the count."""
        with self._live_lock:
            procs = list(self._live)
        for proc in procs:
            self._kill(proc)
        return len(procs)
