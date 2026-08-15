"""Resumable sessions backed by LangGraph's SQLite checkpointer.

Each conversation runs under a ``thread_id``; LangGraph persists the graph state
after every step so a session can be resumed after a crash or restart. The DB
lives at ``<project>/.jarn/state.sqlite`` (or the global home when run outside a
project) and should be gitignored.

The :class:`TranscriptWriter` companion appends one JSON object per line to
``<project>/.jarn/sessions/<session_id>.jsonl`` so sessions are grep-friendly
and survive crashes (partial transcript beats no transcript).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from jarn.config import paths
from jarn.config.secrets import redact_secrets as _central_redact_secrets
from jarn.util.atomic import lock_path_for

# Maximum characters retained from a tool output in the transcript.
# Large outputs (e.g. full file reads) are truncated to keep JSONL files sane.
_TRANSCRIPT_MAX_TOOL_CHARS = 2_000

#: Depth limit when walking a tool-argument value. Tool arguments are shaped by the
#: model and by MCP servers, so the structure is not ours to trust: a deeply nested
#: payload must not recurse until the stack gives out.
_TRANSCRIPT_MAX_ARG_DEPTH = 6

#: Element limit per container. Capping each string bounds every leaf, but a list of
#: ten thousand short strings is still a record nobody wants on disk.
_TRANSCRIPT_MAX_ARG_ITEMS = 100

#: Characters retained across ALL arguments of one tool call.
#:
#: The two bounds above are per-container, so they MULTIPLY: 100 items at each of 6
#: levels is 10**12 leaves. A structure comfortably inside both can still write an
#: enormous record — measured, a 60x60x60 nesting of 200-character strings produced a
#: 44 MB line. This is the bound that actually holds, and it is shared by every
#: argument of the call rather than granted per key.
_TRANSCRIPT_MAX_ARG_TOTAL_CHARS = 20_000
_SQLITE_BUSY_TIMEOUT_MS = 5_000
_SQLITE_SETUP_ATTEMPTS = 8
_SQLITE_RETRY_BASE_SECS = 0.025
_SAFE_THREAD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class UnsafeSessionExportError(OSError):
    """A transcript export path crossed a symbolic-link safety boundary."""


def _lexical_absolute(path: Path) -> Path:
    """Return an absolute path without resolving any filesystem links."""

    expanded = path.expanduser()
    return Path(os.path.abspath(os.fspath(expanded)))


def _refuse_symlink_components(path: Path) -> None:
    """Reject existing symlinks/non-directories in an output parent chain.

    ``Path.resolve()`` is intentionally forbidden here: resolving first erases
    the evidence that a caller-supplied parent was a link and lets an export
    escape to the link target. Missing components are fine; the atomic writer
    creates them only after every existing ancestor has passed this check.
    """

    chain: list[Path] = []
    current = path
    while True:
        chain.append(current)
        if current == current.parent:
            break
        current = current.parent
    for component in reversed(chain):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise UnsafeSessionExportError(f"refusing symbolic-link export parent: {component}")
        if not stat.S_ISDIR(info.st_mode):
            raise UnsafeSessionExportError(f"refusing non-directory export parent: {component}")


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _read_transcript_for_export(path: Path) -> str:
    """Read one regular transcript without following its file/directory links."""

    if path.parent.is_symlink():
        raise UnsafeSessionExportError(
            f"refusing symbolic-link transcript directory: {path.parent}"
        )
    if path.is_symlink():
        raise UnsafeSessionExportError(f"refusing symbolic-link transcript: {path}")

    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        entry_identity = path.lstat()
        if not stat.S_ISREG(entry_identity.st_mode):
            raise UnsafeSessionExportError(f"refusing non-regular transcript: {path}")
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            opened_identity = os.fstat(handle.fileno())
            if not _same_identity(entry_identity, opened_identity):
                raise UnsafeSessionExportError(
                    f"transcript changed while export was opening it: {path}"
                )
            return handle.read()

    parent_fd: int | None = None
    descriptor: int | None = None
    try:
        parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        parent_flags |= getattr(os, "O_NOFOLLOW", 0)
        parent_fd = os.open(path.parent, parent_flags)
        parent_identity = os.fstat(parent_fd)
        if not stat.S_ISDIR(parent_identity.st_mode):
            raise UnsafeSessionExportError(
                f"refusing non-directory transcript parent: {path.parent}"
            )

        try:
            entry_identity = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except TypeError:  # pragma: no cover - old/limited Windows Python
            entry_identity = path.lstat()
        if stat.S_ISLNK(entry_identity.st_mode):
            raise UnsafeSessionExportError(f"refusing symbolic-link transcript: {path}")
        if not stat.S_ISREG(entry_identity.st_mode):
            raise UnsafeSessionExportError(f"refusing non-regular transcript: {path}")

        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path.name, flags, dir_fd=parent_fd)
        except TypeError:  # pragma: no cover - old/limited Windows Python
            descriptor = os.open(path, flags)
        opened_identity = os.fstat(descriptor)
        if not stat.S_ISREG(opened_identity.st_mode) or not _same_identity(
            entry_identity, opened_identity
        ):
            raise UnsafeSessionExportError(
                f"transcript changed while export was opening it: {path}"
            )

        # Detect a directory swap before treating the read as successful. The
        # open descriptor remains safely pinned either way, but a changed path
        # must not be reported as the transcript the user selected.
        current_parent = path.parent.lstat()
        if not _same_identity(parent_identity, current_parent):
            raise UnsafeSessionExportError(
                f"transcript directory changed during export: {path.parent}"
            )
        with os.fdopen(descriptor, "r", encoding="utf-8", errors="replace") as handle:
            descriptor = None
            return handle.read()
    finally:
        if descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        if parent_fd is not None:
            with contextlib.suppress(OSError):
                os.close(parent_fd)


def _destination_entry(parent_fd: int, destination: Path) -> os.stat_result | None:
    try:
        return os.stat(destination.name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except TypeError:  # pragma: no cover - old/limited Windows Python
        try:
            return destination.lstat()
        except FileNotFoundError:
            return None


def _validate_export_destination_entry(entry: os.stat_result | None, destination: Path) -> None:
    if entry is None:
        return
    if stat.S_ISLNK(entry.st_mode):
        raise UnsafeSessionExportError(f"refusing symbolic-link export destination: {destination}")
    if not stat.S_ISREG(entry.st_mode):
        raise UnsafeSessionExportError(f"refusing non-regular export destination: {destination}")


def _atomic_write_session_export(destination: Path, text: str) -> None:
    """Publish a private export without following destination-side links."""

    _refuse_symlink_components(destination.parent)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _refuse_symlink_components(destination.parent)
    if destination.is_symlink():
        raise UnsafeSessionExportError(f"refusing symbolic-link export destination: {destination}")

    # ``dir_fd`` pins the inspected directory through creation and rename. This
    # closes the check/use gap where a malicious project swaps an output parent
    # for a symlink after validation. Windows lacks this complete API; its
    # fallback still performs both static checks and uses atomic os.replace,
    # which replaces a destination link rather than following it.
    supports_dir_fd = (
        os.name != "nt"
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.rename in os.supports_dir_fd
    )
    if not supports_dir_fd:  # pragma: no cover - exercised on Windows CI
        from jarn.util.atomic import atomic_write_text

        atomic_write_text(destination, text, mode=None if os.name == "nt" else 0o600)
        return

    parent_fd: int | None = None
    descriptor: int | None = None
    temp_name = f".jarn-session-export-{uuid.uuid4().hex}.tmp"
    temp_created = False
    try:
        parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        parent_flags |= getattr(os, "O_NOFOLLOW", 0)
        parent_fd = os.open(destination.parent, parent_flags)
        parent_identity = os.fstat(parent_fd)
        if not stat.S_ISDIR(parent_identity.st_mode):
            raise UnsafeSessionExportError(
                f"refusing non-directory export parent: {destination.parent}"
            )
        current_parent = destination.parent.lstat()
        if not _same_identity(parent_identity, current_parent):
            raise UnsafeSessionExportError(
                f"export directory changed during validation: {destination.parent}"
            )

        _validate_export_destination_entry(_destination_entry(parent_fd, destination), destination)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(temp_name, flags, 0o600, dir_fd=parent_fd)
        temp_created = True
        os.fchmod(descriptor, 0o600)
        payload = text.encode("utf-8")
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("session export write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None

        # A newly introduced target symlink is never followed by atomic rename,
        # but fail closed instead of silently deleting a link the user did not
        # approve replacing.
        _validate_export_destination_entry(_destination_entry(parent_fd, destination), destination)
        current_parent = destination.parent.lstat()
        if not _same_identity(parent_identity, current_parent):
            raise UnsafeSessionExportError(
                f"export directory changed before publication: {destination.parent}"
            )
        os.rename(
            temp_name,
            destination.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temp_created = False
        os.fsync(parent_fd)

        published = _destination_entry(parent_fd, destination)
        lexical_published = destination.lstat()
        if published is None or not _same_identity(published, lexical_published):
            raise UnsafeSessionExportError(
                f"export destination changed during publication: {destination}"
            )
    finally:
        if descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        if temp_created and parent_fd is not None:
            with contextlib.suppress(OSError):
                os.unlink(temp_name, dir_fd=parent_fd)
        if parent_fd is not None:
            with contextlib.suppress(OSError):
                os.close(parent_fd)


def _safe_thread_id(thread_id: str) -> str:
    value = str(thread_id)
    if not _SAFE_THREAD_ID.fullmatch(value) or value in {".", ".."}:
        raise ValueError("session id contains unsafe path characters")
    return value


def _is_sqlite_busy(exc: BaseException) -> bool:
    """Return whether *exc* is SQLite's transient lock-contention error."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int) and code & 0xFF == sqlite3.SQLITE_BUSY:
        return True
    return "database is locked" in str(exc).lower()


