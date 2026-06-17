"""SessionDriver stream handling edge cases."""

from __future__ import annotations

from langgraph.types import Overwrite

from jarn.agent.session import EventKind, SessionDriver
from jarn.config.schema import PermissionMode
from jarn.cost import CostTracker
from jarn.permissions import PermissionEngine


def test_record_usage_attributes_cost_to_requested_tool():
    """The usage-bearing AI chunk's tool call labels its cost in per_tool, and a
    plain reply falls into the response bucket — totals still reconcile."""
    from jarn.cost.tracker import RESPONSE_TOOL

    tracker = CostTracker()
    driver = SessionDriver(
        agent=None,
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=tracker,
        thread_id="t1",
        main_model_ref="claude-opus-4-8",
    )

    tool_msg = type("AIMessage", (), {
        "usage_metadata": {"input_tokens": 100, "output_tokens": 50},
        "response_metadata": {},
        "tool_calls": [{"name": "execute", "args": {"command": "ls"}}],
    })()
    reply_msg = type("AIMessage", (), {
        "usage_metadata": {"input_tokens": 10, "output_tokens": 5},
        "response_metadata": {},
        "tool_calls": [],
    })()
    driver._record_usage(tool_msg)
    driver._record_usage(reply_msg)

    assert set(tracker.per_tool) == {"execute", RESPONSE_TOOL}
    assert tracker.per_tool["execute"].input_tokens == 100
    assert tracker.per_tool[RESPONSE_TOOL].input_tokens == 10
    # Per-tool totals reconcile exactly with the grand total.
    assert sum(u.cost_usd for u in tracker.per_tool.values()) == tracker.total.cost_usd
    assert sum(u.calls for u in tracker.per_tool.values()) == tracker.total.calls == 2


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
