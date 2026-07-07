"""SessionDriver stream handling edge cases."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langgraph.types import Overwrite

from jarn.agent.session import EventKind, SessionDriver
from jarn.config.schema import PermissionMode
from jarn.cost import CostTracker
from jarn.permissions import PermissionEngine

#: Exact user-facing text of the once-per-session snapshot-failure NOTICE.
_SNAPSHOT_FAIL_TEXT = (
    "checkpoint failed — /undo unavailable this turn (see ~/.jarn/logs/jarn.log)"
)


class _AIChunk:
    """Minimal assistant chunk for the fake agents below."""

    type = "ai"

    def __init__(self, content: str = "", usage: dict | None = None) -> None:
        self.content = content
        self.usage_metadata = usage if usage is not None else {
            "input_tokens": 1, "output_tokens": 1
        }
        self.response_metadata: dict[str, str] = {}


class _Interrupt:
    def __init__(self, value: object) -> None:
        self.value = value


def _snapshot_notices(events: list) -> list:
    return [e for e in events if e.kind is EventKind.NOTICE and "checkpoint failed" in e.text]


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


def test_record_usage_captures_cache_tokens():
    """A chunk whose usage_metadata carries input_token_details cache fields records
    the cache_read / cache_creation counts into the tracker; a chunk without them
    records zero (so non-cache turns are unaffected)."""
    tracker = CostTracker()
    driver = SessionDriver(
        agent=None,
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=tracker,
        thread_id="t1",
        main_model_ref="claude-opus-4-8",
    )

    cached_msg = type("AIMessage", (), {
        "usage_metadata": {
            "input_tokens": 100,
            "output_tokens": 50,
            "input_token_details": {"cache_read": 800, "cache_creation": 200},
        },
        "response_metadata": {},
        "tool_calls": [],
    })()
    plain_msg = type("AIMessage", (), {
        "usage_metadata": {"input_tokens": 10, "output_tokens": 5},
        "response_metadata": {},
        "tool_calls": [],
    })()

    driver._record_usage(cached_msg)
    assert tracker.total.cache_read_tokens == 800
    assert tracker.total.cache_creation_tokens == 200

    driver._record_usage(plain_msg)
    # The plain turn adds no cache tokens — the running totals are unchanged.
    assert tracker.total.cache_read_tokens == 800
    assert tracker.total.cache_creation_tokens == 200


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


# -- T-1-2: context gauge + _last_usage_totals bookkeeping -------------------


def test_subagent_different_model_does_not_move_gauge() -> None:
    """Usage from a subagent on a different model must not inflate the ctx% gauge."""
    tracker = CostTracker()
    driver = SessionDriver(
        agent=None,
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=tracker,
        thread_id="t1",
        main_model_ref="anthropic/claude-opus-4",
        known_model_refs=("anthropic/claude-opus-4", "anthropic/claude-haiku-4-5"),
    )

    subagent_msg = SimpleNamespace(
        usage_metadata={"input_tokens": 50_000, "output_tokens": 100},
        response_metadata={"model_name": "anthropic/claude-haiku-4-5"},
        tool_calls=[],
        tool_call_chunks=[],
    )
    driver._record_usage(subagent_msg)
    assert tracker.context_tokens == 0


@pytest.mark.asyncio
async def test_last_usage_totals_cleared_at_turn_start() -> None:
    """_last_usage_totals is fully cleared at the start of each turn.

    Regression: the old inverted filter `if k[0] != self.thread_id` kept OTHER
    threads' keys forever (e.g. after /clear, /compact, /rewind), leaking memory
    across process lifetime. The fix replaces it with a full .clear().
    """

    class _EmptyAgent:
        """Fake agent that yields nothing, so run_turn emits only DONE."""

        def astream(self, payload, config, *, stream_mode, subgraphs):
            async def _gen():
                if False:  # pragma: no branch
                    yield  # makes _gen an async generator

            return _gen()

    driver = SessionDriver(
        agent=_EmptyAgent(),
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=CostTracker(),
        thread_id="main_thread",
    )

    # Simulate stale keys from two other threads (100 /clears pattern).
    for i in range(100):
        driver._last_usage_totals[("other_thread_a", f"model-{i}")] = (100, 10, 0, 0)
        driver._last_usage_totals[("other_thread_b", f"model-{i}")] = (200, 20, 0, 0)
    driver._last_usage_totals[("main_thread", "model-0")] = (300, 30, 0, 0)

    assert len(driver._last_usage_totals) == 201  # 200 other + 1 current

    async for _ in driver.run_turn("hi"):
        pass

    # After fix: all stale keys gone.
    assert driver._last_usage_totals == {}


# -- T-1-4: non-blocking snapshot + loud, once-per-session failure ------------


@pytest.mark.asyncio
async def test_snapshot_failure_notice_once() -> None:
    """A snapshot that RAISES surfaces exactly ONE user-visible NOTICE per session
    (deduped across turns), logs a traceback, and never aborts the turn."""
    import logging

    from jarn.agent.session import ApprovalReply

    # Capture on the "jarn" logger directly — setup_logging() sets propagate=False
    # elsewhere in the suite, so caplog's root handler would miss these records.
    jarn_logger = logging.getLogger("jarn")
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Capture(level=logging.ERROR)

    class BoomCheckpoint:
        snapshot_notice_pending: bool = False
        snapshot_notice_shown: bool = False

        def snapshot(self, label, *, now=None, thread_id=None, turn_index=None):
            raise RuntimeError("git exploded")

    class WriteEachTurn:
        """Interrupts on a write on odd astream calls (turn starts) and completes
        on even calls (resumes), so every turn reaches the mutation gate."""

        def __init__(self) -> None:
            self.calls = 0

        async def astream(self, payload, config, stream_mode=None, **kwargs):
            self.calls += 1
            if self.calls % 2 == 1:
                yield ("updates", {"__interrupt__": (
                    _Interrupt({"action_requests": [
                        {"action": "write_file",
                         "args": {"file_path": "a.txt", "content": "x"}}
                    ]}),
                )})
            else:
                yield ("messages", (_AIChunk("done."),))

    async def _approve(_req):
        return ApprovalReply(approved=True)

    driver = SessionDriver(
        agent=WriteEachTurn(),
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=CostTracker(),
        thread_id="t",
        approver=_approve,
        checkpoint=BoomCheckpoint(),
    )
    jarn_logger.addHandler(handler)
    try:
        turn1 = [ev async for ev in driver.run_turn("first")]
        turn2 = [ev async for ev in driver.run_turn("second")]
    finally:
        jarn_logger.removeHandler(handler)

    # Exactly one NOTICE in turn 1 (surfaced at the mutation gate), with the exact
    # user-facing text; none in turn 2 (deduped once per session).
    assert len(_snapshot_notices(turn1)) == 1, turn1
    assert _snapshot_notices(turn1)[0].text == _SNAPSHOT_FAIL_TEXT
    assert _snapshot_notices(turn2) == [], turn2
    # Never aborts: both turns still complete.
    assert any(e.kind is EventKind.DONE for e in turn1)
    assert any(e.kind is EventKind.DONE for e in turn2)
    # The failure was logged with a full traceback on the jarn logger (both turns
    # log; the NOTICE is what is deduped, not the diagnostic log).
    snap_logs = [r for r in captured if "snapshot failed" in r.getMessage().lower()]
    assert snap_logs and snap_logs[0].exc_info is not None


@pytest.mark.asyncio
async def test_snapshot_failure_notice_deferred_to_next_turn() -> None:
    """A no-mutation turn awaits the snapshot in cleanup (past its last yield), so a
    failure there cannot be yielded that turn — its NOTICE is deferred to the START
    of the next turn, still exactly once."""

    class BoomCheckpoint:
        snapshot_notice_pending: bool = False
        snapshot_notice_shown: bool = False

        def snapshot(self, label, *, now=None, thread_id=None, turn_index=None):
            raise RuntimeError("git exploded")

    class ReplyOnly:
        async def astream(self, payload, config, stream_mode=None, **kwargs):
            yield ("messages", (_AIChunk("hi"),))

    driver = SessionDriver(
        agent=ReplyOnly(),
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=CostTracker(),
        thread_id="t",
        checkpoint=BoomCheckpoint(),
    )
    turn1 = [ev async for ev in driver.run_turn("first")]
    turn2 = [ev async for ev in driver.run_turn("second")]

    # No mutation gate in turn 1 → failure discovered in cleanup → no NOTICE there.
    assert _snapshot_notices(turn1) == []
    # Deferred NOTICE surfaces once, at the very start of turn 2 (before any output).
    assert len(_snapshot_notices(turn2)) == 1
    assert turn2[0].kind is EventKind.NOTICE
    assert turn2[0].text == _SNAPSHOT_FAIL_TEXT
    assert any(e.kind is EventKind.DONE for e in turn1)
    assert any(e.kind is EventKind.DONE for e in turn2)


@pytest.mark.asyncio
async def test_snapshot_task_not_leaked_on_cancelled_turn(caplog) -> None:
    """Cancelling a turn while its snapshot is still running must not leak the task
    ("Task was destroyed but it is pending") nor block the cancel on git: the task
    is detached to finish fire-and-forget and its outcome retrieved."""
    import asyncio
    import gc
    import logging
    import threading
    import time

    from jarn.agent.checkpoint import SnapshotResult
    from jarn.agent.session import _DETACHED_SNAPSHOTS

    started = threading.Event()

    class SlowCheckpoint:
        snapshot_notice_pending: bool = False
        snapshot_notice_shown: bool = False

        def snapshot(self, label, *, now=None, thread_id=None, turn_index=None):
            started.set()
            time.sleep(0.2)  # still running when the turn is cancelled
            return SnapshotResult(ok=True)

    class Hang:
        async def astream(self, payload, config, stream_mode=None, **kwargs):
            yield ("messages", (_AIChunk("thinking… "),))
            await asyncio.sleep(10)  # hold the turn open until cancelled

    driver = SessionDriver(
        agent=Hang(),
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=CostTracker(),
        thread_id="t",
        checkpoint=SlowCheckpoint(),
    )
    agen = driver.run_turn("go")
    first = await agen.__anext__()
    assert first.kind is EventKind.TEXT
    # Yield to the loop so the to_thread snapshot actually starts (a synchronous
    # wait here would block the loop and deadlock the thread launch).
    for _ in range(200):
        if started.is_set():
            break
        await asyncio.sleep(0.01)
    assert started.is_set()  # snapshot thread is running
    task = driver._snapshot_task
    assert task is not None and not task.done()

    with caplog.at_level(logging.WARNING, logger="asyncio"):
        await agen.aclose()  # cancel the turn mid-snapshot (GeneratorExit)
        # Cleanup reaped the driver slot and detached the still-running task.
        assert driver._snapshot_task is None
        assert task in _DETACHED_SNAPSHOTS
        # Condition-based wait (robust on slow/loaded CI): poll for the actual
        # post-condition — worker thread finished and its done-callback reaped the
        # task out of the detached set — instead of a fixed sleep that raced.
        for _ in range(500):
            if task.done() and task not in _DETACHED_SNAPSHOTS:
                break
            await asyncio.sleep(0.01)
        gc.collect()
        await asyncio.sleep(0)  # surface any "destroyed while pending" warning

    # The task completed fire-and-forget (not left pending, not cancelled) and the
    # done-callback cleaned it out of the detached set.
    assert task.done() and not task.cancelled()
    assert task not in _DETACHED_SNAPSHOTS
    leaked = [r for r in caplog.records if "was destroyed but it is pending" in r.getMessage()]
    assert not leaked, [r.getMessage() for r in caplog.records]


@pytest.mark.asyncio
async def test_settle_snapshot_noop_when_nothing_pending() -> None:
    """settle_snapshot with no in-flight snapshot (nothing started, nothing detached)
    returns immediately without awaiting or raising — the fast path for /undo, /redo,
    and /abort when the driver is idle."""
    driver = SessionDriver(
        agent=None,
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=CostTracker(),
        thread_id="t",
    )
    assert driver._snapshot_task is None
    await driver.settle_snapshot()
    assert driver._snapshot_task is None


@pytest.mark.asyncio
async def test_settle_snapshot_swallows_failure() -> None:
    """settle_snapshot awaits a snapshot that RAISES and returns WITHOUT propagating:
    a failed snapshot is best-effort (this turn simply has no checkpoint), matching
    the old sync behaviour — so a UI-driven undo/redo/abort is never aborted by it."""
    class BoomCheckpoint:
        snapshot_notice_pending: bool = False
        snapshot_notice_shown: bool = False

        def snapshot(self, label, *, now=None, thread_id=None, turn_index=None):
            raise RuntimeError("git exploded")

    driver = SessionDriver(
        agent=None,
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=CostTracker(),
        thread_id="t",
        checkpoint=BoomCheckpoint(),
    )
    driver._start_snapshot("x", 1.0)  # kicks off to_thread(snapshot) → will raise
    assert driver._snapshot_task is not None
    await driver.settle_snapshot()  # swallows the failure; never raises
    assert driver._snapshot_task is None


# -- T-1-4 fix round 2: session-lifetime snapshot-failure state ---------------


@pytest.mark.asyncio
async def test_snapshot_failure_notice_once_across_drivers() -> None:
    """Simulates the real REPL: a fresh SessionDriver is built per turn while the
    CheckpointManager lives for the whole session.  A persistently-failing snapshot
    must surface the NOTICE exactly ONCE across ALL drivers that share the same
    checkpoint manager.

    RED before fix: the dedupe flag lived on the driver, so each new driver reset it
    and emitted a fresh NOTICE — two NOTICEs across two turns.
    GREEN after fix: both flags live on the CheckpointManager (session-lifetime).
    """
    from jarn.agent.session import ApprovalReply

    class _BoomCp:
        """Fake checkpoint manager with the session-lifetime notice state fields."""
        snapshot_notice_pending: bool = False
        snapshot_notice_shown: bool = False

        def snapshot(self, label, *, now=None, thread_id=None, turn_index=None) -> None:
            raise RuntimeError("git exploded")

    class _WriteEachTurn:
        """Alternates: interrupt on odd astream call, reply on even — reaches the
        mutation gate every turn so the failure fires at the gate, not cleanup."""
        def __init__(self) -> None:
            self.calls = 0

        async def astream(self, payload, config, stream_mode=None, **kwargs):
            self.calls += 1
            if self.calls % 2 == 1:
                yield ("updates", {"__interrupt__": (
                    _Interrupt({"action_requests": [
                        {"action": "write_file",
                         "args": {"file_path": "a.txt", "content": "x"}}
                    ]}),
                )})
            else:
                yield ("messages", (_AIChunk("done."),))

    async def _approve(_req):
        return ApprovalReply(approved=True)

    cp = _BoomCp()
    agent = _WriteEachTurn()

    # Turn 1: driver A
    driver_a = SessionDriver(
        agent=agent,
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=CostTracker(),
        thread_id="t",
        approver=_approve,
        checkpoint=cp,
    )
    turn1 = [ev async for ev in driver_a.run_turn("first")]

    # Turn 2: FRESH driver B — same checkpoint manager, new driver object.
    driver_b = SessionDriver(
        agent=agent,
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=CostTracker(),
        thread_id="t",
        approver=_approve,
        checkpoint=cp,
    )
    turn2 = [ev async for ev in driver_b.run_turn("second")]

    # Exactly one NOTICE in turn 1; zero in turn 2 (deduped at session level).
    assert len(_snapshot_notices(turn1)) == 1, turn1
    assert _snapshot_notices(turn2) == [], f"NOTICE fired again on fresh driver: {turn2}"
    assert any(e.kind is EventKind.DONE for e in turn1)
    assert any(e.kind is EventKind.DONE for e in turn2)


@pytest.mark.asyncio
async def test_deferred_snapshot_failure_surfaces_on_fresh_driver() -> None:
    """A snapshot failure discovered in turn-end cleanup (after the last yield, on a
    no-mutation turn) arms the ``snapshot_notice_pending`` flag.  In the real REPL the
    next turn runs on a FRESH driver — the NOTICE must still surface there.

    RED before fix: the pending flag lived on the old driver, so the fresh driver
    never saw it and the NOTICE was silently lost.
    GREEN after fix: the flag lives on the shared CheckpointManager.
    """
    class _BoomCp:
        snapshot_notice_pending: bool = False
        snapshot_notice_shown: bool = False

        def snapshot(self, label, *, now=None, thread_id=None, turn_index=None) -> None:
            raise RuntimeError("git exploded")

    class _ReplyOnly:
        async def astream(self, payload, config, stream_mode=None, **kwargs):
            yield ("messages", (_AIChunk("hi"),))

    cp = _BoomCp()
    agent = _ReplyOnly()

    # Turn 1 on driver A: no mutation → failure found in cleanup, past last yield.
    driver_a = SessionDriver(
        agent=agent,
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=CostTracker(),
        thread_id="t",
        checkpoint=cp,
    )
    turn1 = [ev async for ev in driver_a.run_turn("first")]

    # Turn 2 on FRESH driver B sharing the same checkpoint manager.
    driver_b = SessionDriver(
        agent=agent,
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=CostTracker(),
        thread_id="t",
        checkpoint=cp,
    )
    turn2 = [ev async for ev in driver_b.run_turn("second")]

    # Turn 1: no mutation gate → failure in cleanup → no NOTICE can be yielded there.
    assert _snapshot_notices(turn1) == []
    # Turn 2 on the fresh driver: NOTICE surfaces at turn start, exactly once.
    assert len(_snapshot_notices(turn2)) == 1, f"no deferred NOTICE on fresh driver: {turn2}"
    assert turn2[0].kind is EventKind.NOTICE
    assert turn2[0].text == _SNAPSHOT_FAIL_TEXT
    assert any(e.kind is EventKind.DONE for e in turn1)
    assert any(e.kind is EventKind.DONE for e in turn2)


# -- T-3-5: subagent progress labels in the stream ---------------------------
#
# Frozen fixture for the real ``astream(subgraphs=True)`` namespace shape,
# determined by reading source (see task-3-5-report.md):
#   * main-graph events carry namespace ``()``.
#   * a delegated subagent's events carry ``("tools:<task_id>",)`` where
#     ``<task_id>`` is a LangGraph checkpoint task id — a UUID-shaped hash
#     (``hex[:8]-hex[8:12]-hex[12:16]-hex[16:20]-hex[20:32]``) computed by
#     langgraph from (checkpoint_id, "tools", step, PUSH, send-idx). It is NOT
#     the ``task`` tool_call_id and does not embed it, so the subagent NAME is
#     captured from the ``task`` tool args at TOOL_START and correlated to the
#     namespace by first-appearance (FIFO) order.
_NS_A = ("tools:9f8e7d6c-1a2b-3c4d-5e6f-0a1b2c3d4e5f",)
_NS_B = ("tools:1234abcd-5678-9012-3456-7890abcdef12",)


class _NSAgent:
    """Single-pass agent that yields explicit ``(namespace, mode, chunk)`` triples,
    mimicking ``astream(subgraphs=True)``."""

    def __init__(self, items: list) -> None:
        self.items = items

    async def astream(self, payload, config, stream_mode=None, **kwargs):
        for item in self.items:
            yield item


def _ns_driver(agent) -> SessionDriver:
    return SessionDriver(
        agent=agent,
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=CostTracker(),
        thread_id="t",
        main_model_ref="claude-opus-4-8",
    )


def _task_launch(*calls) -> dict:
    """A main-graph ``updates`` chunk whose AI message launches ``task`` calls."""
    msg = SimpleNamespace(tool_calls=[
        {"name": "task", "args": {"subagent_type": name, "description": "go"},
         "id": cid}
        for name, cid in calls
    ])
    return {"model": {"messages": [msg]}}


@pytest.mark.asyncio
async def test_subagent_events_tagged():
    """Events streamed from a task-subgraph carry ``data['agent'] == 'researcher'``."""
    agent = _NSAgent([
        ((), "updates", _task_launch(("researcher", "call_1"))),
        (_NS_A, "messages", (_AIChunk("found the answer"),)),
    ])
    events = [ev async for ev in _ns_driver(agent).run_turn("go")]
    tagged = [e for e in events if e.kind is EventKind.TEXT and e.text == "found the answer"]
    assert tagged, "subagent text event missing"
    assert tagged[0].data.get("agent") == "researcher"


@pytest.mark.asyncio
async def test_parallel_tags_independent():
    """Two parallel ``task`` calls tag their subgraph events independently."""
    agent = _NSAgent([
        ((), "updates", _task_launch(("researcher", "c1"), ("coder", "c2"))),
        (_NS_A, "messages", (_AIChunk("research result"),)),
        (_NS_B, "messages", (_AIChunk("code result"),)),
    ])
    events = [ev async for ev in _ns_driver(agent).run_turn("go")]
    by_text = {e.text: e for e in events if e.kind is EventKind.TEXT}
    assert by_text["research result"].data.get("agent") == "researcher"
    assert by_text["code result"].data.get("agent") == "coder"


@pytest.mark.asyncio
async def test_main_untagged():
    """Main-graph events (namespace ``()``) carry no agent tag."""
    agent = _NSAgent([
        ((), "messages", (_AIChunk("main answer"),)),
    ])
    events = [ev async for ev in _ns_driver(agent).run_turn("go")]
    texts = [e for e in events if e.kind is EventKind.TEXT]
    assert texts and all(e.data.get("agent") is None for e in texts)


@pytest.mark.asyncio
async def test_nested_task_does_not_pollute_fifo():
    """A task launch that arrives under a subgraph namespace (a nested subagent
    calling task) must NOT be appended to _subagent_pending.  Before Fix 1,
    that stale name is popped by the NEXT top-level subagent binding and
    mislabels it.

    Sequence:
      1. main-graph launches A  → pending=["A"]
      2. A's subgraph (_NS_A) launches nested  (should be ignored for FIFO)
      3. main-graph launches B  → pending=["A","B"] after fix; ["A","nested","B"] before
      4. _NS_A streams → binds to "A"
      5. _NS_B streams → should bind to "B" (not "nested")
    """
    agent = _NSAgent([
        # 1 — main graph launches A
        ((), "updates", _task_launch(("A", "call_A"))),
        # 2 — A's subgraph launches a nested sub (must not enter FIFO)
        (_NS_A, "updates", _task_launch(("nested", "call_nested"))),
        # 3 — main graph launches B
        ((), "updates", _task_launch(("B", "call_B"))),
        # 4 & 5 — subgraph output
        (_NS_A, "messages", (_AIChunk("A output"),)),
        (_NS_B, "messages", (_AIChunk("B output"),)),
    ])
    events = [ev async for ev in _ns_driver(agent).run_turn("go")]
    by_text = {e.text: e for e in events if e.kind is EventKind.TEXT}
    assert by_text["A output"].data.get("agent") == "A", (
        f"expected 'A', got {by_text['A output'].data.get('agent')!r}"
    )
    assert by_text["B output"].data.get("agent") == "B", (
        f"expected 'B', got {by_text['B output'].data.get('agent')!r} (nested pollution?)"
    )


@pytest.mark.asyncio
async def test_duplicate_task_launch_not_double_counted():
    """When the same task TOOL_START (same tool_call_id) is re-emitted by
    LangGraph (stream_mode=["messages","updates"] + subgraphs=True can surface
    the same update chunk more than once), it must not shift the FIFO.

    Before Fix 2 the duplicate appends the same name a second time so the
    next binding consumes the stale copy and mislabels the second subagent.

    Sequence:
      1. main-graph emits task launch for A (call_id="call_A")  → pending=["A"]
      2. duplicate: same chunk re-emitted (call_id="call_A")   → must NOT append again
      3. main-graph launches B (call_id="call_B")              → pending=["A","B"]
      4. _NS_A streams → binds to "A"
      5. _NS_B streams → must bind to "B" (not stale "A")
    """
    agent = _NSAgent([
        # 1 — first emission
        ((), "updates", _task_launch(("A", "call_A"))),
        # 2 — duplicate of the same TOOL_START
        ((), "updates", _task_launch(("A", "call_A"))),
        # 3 — second subagent launch
        ((), "updates", _task_launch(("B", "call_B"))),
        # 4 & 5 — subgraph output
        (_NS_A, "messages", (_AIChunk("A output"),)),
        (_NS_B, "messages", (_AIChunk("B output"),)),
    ])
    events = [ev async for ev in _ns_driver(agent).run_turn("go")]
    by_text = {e.text: e for e in events if e.kind is EventKind.TEXT}
    assert by_text["A output"].data.get("agent") == "A", (
        f"expected 'A', got {by_text['A output'].data.get('agent')!r}"
    )
    assert by_text["B output"].data.get("agent") == "B", (
        f"expected 'B', got {by_text['B output'].data.get('agent')!r} (duplicate shifted FIFO?)"
    )
