"""SessionDriver stream handling edge cases."""

from __future__ import annotations

import contextlib
import logging
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage
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


def test_record_usage_ttl_scoped_cache_write_billed_as_cache():
    """langchain-anthropic zeroes the generic ``cache_creation`` when TTL-specific
    fields are present, putting the write tokens under ``ephemeral_5m_input_tokens``
    / ``ephemeral_1h_input_tokens``. record_usage must fall back to summing those so
    the write bills at the cache-write rate, not the full input rate.

    Repro (opus, cache_write 6.25/Mtok): input 1000, 800 ephemeral_5m ->
    plain 200@5 ($0.001) + write 800@6.25 ($0.005) = $0.006. The bug recorded 0
    cache_creation -> 1000@5 = $0.005."""
    tracker = CostTracker()
    driver = SessionDriver(
        agent=None,
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=tracker,
        thread_id="t1",
        main_model_ref="claude-opus-4-8",
    )
    msg = type("AIMessage", (), {
        "usage_metadata": {
            "input_tokens": 1000,
            "output_tokens": 0,
            "input_token_details": {
                "cache_read": 0,
                "cache_creation": 0,  # generic field zeroed by langchain-anthropic
                "ephemeral_5m_input_tokens": 800,
            },
        },
        "response_metadata": {"model": "claude-opus-4-8"},
        "tool_calls": [],
    })()

    driver._record_usage(msg)

    assert tracker.total.cache_creation_tokens == 800
    assert tracker.total.cost_usd == pytest.approx(0.006)


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


def test_parallel_same_model_subagents_do_not_over_count() -> None:
    """Two parallel subagents on the SAME model interleave their CUMULATIVE usage
    streams. Keyed only by (thread, model) their streams collapse onto one entry —
    the monotonic check flip-flops and each chunk is mis-deltaed. The namespace in
    the key (record_usage) separates the streams so each is deltaed against its own
    prior total: the exact per-subagent cumulative sum is billed, no over/under-count."""
    tracker = CostTracker()
    driver = SessionDriver(
        agent=None,
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=tracker,
        thread_id="t1",
        main_model_ref="claude-opus-4-8",
        known_model_refs=("claude-opus-4-8",),
    )

    def _chunk(inp: int, out: int):
        # Same model id on every chunk → resolve_model_ref returns the main ref for
        # both subagents; only the namespace distinguishes the two streams.
        return SimpleNamespace(
            usage_metadata={"input_tokens": inp, "output_tokens": out},
            response_metadata={"model_name": "claude-opus-4-8"},
            tool_calls=[],
            tool_call_chunks=[],
        )

    # Interleaved cumulative totals: A and B each grow 100→200 (input), 50→100 (out).
    driver._record_usage(_chunk(100, 50), _NS_A)
    driver._record_usage(_chunk(100, 50), _NS_B)
    driver._record_usage(_chunk(200, 100), _NS_A)
    driver._record_usage(_chunk(200, 100), _NS_B)

    # Each subagent's final cumulative is 200/100, so the honest total is 400/200.
    assert tracker.total.input_tokens == 400
    assert tracker.total.output_tokens == 200


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

    # Simulate stale keys from two other threads (100 /clears pattern). Keys are
    # (thread_id, subgraph-namespace, model_ref) 3-tuples (the "" namespace is the
    # main graph — see record_usage).
    for i in range(100):
        driver._last_usage_totals[("other_thread_a", "", f"model-{i}")] = (100, 10, 0, 0)
        driver._last_usage_totals[("other_thread_b", "", f"model-{i}")] = (200, 20, 0, 0)
    driver._last_usage_totals[("main_thread", "", "model-0")] = (300, 30, 0, 0)

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