def _try_acquire_transcript_lease(path: Path) -> BinaryIO | None:
    """Take the transcript's cross-process lifetime lease without waiting.

    Deletion must never hang behind a live session, and a writer must never wait
    for deletion to finish and then recreate an orphaned transcript. Both sides
    therefore use the same non-blocking sibling lock and fail closed on either
    contention or a filesystem that cannot provide the lock.
    """

    lock = lock_path_for(path)
    descriptor: int | None = None
    handle: BinaryIO | None = None
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock, flags, 0o600)
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        handle = os.fdopen(descriptor, "r+b", buffering=0)
        descriptor = None
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError:
        if handle is not None:
            handle.close()
        elif descriptor is not None:
            os.close(descriptor)
        return None


def _release_transcript_lease(handle: BinaryIO) -> None:
    """Release and close a lease returned by :func:`_try_acquire_transcript_lease`."""

    try:
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI
            import msvcrt

            handle.seek(0)
            with contextlib.suppress(OSError):
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def redact_secrets(text: str) -> str:
    """Thin back-compat alias for the central secret redactor.

    Delegates to :func:`jarn.config.secrets.redact_secrets` so transcript
    scrubbing stays in lockstep with log/error scrubbing. User prompts and
    assistant replies are run through it before they are persisted.
    """
    return _central_redact_secrets(text)


