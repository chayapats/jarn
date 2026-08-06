"""Per-root worker supervisor for the Telegram gateway (T-DMN-1 / #35 / #60).

One long-lived worker subprocess per project root, speaking the private NDJSON
protocol over stdin/stdout pipes. The supervisor:

* acquires :class:`~jarn.gateway.lease.RootLease` before spawn (first holder
  wins; contended roots are refused — no queue);
* handshakes ``schema_version`` on spawn and fails fast on mismatch;
* relies on OS pipe buffering for backpressure (blocking writes; no unbounded
  in-process event buffer);
* fails loud on worker death mid-turn (callback / exception; **no** auto-replay);
* evicts when idle ∧ no live bg job ∧ no turn in flight (parked approvals do
  **not** pin workers — #37).
"""

from __future__ import annotations

import contextlib
import logging
import os
import select
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from jarn.agent.process_util import terminate_process_group
from jarn.config import paths as config_paths
from jarn.gateway.lease import RootLease, RootLeaseHeldError
from jarn.gateway.protocol import (
    SCHEMA_VERSION,
    ApprovalVerdictFrame,
    CancelFrame,
    ErrorFrame,
    EventFrame,
    HandshakeFrame,
    InboundFrame,
    OutboundFrame,
    ProtocolError,
    ShutdownFrame,
    StatusFrame,
    SteerFrame,
    TurnFrame,
    UnsupportedSchemaVersion,
    decode_outbound_line,
    encode_line,
)

_log = logging.getLogger("jarn.gateway.daemon")

#: Default idle threshold for eviction (worker-reported ``idle_ms``).
DEFAULT_IDLE_TIMEOUT_MS = 300_000

#: How long to wait for the post-spawn handshake / first status before failing.
_HANDSHAKE_TIMEOUT_SECS = 10.0

#: Grace period when shutting a worker down (SIGTERM → SIGKILL).
_SHUTDOWN_GRACE_SECS = 3.0

#: Default worker argv — pluggable for tests when ``jarn.gateway.worker`` is absent.
DEFAULT_WORKER_COMMAND: tuple[str, ...] = (
    sys.executable,
    "-m",
    "jarn.gateway.worker",
)

WorkerDeathHook = Callable[["WorkerHandle", int | None, str | None], None]
"""``(handle, exit_code, in_flight_thread_id)`` — fail-loud; no auto-replay."""

OutboundHook = Callable[["WorkerHandle", OutboundFrame], None]


class WorkerProtocolError(RuntimeError):
    """Worker failed the spawn handshake or spoke an unreadable frame."""


class WorkerDeadError(RuntimeError):
    """Worker process exited while the daemon still needed it.

    Raised / delivered via :attr:`DaemonSupervisor.on_worker_death`. The
    supervisor never auto-replays the in-flight turn.
    """

    def __init__(
        self,
        root: Path,
        *,
        exit_code: int | None,
        thread_id: str | None = None,
    ) -> None:
        self.root = root
        self.exit_code = exit_code
        self.thread_id = thread_id
        detail = f"exit={exit_code!r}"
        if thread_id is not None:
            detail += f" thread_id={thread_id}"
        super().__init__(f"gateway worker died for {root} ({detail})")


@dataclass
class WorkerHandle:
    """Live (or recently-dead) per-root worker bookkeeping."""

    root: Path
    popen: subprocess.Popen[bytes]
    lease: RootLease
    started_at: float = field(default_factory=time.monotonic)
    status: StatusFrame | None = None
    #: Thread id of the turn the daemon last dispatched (if any still open).
    in_flight_thread_id: str | None = None
    #: Set when the reader thread observes process exit.
    dead: bool = False
    exit_code: int | None = None
    _write_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _ready: threading.Event = field(default_factory=threading.Event, repr=False)
    _handshake_error: BaseException | None = field(default=None, repr=False)
    _reader: threading.Thread | None = field(default=None, repr=False, init=False)

    @property
    def pid(self) -> int | None:
        return self.popen.pid

    @property
    def alive(self) -> bool:
        return not self.dead and self.popen.poll() is None

    @property
    def turn_in_flight(self) -> bool:
        if self.in_flight_thread_id is not None:
            return True
        if self.status is not None:
            return self.status.turn_in_flight
        return False

    def is_evictable(self, *, idle_timeout_ms: int) -> bool:
        """True when idle ∧ no live bg job ∧ no turn in flight.

        Parked approvals are intentionally ignored (#37).
        """
        if self.turn_in_flight or not self.alive:
            return False
        status = self.status
        if status is None:
            return False
        if status.turn_in_flight:
            return False
        if status.live_bg_jobs > 0:
            return False
        return status.idle_ms >= idle_timeout_ms