# -- T-4-6: mid-turn steering (cooperative checkpoint) -----------------------
#
# The driver injects a steer as a HumanMessage via aupdate_state ONLY at a
# *settled* main-graph tool boundary (last checkpointed message is not an
# AIMessage with unfulfilled tool_calls). These fixtures model the real
# two-mode split (model-node updates carry a pending AIMessage(tool_calls);
# tool RESULTS land later as messages-mode ToolMessages + a tool-node updates
# super-step) so the settled-boundary gate can be exercised without a real graph.


def _steer_once(text: str):
    """A steer_source lambda: returns ``text`` on the first call, ``None`` after
    (mirrors how the driver pulls once per boundary until a steer appears)."""
    state = {"n": 0}

    def _src() -> str | None:
        state["n"] += 1
        return text if state["n"] == 1 else None

    return _src


def _assert_tool_result_adjacency(messages: list) -> None:
    """Provider-acceptability invariant: every ``AIMessage.tool_calls[i].id`` is
    immediately followed by a ``ToolMessage`` with the matching ``tool_call_id`` —
    no orphaned ``tool_use`` and no message (e.g. a steer HumanMessage) between a
    ``tool_use`` and its ``tool_result``."""
    for i, m in enumerate(messages):
        if getattr(m, "type", "") == "ai" and getattr(m, "tool_calls", None):
            expected = [c["id"] for c in m.tool_calls]
            following = messages[i + 1 : i + 1 + len(expected)]
            assert all(getattr(x, "type", "") == "tool" for x in following), (
                f"a non-tool message follows tool_use ids {expected}: {following}"
            )
            got = [getattr(x, "tool_call_id", None) for x in following]
            assert got == expected, (
                f"tool_use ids {expected} not immediately followed by results {got}"
            )


class _SteerAgent:
    """T1 fixture: yields ONE settled (text-only) main-graph updates super-step on
    call 1, then a tool-call super-step on the resume. Records state ops in order."""

    def __init__(self) -> None:
        self.ops: list[tuple[str, object]] = []
        self.calls = 0

    def astream(self, payload, config, *, stream_mode=None, subgraphs=False):
        self.ops.append(("astream", payload))
        self.calls += 1
        call = self.calls

        async def _gen():
            if call == 1:
                yield ((), "updates", {"model": {"messages": [AIMessage(content="thinking…")]}})
            else:
                tool_ai = AIMessage(
                    content="",
                    tool_calls=[{"name": "read_file", "id": "c2", "args": {"file_path": "x"}}],
                )
                yield ((), "updates", {"model": {"messages": [tool_ai]}})

        return _gen()

    async def aget_state(self, config):
        # Last message is a text-only AIMessage (no tool_calls) → settled.
        return SimpleNamespace(values={"messages": [AIMessage(content="thinking…")]})

    async def aupdate_state(self, config, values):
        self.ops.append(("update", values))


@pytest.mark.asyncio
async def test_steer_seen_before_next_tool():
    """T1: a steer set at a settled boundary is appended as a HumanMessage BETWEEN
    the two streams (append then resume with payload=None), and its NOTICE precedes
    the next super-step's tool call. One DONE closes the (single) turn."""
    agent = _SteerAgent()
    driver = _ns_driver(agent)
    driver.steer_source = _steer_once("use pytest not unittest")

    events = [ev async for ev in driver.run_turn("write a test")]

    # append happens BETWEEN the two astream calls; resume payload is None.
    assert [op[0] for op in agent.ops] == ["astream", "update", "astream"]
    assert agent.ops[2][1] is None
    appended = agent.ops[1][1]["messages"][0]
    assert appended.type == "human"
    assert appended.content == "use pytest not unittest"

    # steer NOTICE (data['steer']) lands BEFORE the next tool call.
    steer_idx = next(
        i for i, e in enumerate(events)
        if e.kind is EventKind.NOTICE and e.data.get("steer")
    )
    tool_idx = next(
        i for i, e in enumerate(events)
        if e.kind is EventKind.TOOL_START and e.text == "read_file"
    )
    assert steer_idx < tool_idx
    assert sum(1 for e in events if e.kind is EventKind.DONE) == 1


