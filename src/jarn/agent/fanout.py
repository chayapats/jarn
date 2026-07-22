"""Parallel subagent fan-out: ``spawn_parallel_tasks``.

The deepagents ``task`` tool spawns exactly one subagent at a time and blocks on
it. This module adds a *concurrent* fan-out tool: the model hands it a list of
tasks and they run at once (``asyncio.gather`` over the underlying subagent
sub-graph invocations), returning a single aggregated roll-up instead of N
separate tool round-trips.

Design seams (so the orchestration is testable without a live agent):

* ``SubagentInvoke`` — an async callable ``(subagent_type, description) ->
  result-state``. The runtime wires this to the *same* gated subagent runnables
  deepagents built for the ``task`` tool (see :mod:`jarn.agent.runtime`), so
  every subagent tool call still flows through the permission engine and its
  usage is billed exactly as a ``task``-spawned subagent's is. Tests inject a
  fake to prove concurrency + aggregation deterministically.
* ``CostTracker`` (optional) — per-task usage is recorded into its
  ``per_namespace`` bucket for a SOFT per-task budget + observability. The cap is
  post-hoc: usage is measured from the subagent's returned messages and compared
  to the cap after the task finishes. Hard mid-stream pre-emption is deferred
  (it needs the session driver to abort a specific in-flight sub-graph, which is
  out of this module's reach).

The tool itself is never gated (like ``task``): it only orchestrates subagents,
each of which carries its own ``interrupt_on`` gate. All authority-bearing tool
calls happen *inside* those gated subagents.

HITL under fan-out (approval interrupts):

* A gated action inside a fanned-out subagent raises one of LangGraph's
  ``GraphBubbleUp`` signals (``GraphInterrupt`` = the approval pause). Such a
  signal is RE-RAISED (see ``_run_one``), never converted into a task ``"error"``,
  so it propagates through ``asyncio.gather`` to the SessionDriver's
  approve/resume path exactly as a ``task``-spawned subagent's would.
* LIMITATION (deferred, not faked): resume-exactly-once across N concurrent
  siblings is not implemented. ``asyncio.gather`` surfaces the FIRST interrupt to
  the driver while the other tasks are still in flight; on resume LangGraph
  re-executes the whole ``spawn_parallel_tasks`` tool call, so already-finished
  siblings are re-run rather than resumed. Fan-out is therefore only validated
  under AUTO-RESOLVING permission modes (``yolo`` / pre-approved), where no
  approval interrupt ever fires — which is why the feature stays OPT-IN and
  off by default (``JARN_PARALLEL_SUBAGENTS``). The required correctness property
  here is only that an interrupt is never silently swallowed.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from langgraph.errors import GraphBubbleUp

from jarn.config.secrets import redact_secrets
from jarn.cost import pricing
from jarn.cost.tracker import CostTracker

#: An async subagent invocation: ``(subagent_type, description) -> result state``.
#: The result state is a mapping shaped like a deepagents/langchain agent result
#: (a ``"messages"`` list, optionally a ``"structured_response"``); this module
#: extracts the final answer + usage from it.
SubagentInvoke = Callable[[str, str], Awaitable[Mapping[str, Any]]]

#: Default cap on how many tasks run at once, so a runaway task list cannot spawn
#: an unbounded number of concurrent sub-graphs. Excess tasks queue behind a
#: semaphore (still concurrent up to the cap).
_DEFAULT_MAX_PARALLEL = 8

#: Sentinel used when a task omits ``subagent_type``.
_DEFAULT_SUBAGENT_TYPE = "general-purpose"


@dataclass(slots=True, frozen=True)
class ParallelTask:
    """One unit of parallel work the model requested."""

    description: str
    subagent_type: str = _DEFAULT_SUBAGENT_TYPE
    #: Optional soft per-task USD cap. When set and the task's measured cost meets
    #: or exceeds it, the outcome is flagged ``budget_exceeded`` (post-hoc — the
    #: task is not pre-empted mid-run; see module docstring).
    max_usd: float | None = None


@dataclass(slots=True)
class TaskOutcome:
    """The aggregated result of one fanned-out task."""

    index: int
    subagent_type: str
    status: str  # "ok" | "error" | "unknown_subagent" | "budget_exceeded"
    summary: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    unpriced: bool = False
    budget_exceeded: bool = False
    duration_s: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(slots=True)
class FanoutResult:
    """The full aggregated result of a ``spawn_parallel_tasks`` call."""

    outcomes: list[TaskOutcome] = field(default_factory=list)

    @property
    def total_cost_usd(self) -> float:
        return sum(o.cost_usd for o in self.outcomes)

    @property
    def total_tokens(self) -> int:
        return sum(o.total_tokens for o in self.outcomes)

    def counts(self) -> dict[str, int]:
        """Status → number of tasks with that status."""
        out: dict[str, int] = {}
        for o in self.outcomes:
            out[o.status] = out.get(o.status, 0) + 1
        return out

    def rollup_line(self) -> str:
        """A one-line summary: ``N tasks: k ok, … · $cost · tok``."""
        n = len(self.outcomes)
        parts = [f"{v} {k}" for k, v in sorted(self.counts().items())]
        summary = ", ".join(parts) if parts else "none"
        line = f"{n} task{'s' if n != 1 else ''}: {summary}"
        line += f" · ${self.total_cost_usd:.4f} · {self.total_tokens:,} tok"
        if any(o.unpriced for o in self.outcomes):
            line += " · some unpriced"
        return line


# ---------------------------------------------------------------------------
# Result-state extraction (mirrors deepagents' task-tool extraction).


def _extract_final_text(result: Mapping[str, Any]) -> str:
    """The subagent's final answer: its ``structured_response`` (JSON) if present,
    else the last non-empty ``AIMessage`` text — matching deepagents' own
    ``_return_command_with_state_update`` so a fanned-out subagent reads the same
    as a ``task``-spawned one."""
    import dataclasses
    import json

    structured = result.get("structured_response")
    if structured is not None:
        if hasattr(structured, "model_dump_json"):
            return str(structured.model_dump_json())
        if dataclasses.is_dataclass(structured) and not isinstance(structured, type):
            return json.dumps(dataclasses.asdict(structured))
        return json.dumps(structured)

    messages = result.get("messages") or []
    for msg in reversed(list(messages)):
        # ``.text`` is a property on current langchain (str); guard the access so a
        # version where it is absent/odd falls back to ``.content`` rather than
        # raising. Never *call* it — that path is deprecated.
        try:
            text = msg.text
        except Exception:  # noqa: BLE001 - defensive extraction, fall back to content
            text = None
        if isinstance(text, str) and text.strip():
            return text.strip()
        content = getattr(msg, "content", None)
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


def _extract_usage(
    result: Mapping[str, Any], default_model_ref: str
) -> tuple[str, int, int, int, int]:
    """Sum ``usage_metadata`` across the subagent's ``AIMessage``s.

    Returns ``(model_id, input_tokens, output_tokens, cache_read, cache_creation)``.
    ``model_id`` is the model the last usage-bearing message reported
    (``response_metadata['model_name']``), falling back to ``default_model_ref``
    (the ref this subagent is known to run on) so pricing has a key.
    """
    input_tokens = output_tokens = cache_read = cache_creation = 0
    model_id = default_model_ref
    for msg in result.get("messages") or []:
        um = getattr(msg, "usage_metadata", None)
        if not um:
            continue
        input_tokens += int(um.get("input_tokens", 0) or 0)
        output_tokens += int(um.get("output_tokens", 0) or 0)
        details = um.get("input_token_details") or {}
        cache_read += int(details.get("cache_read", 0) or 0)
        cache_creation += int(details.get("cache_creation", 0) or 0)
        meta = getattr(msg, "response_metadata", None) or {}
        reported = meta.get("model_name") or meta.get("model")
        if reported:
            model_id = str(reported)
    return model_id, input_tokens, output_tokens, cache_read, cache_creation


# ---------------------------------------------------------------------------
# Orchestration.


async def run_parallel_tasks(
    tasks: Sequence[ParallelTask],
    *,
    invoke: SubagentInvoke,
    available_subagents: Sequence[str],
    default_model_ref: str,
    cost_tracker: CostTracker | None = None,
    default_max_usd: float | None = None,
    max_parallel: int = _DEFAULT_MAX_PARALLEL,
    namespace_prefix: str = "fanout",
    clock: Callable[[], float] = time.monotonic,
) -> FanoutResult:
    """Run ``tasks`` concurrently and aggregate their outcomes.

    Concurrency is real: every task launches together under ``asyncio.gather``
    (bounded by a ``max_parallel`` semaphore), so N tasks take ~max(task) wall
    time rather than the sum. An unknown ``subagent_type`` is reported without
    invoking anything; a task whose invocation raises an ordinary exception is
    captured as an ``error`` outcome (one failure never cancels its siblings).

    A LangGraph ``GraphBubbleUp`` (e.g. ``GraphInterrupt`` — the HITL approval
    pause a gated action raises) is the ONE exception that is re-raised instead of
    captured, so it reaches the driver's approve/resume path rather than being
    swallowed into an ``error``. See the module docstring for the concurrent
    resume-exactly-once limitation (deferred; feature stays opt-in/off by default).

    Per-task usage is measured from each subagent's returned messages, priced, and
    recorded into ``cost_tracker.per_namespace`` (when a tracker is given). A task
    with a ``max_usd`` cap (or ``default_max_usd``) whose measured cost meets/exceeds
    the cap is flagged ``budget_exceeded`` — a SOFT, post-hoc cap (see module
    docstring for why hard pre-emption is deferred).
    """
    available = set(available_subagents)
    sem = asyncio.Semaphore(max(1, max_parallel))

    async def _run_one(index: int, task: ParallelTask) -> TaskOutcome:
        sub_type = task.subagent_type or _DEFAULT_SUBAGENT_TYPE
        cap = task.max_usd if task.max_usd is not None else default_max_usd
        if available and sub_type not in available:
            allowed = ", ".join(sorted(available)) or "(none)"
            return TaskOutcome(
                index=index,
                subagent_type=sub_type,
                status="unknown_subagent",
                summary=f"unknown subagent_type {sub_type!r}; available: {allowed}",
            )
        started = clock()
        async with sem:
            try:
                result = await invoke(sub_type, task.description)
            except GraphBubbleUp:
                # HITL boundary. LangGraph raises a ``GraphBubbleUp`` subclass
                # (``GraphInterrupt`` is the approval-pause signal; ``ParentCommand``
                # / ``GraphDrained`` are its siblings) to hand control back to the
                # graph runner. These are NOT task failures — swallowing one into
                # ``status="error"`` (the generic handler below) would silently drop
                # the approval prompt for a gated action a fanned-out subagent hit,
                # so the SessionDriver would never pause/resume. Re-raise so it
                # propagates through ``asyncio.gather`` to the driver's approve/resume
                # path exactly as a ``task``-spawned subagent's interrupt does.
                # (See ``run_parallel_tasks`` for the concurrent-resume limitation.)
                raise
            except Exception as exc:  # noqa: BLE001 - report, never crash the batch
                # A subagent's provider/MCP/subprocess exception text can carry
                # credentials (e.g. an echoed ``Authorization: Bearer …``). This
                # summary is fed back to the orchestrating model, so scrub it with
                # the shared redactor before it leaves the batch (round-9 #3).
                return TaskOutcome(
                    index=index,
                    subagent_type=sub_type,
                    status="error",
                    summary=redact_secrets(f"{type(exc).__name__}: {exc}"),
                    duration_s=clock() - started,
                )
        duration = clock() - started

        model_id, in_tok, out_tok, cache_r, cache_w = _extract_usage(
            result, default_model_ref
        )
        cost = pricing.cost_of(
            model_id, in_tok, out_tok,
            cache_read_tokens=cache_r, cache_creation_tokens=cache_w,
        )
        unpriced = cost is None
        cost = cost or 0.0
        if cost_tracker is not None:
            # Record into the per-namespace budget/observability dimension. Uses the
            # cumulative bucket cost for the cap check (a namespace is unique per
            # task here, so cumulative == this task's cost).
            bucket = cost_tracker.record_task_usage(
                f"{namespace_prefix}[{index}]:{sub_type}",
                model_id, in_tok, out_tok,
                cache_read_tokens=cache_r, cache_creation_tokens=cache_w,
            )
            cost = bucket.cost_usd
            unpriced = bucket.unpriced_calls > 0

        breached = cap is not None and cost >= cap
        return TaskOutcome(
            index=index,
            subagent_type=sub_type,
            status="budget_exceeded" if breached else "ok",
            summary=_extract_final_text(result),
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=cost,
            unpriced=unpriced,
            budget_exceeded=breached,
            duration_s=duration,
        )

    outcomes = await asyncio.gather(
        *(_run_one(i, t) for i, t in enumerate(tasks))
    )
    return FanoutResult(outcomes=list(outcomes))


def format_result(result: FanoutResult) -> str:
    """Render a :class:`FanoutResult` as the tool's string return value.

    A leading roll-up line, then one block per task (index, subagent, status,
    cost, and the subagent's answer) so the orchestrating model can both scan the
    summary and read each result.
    """
    lines = [result.rollup_line(), ""]
    for o in result.outcomes:
        header = f"[task {o.index}] {o.subagent_type} — {o.status}"
        meta = f"(${o.cost_usd:.4f}, {o.total_tokens:,} tok, {o.duration_s:.2f}s"
        if o.budget_exceeded:
            meta += ", budget cap reached"
        if o.unpriced:
            meta += ", unpriced"
        meta += ")"
        lines.append(f"{header} {meta}")
        lines.append(o.summary or "(no output)")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Reusing deepagents' gated subagent runnables.


def extract_subagent_graphs(agent: Any) -> dict[str, Any]:
    """Pull the compiled subagent runnables out of a deepagents ``task`` tool.

    deepagents builds one fully-constructed, permission-gated sub-graph per
    subagent (with the parent's ``interrupt_on`` propagated) and stores them only
    in the ``task`` tool's closure (``subagent_graphs``). Reusing *those exact*
    objects for fan-out is what guarantees a parallel-spawned subagent is
    constructed and gated identically to a ``task``-spawned one.

    Returns ``{name: runnable}`` (empty on any structural mismatch — deepagents
    internals changed — so the caller can degrade by simply not wiring the tool,
    never by silently dropping the permission gate). Mirrors the closure-walk the
    test-suite already does for the HITL middleware.
    """
    try:
        tools_node = agent.nodes["tools"].bound
        task_tool = tools_node.tools_by_name["task"]
    except (KeyError, AttributeError):
        return {}
    fn = getattr(task_tool, "coroutine", None) or getattr(task_tool, "func", None)
    freevars = getattr(getattr(fn, "__code__", None), "co_freevars", ())
    closure = getattr(fn, "__closure__", None) or ()
    for name, cell in zip(freevars, closure, strict=False):
        if name != "subagent_graphs":
            continue
        try:
            value = cell.cell_contents
        except ValueError:
            return {}
        if isinstance(value, dict):
            return dict(value)
    return {}


# ---------------------------------------------------------------------------
# Tool construction.


def build_spawn_parallel_tasks_tool(
    *,
    invoke: SubagentInvoke,
    available_subagents: Sequence[str],
    default_model_ref: str,
    cost_tracker: CostTracker | None = None,
    default_max_usd: float | None = None,
    max_parallel: int = _DEFAULT_MAX_PARALLEL,
    clock: Callable[[], float] = time.monotonic,
) -> Any:
    """Build the ``spawn_parallel_tasks`` LangChain tool.

    The tool is async-only (jarn drives the agent with ``astream``). Its body runs
    :func:`run_parallel_tasks` and returns :func:`format_result`. It is a builtin
    orchestration tool like ``task`` and is intentionally NOT gated — the subagents
    it launches carry their own permission gates.
    """
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel, Field

    available = list(available_subagents)
    agents_desc = ", ".join(available) or "general-purpose"

    class _TaskInput(BaseModel):
        description: str = Field(
            description=(
                "Detailed, self-contained instructions for the subagent, including "
                "all context it needs and the exact output you expect back."
            )
        )
        subagent_type: str = Field(
            default=_DEFAULT_SUBAGENT_TYPE,
            description=f"Which subagent to use. One of: {agents_desc}.",
        )
        max_usd: float | None = Field(
            default=None,
            description=(
                "Optional soft USD cap for this task. If its measured cost reaches "
                "the cap it is flagged (post-hoc); the task is not interrupted "
                "mid-run."
            ),
        )

    class _Args(BaseModel):
        tasks: list[_TaskInput] = Field(
            description="The tasks to run concurrently (each an isolated subagent)."
        )

    def _field(t: Any, key: str, default: Any = None) -> Any:
        # ``t`` is a validated ``_TaskInput`` (pydantic) under a real tool call, but
        # a plain dict when the coroutine is exercised directly — accept both.
        if isinstance(t, Mapping):
            return t.get(key, default)
        return getattr(t, key, default)

    async def spawn_parallel_tasks(tasks: list[Any]) -> str:
        parsed = [
            ParallelTask(
                description=str(_field(t, "description", "")),
                subagent_type=str(
                    _field(t, "subagent_type") or _DEFAULT_SUBAGENT_TYPE
                ),
                max_usd=_field(t, "max_usd"),
            )
            for t in tasks
        ]
        result = await run_parallel_tasks(
            parsed,
            invoke=invoke,
            available_subagents=available,
            default_model_ref=default_model_ref,
            cost_tracker=cost_tracker,
            default_max_usd=default_max_usd,
            max_parallel=max_parallel,
            clock=clock,
        )
        return format_result(result)

    description = (
        "Launch several independent subagents CONCURRENTLY and get back one "
        "aggregated report (per-task status + result, plus a roll-up). Prefer this "
        "over calling `task` sequentially when you have 2+ independent subtasks — "
        "they run in parallel instead of one after another.\n\n"
        f"Available subagent types: {agents_desc}.\n\n"
        "Each task takes a `description` (detailed, self-contained instructions), "
        "an optional `subagent_type`, and an optional `max_usd` soft budget cap. "
        "Only delegate genuinely independent work; the subagents cannot talk to "
        "each other or to you until they finish."
    )
    return StructuredTool.from_function(
        coroutine=spawn_parallel_tasks,
        name="spawn_parallel_tasks",
        description=description,
        args_schema=_Args,
        infer_schema=False,
    )
