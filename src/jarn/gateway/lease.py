"""Per-project-root mutual-exclusion lease (issue #52 / T-PIPE-2).

Contract
--------
One live holder per project root on a host. The first process to acquire wins;
a contended acquire fails immediately (no queue). Different roots never contend.
Global ``~/.jarn`` wiki/memory are *not* covered here — those use store-layer
:func:`jarn.util.atomic.file_lock`.

Lock file
---------
``<root>/.jarn/root.lock``. The path sits under the existing project marker
directory, so creating it cannot invent a *new* project root for
:func:`jarn.config.paths.find_project_root`. The file is left in place on
release (unlinking would race the next opener); ignore ``.jarn/**/*.lock`` in
repos that commit ``.jarn/``.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import BinaryIO, Self

#: Project marker directory (same name as ``jarn.config.paths.PROJECT_DIR_NAME``).
#: Inlined so this module stays free of config imports.
_PROJECT_DIR = ".jarn"

#: Fixed name under ``<root>/.jarn/``. Kept separate from the ``<file>.lock``
#: siblings used by :func:`jarn.util.atomic.file_lock` so a store lock and the
#: root lease never share an inode.
LOCK_FILENAME = "root.lock"


class RootLeaseHeldError(RuntimeError):
    """Raised when another process (or open description) already holds the root."""

    def __init__(self, root: Path, lock_path: Path) -> None:
        self.root = root
        self.lock_path = lock_path
        super().__init__(f"project root lease held: {root} ({lock_path})")


def lock_path_for(root: Path) -> Path:
    """Return ``<root>/.jarn/root.lock`` for *root*."""
    return Path(root).expanduser().resolve() / _PROJECT_DIR / LOCK_FILENAME


class RootLease:
    """Non-blocking exclusive lease on a project root.

    Use as a context manager::

        with RootLease(root):
            ...

    or call :meth:`acquire` / :meth:`release` explicitly. A second acquire on
    the same root raises :exc:`RootLeaseHeldError` immediately.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.lock_path = lock_path_for(self.root)
        self._file: BinaryIO | None = None

    @property
    def held(self) -> bool:
        """True while this instance currently holds the lease."""
        return self._file is not None

    def acquire(self) -> Self:
        """Take the lease without waiting. Raises :exc:`RootLeaseHeldError` if held."""
        if self._file is not None:
            raise RootLeaseHeldError(self.root, self.lock_path)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.lock_path.open("a+b")
        try:
            _try_lock(lock_file)
        except OSError as exc:
            lock_file.close()
            if _is_busy(exc):
                raise RootLeaseHeldError(self.root, self.lock_path) from exc
            raise
        self._file = lock_file
        return self

    def release(self) -> None:
        """Drop the lease. Idempotent when not held."""
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

    def __del__(self) -> None:  # pragma: no cover - best-effort cleanup
        with contextlib.suppress(Exception):
            self.release()


def _try_lock(lock_file: BinaryIO) -> None:
    """Acquire exclusive, non-blocking. Raises ``OSError`` when busy or unsupported."""
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
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
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        import msvcrt

        lock_file.seek(0)
        with contextlib.suppress(OSError):
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        return

    import fcntl

    with contextlib.suppress(OSError):
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _is_busy(exc: OSError) -> bool:
    """True when the OS reports the lock is already held (not a hard I/O failure)."""
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        # msvcrt.locking raises OSError with errno EDEADLK / EACCES when busy.
        return exc.errno in (
            getattr(os, "EDEADLK", 11),
            getattr(os, "EACCES", 13),
            getattr(os, "EAGAIN", 11),
        )
    import errno

    return exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK)
