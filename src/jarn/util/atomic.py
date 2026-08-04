"""Crash-safe file publication and cross-process write locks.

Two primitives, deliberately separate:

:func:`atomic_write_text`
    Publish a complete file in one step — write a UNIQUE temp file beside the
    target, then ``os.replace`` it into place. A reader therefore sees either the
    old file or the new one, never a truncated one, and two concurrent publishers
    never share a temp path.

:func:`file_lock`
    Hold an exclusive lock across processes AND threads for the duration of a
    read-modify-write, so two writers cannot each read the same starting state and
    then overwrite each other.

**Both are needed, and they solve different halves.** Atomic publication alone
still loses data whenever the new content is derived from the old: two writers
each load ``config.yaml``, each append their own rule, and each publish a complete
file — the second simply wins. Locking alone still exposes a torn file to an
unlocked reader mid-write.

Why these are one module rather than three implementations
----------------------------------------------------------
This code exists because the same two defects were written independently in
several places: a *fixed* temp name (``<path>.tmp``, identical for every writer,
so a collision surfaced as ``FileNotFoundError`` from ``os.replace`` and could
rename a half-written file over the real one) and a bare ``read → concat →
write_text`` with no lock (measured at 89-97% of concurrent appends lost).

Locking discipline — read this before adding a call
---------------------------------------------------
**Lock at the store layer; never inside the publisher.** :func:`atomic_write_text`
deliberately takes no lock. POSIX ``flock`` is per *open file description*, so a
second ``open()`` of the same lock file from the SAME process blocks against the
first — a publisher that locked internally would deadlock the moment a store
wrapped a lock around its own read-modify-write. Callers take :func:`file_lock`
around the whole load-mutate-publish sequence and call the publisher inside it.

For the same reason, do not nest two :func:`file_lock` blocks on one path, and
prefer taking locks *sequentially* when an operation touches two files (a page and
its index) rather than holding both at once.

Failure policy — best-effort, never fatal
-----------------------------------------
Locking requires creating a ``<path>.lock`` sibling. A home directory that is
missing or read-only is a real configuration (see ``jarn.config.paths``), and the
lock must not be what takes the process down: :func:`file_lock` yields ``False``
when it could not acquire, having logged once, and the caller proceeds unlocked —
degraded to the pre-lock behaviour rather than broken. It yields ``True`` when the
lock is genuinely held.
"""

from __future__ import annotations

import contextlib
import logging
import os
import stat
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

_log = logging.getLogger("jarn")

#: Suffix for the sibling lock file. Kept beside the target rather than in a
#: shared directory so acquiring a lock needs no writable location the caller was
#: not already writing to — if the target dir is writable, so is the lock.
LOCK_SUFFIX = ".lock"

#: Windows has no ``flock``; ``msvcrt.locking`` is blocking-with-retry, so poll.
_WINDOWS_RETRY_SECS = 0.01

#: Windows refuses ``os.replace`` with ``ERROR_ACCESS_DENIED`` while ANY process
#: holds the destination open — even for reading. POSIX has no such rule, so the
#: publish that fixes torn reads everywhere else would instead make the write FAIL
#: on Windows, precisely under the concurrency it exists for (a reader on
#: ``index.md`` while a rebuild republishes it). Readers hold the handle for
#: microseconds, so a bounded retry closes it; exhausting the deadline raises
#: rather than falling back to a truncating write, because silently reintroducing
#: the torn read would undo the fix.
_REPLACE_DEADLINE_SECS = 2.0
_REPLACE_RETRY_SECS = 0.005


def lock_path_for(path: Path) -> Path:
    """The sibling lock file guarding *path*."""
    return path.with_name(path.name + LOCK_SUFFIX)


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[bool]:
    """Hold an exclusive cross-process lock on *path* for the block's duration.

    Yields ``True`` when the lock is held and ``False`` when it could not be
    acquired (an unwritable or missing parent directory) — the caller proceeds
    either way, since a lost lock must degrade rather than abort. See the module
    docstring for why this is never taken inside :func:`atomic_write_text`.
    """
    lock_file: BinaryIO | None = None
    lock = lock_path_for(path)
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock_file = lock.open("a+b")
        # Acquisition is INSIDE the guard: flock itself fails on filesystems that
        # do not implement it (ENOLCK on some NFS/FUSE mounts), and letting that
        # escape would break the contract in the worst place — `_rebuild_index`
        # runs on the prompt-assembly path, which never raised before.
        _acquire(lock_file)
    except OSError as exc:
        # Only the acquisition is guarded, never the caller's body: wrapping the
        # yield would swallow a genuine write failure (ENOSPC) as a silent no-op.
        if lock_file is not None:
            lock_file.close()
        _log.debug("Could not take write lock %s: %s (proceeding unlocked)", lock, exc)
        yield False
        return
    try:
        yield True
    finally:
        _release(lock_file)
        lock_file.close()


def _acquire(lock_file: BinaryIO) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        import msvcrt

        # msvcrt locks a BYTE RANGE, so the file must be non-empty to have one.
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        while True:
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                return
            except OSError:
                time.sleep(_WINDOWS_RETRY_SECS)
    else:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)


def _release(lock_file: BinaryIO) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        import msvcrt

        lock_file.seek(0)
        with contextlib.suppress(OSError):
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
    else:
        import fcntl

        with contextlib.suppress(OSError):
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _unique_tmp(path: Path) -> Path:
    """A temp sibling no concurrent writer can collide with.

    The pid alone is not enough — two THREADS in one process share it — so the
    name also carries a uuid4. A fixed ``<path>.tmp`` is what made two writers
    rename each other's partial file and raise ``FileNotFoundError``.
    """
    return path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")


def _replace(tmp: Path, path: Path) -> None:
    """``os.replace``, retried on Windows while a reader holds the destination.

    A no-op wrapper on POSIX, where rename over an open file always succeeds.
    """
    if os.name != "nt":
        os.replace(tmp, path)
        return
    deadline = time.monotonic() + _REPLACE_DEADLINE_SECS  # pragma: no cover - Windows
    while True:  # pragma: no cover - exercised on Windows CI
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_REPLACE_RETRY_SECS)


def atomic_write_text(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
) -> None:
    """Publish *text* at *path* atomically: unique temp file, then ``os.replace``.

    Takes NO lock — see the module docstring. Wrap the caller's whole
    read-modify-write in :func:`file_lock` when the new content derives from the
    old.

    ``mode`` is applied to the temp file BEFORE the rename, so the published file
    is never momentarily readable at the process umask. That matters for a secret:
    ``write_text`` followed by ``chmod`` leaves a window in which the file exists
    with default permissions.

    When ``mode`` is omitted, an EXISTING target's permissions are carried over.
    Publishing by rename installs a new inode, so unlike the in-place ``write_text``
    this replaces the target's mode — without this, a user's ``chmod 600`` on a
    memory page would be silently undone by the next save.

    The temp file is removed if publication fails, so a crashed write leaves the
    original intact and no debris behind.

    Windows notes: the rename is retried briefly (see
    :data:`_REPLACE_DEADLINE_SECS`), and ``mode`` is applied through ``chmod``,
    which on Windows controls only the read-only flag — ``0o600`` lands as
    ``0o666``. Restricting a file to its owner there needs an ACL, which is out of
    scope here; the same limit applied before this helper existed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _unique_tmp(path)
    if mode is None:
        with contextlib.suppress(OSError):
            mode = stat.S_IMODE(path.stat().st_mode)
    try:
        tmp.write_text(text, encoding=encoding)
        if mode is not None:
            tmp.chmod(mode)
        _replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
