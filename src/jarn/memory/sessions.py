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
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarn.config import paths
from jarn.config.secrets import redact_secrets as _central_redact_secrets

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


def _is_sqlite_busy(exc: BaseException) -> bool:
    """Return whether *exc* is SQLite's transient lock-contention error."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int) and code & 0xFF == sqlite3.SQLITE_BUSY:
        return True
    return "database is locked" in str(exc).lower()


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
        cursor = await saver.conn.execute(
            f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}"
        )
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

    @property
    def updated_human(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.updated_at))


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

    def touch(self, thread_id: str, title: str, *, when: float) -> None:
        """Insert or bump ``updated_at`` for a session row.

        The title is set only on first insert — later touches refresh the
        timestamp without renaming the session (first user prompt wins).
        """
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO jarn_sessions (thread_id, title, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    updated_at=excluded.updated_at
                """,
                (thread_id, title[:120], when),
            )

    def list(self, limit: int = 30) -> list[SessionInfo]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT thread_id, title, updated_at FROM jarn_sessions "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [SessionInfo(*row) for row in rows]


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
            capped, item_truncated = _cap_transcript_arg(
                item, depth=depth + 1, budget=budget
            )
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
            capped, item_truncated = _cap_transcript_arg(
                item, depth=depth + 1, budget=budget
            )
            items.append(capped)
            truncated = truncated or item_truncated
        return items, truncated

    return value, False


class TranscriptWriter:
    """Append-only, human-readable JSONL transcript for a single session.

    Each call to :meth:`append` flushes one JSON line immediately so a crash
    leaves a valid (partial) transcript — no data is lost waiting for a buffer
    flush at session end.

    Secret safety: the writer never receives raw config or environment values.
    Callers must pass only display-safe strings (tool names, text fragments).
    Large tool outputs are truncated to :data:`_TRANSCRIPT_MAX_TOOL_CHARS` so
    the file stays grep-friendly even for sessions with big file reads.

    The file is created lazily on the first :meth:`append` call; the directory
    is created if it does not exist.
    """

    def __init__(self, session_id: str, *, sessions_dir: Path) -> None:
        self._path = sessions_dir / f"{session_id}.jsonl"
        self._sessions_dir = sessions_dir
        self._file: Any = None  # opened lazily on first write

    @property
    def path(self) -> Path:
        """Resolved path to the JSONL transcript file."""
        return self._path

    def append(self, record: dict[str, Any]) -> None:
        """Append *record* as one JSON line, flushed immediately.

        ``record`` must already be serialisable and must not contain secret
        values — the caller is responsible for sanitising before passing here.
        """
        if self._file is None:
            self._sessions_dir.mkdir(parents=True, exist_ok=True)
            self._file = self._path.open("a", encoding="utf-8")  # noqa: WPS515
        self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._file.flush()

    def close(self) -> None:
        """Close the underlying file handle if it was opened."""
        if self._file is not None:
            self._file.close()
            self._file = None

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
