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
import contextlib
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from langgraph.types import Command, Overwrite

from jarn.agent.permissions_bridge import tool_to_action
from jarn.cost import BudgetExceeded, CostTracker
from jarn.permissions import (
    Action,
    ActionKind,
    Decision,
    PermissionEngine,
    PermissionResult,
    RememberScope,
)

# LangChain message ``.type`` values that represent assistant output.
_ASSISTANT_TYPES = {"ai", "AIMessageChunk"}
# Content-block / kwarg shapes various providers use for extended-reasoning text.
_REASONING_TYPES = {"thinking", "reasoning", "reasoning_content"}
# Upper bound on retained tool output for Ctrl+O expand (guards memory).
_MAX_FULL_CHARS = 100_000


# -- events emitted to the UI ----------------------------------------------

class EventKind(str, Enum):
    TEXT = "text"
    REASONING = "reasoning"      # extended-thinking text (shown dim, secondary)
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    APPROVAL = "approval"        # informational: how an approval was resolved
    NOTICE = "notice"
    ERROR = "error"
    DONE = "done"


@dataclass(slots=True)
class Event:
    kind: EventKind
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)


# -- approval contract ------------------------------------------------------

@dataclass(slots=True)
class SuggestedMemory:
    """A memory the agent proposes for the user to approve, edit, or decline.

    Carried on an :class:`ApprovalRequest` when the agent calls ``suggest_memory``.
    The approver surfaces a "Save this memory?" prompt and, on approval, writes it
    through the existing :class:`~jarn.memory.MemoryStore` (respecting the global
    vs project tier and the project's trust gating)."""

    name: str
    description: str
    body: str
    type: str = "project"
    #: ``"global"`` or ``"project"`` — which store tier to write to. Project writes
    #: are refused on an untrusted project (the approver reports why).
    scope: str = "project"


@dataclass(slots=True)
class ApprovalRequest:
    action: Action
    result: PermissionResult
    description: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    #: Set (to the proposed plan text) when this is a plan-mode handoff request
    #: from ``exit_plan_mode`` rather than an ordinary tool approval. The approver
    #: shows the plan and, on approval, escalates the permission mode.
    plan: str | None = None
    #: Set when this is an agent memory suggestion (``suggest_memory``) rather than
    #: an ordinary tool approval. The approver shows it and, on approval, writes it
    #: through the memory store.
    suggested_memory: SuggestedMemory | None = None


@dataclass(slots=True)
class ApprovalReply:
    approved: bool
    scope: RememberScope = RememberScope.ONCE
    message: str = ""           # reason shown to the model on rejection
    #: When the user chose "edit before apply", the tool args edited in $EDITOR.
    #: The turn resumes with a LangGraph ``edit`` decision carrying these args, so
    #: the *edited* content lands on disk instead of the agent's original. ``None``
    #: means a plain approve (run the tool with its original args).
    # TODO(per-hunk): edit-before-apply replaces the whole new content/replacement.
    # Per-hunk (partial) approval is deferred — it needs hunk parsing + partial
    # apply of a unified diff; not implemented in this pass (see fable-todo.md P4.B).
    edited_args: dict[str, Any] | None = None
    #: For a plan-mode handoff (``exit_plan_mode``): the permission mode the user
    #: chose to escalate to on approval (e.g. ``"auto-edit"``/``"ask"``). The
    #: approver applies it; ``None`` for ordinary approvals.
    plan_mode_target: str | None = None


# approver(request) -> reply
Approver = Callable[[ApprovalRequest], Awaitable[ApprovalReply]]


