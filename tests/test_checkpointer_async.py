"""Regression: a turn must stream through the async checkpointer without the
'SqliteSaver does not support async methods' error (reported from the TUI)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from jarn.agent.builder import build_runtime
from jarn.agent.session import EventKind, SessionDriver
from jarn.cost import CostTracker
from jarn.memory import create_async_checkpointer
from jarn.permissions import PermissionEngine


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