# NOTE: the T1-companion "steer WHILE a tool call is pending" regression guard is
# ``test_steer_real_checkpointer_adjacency_invariant`` (below), driven by the REAL
# AsyncSqliteSaver. A fake agent cannot model aclose()-rollback + resume (a fake has
# no pending writes, so it would report a settled boundary that the real
# checkpointer does not), which is precisely how a fixture-based version would give
# false confidence while the real Critical slipped through — so the guard is the
# real-checkpointer test, not a fixture.


@pytest.mark.asyncio
async def test_abort_during_applied_steer():
    """T2: cancelling the turn right after a steer was applied (mid-resume) must not
    raise and must emit no DONE; the HumanMessage append is durable."""
    import asyncio

    class _SteerThenHang:
        def __init__(self) -> None:
            self.updated = None
            self.calls = 0

        def astream(self, payload, config, *, stream_mode=None, subgraphs=False):
            self.calls += 1
            call = self.calls

            async def _gen():
                if call == 1:
                    yield ((), "updates", {"model": {"messages": [AIMessage(content="thinking")]}})
                else:
                    await asyncio.sleep(10)
                    yield ((), "messages", (AIMessage(content="never"),))

            return _gen()

        async def aget_state(self, config):
            return SimpleNamespace(values={"messages": [AIMessage(content="thinking")]})

        async def aupdate_state(self, config, values):
            self.updated = values

    agent = _SteerThenHang()
    driver = _ns_driver(agent)
    driver.steer_source = _steer_once("actually use pathlib")

    agen = driver.run_turn("go")
    first = await agen.__anext__()  # the applied-steer NOTICE
    assert first.kind is EventKind.NOTICE and first.data.get("steer")
    await agen.aclose()  # cancel mid-resume — must not raise

    assert agent.updated is not None
    assert agent.updated["messages"][0].content == "actually use pathlib"


@pytest.mark.asyncio
async def test_abort_before_steer_boundary_writes_nothing():
    """T2-companion: cancelling before the first (settled) boundary — steer never
    applied — writes nothing to state."""
    import asyncio
    import contextlib

    class _HangFirst:
        def __init__(self) -> None:
            self.updated = None

        def astream(self, payload, config, *, stream_mode=None, subgraphs=False):
            async def _gen():
                await asyncio.sleep(10)
                yield ((), "updates", {"model": {"messages": [AIMessage(content="x")]}})

            return _gen()

        async def aget_state(self, config):
            return SimpleNamespace(values={"messages": []})

        async def aupdate_state(self, config, values):
            self.updated = values

    agent = _HangFirst()
    driver = _ns_driver(agent)
    driver.steer_source = _steer_once("late")

    agen = driver.run_turn("go")
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(agen.__anext__(), timeout=0.1)
    await agen.aclose()

    assert agent.updated is None


@pytest.mark.asyncio
async def test_no_steer_source_is_byte_identical():
    """A driver with steer_source=None never calls aget_state/aupdate_state for
    steering — the existing stream path is unchanged."""
    agent = _SteerAgent()  # has state methods, but steer_source stays None
    driver = _ns_driver(agent)

    events = [ev async for ev in driver.run_turn("go")]

    # No state append occurred (no steer wired).
    assert not any(op[0] == "update" for op in agent.ops)
    assert any(e.kind is EventKind.DONE for e in events)