async def _auto_reject(request: ApprovalRequest) -> ApprovalReply:
    """Default approver used headless: deny anything that needs asking."""
    return ApprovalReply(approved=False, message="auto-denied (no interactive approver)")


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
    #: Accumulates assistant TEXT chunks for the current turn so a single
    #: ``assistant`` event is written per turn rather than one per streaming token.
    _turn_text: str = ""

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

        payload: Any = (
            {"messages": []} if resume
            else {"messages": [{"role": "user", "content": user_input}]}
        )

        # Emit the user prompt to the transcript before the model is called so a
        # crash mid-turn still records what the user asked.
        if self.transcript is not None and not resume:
            self.transcript.write_user(user_input, ts=_time.time())
        self._turn_text = ""

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
                    intr_decisions.append(decision)
                resolved.append((iid, intr_decisions))
            payload = _resume_payload(resolved)

    # -- stream handling ----------------------------------------------------

    def _handle_message_chunk(self, chunk: Any) -> Event | None:
        msg = chunk[0] if isinstance(chunk, tuple) else chunk
        self._record_usage(msg)
        mtype = getattr(msg, "type", "")
        # Tool results (ToolMessage) — e.g. a fetched web page — must not be
        # dumped into the chat, but a one-line summary ("3 lines", "12 results")
        # under the tool call mirrors Claude Code's "⎿ result" affordance.
        if mtype == "tool":
            full = _text_of(getattr(msg, "content", "")).strip()
            tool_name = getattr(msg, "name", "") or ""
            data: dict[str, Any] = {"summary": _tool_summary(full, tool_name)}
            tool_call_id = getattr(msg, "tool_call_id", None)
            if tool_call_id:
                data["tool_call_id"] = str(tool_call_id)
            # Keep the full payload (capped) for on-demand expand (Ctrl+O), but
            # only when there is genuinely more to see than the summary line.
            if full and (full.count("\n") or len(full) > 80):
                data["full"] = full[:_MAX_FULL_CHARS]
            return Event(EventKind.TOOL_END, text=getattr(msg, "name", "") or "tool", data=data)
        # Otherwise only stream ASSISTANT text; the model's reply is what the
        # user should see.
        if mtype not in _ASSISTANT_TYPES:
            return None
        content = _text_of(getattr(msg, "content", ""))
        if content:
            return Event(EventKind.TEXT, text=content)
        # No visible answer text in this chunk: surface extended-reasoning text
        # (Anthropic thinking blocks, DeepSeek `reasoning_content`, …) if present.
        reasoning = _reasoning_of(msg)
        if reasoning:
            return Event(EventKind.REASONING, text=reasoning)
        return None

    def _handle_update_chunk(self, chunk: dict[str, Any], interrupts: list[Any]):
        if not isinstance(chunk, dict):
            return
        if "__interrupt__" in chunk:
            for intr in chunk["__interrupt__"]:
                interrupts.append(intr)
            return
        for _node, update in chunk.items():
            if not isinstance(update, dict):
                continue
            messages = update.get("messages", []) or []
            if isinstance(messages, Overwrite):
                messages = messages.value or []
            for msg in messages:
                for call in getattr(msg, "tool_calls", None) or []:
                    name = call.get("name", "tool")
                    args = call.get("args", {}) or {}
                    if name in ("write_file", "edit_file"):
                        self._last_edit_target = str(
                            args.get("file_path") or args.get("path")
                            or args.get("filename") or ""
                        )
                    data: dict[str, Any] = {"args": args}
                    call_id = call.get("id")
                    if call_id:
                        data["tool_call_id"] = str(call_id)
                    yield Event(EventKind.TOOL_START, text=name, data=data)

    # -- lifecycle hooks ----------------------------------------------------

    async def _run_pre_hooks(self, name: str, action: Action) -> str | None:
        """Run ``pre_tool`` (and ``pre_commit`` for git commits) before a gated
        tool. Returns an abort reason if a *blocking* hook failed, else ``None``.

        NOTE: only *gated* tools reach this (mutating + network/MCP); read-only
        tools never interrupt, so ``pre_tool`` does not fire for them. Hooks run
        off the event loop (``to_thread``) so a slow hook doesn't freeze the UI.
        """
        from jarn.extensibility.hooks import HookEvent

        events = [HookEvent.PRE_TOOL]
        if name == "execute" and _looks_like_git_commit(action.target):
            events.append(HookEvent.PRE_COMMIT)
        for event in events:
            results = await asyncio.to_thread(self.hooks.run, event, target=action.target)
            for result in results:
                if result.should_abort:
                    return f"{event.value}: {_hook_detail(result)}"
        return None

    async def _run_post_hooks(self, name: str):
        """Run ``post_tool`` (and ``post_edit`` for writes/edits) after a tool
        completes. Report-only — the tool already ran — so a failing hook is
        surfaced as a NOTICE rather than aborting the turn. ``post_edit`` is
        scoped by the edited *file path* (so a ``matcher: "*.py"`` glob works),
        ``post_tool`` by the tool name."""
        from jarn.extensibility.hooks import HookEvent

        targets = [(HookEvent.POST_TOOL, name)]
        if name in ("write_file", "edit_file"):
            targets.append((HookEvent.POST_EDIT, self._last_edit_target or name))
        for event, target in targets:
            results = await asyncio.to_thread(self.hooks.run, event, target=target)
            for result in results:
                if not result.ok:
                    yield Event(
                        EventKind.NOTICE,
                        text=f"{event.value} hook failed: {_hook_detail(result)}",
                    )

    def _record_usage(self, msg: Any) -> None:
        usage = getattr(msg, "usage_metadata", None)
        if usage:
            # LangChain reports prompt-cache tokens under ``input_token_details``
            # (``cache_read`` / ``cache_creation``); absent for providers/turns
            # without caching, in which case both default to 0.
            details = usage.get("input_token_details") or {}
            self.tracker.record(
                self._resolve_model_ref(msg),
                int(usage.get("input_tokens", 0)),
                int(usage.get("output_tokens", 0)),
                tool=_first_tool_name(msg),
                cache_read_tokens=int(details.get("cache_read", 0)),
                cache_creation_tokens=int(details.get("cache_creation", 0)),
            )

    def _resolve_model_ref(self, msg: Any) -> str:
        """Attribute a streamed chunk to the model that actually produced it.

        The provider stamps the model on the message itself —
        ``response_metadata['model_name']`` (OpenAI-compatible, incl. OpenRouter)
        or ``['model']`` (Anthropic). We canonicalize that raw name to one of the
        refs we know about (``known_model_refs``: main + subagents + summarizer) so
        the per-model bucket uses our pricing-resolvable ref and the main model
        keeps a single stable label. When the message carries no model (e.g. an
        early streaming chunk) we fall back to the main model — preserving today's
        single-model behavior exactly. Reading the reported model is reliable;
        guessing from the subgraph namespace was not (it omits the subagent name).
        """
        meta = getattr(msg, "response_metadata", None)
        name = ""
        if isinstance(meta, dict):
            name = str(meta.get("model_name") or meta.get("model") or "")
        if not name:
            return self.main_model_ref
        # Canonicalize to the most specific known ref. Substring either direction so
        # "claude-opus-4-8" <-> "anthropic/claude-opus-4-8" both resolve.
        best: str | None = None
        for ref in self.known_model_refs:
            if ref and (name in ref or ref in name) and (best is None or len(ref) > len(best)):
                best = ref
        # No known ref matched: record under the provider's raw name (pricing still
        # substring-resolves it) rather than mislabeling it as the main model.
        return best if best is not None else name

    # -- interrupt resolution ----------------------------------------------

    async def _resolve_interrupts(self, interrupts: list[Any]):
        """For each gated tool call, yield (event, decision-dict)."""
        for intr in interrupts:
            for req in _action_requests(intr):
                name = req.get("action") or req.get("name") or "tool"
                args = req.get("args", {}) or {}

                # Plan-mode handoff: exit_plan_mode is callable *in* plan mode, so
                # it bypasses the engine (which would deny it like any non-read
                # action). The approver shows the plan and escalates the mode on
                # approval; an approve resumes the tool (its return is the model's
                # "go" signal), a reject keeps the agent planning.
                if name == "exit_plan_mode":
                    plan_text = str(args.get("plan", "")).strip()
                    reply = await self.approver(
                        ApprovalRequest(
                            action=Action(ActionKind.READ, target="plan", tool=name),
                            result=PermissionResult(Decision.ASK, "plan ready for review"),
                            description=req.get("description", ""),
                            args=args,
                            plan=plan_text,
                        )
                    )
                    if reply.approved:
                        yield (
                            Event(EventKind.APPROVAL, text="plan approved — executing",
                                  data={"target": "plan"}),
                            {"type": "approve"},
                        )
                    else:
                        yield (
                            Event(EventKind.APPROVAL, text="plan not approved — still planning",
                                  data={"target": "plan"}),
                            {"type": "reject",
                             "message": reply.message
                             or "Keep refining the plan; call exit_plan_mode again when ready."},
                        )
                    continue

                # Memory suggestion: suggest_memory proposes a memory for the user
                # to approve. It never mutates the world on its own (the approver
                # does the write on approval), so it bypasses the engine and routes
                # straight to the approver — an approve resumes the tool (its return
                # is the model's confirmation), a reject keeps the agent going
                # without writing anything.
                if name == "suggest_memory":
                    suggestion = SuggestedMemory(
                        name=str(args.get("name", "")).strip(),
                        description=str(args.get("description", "")).strip(),
                        body=str(args.get("body", "")).strip(),
                        type=str(args.get("type", "project")).strip() or "project",
                        scope=str(args.get("scope", "project")).strip() or "project",
                    )
                    reply = await self.approver(
                        ApprovalRequest(
                            action=Action(ActionKind.READ, target="memory", tool=name),
                            result=PermissionResult(Decision.ASK, "memory suggested"),
                            description=req.get("description", ""),
                            args=args,
                            suggested_memory=suggestion,
                        )
                    )
                    if reply.approved:
                        yield (
                            Event(EventKind.APPROVAL, text="memory saved",
                                  data={"target": "memory"}),
                            {"type": "approve"},
                        )
                    else:
                        yield (
                            Event(EventKind.APPROVAL, text="memory not saved",
                                  data={"target": "memory"}),
                            {"type": "reject",
                             "message": reply.message or "User declined to save the memory."},
                        )
                    continue

                action = tool_to_action(name, args)

                # Pre-tool / pre-commit hooks run before the tool: a blocking
                # hook that fails rejects the call (e.g. tests fail → no commit).
                if self.hooks is not None:
                    abort = await self._run_pre_hooks(name, action)
                    if abort is not None:
                        yield (
                            Event(EventKind.APPROVAL, text=f"blocked by hook: {name} ({abort})",
                                  data={"target": action.target}),
                            {"type": "reject", "message": abort},
                        )
                        continue

                result = self.engine.evaluate(action)

                if result.decision is Decision.ALLOW:
                    yield (
                        Event(EventKind.APPROVAL, text=f"auto-allowed: {name}",
                              data={"target": action.target}),
                        {"type": "approve"},
                    )
                elif result.decision is Decision.DENY:
                    yield (
                        Event(EventKind.APPROVAL, text=f"blocked: {name} ({result.reason})",
                              data={"target": action.target, "dangerous": result.dangerous}),
                        {"type": "reject", "message": result.reason},
                    )
                else:  # ASK
                    reply = await self.approver(
                        ApprovalRequest(action=action, result=result,
                                        description=req.get("description", ""),
                                        args=args)
                    )
                    if reply.approved:
                        # A guard-dangerous action can never be remembered as
                        # ALWAYS — downgrade to SESSION so it isn't persisted.
                        scope = reply.scope
                        if result.block_remember_always and scope is RememberScope.ALWAYS:
                            scope = RememberScope.SESSION
                        self.engine.remember(action, scope)
                        if reply.edited_args is not None:
                            # Edit-before-apply: resume with a LangGraph ``edit``
                            # decision so the tool runs with the user-edited args —
                            # the edited content is what lands on disk.
                            yield (
                                Event(EventKind.APPROVAL, text=f"approved (edited): {name}",
                                      data={"target": action.target, "scope": scope.value}),
                                {"type": "edit",
                                 "edited_action": {"name": name, "args": reply.edited_args}},
                            )
                        else:
                            yield (
                                Event(EventKind.APPROVAL, text=f"approved: {name}",
                                      data={"target": action.target, "scope": scope.value}),
                                {"type": "approve"},
                            )
                    else:
                        self.engine.deny_session(action)
                        yield (
                            Event(EventKind.APPROVAL, text=f"rejected: {name}",
                                  data={"target": action.target}),
                            {"type": "reject", "message": reply.message or "rejected by user"},
                        )


