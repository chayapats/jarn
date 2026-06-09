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

import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarn.config import paths

# Maximum characters retained from a tool output in the transcript.
# Large outputs (e.g. full file reads) are truncated to keep JSONL files sane.
_TRANSCRIPT_MAX_TOOL_CHARS = 2_000

#: Placeholder substituted for any matched secret-shaped substring.
_REDACTED = "[REDACTED]"

#: Patterns matching common secret shapes that may appear in a prompt or reply.
#: The transcript persists to disk indefinitely and is world-readable to the
#: same user, so we scrub recognised secrets before writing. This is a defensive
#: net, not a guarantee — it catches the common vendor key prefixes and
#: ``NAME=secret`` env-style assignments for sensitive-looking variable names.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Vendor API-key prefixes: sk-..., sk-ant-..., ghp_/gho_/ghs_..., xoxb-...,
    # AKIA... (AWS access key id), AIza... (Google), glpat-..., etc.
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:ghp|gho|ghs|ghr|ghu)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b"),
    # NAME=value assignments where NAME looks sensitive (KEY/TOKEN/SECRET/PASSWORD).
    re.compile(
        r"\b([A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|PASSWD|ACCESS_KEY)[A-Z0-9_]*)"
        r"\s*[=:]\s*\S+",
    ),
)


def redact_secrets(text: str) -> str:
    """Replace recognised secret-shaped substrings in *text* with a placeholder.

    Applied to user prompts and assistant replies before they are written to the
    on-disk transcript so an accidentally-pasted key is not persisted verbatim.
    """
    if not text:
        return text
    redacted = text
    for pat in _SECRET_PATTERNS:
        if pat.groups:
            # Keep the variable name, redact only its value.
            redacted = pat.sub(lambda m: f"{m.group(1)}={_REDACTED}", redacted)
        else:
            redacted = pat.sub(_REDACTED, redacted)
    return redacted


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
    await saver.setup()
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
            # call with full file content doesn't bloat the transcript JSONL.
            # Non-string values (ints, booleans, lists, …) are kept as-is.
            capped: dict[str, Any] = {}
            for k, v in args.items():
                if isinstance(v, str) and len(v) > _TRANSCRIPT_MAX_TOOL_CHARS:
                    capped[k] = v[:_TRANSCRIPT_MAX_TOOL_CHARS]
                    capped[f"{k}__truncated"] = True
                else:
                    capped[k] = v
            record["args"] = capped
        if result is not None:
            trimmed = result[:_TRANSCRIPT_MAX_TOOL_CHARS]
            record["result"] = trimmed
            if len(result) > _TRANSCRIPT_MAX_TOOL_CHARS:
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
