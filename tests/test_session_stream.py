"""SessionDriver stream handling edge cases."""

from __future__ import annotations

from langgraph.types import Overwrite

from jarn.agent.session import EventKind, SessionDriver
from jarn.config.schema import PermissionMode
from jarn.cost import CostTracker
from jarn.permissions import PermissionEngine


def test_handle_update_chunk_unwraps_overwrite_messages():
    driver = SessionDriver(
        agent=None,
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=CostTracker(),
        thread_id="t1",
    )
    interrupts: list = []
    ai = type("AIMessage", (), {"tool_calls": [{"name": "web_fetch", "args": {"url": "https://x"}}]})()
    chunk = {"PatchToolCallsMiddleware.before_agent": {"messages": Overwrite([ai])}}
    events = list(driver._handle_update_chunk(chunk, interrupts))
    assert len(events) == 1
    assert events[0].kind is EventKind.TOOL_START
    assert events[0].text == "web_fetch"