class DaemonSupervisor:
    """Spawn / reap / restart per-root gateway workers."""

    def __init__(
        self,
        *,
        worker_command: Sequence[str] | None = None,
        idle_timeout_ms: int = DEFAULT_IDLE_TIMEOUT_MS,
        on_worker_death: WorkerDeathHook | None = None,
        on_outbound: OutboundHook | None = None,
        env: Mapping[str, str] | None = None,
        handshake_timeout_secs: float = _HANDSHAKE_TIMEOUT_SECS,
    ) -> None:
        self._worker_command = list(worker_command or DEFAULT_WORKER_COMMAND)
        self.idle_timeout_ms = idle_timeout_ms
        self.on_worker_death = on_worker_death
        self.on_outbound = on_outbound
        self._env = dict(env) if env is not None else None
        self._handshake_timeout_secs = handshake_timeout_secs
        self._workers: dict[Path, WorkerHandle] = {}
        self._lock = threading.RLock()
        self._closed = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def workers(self) -> Mapping[Path, WorkerHandle]:
        """Snapshot of currently tracked workers (alive or mid-reap)."""
        with self._lock:
            return dict(self._workers)

    def get_worker(self, root: Path | str) -> WorkerHandle | None:
        key = _resolve_root(root)
        with self._lock:
            return self._workers.get(key)

    def ensure_worker(self, root: Path | str) -> WorkerHandle:
        """Return a live worker for *root*, spawning + handshaking if needed.

        Raises :exc:`RootLeaseHeldError` when another process already holds the
        root lease. Raises :exc:`WorkerProtocolError` on handshake failure.
        """
        key = _resolve_root(root)
        with self._lock:
            if self._closed:
                raise RuntimeError("DaemonSupervisor is shut down")
            existing = self._workers.get(key)
            if existing is not None and existing.alive:
                return existing
            if existing is not None:
                # Stale dead handle — drop before respawn.
                self._forget_locked(existing, release_lease=True)
            return self._spawn_locked(key)

    def restart_worker(self, root: Path | str) -> WorkerHandle:
        """Reap any existing worker for *root*, then spawn a fresh one."""
        key = _resolve_root(root)
        with self._lock:
            if self._closed:
                raise RuntimeError("DaemonSupervisor is shut down")
            existing = self._workers.get(key)
            if existing is not None:
                self._reap_locked(existing, graceful=True)
            return self._spawn_locked(key)

    def reap_worker(self, root: Path | str, *, graceful: bool = True) -> None:
        """Shut down and forget the worker for *root* (no-op if absent)."""
        key = _resolve_root(root)
        with self._lock:
            existing = self._workers.get(key)
            if existing is None:
                return
            self._reap_locked(existing, graceful=graceful)

    def evict_idle(self) -> list[Path]:
        """Reap workers matching the eviction predicate. Returns reaped roots."""
        reaped: list[Path] = []
        with self._lock:
            for handle in list(self._workers.values()):
                if handle.is_evictable(idle_timeout_ms=self.idle_timeout_ms):
                    self._reap_locked(handle, graceful=True)
                    reaped.append(handle.root)
        return reaped

    def shutdown(self) -> None:
        """Reap every worker and refuse further spawns."""
        with self._lock:
            self._closed = True
            for handle in list(self._workers.values()):
                self._reap_locked(handle, graceful=True)

    # ------------------------------------------------------------------
    # Pipe I/O
    # ------------------------------------------------------------------

    def send(self, root: Path | str, frame: InboundFrame) -> None:
        """Write one inbound frame (blocking; OS-pipe backpressure)."""
        handle = self.ensure_worker(root)
        self._write_frame(handle, frame)
        if isinstance(frame, TurnFrame):
            handle.in_flight_thread_id = frame.thread_id

    def send_turn(
        self,
        root: Path | str,
        *,
        thread_id: str,
        text: str,
        media: list[Any] | None = None,
    ) -> None:
        from jarn.gateway.protocol import MediaRef

        refs = list(media or [])
        if refs and not isinstance(refs[0], MediaRef):
            raise TypeError("media must be MediaRef instances")
        self.send(root, TurnFrame(thread_id=thread_id, text=text, media=refs))

    def cancel(self, root: Path | str, thread_id: str) -> None:
        self.send(root, CancelFrame(thread_id=thread_id))

    def steer(self, root: Path | str, thread_id: str, text: str) -> None:
        self.send(root, SteerFrame(thread_id=thread_id, text=text))

    def send_approval_verdict(
        self,
        root: Path | str,
        *,
        token: str,
        approved: bool,
        scope: str = "once",
        message: str = "",
        plan_mode_target: str | None = None,
    ) -> None:
        self.send(
            root,
            ApprovalVerdictFrame(
                token=token,
                approved=approved,
                scope=scope,
                message=message,
                plan_mode_target=plan_mode_target,
            ),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _spawn_locked(self, root: Path) -> WorkerHandle:
        lease = RootLease(root)
        try:
            lease.acquire()
        except RootLeaseHeldError:
            raise

        # Runtime transcripts/state must not be pushed with the project (T-OPS-1).
        config_paths.ensure_project_gitignore(root)

        env = os.environ.copy()
        if self._env is not None:
            env.update(self._env)
        env["JARN_GATEWAY_ROOT"] = str(root)

        try:
            popen = subprocess.Popen(  # noqa: S603 - argv is caller-controlled
                self._worker_command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(root),
                env=env,
                bufsize=0,  # unbuffered binary; we frame by NDJSON lines
                start_new_session=True,
            )
        except Exception:
            lease.release()
            raise

        handle = WorkerHandle(root=root, popen=popen, lease=lease)
        self._workers[root] = handle
        reader = threading.Thread(
            target=self._reader_loop,
            args=(handle,),
            name=f"jarn-gateway-worker-{root.name}",
            daemon=True,
        )
        handle._reader = reader
        reader.start()

        try:
            self._write_frame(handle, HandshakeFrame(schema_version=SCHEMA_VERSION))
        except WorkerDeadError:
            self._forget_locked(handle, release_lease=True)
            raise WorkerProtocolError(
                f"worker for {root} died before accepting handshake"
            ) from None

        if not handle._ready.wait(timeout=self._handshake_timeout_secs):
            self._reap_locked(handle, graceful=False)
            raise WorkerProtocolError(
                f"worker for {root} did not complete handshake within "
                f"{self._handshake_timeout_secs:.1f}s"
            )
        if handle._handshake_error is not None:
            err = handle._handshake_error
            self._forget_locked(handle, release_lease=True)
            if isinstance(err, UnsupportedSchemaVersion):
                raise WorkerProtocolError(str(err)) from err
            raise WorkerProtocolError(f"worker handshake failed: {err}") from err
        if not handle.alive:
            code = handle.exit_code
            self._forget_locked(handle, release_lease=True)
            raise WorkerProtocolError(
                f"worker for {root} exited during handshake (exit={code!r})"
            )
        return handle

    def _write_frame(self, handle: WorkerHandle, frame: InboundFrame) -> None:
        if handle.dead or handle.popen.poll() is not None:
            raise WorkerDeadError(
                handle.root,
                exit_code=handle.exit_code if handle.dead else handle.popen.poll(),
                thread_id=handle.in_flight_thread_id,
            )
        stdin = handle.popen.stdin
        if stdin is None:
            raise WorkerProtocolError(f"worker for {handle.root} has no stdin")
        line = encode_line(frame).encode("utf-8")
        with handle._write_lock:
            try:
                stdin.write(line)
                stdin.flush()
            except BrokenPipeError as exc:
                handle.dead = True
                handle.exit_code = handle.popen.poll()
                raise WorkerDeadError(
                    handle.root,
                    exit_code=handle.exit_code,
                    thread_id=handle.in_flight_thread_id,
                ) from exc

    def _reader_loop(self, handle: WorkerHandle) -> None:
        stdout = handle.popen.stdout
        if stdout is None:
            handle._handshake_error = WorkerProtocolError("worker has no stdout")
            handle._ready.set()
            return
        try:
            while True:
                try:
                    line = stdout.readline()
                except ValueError:
                    # Pipe closed by reap while we were reading.
                    break
                if not line:
                    break
                try:
                    text = line.decode("utf-8")
                    frame = decode_outbound_line(text)
                except (UnicodeDecodeError, ProtocolError) as exc:
                    _log.warning(
                        "gateway worker %s bad outbound frame: %s", handle.root, exc
                    )
                    # Still count as "alive enough" for handshake if we haven't.
                    if not handle._ready.is_set():
                        handle._handshake_error = exc
                        handle._ready.set()
                    continue
                self._dispatch_outbound(handle, frame)
        finally:
            self._mark_dead(handle)

    def _dispatch_outbound(self, handle: WorkerHandle, frame: OutboundFrame) -> None:
        if isinstance(frame, StatusFrame):
            handle.status = frame
            if not frame.turn_in_flight:
                handle.in_flight_thread_id = None
            if not handle._ready.is_set():
                handle._ready.set()
        elif isinstance(frame, ErrorFrame):
            if not handle._ready.is_set():
                if frame.code in {"unsupported_schema_version", "schema_version"}:
                    try:
                        got = int((frame.message or "0").split()[-1])
                    except ValueError:
                        got = -1
                    handle._handshake_error = UnsupportedSchemaVersion(got)
                else:
                    handle._handshake_error = WorkerProtocolError(frame.message)
                handle._ready.set()
            if frame.thread_id and handle.in_flight_thread_id == frame.thread_id:
                # Worker reported turn failure; clear local in-flight marker.
                handle.in_flight_thread_id = None
        elif isinstance(frame, EventFrame):
            if not handle._ready.is_set():
                handle._ready.set()
            kind = frame.kind.lower()
            if kind in {"done", "cancelled", "error"} and (
                handle.in_flight_thread_id is None
                or handle.in_flight_thread_id == frame.thread_id
            ):
                handle.in_flight_thread_id = None

        hook = self.on_outbound
        if hook is not None:
            try:
                hook(handle, frame)
            except Exception:  # noqa: BLE001 - chat layer must not kill reader
                _log.exception(
                    "on_outbound hook failed for worker %s", handle.root
                )

    def _mark_dead(self, handle: WorkerHandle) -> None:
        if handle.dead:
            return
        code = handle.popen.poll()
        # Drain stderr for diagnostics (bounded).
        stderr_tail = _read_stderr_tail(handle.popen.stderr)
        handle.dead = True
        handle.exit_code = code
        in_flight = handle.in_flight_thread_id
        handle.in_flight_thread_id = None
        if not handle._ready.is_set():
            if handle._handshake_error is None:
                msg = f"worker exited during handshake (exit={code!r})"
                if stderr_tail:
                    msg = f"{msg}: {stderr_tail}"
                handle._handshake_error = WorkerProtocolError(msg)
            handle._ready.set()
        # Fail-loud mid-turn (and unexpected post-handshake death): notify
        # chat layer. Never auto-replay.
        hook = self.on_worker_death
        should_notify = in_flight is not None or (
            handle.status is not None and code not in (0, None)
        )
        if hook is not None and should_notify:
            try:
                hook(handle, code, in_flight)
            except Exception:  # noqa: BLE001
                _log.exception(
                    "on_worker_death hook failed for worker %s", handle.root
                )
        if stderr_tail:
            _log.warning(
                "gateway worker %s exited (%s): %s", handle.root, code, stderr_tail
            )

    def _reap_locked(self, handle: WorkerHandle, *, graceful: bool) -> None:
        if handle.alive:
            try:
                if graceful:
                    with contextlib.suppress(
                        WorkerDeadError, WorkerProtocolError, OSError
                    ):
                        self._write_frame(handle, ShutdownFrame())
                    try:
                        handle.popen.wait(timeout=_SHUTDOWN_GRACE_SECS)
                    except subprocess.TimeoutExpired:
                        terminate_process_group(
                            handle.popen.pid,
                            grace_secs=1.0,
                            reap=handle.popen.poll,
                        )
                else:
                    terminate_process_group(
                        handle.popen.pid,
                        grace_secs=0.5,
                        reap=handle.popen.poll,
                    )
            except (ProcessLookupError, OSError):
                pass
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                handle.popen.wait(timeout=2.0)
        self._forget_locked(handle, release_lease=True)

    def _forget_locked(self, handle: WorkerHandle, *, release_lease: bool) -> None:
        self._workers.pop(handle.root, None)
        # Close pipes; the reader thread treats ValueError/EOF as exit.
        # Do not join the reader here — we may hold ``_lock`` and the reader
        # invokes death hooks that call back into the supervisor.
        for stream in (handle.popen.stdin, handle.popen.stdout, handle.popen.stderr):
            if stream is not None:
                with contextlib.suppress(OSError, ValueError):
                    stream.close()
        if release_lease:
            with contextlib.suppress(Exception):
                handle.lease.release()
        handle.dead = True
        if handle.exit_code is None:
            handle.exit_code = handle.popen.poll()


def _resolve_root(root: Path | str) -> Path:
    return Path(root).expanduser().resolve()


def _read_stderr_tail(stderr: IO[bytes] | None, *, limit: int = 2000) -> str:
    if stderr is None:
        return ""
    try:
        # Non-blocking drain so a chatty worker cannot stall reap.
        if hasattr(select, "select"):
            try:
                ready, _, _ = select.select([stderr], [], [], 0)
            except (ValueError, OSError):
                return ""
            if not ready:
                return ""
        data = stderr.read(limit) or b""
    except (OSError, ValueError):
        return ""
    return data.decode("utf-8", errors="replace").strip()


__all__ = [
    "DEFAULT_IDLE_TIMEOUT_MS",
    "DEFAULT_WORKER_COMMAND",
    "DaemonSupervisor",
    "WorkerDeadError",
    "WorkerHandle",
    "WorkerProtocolError",
]