# -- helpers ----------------------------------------------------------------

def _unpack_stream_item(item: Any) -> tuple[Any, str | None, Any]:
    """Normalize a LangGraph astream item to ``(namespace, mode, chunk)``.

    With ``subgraphs=True`` items are ``(namespace, mode, chunk)``; without it
    they are ``(mode, chunk)`` (namespace then defaults to ``()``). The namespace
    is a *tuple* path of subgraph node names (e.g. ``("tools:<id>", …)``) — it is
    used to attribute usage to the subagent model that produced the chunk; subagent
    *output* is still shown just like top-level output.
    """
    if isinstance(item, tuple):
        if len(item) == 3:
            return item[0], item[1], item[2]
        if len(item) == 2:
            return (), item[0], item[1]
    return (), None, None


def _reasoning_of(msg: Any) -> str:
    """Extract extended-reasoning text from a chunk, across provider shapes."""
    parts: list[str] = []
    content = getattr(msg, "content", "")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") in _REASONING_TYPES:
                parts.append(
                    block.get("thinking")
                    or block.get("reasoning")
                    or block.get("reasoning_content")
                    or block.get("text")
                    or ""
                )
    extra = getattr(msg, "additional_kwargs", None) or {}
    if isinstance(extra, dict) and extra.get("reasoning_content"):
        parts.append(str(extra["reasoning_content"]))
    return "".join(parts)


