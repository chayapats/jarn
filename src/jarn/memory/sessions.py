"""Resumable sessions backed by LangGraph's SQLite checkpointer.

Each conversation runs under a ``thread_id``; LangGraph persists the graph state
after every step so a session can be resumed after a crash or restart. The DB
lives at ``<project>/.jarn/state.sqlite`` (or the global home when run outside a
project) and should be gitignored.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from jarn.config import paths


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
