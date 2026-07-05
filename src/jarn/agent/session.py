"""The session driver — streams a deep-agent turn and mediates approvals.

This is the async engine the TUI sits on top of. It:

* streams assistant text token-by-token,
* surfaces tool calls and results,
* on a HITL interrupt, classifies each gated tool call with the permission
  engine and either auto-resolves it (ALLOW/DENY) or asks the UI (ASK),
* tracks token usage / cost and enforces the budget before each turn.

The approval prompt is delegated to an async ``approver`` callback so the same
driver works headless (tests) and inside Textual.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any

from jarn.agent.events import (
    ApprovalReply,
    ApprovalRequest,
    Approver,
    Event,
    EventKind,
    SuggestedMemory,
    _auto_reject,
)
from jarn.agent.interrupts import (
    _action_requests,
    _hook_detail,
    _looks_like_git_commit,
    _resume_payload,
    resolve_interrupts,
    run_post_hooks,
    run_pre_hooks,
)
from jarn.agent.prompts import date_context
from jarn.agent.stream_handlers import (
    _first_tool_name,
    _is_auth_error,
    _is_retryable_error,
    _provider_of_ref,
    _tool_summary,
    _unpack_stream_item,
    handle_message_chunk,
    handle_update_chunk,
    record_usage,
    resolve_model_ref,
)
from jarn.cost import BudgetExceeded, CostTracker
from jarn.permissions import PermissionEngine

__all__ = [
    "ApprovalReply",
    "ApprovalRequest",
    "Approver",
    "Event",
    "EventKind",
    "SessionDriver",
    "SuggestedMemory",
    "_action_requests",
    "_auto_reject",
    "_first_tool_name",
    "_hook_detail",
    "_is_auth_error",
    "_is_retryable_error",
    "_looks_like_git_commit",
    "_provider_of_ref",
    "_resume_payload",
    "_tool_summary",
    "_unpack_stream_item",
]


@dataclass(slots=True)
class SessionDriver:
    """Drives one conversation thread against a compiled deep agent."""

    agent: Any
    engine: PermissionEngine
    tracker: CostTracker
    thread_id: str
    main_model_ref: str = "unknown"
    #: Model refs we know about (main + subagents + summarizer). Streamed usage is
    #: attributed to whichever of these matches the model the *provider* reports on
    #: the message (``response_metadata``); anything unmatched or absent falls back
    #: to the main model. This bills a delegated subagent on a different model to
    #: that model without guessing from the subgraph namespace (which does not
    #: embed the configured subagent name).
    known_model_refs: tuple[str, ...] = ()
    approver: Approver = _auto_reject
    #: Optional :class:`jarn.extensibility.hooks.HookRunner` for lifecycle hooks
    #: (pre/post tool, post-edit, pre-commit). ``None`` disables hook firing.
    hooks: Any = None
    #: Optional :class:`jarn.memory.sessions.TranscriptWriter`. When set, every
    #: user prompt, assistant reply, and tool call/result is appended to the JSONL
    #: transcript file immediately (crash-safe). ``None`` disables transcription.
    transcript: Any = None
    #: Optional :class:`jarn.agent.checkpoint.CheckpointManager`. When set and
    #: enabled, the working tree is snapshotted at the start of each turn so
    #: ``/undo`` can revert the turn's file edits. Best-effort: a snapshot
    #: failure never aborts the turn.
    checkpoint: Any = None
    #: Most recent write/edit file path, so post_edit hooks can scope by path.
    _last_edit_target: str = ""
    #: Last date block injected into the agent payload (per-turn re-injection).
    _last_date: str | None = field(default=None, repr=False)
    #: Accumulates assistant TEXT chunks for the current turn so a single
    #: ``assistant`` event is written per turn rather than one per streaming token.
    _turn_text: str = ""
    #: Post-edit verify gate: ``off`` | ``suggest`` | ``auto`` (from config).
    verify_gate: str = "off"
    #: Project root for capability detection (``None`` disables verify).
    project_root: Any = None
    #: Optional shell executor for ``verify.gate: auto`` (mock-friendly).
    verify_executor: Any = None
    #: Set to True when at least one write_file/edit_file TOOL_END occurs this turn.
    #: Cleared at turn start; triggers one deferred verify call when the turn
    #: completes without pending interrupts (see debounce logic in run_turn).
    _verify_dirty: bool = False
    #: Last cumulative usage seen per (thread, model) — streaming providers resend totals.
    _last_usage_totals: dict[tuple[str, str], tuple[int, int, int, int]] = field(
        default_factory=dict, repr=False
    )

    def _config(self) -> dict[str, Any]:
        return {"configurable": {"thread_id": self.thread_id}}

    async def run_turn(self, user_input: str, *, resume: bool = False):
        """Async-generate :class:`Event`s for one user turn.

        Raises :class:`jarn.cost.BudgetExceeded` if the hard budget cap is hit
        before the turn starts.

        ``resume=True`` re-runs the model on the thread's *existing* state without
        appending a new user message — used by the front-end's fallback retry,
        since LangGraph already checkpointed the user message before the (failed)
        model call. This prevents a duplicate human message in the thread.
        """
        import time as _time

        self.tracker.check_or_raise()

        messages: list[dict[str, str]] = []
        date_block = date_context()
        if self._last_date != date_block:
            messages.append({"role": "system", "content": date_block})
            self._last_date = date_block

        if resume:
            payload: Any = {"messages": messages}
        else:
            messages.append({"role": "user", "content": user_input})
            payload = {"messages": messages}

        # Emit the user prompt to the transcript before the model is called so a
        # crash mid-turn still records what the user asked.
        if self.transcript is not None and not resume:
            self.transcript.write_user(user_input, ts=_time.time())
        self._turn_text = ""
        self._verify_dirty = False
        if not resume:
            # Clear ALL entries at turn start, not just the current thread's.
            # The cumulative-stream dedup uses this dict to baseline provider totals
            # WITHIN a turn (a provider resends cumulative counts on each chunk; we
            # delta them). Clearing at turn start is correct: deltas are only
            # meaningful within a single turn, and a fresh turn always starts from
            # zero. The old inverted filter (`if k[0] != self.thread_id`) kept OTHER
            # threads' keys forever — after /clear, /compact, or /rewind those stale
            # (thread_id, model_ref) pairs accumulated unboundedly. Clearing also
            # cannot break mid-turn dedup: keys are re-populated by record_usage as
            # each streaming chunk arrives, so the first chunk of a new turn gets
            # treated as an absolute count (no prev → delta = cumulative) which is
            # the correct baseline for a fresh API call.
            self._last_usage_totals.clear()

        # Snapshot the working tree before the agent can edit files, so /undo can
        # revert this turn. Best-effort — a checkpoint failure must never abort
        # the turn (the manager itself no-ops cleanly when disabled / not a repo).
        if self.checkpoint is not None and not resume:
            # A snapshot failure must never abort the turn.
            with contextlib.suppress(Exception):
                self.checkpoint.snapshot(user_input[:80], now=_time.time())

        while True:
            interrupts: list[Any] = []
            try:
                # subgraphs=True so output from delegated subagents (the `task`
                # tool) is also streamed back — otherwise nested subagent replies
                # never surface and the turn looks like it produced no answer.
                async for item in self.agent.astream(
                    payload, self._config(),
                    stream_mode=["messages", "updates"], subgraphs=True,
                ):
                    _, mode, chunk = _unpack_stream_item(item)
                    if mode is None:
                        continue
                    if mode == "messages":
                        ev = self._handle_message_chunk(chunk)
                        if ev:
                            # Accumulate assistant text chunks for the transcript.
                            if ev.kind is EventKind.TEXT and self.transcript is not None:
                                self._turn_text += ev.text
                            # Write tool results incrementally (crash-safe).
                            if ev.kind is EventKind.TOOL_END and self.transcript is not None:
                                self.transcript.write_tool(
                                    ev.text,
                                    ts=_time.time(),
                                    result=ev.data.get("summary", ""),
                                )
                            yield ev
                            if ev.kind is EventKind.TOOL_END and self.hooks is not None:
                                async for note in self._run_post_hooks(ev.text):
                                    yield note
                            if ev.kind is EventKind.TOOL_END and ev.text in (
                                "write_file",
                                "edit_file",
                            ):
                                self._verify_dirty = True
                        # Mid-turn budget enforcement: usage was just recorded for
                        # this message, so re-check the hard-stop the same way it is
                        # checked before the turn and abort cleanly if exceeded.
                        # (A true pre-invoke per-call budget would need a runnable
                        # hook — see follow_up — this is the pragmatic guard.)
                        if self.tracker.should_stop():
                            yield Event(
                                EventKind.ERROR,
                                text=str(BudgetExceeded(
                                    spent=self.tracker.total.cost_usd,
                                    limit=self.tracker.limit or 0.0,
                                )),
                                data={"retryable": False, "budget": True},
                            )
                            return
                    elif mode == "updates":
                        for ev in self._handle_update_chunk(chunk, interrupts):
                            # Write tool-start events incrementally.
                            if ev.kind is EventKind.TOOL_START and self.transcript is not None:
                                self.transcript.write_tool(
                                    ev.text,
                                    ts=_time.time(),
                                    args=ev.data.get("args"),
                                )
                            yield ev
            except Exception as exc:  # noqa: BLE001 - surface to UI, don't crash
                # Tag retryable provider failures (rate-limit/timeout/5xx/etc.)
                # so the front-end can transparently fall back to another model
                # — but only when nothing has been emitted yet (it decides that).
                # Auth/401 failures are *not* retryable (rotating models won't fix a
                # rejected key); tag them so the front-end can show a friendly,
                # actionable message naming the provider instead of the raw SDK JSON.
                data: dict[str, Any] = {"retryable": _is_retryable_error(exc)}
                if _is_auth_error(exc):
                    data["auth"] = True
                    data["provider"] = _provider_of_ref(self.main_model_ref)
                yield Event(EventKind.ERROR, text=str(exc), data=data)
                return

            if not interrupts:
                # Deferred verify: run exactly once after all edits in the turn,
                # only when the turn completes normally (not on cancel/abort — those
                # raise CancelledError which propagates past this branch).
                if self._verify_dirty:
                    from jarn.agent.verify import verify_after_edit

                    notice = await verify_after_edit(self, "write_file")
                    if notice is not None:
                        yield notice
                    self._verify_dirty = False
                # Flush the accumulated assistant reply as a single transcript line.
                if self.transcript is not None and self._turn_text:
                    self.transcript.write_assistant(self._turn_text, ts=_time.time())
                yield Event(EventKind.DONE, data={"usage": self.tracker.summary_line()})
                return

            # Dedupe interrupts by id: with stream_mode=["messages","updates"] +
            # subgraphs=True the same __interrupt__ can surface more than once, and
            # resuming with one decision per duplicate over-counts ("Number of human
            # decisions (N) does not match number of hanging tool calls" — seen when
            # the model batches parallel tool calls). One decision per unique
            # interrupt keeps the count aligned with the hanging calls.
            seen_intr: set[Any] = set()
            unique_interrupts = []
            for intr in interrupts:
                key = getattr(intr, "id", None) or id(intr)
                if key in seen_intr:
                    continue
                seen_intr.add(key)
                unique_interrupts.append(intr)

            # Resolve each interrupt's gated calls, grouped BY interrupt so the
            # resume can be addressed per interrupt id. With subagents, each
            # subagent raises its own HITL interrupt — so more than one can be
            # pending at once, and LangGraph then requires a resume keyed by
            # interrupt id (see _resume_payload).
            resolved: list[tuple[Any, list[Any]]] = []
            for intr in unique_interrupts:
                iid = getattr(intr, "id", None)
                intr_decisions: list[Any] = []
                async for ev, decision in self._resolve_interrupts([intr]):
                    if ev is not None:
                        yield ev
                    # A ``None`` decision means "event only, no resolve vote"
                    # (e.g. a non-fatal hook-failure NOTICE) — it must not be
                    # sent to LangGraph as a resume decision.
                    if decision is not None:
                        intr_decisions.append(decision)
                resolved.append((iid, intr_decisions))
            payload = _resume_payload(resolved)

    # -- thin wrappers for tests / backward compatibility -------------------

    def _handle_message_chunk(self, chunk: Any) -> Event | None:
        return handle_message_chunk(self, chunk)

    def _handle_update_chunk(self, chunk: dict[str, Any], interrupts: list[Any]):
        yield from handle_update_chunk(self, chunk, interrupts)

    def _record_usage(self, msg: Any) -> None:
        record_usage(self, msg)

    def _resolve_model_ref(self, msg: Any) -> str:
        return resolve_model_ref(self, msg)

    async def _resolve_interrupts(self, interrupts: list[Any]):
        async for item in resolve_interrupts(self, interrupts):
            yield item

    async def _run_pre_hooks(self, name: str, action: Any) -> tuple[str | None, list[str]]:
        return await run_pre_hooks(self, name, action)

    async def _run_post_hooks(self, name: str):
        async for item in run_post_hooks(self, name):
            yield item