def _first_tool_name(msg: Any) -> str | None:
    """The tool a model call requested, for per-tool cost attribution.

    The usage-bearing AI chunk also carries the (accumulated) tool calls it
    decided on — ``tool_calls`` on a complete message, ``tool_call_chunks`` while
    streaming. The first named call labels this call's cost; ``None`` (a plain
    reply) falls back to ``RESPONSE_TOOL`` in the tracker so totals reconcile.
    """
    for attr in ("tool_calls", "tool_call_chunks"):
        for call in getattr(msg, attr, None) or []:
            name = call.get("name") if isinstance(call, dict) else None
            if name:
                return str(name)
    return None


#: Substrings / exception-name fragments that mark a provider error as worth a
#: model fallback (transient or capacity-related, not a logic bug).
_RETRYABLE_NAME_HINTS = ("timeout", "connecterror", "connectionerror", "ratelimit",
                         "overloaded", "serviceunavailable", "apierror", "internalserver")
# Narrow phrases only — a bare "connection"/"timeout" can appear in unrelated
# tool-result strings, which would trigger a needless model rotation.
_RETRYABLE_MSG_HINTS = ("rate limit", "rate-limit", "overloaded",
                        "temporarily unavailable", "service unavailable",
                        "connection reset", "connection refused", "connection aborted",
                        "connection error", "read timed out", "request timed out")


