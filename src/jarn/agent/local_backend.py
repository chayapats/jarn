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

import contextlib
import logging
import queue
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from deepagents.backends import LocalShellBackend
from deepagents.backends.filesystem import _raise_if_symlink_loop
from deepagents.backends.protocol import ExecuteResponse

from jarn.agent.events import ToolProgress
from jarn.agent.process_util import terminate_process_group

logger = logging.getLogger("jarn.agent.local_backend")

#: One-time warning state: emitted at most once per process per backend instance
#: when ``sandbox_mode="auto"`` and no sandbox is available.
_WARNED_SANDBOX_UNAVAILABLE: set[int] = set()

#: Defaults for live foreground-``execute`` progress (only active when a
#: ``progress_sink`` is wired). Heartbeat cadence is intentionally coarse — a
#: quiet build should reassure ("still running… 30s"), not spam.
_DEFAULT_HEARTBEAT_SECS = 5.0
_DEFAULT_TAIL_LINES = 10
_DEFAULT_POLL_SECS = 0.2


@dataclass(slots=True)
class _TailTracker:
    """Rolling output tail + heartbeat bookkeeping for a streaming ``execute``.

    Kept separate from the subprocess plumbing so the *timing* decisions (elapsed
    seconds, whether a quiet spell warrants a heartbeat) are pure and unit-testable
    with an injected ``clock`` — no process needs to be spawned to exercise them.
    ``on_output`` feeds a new stdout/stderr chunk (updating the bounded tail and
    stamping "last activity"); ``maybe_heartbeat`` is polled while the queue is
    quiet and returns a heartbeat :class:`ToolProgress` only once ``heartbeat_secs``
    have elapsed since the last emit."""

    clock: Callable[[], float]
    start: float
    heartbeat_secs: float = _DEFAULT_HEARTBEAT_SECS
    tail_lines: int = _DEFAULT_TAIL_LINES
    command: str = ""
    tool_name: str = "execute"
    _lines: deque[str] = field(init=False, repr=False)
    #: Monotonic time of the last emitted progress OR heartbeat — the quiet-window
    #: anchor. Seeded to ``start`` so the first heartbeat is measured from launch.
    _last_emit: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        self._lines = deque(maxlen=max(1, self.tail_lines))
        self._last_emit = self.start

    def elapsed(self) -> float:
        return max(0.0, self.clock() - self.start)

    def _tail_text(self) -> str:
        # Lines retain their trailing newline, so a plain join reconstructs the
        # visible tail exactly (the deque already bounds it to ``tail_lines``).
        return "".join(self._lines)

    def on_output(self, text: str) -> ToolProgress:
        """Record a new output chunk and return a (non-heartbeat) progress record."""
        for line in text.splitlines(keepends=True):
            self._lines.append(line)
        now = self.clock()
        self._last_emit = now
        return ToolProgress(
            command=self.command,
            chunk=text,
            tail=self._tail_text(),
            elapsed=max(0.0, now - self.start),
            heartbeat=False,
            tool_name=self.tool_name,
        )

    def maybe_heartbeat(self) -> ToolProgress | None:
        """Return a heartbeat record iff output has been quiet for ``heartbeat_secs``."""
        now = self.clock()
        if now - self._last_emit < self.heartbeat_secs:
            return None
        self._last_emit = now
        return ToolProgress(
            command=self.command,
            chunk="",
            tail=self._tail_text(),
            elapsed=max(0.0, now - self.start),
            heartbeat=True,
            tool_name=self.tool_name,
        )


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
    extra_roots:
        Added roots (``--add-dir`` / ``/add-dir``). Absolute paths that resolve
        (symlinks followed) inside one of these are permitted through the
        virtual-mode FS guard, in addition to the primary ``root_dir``. This
        keeps the FS-layer bound in sync with the permission engine's multi-root
        scope. The per-root ``resolve()`` discipline holds: a symlink inside an
        added root that points outside every root does NOT match and falls back
        to the (rejecting) primary-root guard.
    """

    def __init__(
        self,
        *args,
        sandbox_mode: str = "off",
        project_root: Path | None = None,
        sandbox_allow_network: bool = True,
        sandbox_extra_writable: list[Path] | None = None,
        extra_roots: list[Path] | None = None,
        progress_sink: Callable[[ToolProgress], None] | None = None,
        clock: Callable[[], float] | None = None,
        progress_heartbeat_secs: float = _DEFAULT_HEARTBEAT_SECS,
        progress_tail_lines: int = _DEFAULT_TAIL_LINES,
        progress_poll_secs: float = _DEFAULT_POLL_SECS,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._live: set[subprocess.Popen] = set()
        self._live_lock = threading.Lock()
        self._sandbox_mode = sandbox_mode
        self._sandbox_project_root = project_root or Path(self.cwd)
        self._sandbox_allow_network = sandbox_allow_network
        self._sandbox_extra_writable: list[Path] = sandbox_extra_writable or []
        self._extra_roots: list[Path] = [
            Path(p).resolve() for p in (extra_roots or [])
        ]
        # Live foreground-``execute`` progress. When ``progress_sink`` is ``None``
        # (the default, and every existing call site) ``execute`` takes the original
        # blocking ``communicate`` path BYTE-FOR-BYTE — the hot path is untouched.
        # The clock is injected so progress/heartbeat timing is deterministic in
        # tests; it defaults to ``time.monotonic``.
        self._progress_sink = progress_sink
        self._clock: Callable[[], float] = clock or time.monotonic
        self._progress_heartbeat_secs = progress_heartbeat_secs
        self._progress_tail_lines = progress_tail_lines
        self._progress_poll_secs = progress_poll_secs

    def _resolve_path(self, key: str) -> Path:
        """Extend the virtual-mode FS guard to also accept added roots.

        An absolute path whose realpath (symlinks followed) lies inside one of
        ``self._extra_roots`` is allowed and returned as its resolved absolute
        path — mirroring the permission engine's added-root scope so an
        engine-allowed write is not then blocked at the FS layer. Everything else
        (relative/virtual paths, and absolute paths NOT inside an added root)
        falls through to the base virtual-mode guard unchanged. A symlink inside
        an added root that escapes it resolves outside every root, does not match
        here, and is rejected by the base guard — preserving the symlink-escape
        discipline per-root.
        """
        if self._extra_roots:
            candidate = Path(key)
            if candidate.is_absolute():
                try:
                    resolved = candidate.resolve()
                except (OSError, RuntimeError):
                    resolved = None
                if resolved is not None:
                    for r in self._extra_roots:
                        if resolved == r or r in resolved.parents:
                            _raise_if_symlink_loop(resolved)
                            return resolved
        return super()._resolve_path(key)

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

        proc = self._spawn(command, sandbox_argv)
        with self._live_lock:
            self._live.add(proc)
        try:
            # A ``progress_sink`` opts into incremental tailing (a live tail +
            # heartbeat while the command runs). Without one — every existing call
            # site — this is the original blocking ``communicate`` path, unchanged.
            if self._progress_sink is not None:
                outcome = self._communicate_streaming(proc, command, effective_timeout)
            else:
                outcome = self._communicate_blocking(proc, effective_timeout)
        finally:
            with self._live_lock:
                self._live.discard(proc)

        if outcome is None:  # timed out (killed + reaped inside the communicator)
            return ExecuteResponse(
                output=f"Error: Command timed out after {effective_timeout} seconds.",
                exit_code=124, truncated=False,
            )
        stdout, stderr = outcome
        return self._finalize_output(stdout, stderr, proc.returncode or 0)

    def _spawn(self, command: str, sandbox_argv: list[str] | None) -> subprocess.Popen:
        """Start the command in its own process session (killable tree). Identical
        Popen shape as before; only extracted so both the blocking and streaming
        communicators share one spawn site."""
        if sandbox_argv is not None:
            # Sandboxed: argv is fully constructed; do NOT use shell=True.
            return subprocess.Popen(  # noqa: S603
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
        return subprocess.Popen(  # noqa: S602
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

    def _communicate_blocking(
        self, proc: subprocess.Popen, timeout: float
    ) -> tuple[str, str] | None:
        """Original path: block until the command finishes. Returns ``(stdout,
        stderr)``, or ``None`` on timeout (after killing + reaping the tree)."""
        try:
            return proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill(proc)
            proc.communicate()  # reap
            return None

    def _communicate_streaming(
        self, proc: subprocess.Popen, command: str, timeout: float
    ) -> tuple[str, str] | None:
        """Incremental path: drain stdout/stderr through reader threads while the
        command runs, surfacing a live tail + heartbeat via ``progress_sink``.

        stdout and stderr are pumped by two daemon threads into separate buffers
        (so the final ``[stderr]``-prefixed combining is byte-identical to the
        blocking path) AND onto a shared queue that this thread drains: each line
        refreshes the tail and emits progress; a quiet spell (queue idle) emits a
        heartbeat once ``heartbeat_secs`` pass. The whole-command timeout is enforced
        against the injected clock. Returns ``(stdout, stderr)``, or ``None`` on
        timeout (after killing + reaping)."""
        out_buf: list[str] = []
        err_buf: list[str] = []
        q: queue.Queue[tuple[str, str | None]] = queue.Queue()

        def pump(stream, buf: list[str], tag: str) -> None:
            try:
                for line in iter(stream.readline, ""):
                    buf.append(line)
                    q.put((tag, line))
            finally:
                # Best-effort close; the sentinel tells the drain this stream ended.
                with contextlib.suppress(Exception):
                    stream.close()
                q.put((tag, None))

        threads = [
            threading.Thread(target=pump, args=(proc.stdout, out_buf, "out"), daemon=True),
            threading.Thread(target=pump, args=(proc.stderr, err_buf, "err"), daemon=True),
        ]
        for t in threads:
            t.start()

        start = self._clock()
        tracker = _TailTracker(
            clock=self._clock,
            start=start,
            heartbeat_secs=self._progress_heartbeat_secs,
            tail_lines=self._progress_tail_lines,
            command=command,
        )
        # Poll no coarser than the heartbeat cadence, else a heartbeat can't fire on
        # time (it is only checked when the queue goes idle for one poll).
        poll = max(0.01, min(self._progress_poll_secs, self._progress_heartbeat_secs))
        eofs = 0
        timed_out = False
        while eofs < 2:
            if tracker.elapsed() >= timeout:
                timed_out = True
                break
            try:
                _tag, line = q.get(timeout=poll)
            except queue.Empty:
                self._emit_progress(tracker.maybe_heartbeat())
                continue
            if line is None:
                eofs += 1
                continue
            self._emit_progress(tracker.on_output(line))

        if timed_out:
            self._kill(proc)
            for t in threads:
                t.join(timeout=1.0)
            # Reap best-effort; a wedged wait must never hang the turn.
            with contextlib.suppress(Exception):
                proc.wait(timeout=5)
            return None

        for t in threads:
            t.join(timeout=1.0)
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)
        return "".join(out_buf), "".join(err_buf)

    def _emit_progress(self, progress: ToolProgress | None) -> None:
        """Hand a progress record to the sink; a sink error must never break the
        command (progress is a display affordance, not part of execution)."""
        if progress is None or self._progress_sink is None:
            return
        try:
            self._progress_sink(progress)
        except Exception:  # noqa: BLE001 - best-effort; log and keep running
            logger.debug("progress sink raised", exc_info=True)

    def _finalize_output(
        self, stdout: str, stderr: str, code: int
    ) -> ExecuteResponse:
        """Combine stdout/stderr, truncate, and append a nonzero exit code —
        identical to the base backend so streamed and blocking runs read the same."""
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