@pytest.mark.asyncio
async def test_steer_not_consumed_at_subagent_boundary():
    """A steer targets the MAIN graph only (spec §4): a subagent-subgraph updates
    boundary (namespace != ()) must NOT consume/inject the steer — it is held for
    the next MAIN-graph boundary."""

    class _SubThenMain:
        def __init__(self) -> None:
            self.ops: list[tuple[str, object]] = []

        def astream(self, payload, config, *, stream_mode=None, subgraphs=False):
            self.ops.append(("astream", payload))
            first = len(self.ops) == 1

            async def _gen():
                if first:
                    # a subagent (subgraph) model boundary comes FIRST …
                    yield (_NS_A, "updates", {"model": {"messages": [AIMessage(content="sub")]}})
                    # … then a MAIN-graph model boundary.
                    yield ((), "updates", {"model": {"messages": [AIMessage(content="main")]}})
                else:
                    yield ((), "messages", (AIMessage(content="done"),))

            return _gen()

        async def aget_state(self, config):
            return SimpleNamespace(values={"messages": [AIMessage(content="main")]})

        async def aupdate_state(self, config, values):
            self.ops.append(("update", values))

    agent = _SubThenMain()
    driver = _ns_driver(agent)
    driver.steer_source = _steer_once("steer the orchestrator")

    events = [ev async for ev in driver.run_turn("go")]

    # exactly one steer, injected at the MAIN boundary (append is between the two
    # astream calls — never at the subagent boundary within call 1).
    assert [op[0] for op in agent.ops] == ["astream", "update", "astream"]
    assert agent.ops[1][1]["messages"][0].content == "steer the orchestrator"
    assert sum(1 for e in events if e.kind is EventKind.NOTICE and e.data.get("steer")) == 1
    assert sum(1 for e in events if e.kind is EventKind.DONE) == 1


@pytest.mark.asyncio
async def test_steer_transcript_ordering():
    """T3: the steer is recorded in the display transcript as '(steered) …' at the
    point it was injected — after the turn's prompt and before the next tool call."""

    class _Transcript:
        def __init__(self) -> None:
            self.rows: list[tuple[str, str]] = []

        def write_user(self, text, *, ts=None) -> None:
            self.rows.append(("user", text))

        def write_assistant(self, text, *, ts=None) -> None:
            self.rows.append(("assistant", text))

        def write_tool(self, name, *, ts=None, args=None, result=None) -> None:
            self.rows.append(("tool", name))

    agent = _SteerAgent()
    driver = _ns_driver(agent)
    driver.transcript = _Transcript()
    driver.steer_source = _steer_once("switch to pathlib")

    _ = [ev async for ev in driver.run_turn("write a test")]

    rows = driver.transcript.rows
    assert rows[0] == ("user", "write a test")  # the turn prompt leads
    steer_row = next(
        i for i, r in enumerate(rows) if r == ("user", "(steered) switch to pathlib")
    )
    tool_row = next(i for i, r in enumerate(rows) if r == ("tool", "read_file"))
    assert steer_row < tool_row


class _SteerAfterTextAgent:
    """T-4-6 M1 fixture: call 1 STREAMS assistant text, then hits a model-step
    boundary (AIMessage with tool_calls) where a steer is injected; call 2 streams
    the real (re-run) answer. Models the abandoned-partial-text scenario."""

    def __init__(self) -> None:
        self.calls = 0

    def astream(self, payload, config, *, stream_mode=None, subgraphs=False):
        self.calls += 1
        call = self.calls

        async def _gen():
            if call == 1:
                # Assistant streams a partial reply (accumulates into _turn_text)…
                yield ((), "messages", (_AIChunk("abandoned reply "),))
                # …then a model-step boundary carrying tool_calls → steer fires here,
                # rolling this super-step back (its text is discarded from state).
                yield ((), "updates", {"model": {"messages": [AIMessage(
                    content="abandoned reply ",
                    tool_calls=[{"name": "read_file", "id": "c1", "args": {"file_path": "x"}}],
                )]}})
            else:
                # Re-run after the steer: the real answer.
                yield ((), "messages", (_AIChunk("final answer"),))
                yield ((), "updates", {"model": {"messages": [AIMessage(content="final answer")]}})

        return _gen()

    async def aget_state(self, config):
        # Settled boundary (text-only AIMessage) so the steer is injected.
        return SimpleNamespace(values={"messages": [AIMessage(content="prior")]})

    async def aupdate_state(self, config, values):
        pass