def _is_retryable_error(exc: BaseException) -> bool:
    """Heuristic: is this a transient/capacity provider error worth falling back?

    Providers raise wildly different exception types, so we match on the type
    name and message rather than a fixed exception class. A numeric ``status_code``
    in the 429/5xx family also counts.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and (status == 429 or 500 <= status < 600):
        return True
    name = type(exc).__name__.lower()
    if any(h in name for h in _RETRYABLE_NAME_HINTS):
        return True
    msg = str(exc).lower()
    return any(h in msg for h in _RETRYABLE_MSG_HINTS)


#: Exception-name fragments and message phrases that mark a provider failure as an
#: authentication/authorization rejection — an invalid/expired/missing API key.
#: Rotating to another model won't fix this, so it is handled separately from the
#: retryable heuristic above (see ``_is_retryable_error``).
_AUTH_NAME_HINTS = ("authenticationerror", "permissiondenied", "unauthorized")
_AUTH_MSG_HINTS = ("invalid x-api-key", "invalid api key", "invalid_api_key",
                   "incorrect api key", "authentication_error", "authentication error",
                   "unauthorized", "no auth credentials", "missing api key",
                   "expired api key", "invalid bearer token", "permission denied")


def _is_auth_error(exc: BaseException) -> bool:
    """Heuristic: is this a 401/403 auth rejection (bad/expired/missing key)?

    Matches on a numeric ``status_code`` of 401/403, the exception type name, or
    known message phrases. Kept deliberately narrow so a generic "permission"
    string in an unrelated tool result doesn't get misclassified.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in (401, 403):
        return True
    name = type(exc).__name__.lower()
    if any(h in name for h in _AUTH_NAME_HINTS):
        return True
    msg = str(exc).lower()
    if "401" in msg or "403" in msg:
        return True
    return any(h in msg for h in _AUTH_MSG_HINTS)


def _provider_of_ref(ref: str) -> str:
    """Best-effort provider/profile name from a model ref (e.g. ``anthropic`` from
    ``anthropic/claude-opus-4-8``). Returns ``""`` when it can't be determined so
    the caller can fall back to a generic phrasing."""
    if not ref or ref == "unknown":
        return ""
    return ref.split("/", 1)[0] if "/" in ref else ""


