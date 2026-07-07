"""Shared process-group termination helpers."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time


def _pg_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def _wait_dead(alive_fn, grace_secs: float) -> None:
    deadline = time.monotonic() + grace_secs
    while time.monotonic() < deadline:
        if not alive_fn():
            return
        time.sleep(0.05)


def _terminate_windows(pid: int) -> None:
    """Best-effort terminate the process tree rooted at *pid* on Windows.

    Windows has no POSIX process groups (``start_new_session`` is a no-op there)
    and no graceful ``SIGTERM``, so ``taskkill /T`` — which kills the whole tree —
    is the closest analog to ``killpg``; a single ``TerminateProcess`` (via
    ``os.kill``) is the fallback when ``taskkill`` is unavailable.

    The bounded ``timeout`` guarantees this never blocks the caller. We must NOT
    reuse the POSIX ``os.kill(pid, 0)`` liveness probe here: on Windows that call
    maps to ``TerminateProcess`` (it is destructive, not a probe) and busy-waiting
    on it spins until the process is force-killed — the cause of the original
    Windows CI hang.
    """
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            check=False,
            capture_output=True,
            timeout=5,
        )
        return
    except (OSError, subprocess.SubprocessError):
        # taskkill missing or timed out — fall back to a single-process kill.
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)


def terminate_process_group(pid: int, *, grace_secs: float = 0) -> None:
    """Best-effort terminate the whole process tree rooted at *pid*.

    On POSIX uses ``killpg`` (``SIGTERM`` then ``SIGKILL`` after *grace_secs*
    when it is > 0, else an immediate ``SIGKILL``). On Windows delegates to
    :func:`_terminate_windows` (``taskkill /T``); *grace_secs* is ignored there
    because the platform has no graceful terminate.
    """
    try:
        if os.name == "posix":
            pgid = os.getpgid(pid)
            if grace_secs > 0:
                os.killpg(pgid, signal.SIGTERM)
                _wait_dead(lambda: _pg_alive(pgid), grace_secs)
                if _pg_alive(pgid):
                    os.killpg(pgid, signal.SIGKILL)
            else:
                os.killpg(pgid, signal.SIGKILL)
        else:
            _terminate_windows(pid)
    except (ProcessLookupError, PermissionError, OSError):
        pass