@pytest.mark.asyncio
async def test_steer_does_not_double_emit_discarded_text():
    """T-4-6 M1: a steer that interrupts a text-streaming model step must NOT leave
    the discarded partial text in _turn_text — otherwise the transcript's single
    assistant line double-emits the abandoned reply concatenated with the re-run
    reply. State is correct (the rolled-back text isn't in `messages`); this pins
    transcript fidelity."""

    class _Transcript:
        def __init__(self) -> None:
            self.rows: list[tuple[str, str]] = []

        def write_user(self, text, *, ts=None) -> None:
            self.rows.append(("user", text))

        def write_assistant(self, text, *, ts=None) -> None:
            self.rows.append(("assistant", text))

        def write_tool(self, name, *, ts=None, args=None, result=None) -> None:
            self.rows.append(("tool", name))

    agent = _SteerAfterTextAgent()
    driver = _ns_driver(agent)
    driver.transcript = _Transcript()
    driver.steer_source = _steer_once("do it differently")

    _ = [ev async for ev in driver.run_turn("go")]

    assistant_rows = [text for kind, text in driver.transcript.rows if kind == "assistant"]
    assert assistant_rows, "expected a flushed assistant transcript line"
    joined = "".join(assistant_rows)
    # The abandoned (rolled-back) prefix must NOT appear in the transcript.
    assert "abandoned reply" not in joined, (
        f"discarded model-step text leaked into the transcript: {assistant_rows!r}"
    )
    assert "final answer" in joined


# -- T-4-6: real-checkpointer integration (spec §6 risk 1 — the TOP risk) ----
#
# The fixture tests above use fake agents; they cannot prove the tool_use/
# tool_result adjacency invariant against the REAL AsyncSqliteSaver. This builds
# a trivial model→tools→model graph on a temp state.sqlite, steers DURING the
# pending tool round, and asserts every AIMessage.tool_calls[i].id is immediately
# followed by a ToolMessage — no HumanMessage ever separates them — plus a clean
# resume. This is the exact defect the design review caught in the first draft.


@pytest.mark.asyncio
async def test_steer_real_checkpointer_adjacency_invariant(tmp_path):
    from langgraph.graph import END, START, MessagesState, StateGraph

    from jarn.memory import create_async_checkpointer

    async def _model(state):
        # First model call → a tool round; once a tool round exists → final answer.
        already = any(
            getattr(m, "type", "") == "ai" and getattr(m, "tool_calls", None)
            for m in state["messages"]
        )
        if not already:
            return {"messages": [AIMessage(
                content="",
                tool_calls=[{"name": "read_file", "id": "c1", "args": {"path": "x"}}],
            )]}
        return {"messages": [AIMessage(content="done")]}

    def _route(state):
        last = state["messages"][-1]
        if getattr(last, "type", "") == "ai" and getattr(last, "tool_calls", None):
            return "tools"
        return END

    async def _tools(state):
        last = state["messages"][-1]
        return {"messages": [
            ToolMessage(content="file contents", tool_call_id=tc["id"], name=tc["name"])
            for tc in last.tool_calls
        ]}

    g = StateGraph(MessagesState)
    g.add_node("model", _model)
    g.add_node("tools", _tools)
    g.add_edge(START, "model")
    g.add_conditional_edges("model", _route, {"tools": "tools", END: END})
    g.add_edge("tools", "model")
    saver, saver_cm = await create_async_checkpointer(tmp_path / "state.sqlite")
    agent = g.compile(checkpointer=saver)

    # steer_source returns None on the FIRST pull (model-node boundary, committed
    # settled) and the steer on the SECOND (tool-node boundary, committed ends in a
    # PENDING AIMessage(tool_calls)) — so the steer is pulled DURING the pending
    # round and must be HELD until the ToolMessage commits.
    pulls = {"n": 0}

    def _src():
        pulls["n"] += 1
        return "stop, use the other file" if pulls["n"] == 2 else None

    driver = SessionDriver(
        agent=agent,
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=CostTracker(),
        thread_id="itest",
        main_model_ref="fake",
    )
    driver.steer_source = _src

    try:
        events = [ev async for ev in driver.run_turn("read the file")]

        # a single steer NOTICE, a single DONE (one turn, clean resume — no orphan).
        assert sum(1 for e in events if e.kind is EventKind.NOTICE and e.data.get("steer")) == 1
        assert sum(1 for e in events if e.kind is EventKind.DONE) == 1

        state = await agent.aget_state(driver._config())
        messages = state.values["messages"]
        # THE invariant: no HumanMessage between a tool_use and its tool_result.
        _assert_tool_result_adjacency(messages)
        # the steer landed AFTER the completed tool round (deferred from the pending
        # boundary), as a real HumanMessage on the thread.
        steers = [m for m in messages
                  if getattr(m, "type", "") == "human" and "other file" in str(m.content)]
        assert len(steers) == 1
        types = [m.type for m in messages]
        # …read_file(ai tool_calls) → tool result → steer → final answer
        assert types.count("tool") == 1
        tool_pos = types.index("tool")
        steer_pos = next(i for i, m in enumerate(messages)
                         if getattr(m, "type", "") == "human" and "other file" in str(m.content))
        assert tool_pos < steer_pos  # steer applied only after the tool result landed
    finally:
        if saver_cm is not None:
            await saver_cm.__aexit__(None, None, None)