def _looks_like_git_commit(command: str) -> bool:
    """True if ``command`` actually invokes ``git commit`` (not a quoted string
    or another git subcommand). Tolerates global flags like ``-C dir`` / ``-c k=v``."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    i = 0
    while i < len(tokens):
        if tokens[i] == "git" or tokens[i].endswith("/git"):
            j = i + 1
            while j < len(tokens):
                tok = tokens[j]
                if tok in ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"):
                    j += 2  # flag that takes a value
                    continue
                if tok.startswith("-"):
                    j += 1
                    continue
                return tok == "commit"
            return False
        i += 1
    return False


def _hook_detail(result: Any) -> str:
    """Last meaningful line of a hook's output, for a compact abort/notice msg."""
    text = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()
    if text:
        return text.splitlines()[-1]
    return f"exit {getattr(result, 'exit_code', '?')}"


def _web_search_summary(content: str) -> str:
    """Richer one-line summary for web_search results: count + top source hosts."""
    import re as _re
    from urllib.parse import urlparse as _urlparse

    # Count result entries (each starts with "- <Title>") by counting "  https?://" lines.
    urls = _re.findall(r"^\s{2}(https?://[^\s]+)", content, _re.MULTILINE)
    if not urls:
        # Fallback: no URLs found — use generic summary.
        return _tool_summary(content)
    count = len(urls)
    # Build a deduplicated list of hosts in order of first appearance.
    seen: dict[str, None] = {}
    for u in urls:
        # .hostname (not .netloc) so a credential-bearing URL never leaks its
        # user:pass@ userinfo (or :port) into the inline summary.
        host = _urlparse(u).hostname or _urlparse(u).netloc or u
        # Strip www. prefix for compactness.
        if host.startswith("www."):
            host = host[4:]
        seen[host] = None
    hosts = list(seen.keys())
    # Show up to 3 hosts; append "…" when there are more.
    shown = hosts[:3]
    suffix = ", …" if len(hosts) > 3 else ""
    hosts_str = ", ".join(shown) + suffix
    return f"🔍 {count} result{'s' if count != 1 else ''} · {hosts_str}"


def _tool_summary(content: str, tool_name: str = "") -> str:
    """A compact one-line summary of a tool result (never the full payload)."""
    if tool_name == "web_search":
        return _web_search_summary(content)
    txt = content.strip()
    if not txt:
        return "(no output)"
    lines = txt.count("\n") + 1
    if lines > 1:
        return f"{lines} lines"
    return txt if len(txt) <= 80 else txt[:79] + "…"


def _text_of(content: Any) -> str:
    """Flatten LangChain message content (str or list of blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, str):
                out.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                out.append(block.get("text", ""))
        return "".join(out)
    return ""


def _resume_payload(resolved: list[tuple[Any, list[Any]]]) -> Command:
    """Build the LangGraph resume ``Command`` for the pending interrupt(s).

    ``resolved`` is ``[(interrupt_id, decisions), ...]`` — one entry per unique
    pending interrupt, in resolution order.

    A SINGLE pending interrupt resumes with the bundled ``{"decisions": [...]}``
    value its HITL middleware expects. With MULTIPLE pending interrupts (each
    delegated subagent raises its own), LangGraph requires the resume to be a map
    keyed by interrupt id — otherwise it raises *"When there are multiple pending
    interrupts, you must specify the interrupt id when resuming."*
    (``langgraph/pregel/_loop.py``). The id is an xxh3-128 hexdigest, which is how
    LangGraph recognises the value as a per-interrupt resume map.
    """
    keyed = [(iid, decisions) for iid, decisions in resolved if iid is not None]
    if len(resolved) > 1 and len(keyed) == len(resolved):
        return Command(resume={iid: {"decisions": decisions} for iid, decisions in keyed})
    # Single interrupt (or, defensively, ids unavailable): the legacy bundled
    # resume — flatten every decision into one list.
    decisions = [d for _, ds in resolved for d in ds]
    return Command(resume={"decisions": decisions})


def _action_requests(interrupt: Any) -> list[dict[str, Any]]:
    """Extract action-request dicts from a LangGraph interrupt value."""
    value = getattr(interrupt, "value", interrupt)
    if isinstance(value, dict) and "action_requests" in value:
        return list(value["action_requests"])
    if isinstance(value, list):
        items: list[dict[str, Any]] = []
        for v in value:
            if isinstance(v, dict) and "action_requests" in v:
                items.extend(v["action_requests"])
        return items
    return []
