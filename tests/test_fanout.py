"""Parallel subagent fan-out tests.

Covers the three load-bearing guarantees of ``spawn_parallel_tasks``:

1. Tasks run CONCURRENTLY, not serialized (proven deterministically with an
   ``asyncio.Barrier`` — a serial run would deadlock it).
2. The aggregated result shape is correct (per-task status/summary + roll-up).
3. Per-task budget accounting records usage into the cost tracker's per-namespace
   dimension and flags a soft-cap breach — without double-counting the session
   total.
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from jarn.agent.fanout import (
    ParallelTask,
    build_spawn_parallel_tasks_tool,
    extract_subagent_graphs,
    format_result,
    run_parallel_tasks,
)
from jarn.cost.tracker import CostTracker

MODEL = "openrouter/anthropic/claude-haiku-4-5"


def _ai(text: str, *, in_tok: int = 0, out_tok: int = 0, cache_read: int = 0) -> AIMessage:
    """An AIMessage carrying usage_metadata, as a real subagent would return."""
    return AIMessage(
        content=text,
        usage_metadata={
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "total_tokens": in_tok + out_tok,
            "input_token_details": {"cache_read": cache_read},
        },
        response_metadata={"model_name": MODEL},
    )


def _result(text: str, **usage) -> dict:
    return {"messages": [HumanMessage(content="go"), _ai(text, **usage)]}


def _ai_raw(text: str, details: dict, *, in_tok: int, out_tok: int = 0) -> AIMessage:
    """An AIMessage with a hand-written ``input_token_details``.

    Lets a test reproduce a provider's exact field layout — notably the TTL-scoped
    cache-write shape langchain-anthropic emits — instead of the tidy subset ``_ai``
    builds.
    """
    return AIMessage(
        content=text,
        usage_metadata={
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "total_tokens": in_tok + out_tok,
            "input_token_details": details,
        },
        response_metadata={"model_name": MODEL},
    )


# --- 1. concurrency ---------------------------------------------------------


@pytest.mark.asyncio
async def test_tasks_run_concurrently_not_serialized():
    """Two tasks must be in flight at the same time. A Barrier(2) only releases
    when both coroutines reach it — a serial implementation would hang, which the
    wait_for turns into a fast failure instead of a real deadlock."""
    barrier = asyncio.Barrier(2)
    seen: list[str] = []

    async def invoke(subagent_type: str, description: str):
        seen.append(f"start:{description}")
        await barrier.wait()  # both tasks must arrive here together
        seen.append(f"end:{description}")
        return _result(f"done {description}", in_tok=10, out_tok=5)

    tasks = [ParallelTask("A"), ParallelTask("B")]
    result = await asyncio.wait_for(
        run_parallel_tasks(
            tasks,
            invoke=invoke,
            available_subagents=["general-purpose"],
            default_model_ref=MODEL,
        ),
        timeout=2.0,
    )
    # Both started before either ended → genuinely concurrent.
    assert seen[0].startswith("start:") and seen[1].startswith("start:")
    assert {o.status for o in result.outcomes} == {"ok"}
    assert [o.summary for o in result.outcomes] == ["done A", "done B"]


@pytest.mark.asyncio
async def test_serial_invoke_would_deadlock_the_barrier():
    """Guard the guard: a genuinely serial runner deadlocks the Barrier(2), so the
    concurrency test above is meaningful (it does not pass trivially)."""
    barrier = asyncio.Barrier(2)

    async def invoke(subagent_type: str, description: str):
        await barrier.wait()
        return _result("x")

    async def serial():
        for t in [ParallelTask("A"), ParallelTask("B")]:
            await invoke(t.subagent_type, t.description)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(serial(), timeout=0.3)


# --- 1b. HITL interrupt propagation (BUG 1) --------------------------------


@pytest.mark.asyncio
async def test_graph_interrupt_propagates_not_swallowed():
    """A gated action inside a fanned-out subagent raises LangGraph's
    ``GraphInterrupt`` (the HITL approval signal). It derives from ``Exception``,
    so the generic per-task handler would otherwise convert it into a task
    ``status="error"`` and the SessionDriver would never see the approval prompt.
    It MUST instead propagate out of ``run_parallel_tasks`` to the driver's
    approve/resume path."""
    from langgraph.errors import GraphInterrupt

    async def invoke(subagent_type: str, description: str):
        # Stand-in for a gated tool call inside the subagent sub-graph pausing for
        # approval (LangGraph raises this to hand control back to the graph runner).
        raise GraphInterrupt(())

    with pytest.raises(GraphInterrupt):
        await run_parallel_tasks(
            [ParallelTask("gated", subagent_type="general-purpose")],
            invoke=invoke,
            available_subagents=["general-purpose"],
            default_model_ref=MODEL,
        )


@pytest.mark.asyncio
async def test_graph_bubbleup_propagates_over_sibling_ordinary_error():
    """When one task hits the HITL interrupt and another raises an ordinary error,
    the bubble-up wins: it propagates (not swallowed) rather than being masked by a
    sibling's captured ``error`` outcome. Ordinary exceptions are still captured;
    only ``GraphBubbleUp`` re-raises."""
    from langgraph.errors import GraphInterrupt

    async def invoke(subagent_type: str, description: str):
        if description == "gated":
            raise GraphInterrupt(())
        raise RuntimeError("ordinary failure")  # captured as an error outcome

    with pytest.raises(GraphInterrupt):
        await run_parallel_tasks(
            [ParallelTask("gated"), ParallelTask("boom")],
            invoke=invoke,
            available_subagents=["general-purpose"],
            default_model_ref=MODEL,
        )


# --- 2. aggregation shape ---------------------------------------------------


@pytest.mark.asyncio
async def test_aggregation_shape_and_error_isolation():
    """Unknown subagent + a raising task + an ok task all land as distinct
    outcomes; one failure never cancels its siblings."""

    async def invoke(subagent_type: str, description: str):
        if description == "boom":
            raise RuntimeError("subagent blew up")
        return _result(f"ok:{description}", in_tok=100, out_tok=20)

    tasks = [
        ParallelTask("good", subagent_type="general-purpose"),
        ParallelTask("boom", subagent_type="general-purpose"),
        ParallelTask("x", subagent_type="does-not-exist"),
    ]
    result = await run_parallel_tasks(
        tasks,
        invoke=invoke,
        available_subagents=["general-purpose"],
        default_model_ref=MODEL,
    )
    by_index = {o.index: o for o in result.outcomes}
    assert by_index[0].status == "ok" and by_index[0].summary == "ok:good"
    assert by_index[1].status == "error" and "blew up" in by_index[1].summary
    assert by_index[2].status == "unknown_subagent"
    assert result.counts() == {"ok": 1, "error": 1, "unknown_subagent": 1}

    rendered = format_result(result)
    assert "3 tasks:" in rendered
    assert "[task 0] general-purpose — ok" in rendered
    assert "[task 2] does-not-exist — unknown_subagent" in rendered


@pytest.mark.asyncio
async def test_error_summary_redacts_credentials():
    """round-9 #3: a subagent exception whose text carries a credential is scrubbed
    before it reaches the orchestrating model — in the outcome summary AND the
    rendered roll-up (fan-out errors previously copied ``str(exc)`` verbatim)."""
    secret = "sk-proj-ABCDEFGH1234567890WXYZ"

    async def invoke(subagent_type: str, description: str):
        raise RuntimeError(f"Authorization: Bearer {secret}")

    result = await run_parallel_tasks(
        [ParallelTask("boom", subagent_type="general-purpose")],
        invoke=invoke,
        available_subagents=["general-purpose"],
        default_model_ref=MODEL,
    )
    outcome = result.outcomes[0]
    assert outcome.status == "error"
    assert secret not in outcome.summary       # credential scrubbed from the summary
    assert secret not in format_result(result)  # …and from the model-facing roll-up


@pytest.mark.asyncio
async def test_error_isolation_survives_raising_exception_str():
    """round-10/11: an exception whose ``__str__`` itself raises must still isolate as
    an ``error`` outcome — the formatter must not let the crash escape the batch, even
    when ``__str__`` raises a BaseException (not just an Exception)."""

    class _Explode(BaseException):
        pass

    class _BadStr:
        def __str__(self):
            raise _Explode("string conversion failed")

    async def invoke(subagent_type: str, description: str):
        raise RuntimeError(_BadStr())

    result = await run_parallel_tasks(
        [ParallelTask("boom", subagent_type="general-purpose")],
        invoke=invoke,
        available_subagents=["general-purpose"],
        default_model_ref=MODEL,
    )
    outcome = result.outcomes[0]
    assert outcome.status == "error"        # isolated, not crashed
    assert "RuntimeError" in outcome.summary  # falls back to the type name
    format_result(result)                    # rendering must not raise either


# --- 3. per-task budget accounting -----------------------------------------


@pytest.mark.asyncio
async def test_per_task_budget_records_namespace_and_flags_breach():
    """Per-task usage is priced and recorded into the tracker's per_namespace
    dimension (never into total), and a task over its soft cap is flagged."""
    tracker = CostTracker()

    async def invoke(subagent_type: str, description: str):
        # 1e6 in + 1e6 out on haiku == $6.00 for the "big" task.
        if description == "big":
            return _result("big done", in_tok=1_000_000, out_tok=1_000_000)
        return _result("small done", in_tok=10, out_tok=5)

    tasks = [
        ParallelTask("big", max_usd=1.0),   # $6.00 >= $1.00 cap → breach
        ParallelTask("small", max_usd=1.0),  # ~$0 → ok
    ]
    result = await run_parallel_tasks(
        tasks,
        invoke=invoke,
        available_subagents=["general-purpose"],
        default_model_ref=MODEL,
        cost_tracker=tracker,
    )
    big = next(o for o in result.outcomes if o.summary == "big done")
    small = next(o for o in result.outcomes if o.summary == "small done")
    assert big.status == "budget_exceeded" and big.budget_exceeded
    assert big.cost_usd == pytest.approx(6.0)
    assert small.status == "ok" and not small.budget_exceeded

    # Recorded per-namespace, and NOT summed into the streamed session total.
    assert len(tracker.per_namespace) == 2
    assert tracker.total.cost_usd == 0.0
    ns_cost = sum(u.cost_usd for u in tracker.per_namespace.values())
    assert ns_cost == pytest.approx(6.0, abs=1e-3)  # big $6.00 + small ~$0


@pytest.mark.asyncio
async def test_a_later_fanout_does_not_inherit_an_earlier_ones_spend():
    """#81: the cost namespace must be unique per INVOCATION, not just per task.

    The namespace was ``fanout[<index>]:<type>``, built from the task's position in
    its own batch — identical for every call. ``record_task_usage`` returns the
    bucket's CUMULATIVE usage and the soft cap is checked against that, so a second
    fan-out inherited the first's spend: same cap, same real cost, different verdict.
    """
    tracker = CostTracker()

    async def invoke(subagent_type: str, description: str):
        return _result("done", in_tok=100_000, out_tok=100_000)  # $0.60 on haiku

    async def one_fanout():
        result = await run_parallel_tasks(
            [ParallelTask("t", subagent_type="general-purpose", max_usd=1.0)],
            invoke=invoke,
            available_subagents=["general-purpose"],
            default_model_ref=MODEL,
            cost_tracker=tracker,
        )
        return result.outcomes[0]

    first, second, third = [await one_fanout() for _ in range(3)]

    # Every call did the identical $0.60 of work under the identical $1.00 cap, so
    # every call must reach the identical verdict.
    for call, outcome in enumerate((first, second, third), start=1):
        assert outcome.cost_usd == pytest.approx(0.60), f"call {call} reports cumulative spend"
        assert outcome.status == "ok", f"call {call} breached on an earlier call's spend"
        assert not outcome.budget_exceeded

    # Three invocations ⇒ three buckets, not one bucket counted three times.
    assert len(tracker.per_namespace) == 3
    assert sum(u.calls for u in tracker.per_namespace.values()) == 3
    assert sum(u.cost_usd for u in tracker.per_namespace.values()) == pytest.approx(1.80)


@pytest.mark.asyncio
async def test_ttl_scoped_cache_writes_are_billed_as_cache_writes():
    """#82: fan-out read only the generic ``cache_creation`` field.

    langchain-anthropic zeroes that field when it reports TTL-specific cache writes,
    putting them under ``ephemeral_5m_input_tokens`` / ``ephemeral_1h_input_tokens``.
    ``cost_of`` subtracts cache tokens from the plain-input charge and reprices them,
    so missing them leaves the writes billed at the INPUT rate.

    Repro (haiku: input $1.00/Mtok, cache_write $1.25/Mtok): 1,000,000 input of which
    800k+200k are cache writes → plain 0 + write 1M @ $1.25 = **$1.25**. Reading only
    the generic field saw 0 cache tokens → 1M @ $1.00 = $1.00, a 20% undercount.
    """
    tracker = CostTracker()

    async def invoke(subagent_type: str, description: str):
        return {
            "messages": [
                HumanMessage(content="go"),
                _ai_raw(
                    "done",
                    {
                        "cache_read": 0,
                        "cache_creation": 0,  # zeroed by langchain-anthropic
                        "ephemeral_5m_input_tokens": 800_000,
                        "ephemeral_1h_input_tokens": 200_000,
                    },
                    in_tok=1_000_000,
                ),
            ]
        }

    result = await run_parallel_tasks(
        [ParallelTask("t", subagent_type="general-purpose")],
        invoke=invoke,
        available_subagents=["general-purpose"],
        default_model_ref=MODEL,
        cost_tracker=tracker,
    )
    assert result.outcomes[0].cost_usd == pytest.approx(1.25)
    bucket = next(iter(tracker.per_namespace.values()))
    assert bucket.cache_creation_tokens == 1_000_000


@pytest.mark.asyncio
async def test_fanout_prices_a_message_exactly_as_the_streaming_path_does():
    """Parity: one message, two readers, one cost.

    Both paths feed the same tracker through the same ``cost_of``, so any divergence
    is a cost error. They diverged because each had its own copy of the
    ``usage_metadata`` reading; this pins them to the same answer for the layout that
    exposed it.
    """
    from jarn.agent.session import SessionDriver
    from jarn.permissions.engine import PermissionEngine, PermissionMode

    details = {
        "cache_read": 50_000,
        "cache_creation": 0,
        "ephemeral_5m_input_tokens": 800_000,
        "ephemeral_1h_input_tokens": 200_000,
    }
    msg = _ai_raw("done", details, in_tok=1_500_000, out_tok=1_000)

    # Streaming path.
    stream_tracker = CostTracker()
    driver = SessionDriver(
        agent=None,
        engine=PermissionEngine(mode=PermissionMode.ASK),
        tracker=stream_tracker,
        thread_id="t1",
        main_model_ref=MODEL,
    )
    driver._record_usage(msg)

    # Fan-out path.
    fanout_tracker = CostTracker()

    async def invoke(subagent_type: str, description: str):
        return {"messages": [HumanMessage(content="go"), msg]}

    await run_parallel_tasks(
        [ParallelTask("t", subagent_type="general-purpose")],
        invoke=invoke,
        available_subagents=["general-purpose"],
        default_model_ref=MODEL,
        cost_tracker=fanout_tracker,
    )

    streamed = stream_tracker.total
    fanned = next(iter(fanout_tracker.per_namespace.values()))
    assert fanned.cost_usd == pytest.approx(streamed.cost_usd)
    assert fanned.cache_creation_tokens == streamed.cache_creation_tokens == 1_000_000
    assert fanned.cache_read_tokens == streamed.cache_read_tokens == 50_000
    assert streamed.cost_usd > 0


@pytest.mark.asyncio
async def test_namespaces_stay_distinct_within_one_fanout_and_readable():
    """Uniqueness must not come at the cost of attribution.

    Within one invocation the buckets still separate by task index and subagent
    type, and the label still carries the caller's prefix so a reader can tell what
    produced it.
    """
    tracker = CostTracker()

    async def invoke(subagent_type: str, description: str):
        return _result(f"done {description}", in_tok=10, out_tok=5)

    await run_parallel_tasks(
        [
            ParallelTask("a", subagent_type="general-purpose"),
            ParallelTask("b", subagent_type="general-purpose"),
            ParallelTask("c", subagent_type="general-purpose"),
        ],
        invoke=invoke,
        available_subagents=["general-purpose"],
        default_model_ref=MODEL,
        cost_tracker=tracker,
    )
    keys = sorted(tracker.per_namespace)
    assert len(keys) == 3, keys
    assert all(k.startswith("fanout") for k in keys), keys
    assert all("general-purpose" in k for k in keys), keys
    # The task index survives, so a bucket is still attributable to a task.
    assert {"[0]", "[1]", "[2]"} == {k[k.index("[") : k.index("]") + 1] for k in keys}


@pytest.mark.asyncio
async def test_explicit_namespace_prefix_is_still_honoured():
    """A caller-supplied prefix keeps its place at the head of the label."""
    tracker = CostTracker()

    async def invoke(subagent_type: str, description: str):
        return _result("done", in_tok=10, out_tok=5)

    await run_parallel_tasks(
        [ParallelTask("a", subagent_type="general-purpose")],
        invoke=invoke,
        available_subagents=["general-purpose"],
        default_model_ref=MODEL,
        cost_tracker=tracker,
        namespace_prefix="research",
    )
    key = next(iter(tracker.per_namespace))
    assert key.startswith("research"), key


@pytest.mark.asyncio
async def test_default_subagent_type_and_tool_wrapper_returns_string():
    """The built tool parses raw dicts, applies the default subagent_type, and
    returns a formatted string (its ToolMessage payload)."""

    async def invoke(subagent_type: str, description: str):
        assert subagent_type == "general-purpose"  # default applied
        return _result(f"handled {description}", in_tok=5, out_tok=5)

    tool = build_spawn_parallel_tasks_tool(
        invoke=invoke,
        available_subagents=["general-purpose"],
        default_model_ref=MODEL,
    )
    assert tool.name == "spawn_parallel_tasks"
    out = await tool.ainvoke({"tasks": [{"description": "alpha"}, {"description": "beta"}]})
    assert isinstance(out, str)
    assert "2 tasks:" in out
    assert "handled alpha" in out and "handled beta" in out


# --- extraction helper ------------------------------------------------------


def test_extract_subagent_graphs_empty_on_bad_agent():
    """A non-deepagents object yields {} (degrade safely, never raise)."""
    assert extract_subagent_graphs(object()) == {}


# --- runtime wiring (flag gating + real gated-graph extraction) -------------


def _tool_names(agent) -> set[str]:
    return set(agent.nodes["tools"].bound.tools_by_name.keys())


def _find_hitl(obj, _depth=0, _seen=None):
    """Walk closures/attrs to the installed HumanInTheLoopMiddleware (same approach
    as test_async_subagents)."""
    from langchain.agents.middleware import HumanInTheLoopMiddleware

    if _seen is None:
        _seen = set()
    if id(obj) in _seen or _depth > 6:
        return None
    _seen.add(id(obj))
    if isinstance(obj, HumanInTheLoopMiddleware):
        return obj
    for cell in getattr(obj, "__closure__", None) or ():
        try:
            found = _find_hitl(cell.cell_contents, _depth + 1, _seen)
        except ValueError:
            continue
        if found is not None:
            return found
    for attr in ("func", "bound", "__self__", "__wrapped__"):
        value = getattr(obj, attr, None)
        if value is not None and (found := _find_hitl(value, _depth + 1, _seen)):
            return found
    return None


def _hitl_interrupt_keys(agent) -> set[str]:
    node = agent.nodes.get("HumanInTheLoopMiddleware.after_model")
    assert node is not None, "HumanInTheLoopMiddleware not installed"
    hitl = _find_hitl(node.bound)
    assert hitl is not None
    return set(hitl.interrupt_on.keys())


def test_fanout_tool_absent_by_default(base_config, tmp_path, monkeypatch):
    """Default tool availability is unchanged: no opt-in → no fan-out tool."""
    from unittest.mock import patch

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from jarn.agent import builder

    monkeypatch.delenv("JARN_PARALLEL_SUBAGENTS", raising=False)
    fake = GenericFakeChatModel(messages=iter([]))
    with patch("jarn.providers.models.ModelFactory.build", return_value=fake):
        rt = builder.build_runtime(base_config, project_root=tmp_path)
    assert "spawn_parallel_tasks" not in _tool_names(rt.agent)


def test_fanout_tool_wired_and_ungated_when_enabled(base_config, tmp_path, monkeypatch):
    """Opt-in registers the tool, shares the task tool's gated general-purpose
    sub-graph by reference, and leaves the orchestration tool itself ungated
    (like ``task``)."""
    from unittest.mock import patch

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from jarn.agent import builder

    monkeypatch.setenv("JARN_PARALLEL_SUBAGENTS", "1")
    fake = GenericFakeChatModel(messages=iter([]))
    with patch("jarn.providers.models.ModelFactory.build", return_value=fake):
        rt = builder.build_runtime(base_config, project_root=tmp_path)

    names = _tool_names(rt.agent)
    assert "spawn_parallel_tasks" in names and "task" in names

    # The fan-out tool reuses the EXACT gated sub-graphs deepagents built.
    graphs = extract_subagent_graphs(rt.agent)
    assert "general-purpose" in graphs

    # Orchestration tool is not itself gated (its subagents carry the gates);
    # the mutating/read builtins still are.
    keys = _hitl_interrupt_keys(rt.agent)
    assert "spawn_parallel_tasks" not in keys
    assert "task" not in keys
    assert "edit_file" in keys
