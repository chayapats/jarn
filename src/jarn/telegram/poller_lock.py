"""Host-local flock for the Telegram long-poll process (#53 / T-TG-2).

Same-host exclusion: first holder wins (restart racing predecessor, stale
unit, manual second run). Across hosts exclusion is impossible — stand down
on the first Telegram 409 instead (see :mod:`jarn.telegram.bot`).

Never call ``logOut`` from this package.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import BinaryIO, Self

from jarn.config import paths

__all__ = [
    "POLLER_LOCK_FILENAME",
    "PollerLock",
    "PollerLockHeldError",
    "poller_lock_path",
]

#: Under ``~/.jarn/gateway/``. Left in place on release (unlink races).
POLLER_LOCK_FILENAME = "telegram.poll.lock"


class PollerLockHeldError(RuntimeError):
    """Another process on this host already holds the Telegram poller flock."""

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        super().__init__(f"telegram poller lock held: {lock_path}")


def poller_lock_path(*, home: Path | None = None) -> Path:
    """Return ``~/.jarn/gateway/telegram.poll.lock`` (or under *home*)."""
    base = Path(home) if home is not None else paths.global_home()
    return base / "gateway" / POLLER_LOCK_FILENAME


class PollerLock:
    """Non-blocking exclusive flock for the single Telegram long-poller on a host.

    Use as a context manager::

        with PollerLock():
            await run_polling(...)

    Contended acquire raises :exc:`PollerLockHeldError` immediately (no queue).
    """

    def __init__(self, path: Path | None = None) -> None:
        self.lock_path = path if path is not None else poller_lock_path()
        self._file: BinaryIO | None = None

    @property
    def held(self) -> bool:
        return self._file is not None

    def acquire(self) -> Self:
        if self._file is not None:
            raise PollerLockHeldError(self.lock_path)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.lock_path.open("a+b")
        try:
            _try_lock(lock_file)
        except OSError as exc:
            lock_file.close()
            if _is_busy(exc):
                raise PollerLockHeldError(self.lock_path) from exc
            raise
        self._file = lock_file
        return self

    def release(self) -> None:
        lock_file = self._file
        if lock_file is None:
            return
        self._file = None
        try:
            _unlock(lock_file)
        finally:
            lock_file.close()

    def __enter__(self) -> Self:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()

    def __del__(self) -> None:  # pragma: no cover
        with contextlib.suppress(Exception):
            self.release()


def _try_lock(lock_file: BinaryIO) -> None:
    if os.name == "nt":  # pragma: no cover
        import msvcrt

        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        return

    import fcntl

    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(lock_file: BinaryIO) -> None:
    if os.name == "nt":  # pragma: no cover
        import msvcrt

        lock_file.seek(0)
        with contextlib.suppress(OSError):
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        return

    import fcntl

    with contextlib.suppress(OSError):
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _is_busy(exc: OSError) -> bool:
    if os.name == "nt":  # pragma: no cover
        return exc.errno in (
            getattr(os, "EDEADLK", 11),
            getattr(os, "EACCES", 13),
            getattr(os, "EAGAIN", 11),
        )
    import errno

    return exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK)