def default_db_path(project_root: Path | None = None) -> Path:
    db = paths.project_state_db(project_root)
    if db is not None:
        db.parent.mkdir(parents=True, exist_ok=True)
        return db
    home = paths.global_home()
    home.mkdir(parents=True, exist_ok=True)
    return home / "state.sqlite"


def new_thread_id() -> str:
    return uuid.uuid4().hex


@contextmanager
def open_checkpointer(db_path: Path | None = None):
    """Yield a LangGraph SqliteSaver bound to ``db_path``.

    Used as ``checkpointer=`` when compiling the deep agent.
    """
    saver, conn = create_checkpointer(db_path)
    try:
        yield saver
    finally:
        conn.close()


def create_checkpointer(db_path: Path | None = None):
    """Create a SqliteSaver and its connection for an app-lifetime checkpointer.

    Returns ``(saver, connection)``; the caller is responsible for closing the
    connection on shutdown. Use :func:`open_checkpointer` for scoped use instead.

    NOTE: this is the *sync* saver. The TUI drives the agent with async streaming
    and must use :func:`create_async_checkpointer` instead — a sync saver raises
    "does not support async methods" under ``astream``.
    """
    from langgraph.checkpoint.sqlite import SqliteSaver

    path = db_path or default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver, conn


async def create_async_checkpointer(db_path: Path | None = None):
    """Create an AsyncSqliteSaver for an app-lifetime, async-driven checkpointer.

    Returns ``(saver, context_manager)``. Close on shutdown with
    ``await context_manager.__aexit__(None, None, None)``. Required whenever the
    graph is run with ``astream``/``ainvoke`` (i.e. the TUI).
    """
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    path = db_path or default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    cm = AsyncSqliteSaver.from_conn_string(str(path))
    saver = await cm.__aenter__()
    try:
        # LangGraph's setup starts with ``PRAGMA journal_mode=WAL``. On a new DB,
        # two processes can race that mode change; SQLite returns BUSY immediately
        # for this operation instead of honoring the connection's default timeout.
        cursor = await saver.conn.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
        await cursor.close()
        for attempt in range(_SQLITE_SETUP_ATTEMPTS):
            try:
                await saver.setup()
                break
            except sqlite3.OperationalError as exc:
                if not _is_sqlite_busy(exc) or attempt == _SQLITE_SETUP_ATTEMPTS - 1:
                    raise
                delay = min(_SQLITE_RETRY_BASE_SECS * (2**attempt), 0.25)
                await asyncio.sleep(delay)
    except BaseException:
        # Do not leak aiosqlite's worker thread/connection when setup never succeeds.
        with contextlib.suppress(Exception):
            await cm.__aexit__(None, None, None)
        raise
    return saver, cm


