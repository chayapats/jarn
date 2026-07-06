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

import asyncio
import logging
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jarn.agent.diagnostics import collect_diagnostics, format_diagnostics
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
    classify_error,
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
    "classify_error",
]

_log = logging.getLogger("jarn")

#: User-facing NOTICE emitted (exactly once per session) when a checkpoint
#: snapshot raises — so a silently-disabled ``/undo`` is no longer invisible.
SNAPSHOT_FAIL_NOTICE = (
    "checkpoint failed — /undo unavailable this turn (see ~/.jarn/logs/jarn.log)"
)

#: Strong references to snapshot tasks detached from a cancelled/closed turn so
#: they finish in their worker thread without being garbage-collected while
#: pending (which would emit "Task was destroyed but it is pending"). Each task's
#: done-callback removes its own entry, so this set self-drains.
_DETACHED_SNAPSHOTS: set[asyncio.Task[Any]] = set()


def _build_user_content(text: str, images: list[Path] | None) -> Any:
    """Return the user-message ``content`` for a turn (T-3-7).

    With no ``images`` (the common path), returns the plain ``text`` string —
    byte-for-byte the pre-existing behaviour. With images, returns a multimodal
    block list ``[{"type":"text", "text": text}, <image block>, …]`` where the
    text (``@path`` intact) leads and each image is a langchain-core base64 image
    block. Images that fail to encode are dropped; if none survive, we fall back to
    the plain string so a turn is never sent empty."""
    if not images:
        return text
    from jarn.agent.files import image_content_block

    blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for path in images:
        block = image_content_block(path)
        if block is not None:
            blocks.append(block)
    if len(blocks) == 1:  # every image failed to encode → plain text
        return text
    return blocks


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
    #: Paths edited this turn (populated from ``_last_edit_target`` on each
    #: write_file / edit_file TOOL_END). Scopes diagnostics to edited files only.
    _edited_paths: set[str] = field(default_factory=set)
    #: Diagnostics feedback mode: ``off`` | ``suggest`` | ``auto`` (from config).
    diagnostics_mode: str = "off"
    #: Max consecutive auto-fix rounds per turn-chain (loop guard).
    diagnostics_max_rounds: int = 1
    #: Also run ``npx tsc --noEmit`` in the diagnostics pass (opt-in; tsc has no
    #: per-file mode so it checks the whole project — slow on big codebases).
    diagnostics_ts: bool = False
    #: Which auto-diag round this driver is in (0 = first/real turn).  Set per
    #: turn by the controller; incremented in the REPL when an auto-queue event
    #: fires.
    _diag_round: int = 0
    #: Last cumulative usage seen per (thread, model) — streaming providers resend totals.
    _last_usage_totals: dict[tuple[str, str], tuple[int, int, int, int]] = field(
        default_factory=dict, repr=False
    )
    #: T-3-5 subagent stream tagging (display-only). ``_subagent_pending`` is a FIFO
    #: of subagent names launched via the ``task`` tool this turn (recorded at
    #: TOOL_START, in call order); each newly-seen subgraph namespace consumes the
    #: next pending name. ``_ns_agent`` remembers namespace-key → name bindings for
    #: the turn so all of a subagent's events carry the same tag.
    #: ``_subagent_seen_calls`` guards against re-emitted update chunks
    #: (stream_mode=["messages","updates"] + subgraphs=True can surface the same
    #: TOOL_START more than once) double-appending the same name and shifting
    #: the FIFO. All three reset at turn start.
    _subagent_pending: list[str] = field(default_factory=list, repr=False)
    _ns_agent: dict[str, str] = field(default_factory=dict, repr=False)
    _subagent_seen_calls: set[str] = field(default_factory=set, repr=False)
    #: In-flight working-tree snapshot for the current turn — started off the
    #: event loop at turn start, awaited at the first mutation gate (and at turn
    #: end for a no-mutation turn), reaped/detached in ``run_turn``'s cleanup.
    #: ``None`` when idle.
    _snapshot_task: asyncio.Task[Any] | None = field(default=None, repr=False)

    def _config(self) -> dict[str, Any]:
        return {"configurable": {"thread_id": self.thread_id}}

    async def run_turn(
        self,
        user_input: str,
        *,
        resume: bool = False,
        images: list[Path] | None = None,
    ):
        """Async-generate :class:`Event`s for one user turn.

        Raises :class:`jarn.cost.BudgetExceeded` if the hard budget cap is hit
        before the turn starts.

        ``resume=True`` re-runs the model on the thread's *existing* state without
        appending a new user message — used by the front-end's fallback retry,
        since LangGraph already checkpointed the user message before the (failed)
        model call. This prevents a duplicate human message in the thread.

        ``images`` (T-3-7) is a list of image paths to inline as native multimodal
        content blocks alongside the text. When provided (and not a ``resume``), the
        user message ``content`` becomes ``[{"type":"text",…}, {"type":"image",…}]``
        instead of a plain string; the original text (with its ``@path`` intact)
        stays in the text block so the ``read_file`` fallback still works and
        transcripts stay greppable. When absent, ``content`` is the plain string —
        the pre-existing path, byte-for-byte unchanged.
        """
        self.tracker.check_or_raise()

        messages: list[dict[str, Any]] = []
        date_block = date_context()
        if self._last_date != date_block:
            messages.append({"role": "system", "content": date_block})
            self._last_date = date_block

        if resume:
            payload: Any = {"messages": messages}
        else:
            messages.append(
                {"role": "user", "content": _build_user_content(user_input, images)}
            )
            payload = {"messages": messages}

        # Emit the user prompt to the transcript before the model is called so a
        # crash mid-turn still records what the user asked.
        if self.transcript is not None and not resume:
            self.transcript.write_user(user_input, ts=_time.time())
        self._turn_text = ""
        self._verify_dirty = False
        self._edited_paths = set()
        # Fresh subagent-tagging state each turn (correlation is per-turn only).
        self._subagent_pending = []
        self._ns_agent = {}
        self._subagent_seen_calls = set()
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

        # A snapshot failure detected during a PRIOR turn's cleanup (after that
        # turn's last yield) could not be surfaced then — emit its NOTICE now, at
        # the very start of this turn, still exactly once per session.
        notice = self._pending_snapshot_notice()
        if notice is not None:
            yield notice

        # Snapshot the working tree BEFORE the agent can edit files (so /undo can
        # revert this turn) — but OFF the event loop, so turn start no longer
        # blocks on O(repo) git work. The model call runs concurrently; the
        # snapshot is awaited at the first mutation gate (and, for a no-mutation
        # turn, at turn end) so no mutating tool ever runs against an uncaptured
        # tree. Best-effort: a failure never aborts the turn — it is logged with a
        # traceback and surfaced as a NOTICE exactly once per session.
        if self.checkpoint is not None and not resume:
            turn_index = await self._current_turn_index()
            self._start_snapshot(user_input[:80], _time.time(), turn_index=turn_index)

        try:
            async for ev in self._stream_turn(payload):
                yield ev
            # Turn-end reap (reached only on a NON-cancelled completion): wait for
            # the snapshot so it is guaranteed to have landed and any failure is
            # recorded. This runs at turn END only (never turn start) and is
            # typically instant — the snapshot overlapped the whole model response.
            # A failure discovered here is past the turn's last yield, so its NOTICE
            # is deferred to the start of the next turn.
            await self._ensure_snapshot()
        finally:
            # Never leak the snapshot task: a cancelled/closed turn skips the reap
            # above (GeneratorExit/CancelledError propagates past it), so settle it
            # here without blocking — detach it to finish in its worker thread (the
            # snapshot still lands; any failure surfaces on the next turn).
            self._detach_pending_snapshot()

    async def _stream_turn(self, payload: Any):
        """Stream one turn's model output and resolve HITL interrupts.

        Extracted from :meth:`run_turn` so the snapshot task can be reaped in a
        single ``finally`` there without indenting this loop. ``payload`` is
        rebuilt in place on each interrupt resume.
        """
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
                    namespace, mode, chunk = _unpack_stream_item(item)
                    if mode is None:
                        continue
                    if mode == "messages":
                        ev = self._handle_message_chunk(chunk, namespace)
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
                                if self._last_edit_target:
                                    self._edited_paths.add(self._last_edit_target)
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
                        for ev in self._handle_update_chunk(chunk, interrupts, namespace):
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
                data: dict[str, Any] = classify_error(exc)
                if data.get("auth"):
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
                # Deferred diagnostics: run ruff/pyright on the edited files and
                # emit a NOTICE (suggest) or a queue-trigger (auto).
                if self._edited_paths and self.diagnostics_mode != "off":
                    diag_ev = await _diagnostics_after_edit(self)
                    if diag_ev is not None:
                        yield diag_ev
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

            # Mutation gate. Every mutating tool (write_file / edit_file / execute
            # — plus SHELL-gated run_in_background starts) ALWAYS raises a HITL
            # interrupt (permissions_bridge.MUTATING_TOOLS), even when the engine
            # auto-approves without prompting, and the tool only EXECUTES when the
            # graph is resumed at the top of this loop with ``payload``. Awaiting
            # the snapshot HERE — after the resume decision is built, before the
            # loop resumes — therefore guarantees the working tree is captured
            # before ANY mutation runs, on every resolution path (auto-allow, ask,
            # edit-before-apply, background start). Idempotent: the task is reaped
            # on the first gate, so later gates in the same turn are no-ops. This
            # gate also (harmlessly) fires on a non-mutating NETWORK/MCP resume —
            # those tools raise their own HITL interrupt and resume through this
            # same loop; awaiting an already-reaped or about-to-complete snapshot
            # there is a cheap no-op and never wrong.
            await self._ensure_snapshot()
            notice = self._pending_snapshot_notice()
            if notice is not None:
                yield notice

    # -- checkpoint snapshot lifecycle --------------------------------------

    async def _current_turn_index(self) -> int | None:
        """0-based index of the turn about to run = the count of human messages
        ALREADY in this thread's checkpointed state (before this turn's message is
        appended). Recorded on the turn-start snapshot so ``/rewind`` can resolve a
        chosen turn back to its checkpoint (:meth:`CheckpointManager.find_for_turn`).

        Returns ``None`` when the graph state can't be read (a fake agent without
        ``aget_state`` in tests, or any error) — the snapshot is still taken, just
        as an untagged old-format record that ``find_for_turn`` won't match."""
        get_state = getattr(self.agent, "aget_state", None)
        if get_state is None:
            return None
        try:
            state = await get_state(self._config())
        except Exception:  # noqa: BLE001 - metadata is best-effort; never abort the turn
            return None
        messages = (getattr(state, "values", {}) or {}).get("messages", []) or []
        return sum(1 for m in messages if getattr(m, "type", "") == "human")

    def _start_snapshot(
        self, label: str, ts: float, *, turn_index: int | None = None
    ) -> None:
        """Kick off the working-tree snapshot in a worker thread (off the event
        loop). The manager's git work is synchronous subprocess code, safe to run
        via ``to_thread``. Awaited later at the first mutation gate / turn end;
        reaped or detached in ``run_turn``'s cleanup.

        ``turn_index`` (with this driver's ``thread_id``) tags the snapshot so
        ``/rewind`` can resolve it back to the turn that produced it."""
        self._snapshot_task = asyncio.create_task(
            asyncio.to_thread(
                self.checkpoint.snapshot,
                label,
                now=ts,
                thread_id=self.thread_id,
                turn_index=turn_index,
            )
        )

    async def _ensure_snapshot(self) -> None:
        """Block until the in-flight snapshot has landed, so a mutating tool never
        runs against an uncaptured tree. Idempotent — a turn snapshots once, so
        later gates are no-ops. A git *exception* is logged with a traceback and
        recorded as a once-per-session pending NOTICE; a benign ``ok=False`` result
        (disabled / not-a-repo / nothing-new) is not a failure and surfaces nothing.
        Never aborts the turn (a snapshot failure is swallowed here)."""
        task = self._snapshot_task
        if task is None:
            return
        if not task.done():
            try:
                await task
            except asyncio.CancelledError:
                # The turn is being cancelled while we wait — leave the task on the
                # slot so run_turn's cleanup detaches it (don't orphan it here).
                raise
            except Exception:  # noqa: BLE001 - snapshot is best-effort; never abort
                pass  # the failure is read back from the task below
        # Guard against a race with _detach_pending_snapshot: if that method ran
        # during the await above (e.g. the cancelled turn's finally fired while
        # settle_snapshot was awaiting here), it already took ownership of the task
        # and registered a done-callback that will call _collect_snapshot_result.
        # Collecting here too would log the traceback a second time (the NOTICE is
        # deduped by snapshot_notice_shown, but the log entry is not).
        if self._snapshot_task is not task:
            return
        self._snapshot_task = None
        self._collect_snapshot_result(task)

    def _detach_pending_snapshot(self) -> None:
        """``run_turn`` cleanup safety net. If the snapshot task is still set — a
        cancelled/closed turn skipped the turn-end reap — settle it WITHOUT
        blocking: collect it if already finished, else detach it to complete in its
        worker thread (the snapshot still lands; the done-callback surfaces any
        failure on the next turn). Never awaits, so it is safe under GeneratorExit."""
        task = self._snapshot_task
        if task is None:
            return
        self._snapshot_task = None
        if task.done():
            self._collect_snapshot_result(task)
        else:
            self._detach_snapshot(task)

    def _detach_snapshot(self, task: asyncio.Task[Any]) -> None:
        """Hold a strong reference to a still-running snapshot task and arrange for
        its outcome to be retrieved when it finishes — so it neither leaks (GC'd
        while pending) nor warns about an unretrieved exception."""
        _DETACHED_SNAPSHOTS.add(task)
        task.add_done_callback(self._on_detached_snapshot_done)

    def _on_detached_snapshot_done(self, task: asyncio.Task[Any]) -> None:
        _DETACHED_SNAPSHOTS.discard(task)
        self._collect_snapshot_result(task)

    def _collect_snapshot_result(self, task: asyncio.Task[Any]) -> None:
        """Read a finished snapshot task's outcome and record a failure NOTICE if it
        raised. A cancelled task carries no outcome to surface."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._handle_snapshot_failure(exc)

    def _handle_snapshot_failure(self, exc: BaseException) -> None:
        """Log the snapshot failure with a full traceback and arm the once-per-
        session NOTICE (deduped via ``snapshot_notice_shown`` on the shared
        CheckpointManager — session-lifetime, so it survives across per-turn
        driver instances)."""
        _log.error(
            "checkpoint snapshot failed; /undo unavailable this turn", exc_info=exc
        )
        cp = self.checkpoint
        if cp is not None and not cp.snapshot_notice_shown:
            cp.snapshot_notice_pending = True

    def _pending_snapshot_notice(self) -> Event | None:
        """Return the once-per-session snapshot-failure NOTICE if one is armed and
        not yet shown (marking it shown), else ``None``.  Reads/writes state on the
        CheckpointManager so the dedupe survives across fresh per-turn drivers."""
        cp = self.checkpoint
        if cp is None:
            return None
        if cp.snapshot_notice_pending and not cp.snapshot_notice_shown:
            cp.snapshot_notice_pending = False
            cp.snapshot_notice_shown = True
            return Event(EventKind.NOTICE, text=SNAPSHOT_FAIL_NOTICE)
        return None

    async def settle_snapshot(self) -> None:
        """Await every in-flight checkpoint snapshot so a UI-driven checkpoint-stack
        mutation (``/undo`` / ``/redo`` / an ``/abort`` rollback) can never race a
        snapshot that is still building its tree OFF ``_checkpoint_lock``.

        Two sources are drained: this driver's own pending task (covers ``/abort``
        firing *before* the cancelled turn's ``finally`` has detached it — the task
        is still on the slot) and the module-level ``_DETACHED_SNAPSHOTS`` set
        (snapshots a cancelled/closed turn detached fire-and-forget, which may still
        be building in their worker thread). A snapshot's function only returns after
        it has pushed under the lock, so once its task is done the checkpoint is on
        the stack — the undo/redo/abort that follows then targets THIS turn's entry.

        Without this, ``undo()`` takes the lock first and pops the PREVIOUS turn's
        checkpoint (this turn's snapshot has not pushed yet), reverting the tree an
        extra turn back (over-revert) while the late snapshot then pushes — leaving
        the stack out of sync with disk.

        Best-effort and non-fatal: snapshot exceptions are swallowed (a failed
        snapshot just means this turn has no checkpoint — same as the old sync
        behaviour, still surfaced via the once-per-session NOTICE path); this never
        raises on them. Fast when nothing is pending — an idle driver returns without
        awaiting."""
        # 1. This driver's own task, if /abort beat the cancelled turn's finally
        #    (the snapshot is still on the slot, not yet detached). No-op when the
        #    turn already reaped/detached it (``_snapshot_task is None``).
        await self._ensure_snapshot()
        # 2. Snapshots detached by a cancelled/closed turn: await each still-pending
        #    one, letting its own done-callback record any failure and self-remove.
        #    Loop on done-ness (not set membership) so a callback that has not yet
        #    run on the loop can't spin us; ``return_exceptions`` keeps a failing
        #    snapshot from propagating out of settle.
        while True:
            pending = [t for t in _DETACHED_SNAPSHOTS if not t.done()]
            if not pending:
                break
            await asyncio.gather(*pending, return_exceptions=True)

    # -- thin wrappers for tests / backward compatibility -------------------

    def _handle_message_chunk(self, chunk: Any, namespace: Any = ()) -> Event | None:
        return handle_message_chunk(self, chunk, namespace)

    def _handle_update_chunk(
        self, chunk: dict[str, Any], interrupts: list[Any], namespace: Any = ()
    ):
        yield from handle_update_chunk(self, chunk, interrupts, namespace)

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


# ---------------------------------------------------------------------------
# Diagnostics helper (T-3-3)
# ---------------------------------------------------------------------------


async def _diagnostics_after_edit(driver: SessionDriver) -> Any | None:
    """Run diagnostics on edited files and return an Event or None.

    Called only when ``driver._edited_paths`` is non-empty and
    ``driver.diagnostics_mode != "off"``.

    Modes:
    - ``suggest``: always emit a NOTICE with the formatted diagnostics text.
    - ``auto``:    queue a follow-up turn if there are *error*-severity findings
                   and we haven't hit ``diagnostics_max_rounds``.
    """
    mode = driver.diagnostics_mode
    if mode == "off":
        return None
    paths = [Path(p) for p in driver._edited_paths if p]
    project_root = driver.project_root
    if not paths or project_root is None:
        return None
    try:
        diags = await asyncio.wait_for(
            asyncio.to_thread(
                collect_diagnostics, paths, project_root, ts=driver.diagnostics_ts
            ),
            timeout=30.0,
        )
    except TimeoutError:
        return None
    if not diags:
        return None
    error_diags = [d for d in diags if d.severity == "error"]
    if mode == "suggest":
        text = format_diagnostics(diags)
        return Event(
            EventKind.NOTICE,
            data={"diagnostics": {"text": text, "count": len(diags)}},
        )
    # auto mode: queue only if errors exist and rounds remain
    if not error_diags:
        return None
    if driver._diag_round >= driver.diagnostics_max_rounds:
        return None
    payload = f"Diagnostics after your edits:\n{format_diagnostics(diags)}\nFix them."
    return Event(EventKind.NOTICE, data={"diagnostics_auto_queue": payload})