# -- turn observability: jarn.turn span + turn-path log records --------------
#
# run_turn wraps each turn in the ``jarn.turn`` span (session.py ~334) and emits
# turn-path log records — turn start (info), tool start (debug), turn done (info),
# and a classified turn error (warning). The unit tests above exercise the stream
# handlers directly; nothing drove the span/log instrumentation THROUGH
# SessionDriver, so deleting it kept every other test green. These two integration
# tests pin it end-to-end.


@contextlib.contextmanager
def _capture_jarn(level: int = logging.DEBUG):
    """Capture records emitted on the "jarn" logger at *level*.

    Attaches a handler DIRECTLY to the jarn logger (not caplog's root handler):
    setup_logging() sets propagate=False elsewhere in the suite, so a root-level
    capture would miss these records (mirrors test_snapshot_failure_notice_once).
    Also lowers the jarn logger level so DEBUG records are actually emitted,
    restoring it afterwards."""
    logger = logging.getLogger("jarn")
    records: list[logging.LogRecord] = []

    class _Cap(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Cap(level=level)
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(level)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


@pytest.mark.asyncio
async def test_turn_span_and_log_records_on_success(monkeypatch) -> None:
    """A successful turn enters the ``jarn.turn`` span exactly once (with the
    driver's thread_id + model), and logs a ``turn start`` (info, carrying the
    thread id), a ``tool start`` (debug, when the model emits a tool call), and a
    ``turn done`` (info) record — all driven THROUGH SessionDriver."""
    import jarn.agent.session as session_mod

    # Record each _span entry as (name, kwargs) via a recording contextmanager.
    spans: list[tuple[str, dict]] = []

    @contextlib.contextmanager
    def _recording_span(name, **kwargs):
        spans.append((name, kwargs))
        yield

    monkeypatch.setattr(session_mod, "_span", _recording_span)

    # One main-graph tool-call super-step (→ TOOL_START) then a text reply (→ DONE).
    agent = _NSAgent([
        ((), "updates", {"model": {"messages": [
            SimpleNamespace(tool_calls=[
                {"name": "read_file", "args": {"file_path": "x"}, "id": "c1"}
            ])
        ]}}),
        ((), "messages", (_AIChunk("all done"),)),
    ])
    driver = SessionDriver(
        agent=agent,
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=CostTracker(),
        thread_id="obs-thread",
        main_model_ref="claude-opus-4-8",
    )

    with _capture_jarn() as records:
        events = [ev async for ev in driver.run_turn("go")]

    # Span: entered once, wrapping the turn, with the driver's identifiers.
    assert len(spans) == 1
    name, kwargs = spans[0]
    assert name == "jarn.turn"
    assert kwargs == {"thread_id": "obs-thread", "model": "claude-opus-4-8"}

    def _at(lvl: int, needle: str) -> list[logging.LogRecord]:
        return [r for r in records if r.levelno == lvl and needle in r.getMessage()]

    starts = _at(logging.INFO, "turn start")
    assert len(starts) == 1
    assert "obs-thread" in starts[0].getMessage()  # thread id carried on the record
    assert _at(logging.DEBUG, "tool start"), "no tool-start debug record"
    assert _at(logging.INFO, "turn done"), "no turn-done info record"
    # Sanity: the turn actually reached completion.
    assert any(e.kind is EventKind.DONE for e in events)


@pytest.mark.asyncio
async def test_turn_error_logs_classification() -> None:
    """A classified provider error raised inside astream is logged at WARNING with
    the retryable / auth / classified_by fields (driven THROUGH SessionDriver), and
    surfaced as a tagged ERROR event."""

    class _RateLimited(Exception):
        # status_code=429 → classify_error → retryable=True, auth=False, by "type".
        status_code = 429

    class _BoomAgent:
        async def astream(self, payload, config, stream_mode=None, **kwargs):
            raise _RateLimited("rate limited")
            yield  # pragma: no cover - makes this an async generator function

    driver = SessionDriver(
        agent=_BoomAgent(),
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=CostTracker(),
        thread_id="err-thread",
        main_model_ref="claude-opus-4-8",
    )

    with _capture_jarn(logging.WARNING) as records:
        events = [ev async for ev in driver.run_turn("go")]

    warnings = [
        r for r in records
        if r.levelno == logging.WARNING and "turn error" in r.getMessage()
    ]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    # The classification fields are all present on the record.
    assert "retryable=True" in msg
    assert "auth=False" in msg
    assert "classified_by=type" in msg
    # The classified error also surfaces as a retryable ERROR event.
    errors = [e for e in events if e.kind is EventKind.ERROR]
    assert errors and errors[0].data.get("retryable") is True
    assert errors[0].data.get("classified_by") == "type"


# --- TOOL_PROGRESS event construction (live tool-output streaming) ----------


def test_make_tool_progress_event_carries_tail_and_identity():
    """A backend ToolProgress record becomes a TOOL_PROGRESS Event with the tail,
    elapsed, heartbeat flag, and correlating tool_call_id in its data."""
    from jarn.agent.events import ToolProgress
    from jarn.agent.stream_handlers import make_tool_progress_event

    p = ToolProgress(
        command="make build",
        chunk="compiling bar.c\n",
        tail="compiling foo.c\ncompiling bar.c\n",
        elapsed=3.0,
        heartbeat=False,
        tool_name="execute",
    )
    ev = make_tool_progress_event(p, tool_call_id="call-1")
    assert ev.kind is EventKind.TOOL_PROGRESS
    assert ev.text == "execute"
    assert ev.data["tail"] == "compiling foo.c\ncompiling bar.c\n"
    assert ev.data["chunk"] == "compiling bar.c\n"
    assert ev.data["elapsed"] == 3.0
    assert ev.data["heartbeat"] is False
    assert ev.data["tool_call_id"] == "call-1"
    assert ev.data["command"] == "make build"


def test_make_tool_progress_event_heartbeat_and_agent_tag():
    """A quiet-time heartbeat and a subagent tag both round-trip into the event."""
    from jarn.agent.events import ToolProgress
    from jarn.agent.stream_handlers import make_tool_progress_event

    p = ToolProgress(tail="still building\n", elapsed=10.0, heartbeat=True)
    ev = make_tool_progress_event(p, agent="builder")
    assert ev.kind is EventKind.TOOL_PROGRESS
    assert ev.data["heartbeat"] is True
    assert ev.data["elapsed"] == 10.0
    assert ev.data["agent"] == "builder"