@dataclass(slots=True, frozen=True)
class SessionInfo:
    thread_id: str
    title: str
    updated_at: float
    project_root: str = ""
    model: str = ""
    state: str = "complete"

    @property
    def updated_human(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.updated_at))


def session_label(session: Any) -> str:
    """Compact label shared by `/sessions` and the `/resume` picker.

    ``{updated}  {title}  {thread_id[:8]}`` — plain text, no markup.
    """
    title = getattr(session, "title", "") or "(untitled)"
    return f"{session.updated_human}  {title}  {str(session.thread_id)[:8]}"


class SessionIndex:
    """A tiny side-table mapping thread ids to human titles/timestamps.

    LangGraph stores the heavy checkpoint blobs; this keeps a lightweight,
    queryable list for the ``/sessions`` picker without parsing checkpoints.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_table()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path), check_same_thread=False)

    def _init_table(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jarn_sessions (
                    thread_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

            # Additive, in-place migration for databases created by releases
            # before the GA session lifecycle fields existed. SQLite preserves
            # every old row and makes this safe to re-run.
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(jarn_sessions)")}
            additions = {
                "project_root": "TEXT NOT NULL DEFAULT ''",
                "model": "TEXT NOT NULL DEFAULT ''",
                "state": "TEXT NOT NULL DEFAULT 'complete'",
                "schema_version": "INTEGER NOT NULL DEFAULT 1",
            }
            for name, declaration in additions.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE jarn_sessions ADD COLUMN {name} {declaration}")

    def touch(
        self,
        thread_id: str,
        title: str,
        *,
        when: float,
        project_root: str | Path | None = None,
        model: str | None = None,
        state: str = "incomplete",
    ) -> None:
        """Insert or bump ``updated_at`` for a session row.

        The title is set only on first insert — later touches refresh the
        timestamp without renaming the session (first user prompt wins).
        """
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO jarn_sessions
                    (thread_id, title, updated_at, project_root, model, state, schema_version)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(thread_id) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    project_root=CASE
                        WHEN excluded.project_root != '' THEN excluded.project_root
                        ELSE jarn_sessions.project_root
                    END,
                    model=CASE
                        WHEN excluded.model != '' THEN excluded.model
                        ELSE jarn_sessions.model
                    END,
                    state=excluded.state,
                    schema_version=1
                """,
                (
                    thread_id,
                    title[:120],
                    when,
                    str(project_root or ""),
                    str(model or ""),
                    state,
                ),
            )

    def mark_complete(self, thread_id: str, *, when: float | None = None) -> bool:
        """Mark a turn as durably complete after its terminal event."""
        timestamp = time.time() if when is None else when
        with self._conn() as conn:
            result = conn.execute(
                "UPDATE jarn_sessions SET state='complete', updated_at=? WHERE thread_id=?",
                (timestamp, thread_id),
            )
        return bool(result.rowcount)

    def get(self, thread_id: str) -> SessionInfo | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT thread_id, title, updated_at, project_root, model, state "
                "FROM jarn_sessions WHERE thread_id=?",
                (thread_id,),
            ).fetchone()
        return SessionInfo(*row) if row else None

    def list(self, limit: int = 30) -> list[SessionInfo]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT thread_id, title, updated_at, project_root, model, state "
                "FROM jarn_sessions "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [SessionInfo(*row) for row in rows]

    def latest_incomplete(self) -> SessionInfo | None:
        """Return the most recent interrupted session, if one exists."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT thread_id, title, updated_at, project_root, model, state "
                "FROM jarn_sessions WHERE state='incomplete' "
                "ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        return SessionInfo(*row) if row else None

    def transcript_path(self, thread_id: str) -> Path:
        return self.db_path.parent / "sessions" / f"{_safe_thread_id(thread_id)}.jsonl"

    def export(self, thread_id: str, destination: Path) -> Path:
        """Export valid redacted transcript records with an atomic write."""
        source = self.transcript_path(thread_id)
        if self.get(thread_id) is None:
            raise KeyError(f"Unknown session {thread_id!r}")
        try:
            transcript = _read_transcript_for_export(source)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"Session {thread_id!r} has no transcript") from exc

        lines: list[str] = []
        for raw in transcript.splitlines():
            try:
                record = json.loads(raw)
            except (TypeError, ValueError):
                continue
            safe = _redact_json_value(record)
            lines.append(json.dumps(safe, ensure_ascii=False))
        destination = _lexical_absolute(destination)
        _atomic_write_session_export(
            destination,
            "\n".join(lines) + ("\n" if lines else ""),
        )
        return destination

    def delete(self, thread_id: str) -> bool:
        """Delete one session without making a failed transcript unlink final.

        Filesystem and SQLite changes cannot share one transaction. The
        transcript is therefore renamed to a same-directory tombstone first,
        database rows are deleted in an uncommitted transaction, and the
        tombstone is unlinked before that transaction commits. If staging or
        unlinking fails, every database deletion is rolled back and the
        transcript is renamed back, leaving the indexed session retryable.
        """
        source = self.transcript_path(thread_id)
        tombstone = source.with_name(f".{source.name}.delete-pending")
        lease = _try_acquire_transcript_lease(source)
        if lease is None:
            # A live TranscriptWriter owns this lease for its whole open
            # lifetime. Refuse immediately: waiting until it closes and then
            # deleting would race user-visible writes and can strand DB/file
            # state on opposite sides of the deletion.
            return False
        try:
            return self._delete_while_leased(thread_id, source, tombstone)
        finally:
            _release_transcript_lease(lease)

    def _delete_while_leased(
        self,
        thread_id: str,
        source: Path,
        tombstone: Path,
    ) -> bool:
        """Perform deletion while the caller owns the transcript lease."""

        # Recover a crash that happened after staging but before the database
        # transaction completed. Never overwrite an independently created
        # transcript if an uncooperative process ignored the shared lock.
        if tombstone.exists() or tombstone.is_symlink():
            if source.exists() or source.is_symlink():
                return False
            try:
                os.replace(tombstone, source)
            except OSError:
                return False

        staged = False
        if source.exists() or source.is_symlink():
            try:
                os.replace(source, tombstone)
            except OSError:
                return False
            staged = True

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for table in (
                "checkpoint_writes",
                "writes",
                "checkpoint_blobs",
                "checkpoints",
            ):
                if table not in tables:
                    continue
                columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
                if "thread_id" in columns:
                    conn.execute(f"DELETE FROM {table} WHERE thread_id=?", (thread_id,))
            result = conn.execute("DELETE FROM jarn_sessions WHERE thread_id=?", (thread_id,))
            removed = bool(result.rowcount)

            if staged:
                try:
                    tombstone.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    conn.rollback()
                    if not (source.exists() or source.is_symlink()):
                        with contextlib.suppress(OSError):
                            os.replace(tombstone, source)
                    return False
            conn.commit()
            return removed
        except BaseException:
            conn.rollback()
            if staged and not (source.exists() or source.is_symlink()):
                with contextlib.suppress(OSError):
                    os.replace(tombstone, source)
            raise
        finally:
            conn.close()


def _redact_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return _central_redact_secrets(value)
    if isinstance(value, dict):
        return {str(key): _redact_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_json_value(item) for item in value]
    return value


def _cap_transcript_arg(
    value: Any, *, depth: int = 0, budget: list[int] | None = None
) -> tuple[Any, bool]:
    """Redact and size-cap one tool-argument value. Returns ``(value, truncated)``.

    ``budget`` is a single-element list holding the characters still available to the
    whole call — a mutable cell so the walk spends one shared allowance instead of
    granting each branch its own. Callers pass one; the default exists so the helper
    can be exercised on its own.

    :meth:`TranscriptWriter.append` states that the caller must sanitise before
    passing a record in, and :meth:`TranscriptWriter.write_tool` is that caller. It
    enforced both halves of the contract — the redactor and the length cap — on
    TOP-LEVEL strings only, so a string reached through a list or a dict got
    neither. Structured tool arguments are ordinary (an edit list, an MCP tool's
    object payload), so this walks the whole value.

    Containers are bounded as well as their leaves: ``_TRANSCRIPT_MAX_ARG_DEPTH``
    stops the walk, ``_TRANSCRIPT_MAX_ARG_ITEMS`` stops a very wide one. Both mark
    the result truncated rather than dropping it silently. Scalars pass through
    untouched, exactly as before — ``append`` serialises with a plain
    ``json.dumps``, so this must not start converting types it used to leave alone.
    """
    if budget is None:
        budget = [_TRANSCRIPT_MAX_ARG_TOTAL_CHARS]

    if isinstance(value, str):
        redacted = _central_redact_secrets(value)
        allowance = min(_TRANSCRIPT_MAX_TOOL_CHARS, max(0, budget[0]))
        if len(redacted) > allowance:
            budget[0] -= allowance
            return redacted[:allowance], True
        budget[0] -= len(redacted)
        return redacted, False

    if isinstance(value, dict):
        if depth >= _TRANSCRIPT_MAX_ARG_DEPTH:
            return f"<dict omitted: deeper than {_TRANSCRIPT_MAX_ARG_DEPTH} levels>", True
        out: dict[str, Any] = {}
        truncated = False
        for index, (key, item) in enumerate(value.items()):
            if index >= _TRANSCRIPT_MAX_ARG_ITEMS or budget[0] <= 0:
                truncated = True
                break
            capped, item_truncated = _cap_transcript_arg(item, depth=depth + 1, budget=budget)
            out[str(key)] = capped
            truncated = truncated or item_truncated
        return out, truncated

    if isinstance(value, (list, tuple)):
        if depth >= _TRANSCRIPT_MAX_ARG_DEPTH:
            return f"<list omitted: deeper than {_TRANSCRIPT_MAX_ARG_DEPTH} levels>", True
        items: list[Any] = []
        truncated = False
        for index, item in enumerate(value):
            if index >= _TRANSCRIPT_MAX_ARG_ITEMS or budget[0] <= 0:
                truncated = True
                break
            capped, item_truncated = _cap_transcript_arg(item, depth=depth + 1, budget=budget)
            items.append(capped)
            truncated = truncated or item_truncated
        return items, truncated

    return value, False


def _repair_partial_jsonl(path: Path) -> None:
    """Drop only a crash-truncated final JSONL fragment before appending."""
    if path.is_symlink():
        raise OSError(f"refusing transcript symbolic link: {path}")
    try:
        with path.open("r+b") as handle:
            data = handle.read()
            if not data or data.endswith(b"\n"):
                return
            boundary = data.rfind(b"\n")
            final = data[boundary + 1 :]
            try:
                parsed = json.loads(final)
            except (TypeError, ValueError, UnicodeDecodeError):
                handle.truncate(boundary + 1 if boundary >= 0 else 0)
            else:
                if not isinstance(parsed, dict):
                    handle.truncate(boundary + 1 if boundary >= 0 else 0)
                else:
                    handle.seek(0, os.SEEK_END)
                    handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileNotFoundError:
        return


class TranscriptWriter:
    """Append-only, human-readable JSONL transcript for a single session.

    Each call to :meth:`append` flushes one JSON line immediately so a crash
    leaves a valid (partial) transcript — no data is lost waiting for a buffer
    flush at session end.

    Secret safety: convenience methods pass every string through the central
    redactor, including exact values previously resolved from configured
    environment/keychain/file references.  Callers should still avoid passing
    raw credentials. Large tool outputs are truncated to
    :data:`_TRANSCRIPT_MAX_TOOL_CHARS` so the file stays grep-friendly even for
    sessions with big file reads.

    The file is created lazily on the first :meth:`append` call; the directory
    is created if it does not exist.
    """

    def __init__(self, session_id: str, *, sessions_dir: Path) -> None:
        safe_id = _safe_thread_id(session_id)
        self._path = sessions_dir / f"{safe_id}.jsonl"
        self._sessions_dir = sessions_dir
        self._file: Any = None  # opened lazily on first write
        self._lease: BinaryIO | None = None
        self._write_lock = threading.Lock()

    @property
    def path(self) -> Path:
        """Resolved path to the JSONL transcript file."""
        return self._path

    def append(self, record: dict[str, Any]) -> None:
        """Append *record* as one JSON line, flushed immediately.

        ``record`` must already be serialisable and must not contain secret
        values — the caller is responsible for sanitising before passing here.
        """
        with self._write_lock:
            if self._file is None:
                if self._sessions_dir.is_symlink():
                    raise OSError(
                        f"refusing symbolic-link transcript directory: {self._sessions_dir}"
                    )
                self._sessions_dir.mkdir(parents=True, exist_ok=True)
                lease = _try_acquire_transcript_lease(self._path)
                if lease is None:
                    raise OSError(
                        f"transcript session is already active or cannot be locked: {self._path}"
                    )
                descriptor: int | None = None
                try:
                    # Repair and open under one lease. Keeping that same lease
                    # until close prevents deletion from renaming an open file
                    # and leaving later appends on an unlinked inode.
                    _repair_partial_jsonl(self._path)
                    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
                    flags |= getattr(os, "O_NOFOLLOW", 0)
                    descriptor = os.open(self._path, flags, 0o600)
                    if os.name != "nt":
                        os.fchmod(descriptor, 0o600)
                    opened = os.fdopen(descriptor, "a", encoding="utf-8")
                    descriptor = None
                except BaseException:
                    if descriptor is not None:
                        with contextlib.suppress(OSError):
                            os.close(descriptor)
                    _release_transcript_lease(lease)
                    raise
                self._file = opened
                self._lease = lease

            self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._file.flush()
            os.fsync(self._file.fileno())

    def close(self) -> None:
        """Close the transcript and release its session-lifetime lease."""

        with self._write_lock:
            opened = self._file
            lease = self._lease
            self._file = None
            self._lease = None
            try:
                if opened is not None:
                    opened.close()
            finally:
                if lease is not None:
                    _release_transcript_lease(lease)

    # -- convenience helpers ------------------------------------------------

    def write_user(self, text: str, *, ts: float) -> None:
        """Record a user prompt event (secret-shaped substrings redacted)."""
        self.append({"ts": ts, "type": "user", "text": redact_secrets(text)})

    def write_assistant(self, text: str, *, ts: float) -> None:
        """Record the assistant's final reply text for a turn.

        Callers accumulate TEXT chunks and call this once per turn with the
        joined result so each turn produces a single readable assistant line.
        Secret-shaped substrings are redacted before the line is persisted.
        """
        self.append({"ts": ts, "type": "assistant", "text": redact_secrets(text)})

    def write_tool(
        self,
        name: str,
        *,
        ts: float,
        args: dict[str, Any] | None = None,
        result: str | None = None,
    ) -> None:
        """Record a tool invocation (start) or result (end).

        ``result`` is truncated to :data:`_TRANSCRIPT_MAX_TOOL_CHARS` so large
        payloads (file reads, web pages) don't bloat the transcript.
        """
        record: dict[str, Any] = {"ts": ts, "type": "tool", "name": name}
        if args is not None:
            # Truncate large string argument values so a wiki_write / write_file
            # call with full file content doesn't bloat the transcript JSONL, and
            # run every one through the central redactor so a credential passed in a
            # tool argument is not persisted verbatim. Both apply at any depth — see
            # ``_cap_transcript_arg``; scalars (ints, booleans, None) are kept as-is.
            capped: dict[str, Any] = {}
            # One budget for the whole call, so a wide record cannot spend the
            # allowance once per key.
            budget = [_TRANSCRIPT_MAX_ARG_TOTAL_CHARS]
            for k, v in args.items():
                value, truncated = _cap_transcript_arg(v, budget=budget)
                capped[k] = value
                if truncated:
                    capped[f"{k}__truncated"] = True
            record["args"] = capped
        if result is not None:
            redacted = _central_redact_secrets(result) if isinstance(result, str) else result
            redacted_str = redacted if isinstance(redacted, str) else str(redacted)
            trimmed = redacted_str[:_TRANSCRIPT_MAX_TOOL_CHARS]
            record["result"] = trimmed
            if len(redacted_str) > _TRANSCRIPT_MAX_TOOL_CHARS:
                record["truncated"] = True
        self.append(record)


def make_transcript_writer(
    session_id: str,
    *,
    project_root: Path | None = None,
) -> TranscriptWriter:
    """Construct a :class:`TranscriptWriter` for *session_id*.

    Uses ``<project>/.jarn/sessions/`` when a project root is discoverable,
    falling back to ``~/.jarn/sessions/`` otherwise.  The directory is created
    lazily by :class:`TranscriptWriter` on the first write.
    """
    sessions_dir = paths.project_sessions_dir(project_root)
    if sessions_dir is None:
        sessions_dir = paths.global_sessions_dir()
    return TranscriptWriter(session_id, sessions_dir=sessions_dir)
