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


# -- resume payload: single bundled vs multi keyed-by-interrupt-id -----------


def test_resume_payload_single_interrupt_is_bundled():
    """One pending interrupt resumes with the legacy bundled {"decisions": [...]}."""
    from jarn.agent.session import _resume_payload

    cmd = _resume_payload([("abc", [{"type": "approve"}, {"type": "reject"}])])
    assert cmd.resume == {"decisions": [{"type": "approve"}, {"type": "reject"}]}


def test_resume_payload_multiple_interrupts_keyed_by_id():
    """Multiple pending interrupts (e.g. one per subagent) must resume keyed by
    interrupt id — otherwise LangGraph raises "you must specify the interrupt id
    when resuming"."""
    from jarn.agent.session import _resume_payload

    cmd = _resume_payload([
        ("id1", [{"type": "approve"}]),
        ("id2", [{"type": "reject", "message": "no"}]),
    ])
    assert cmd.resume == {
        "id1": {"decisions": [{"type": "approve"}]},
        "id2": {"decisions": [{"type": "reject", "message": "no"}]},
    }


def test_resume_payload_falls_back_to_bundled_without_ids():
    """If an interrupt id is missing, fall back to the bundled form (defensive)."""
    from jarn.agent.session import _resume_payload

    cmd = _resume_payload([
        ("id1", [{"type": "approve"}]),
        (None, [{"type": "approve"}]),
    ])
    assert cmd.resume == {"decisions": [{"type": "approve"}, {"type": "approve"}]}
