"""Regression: a turn must stream through the async checkpointer without the
'SqliteSaver does not support async methods' error (reported from the TUI)."""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from jarn.agent.builder import build_runtime
from jarn.agent.session import EventKind, SessionDriver
from jarn.cost import CostTracker
from jarn.memory import create_async_checkpointer
from jarn.permissions import PermissionEngine


@pytest.mark.asyncio
async def test_checkpointer_setup_retries_transient_first_run_lock(monkeypatch, tmp_path):
    """Regression for #57: WAL setup retries SQLITE_BUSY after setting a timeout."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    statements: list[str] = []

    class _Cursor:
        async def close(self):
            return None

    class _Connection:
        async def execute(self, statement):
            statements.append(statement)
            return _Cursor()

    class _Saver:
        def __init__(self):
            self.conn = _Connection()
            self.setup_calls = 0

        async def setup(self):
            self.setup_calls += 1
            if self.setup_calls < 3:
                raise sqlite3.OperationalError("database is locked")

    class _ContextManager:
        def __init__(self):
            self.saver = _Saver()

        async def __aenter__(self):
            return self.saver

        async def __aexit__(self, exc_type, exc, tb):
            return None

    cm = _ContextManager()
    monkeypatch.setattr(
        AsyncSqliteSaver,
        "from_conn_string",
        classmethod(lambda cls, conn_string: cm),
    )

    saver, returned_cm = await create_async_checkpointer(tmp_path / "state.sqlite")

    assert saver is cm.saver
    assert returned_cm is cm
    assert cm.saver.setup_calls == 3
    assert statements == ["PRAGMA busy_timeout=5000"]


class _FakeToolModel(GenericFakeChatModel):
    """GenericFakeChatModel + a no-op bind_tools so it works inside an agent."""

    def bind_tools(self, tools, **kwargs):  # noqa: D401, ANN001
        return self


@pytest.mark.asyncio
async def test_turn_streams_with_async_checkpointer(base_config, tmp_path):
    db = tmp_path / "state.sqlite"
    saver, cm = await create_async_checkpointer(db)
    try:
        fake = _FakeToolModel(messages=iter([AIMessage(content="hello back")]))
        with patch("jarn.providers.models.ModelFactory.build", return_value=fake):
            rt = build_runtime(base_config, project_root=tmp_path, checkpointer=saver)

        driver = SessionDriver(
            agent=rt.agent,
            engine=PermissionEngine(),
            tracker=CostTracker(),
            thread_id="t1",
            main_model_ref="x",
        )
        events = [ev async for ev in driver.run_turn("hi")]
    finally:
        await cm.__aexit__(None, None, None)

    kinds = [e.kind for e in events]
    # The turn completed and — crucially — produced no async-checkpointer error.
    assert EventKind.DONE in kinds
    errors = [e.text for e in events if e.kind is EventKind.ERROR]
    assert not errors, f"unexpected errors: {errors}"
    assert not any("async" in e.lower() or "SqliteSaver" in e for e in errors)
