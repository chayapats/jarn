"""Shared process-group termination helpers."""

from __future__ import annotations

import os
import signal
import time


def _pg_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _wait_dead(alive_fn, grace_secs: float) -> None:
    deadline = time.monotonic() + grace_secs
    while time.monotonic() < deadline:
        if not alive_fn():
            return
        time.sleep(0.05)


def terminate_process_group(pid: int, *, grace_secs: float = 0) -> None:
    """Best-effort terminate the whole process group rooted at *pid*.

    On POSIX uses ``killpg`` (``SIGTERM`` then optional ``SIGKILL`` when
    *grace_secs* > 0). On other platforms falls back to ``os.kill`` on *pid*.
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
        else:  # pragma: no cover - non-posix fallback
            if grace_secs > 0:
                os.kill(pid, signal.SIGTERM)
                _wait_dead(lambda: _pid_alive(pid), grace_secs)
                if _pid_alive(pid):
                    os.kill(pid, signal.SIGTERM)
            else:
                os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
