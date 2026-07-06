"""Session controller — the framework-agnostic brain behind the TUI.

Owns the runtime, permission engine, cost tracker, checkpointer, and the current
thread. Builds/rebuilds the deep agent, creates :class:`SessionDriver`s, and
handles built-in slash commands. Kept free of Textual imports so it is unit
testable on its own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarn.agent.builder import JarnRuntime, build_runtime
from jarn.agent.checkpoint import CheckpointManager
from jarn.agent.session import Approver, SessionDriver, SuggestedMemory
from jarn.config.schema import Config, PermissionMode
from jarn.controller import config_helpers, session_helpers
from jarn.cost import CostTracker
from jarn.extensibility.mcp import load_mcp_tools
from jarn.memory import (
    SessionIndex,
    create_async_checkpointer,
    default_db_path,
    new_thread_id,
)
from jarn.permissions import PermissionEngine
from jarn.tui import palette

_log = logging.getLogger("jarn")

def _log_hook_warning(message: str) -> None:
    """Log a lifecycle-hook failure at WARNING (never silent, never fatal)."""
    _log.warning("hooks: %s", message)

@dataclass(slots=True)
class CommandResult:
    text: str
    rebuilt: bool = False
    clear_screen: bool = False
    quit: bool = False


@dataclass(slots=True)
class ShellNote:
    """One captured shell-escape run, stored for context injection into the next turn."""

    cmd: str
    exit_code: int | None
    tail: str


class Controller:
    def __init__(
        self,
        config: Config,
        project_root: Path | None,
        *,
        project_trusted: bool = True,
        system_prompt_override: str | None = None,
        response_format: Any | None = None,
    ) -> None:
        self.config = config
        self.project_root = project_root
        self.project_trusted = project_trusted
        # When set, build_runtime swaps J.A.R.N.'s assembled system prompt for
        # this string (eval-harness A/B of the harness prompt; see build_runtime).
        self.system_prompt_override = system_prompt_override
        # When set, build_runtime passes this as response_format to create_deep_agent
        # so the agent constrains its final answer to the given JSON schema.
        self.response_format = response_format
        self.engine = PermissionEngine(
            mode=config.permission_mode,
            rules=config.permissions,
            project_root=project_root,
        )
        # Persist ALWAYS-scoped approvals to the project config so they survive
        # across processes (no-op outside a project).
        from jarn.config import paths
        from jarn.permissions.rule_store import PermissionRuleStore

        self.rule_store = PermissionRuleStore(paths.project_config_path(project_root))
        self.engine.persist = self.rule_store.add_allow
        self.tracker = CostTracker(budget=config.budget)
        self.thread_id = new_thread_id()
        self._db_path = default_db_path(project_root)
        # Async checkpointer is created lazily inside ensure_runtime (needs the
        # event loop); the TUI drives the agent with astream, which requires it.
        self._saver = None
        self._saver_cm = None
        self.sessions = SessionIndex(self._db_path)
        from jarn.observability import Telemetry

        self.telemetry = Telemetry.from_config(config.observability.telemetry)
        self.health = "unknown"            # unknown | ok | error | degraded
        self.last_error: str | None = None
        # Resolved context-window sizes per model ref (0 = unknown / gave up), so
        # a local-model endpoint (LM Studio / Ollama) is queried at most once.
        self._ctx_window_cache: dict[str, int] = {}
        # Auto-checkpoint manager: snapshots working tree before agent turns so
        # /undo and /redo can revert or re-apply file changes without touching HEAD.
        _cp_root = project_root or Path.cwd()
        self.checkpoint_manager = CheckpointManager(
            repo_root=_cp_root,
            enabled=config.git.autocheckpoint,
        )
        # Set once the degraded/error state has been surfaced to the user, so the
        # TUI shows it a single time per session rather than on every turn.
        self.health_notice_shown = False
        # One-time per-session hint that /undo is unavailable (autocheckpoint off).
        self._autocheckpoint_hint_shown = False
        # Per-server MCP health, populated by ensure_runtime: name -> "ok"/"error".
        self.mcp_health: dict[str, str] = {}
        self.mcp_errors: dict[str, str] = {}
        # Ordered model candidates for turn-level fallback: main + configured chain.
        main = config.resolved_main_model()
        self._candidates = ([main] if main else []) + list(config.routing.fallback)
        self._candidate_idx = 0
        self.runtime: JarnRuntime | None = None
        # The most recently created SessionDriver (one per turn). Retained so a
        # UI-driven checkpoint mutation (/undo, /redo, /abort rollback) can await
        # its in-flight turn-start snapshot before touching the stack — see
        # ``settle_snapshot``. A stale (completed/cancelled) driver settles instantly.
        self._active_driver: SessionDriver | None = None
        # Lifecycle hooks (built lazily from config); session_start fires once
        # the runtime is first ready, session_end on close.
        self._hooks_runner = None
        self._session_started = False
        # Non-fatal lifecycle-hook notice to surface once (e.g. a failed
        # session_start hook, or the global-hooks-trust gate refusing to run).
        self._lifecycle_notice: str | None = None
        # Shell-escape output captured by ! commands; injected once into the next
        # turn via enrich_turn_input and then cleared.
        self.pending_shell_context: list[ShellNote] = []
        # Diagnostics auto-fix chain round (T-3-3): 0 for a real user turn,
        # incremented when an auto-fix round is queued, reset on real user
        # input. Passed to each per-turn SessionDriver so the round cap holds
        # across the driver instances that make up one auto-fix chain.
        self._diag_chain_round: int = 0

    # -- lifecycle ----------------------------------------------------------

    async def ensure_runtime(self) -> JarnRuntime:
        if self._saver is None:
            self._saver, self._saver_cm = await create_async_checkpointer(self._db_path)
        if self.runtime is None:
            from jarn.agent.builder import AmbientKeyLeakError, SandboxUnavailable

            mcp = await load_mcp_tools(self.config.mcp_servers)
            tools = mcp.tools
            self.mcp_health = dict(mcp.health)
            self.mcp_errors = dict(mcp.errors)
            # Mirror per-server health onto the config entries so status/UI can
            # read it without re-loading; degrade the session if any failed.
            for server in self.config.mcp_servers:
                if server.name in self.mcp_health:
                    server.health = self.mcp_health[server.name]
            if mcp.degraded:
                self.health = "degraded"
                failed = ", ".join(sorted(mcp.errors))
                first = next(iter(sorted(mcp.errors)))
                self.last_error = f"MCP server(s) failed: {failed} ({mcp.errors[first]})"
            try:
                self.runtime = build_runtime(
                    self.config,
                    project_root=self.project_root,
                    project_trusted=self.project_trusted,
                    checkpointer=self._saver,
                    extra_tools=tools,
                    system_prompt_override=self.system_prompt_override,
                    response_format=self.response_format,
                )
            except AmbientKeyLeakError as exc:
                self.health = "error"
                self.last_error = exc.messages[0]
                raise
            except SandboxUnavailable as exc:
                # Fail closed: a requested sandbox that can't start must NOT
                # silently downgrade to running on the host — that quietly
                # removes the isolation the user asked for. Only fall back when
                # explicitly opted in (execution.allow_local_fallback).
                if not self.config.execution.allow_local_fallback:
                    self.health = "error"
                    self.last_error = (
                        f"sandbox unavailable: {exc}. Refusing to run on the host. "
                        "Use /sandbox off to run locally on purpose, or set "
                        "execution.allow_local_fallback: true."
                    )
                    raise
                self.last_error = f"sandbox unavailable, running on host (opted in): {exc}"
                self.health = "degraded"
                self.config.execution.backend = "local"
                self.runtime = build_runtime(
                    self.config,
                    project_root=self.project_root,
                    project_trusted=self.project_trusted,
                    checkpointer=self._saver,
                    extra_tools=tools,
                    system_prompt_override=self.system_prompt_override,
                    response_format=self.response_format,
                )
        if not self._session_started:
            self._fire_lifecycle("session_start")
            self._session_started = True
        return self.runtime

    def _hook_runner(self):
        """Lazily build the lifecycle :class:`HookRunner` (None if no hooks).

        Honours two hardening flags from config:
        ``hook_global_require_trust`` skips hook execution entirely (and records
        a notice) until the user has run ``jarn trust-hooks`` once — a one-time
        accept for the otherwise-ungated global hooks tier. ``hook_inherit_env``
        forwards the full env to hook subprocesses (default: minimal allowlist).
        """
        if self._hooks_runner is None and self.config.hooks:
            if self.config.hook_global_require_trust:
                from jarn.config.trust import global_hooks_trusted

                if not global_hooks_trusted():
                    self._lifecycle_notice = (
                        "lifecycle hooks disabled: `hook_global_require_trust` is on "
                        "and global hooks haven't been accepted — run `jarn trust-hooks`."
                    )
                    _log_hook_warning(self._lifecycle_notice)
                    return None
            from jarn.extensibility.hooks import HookRunner

            self._hooks_runner = HookRunner(
                hooks=self.config.hooks,
                cwd=self.project_root or Path.cwd(),
                inherit_env=self.config.hook_inherit_env,
            )
        return self._hooks_runner

    def _fire_lifecycle(self, event_name: str) -> None:
        runner = self._hook_runner()
        if runner is None:
            return
        from jarn.extensibility.hooks import HookEvent

        try:
            results = runner.run(HookEvent(event_name))
        except Exception as exc:  # noqa: BLE001 — non-fatal, must not kill the turn
            msg = f"lifecycle hook {event_name} errored: {exc}"
            _log_hook_warning(msg)
            if self.last_error is None:
                self.last_error = msg
            return
        for r in results:
            if not r.ok:
                msg = (
                    f"{event_name} hook {r.spec.name or r.spec.command!r} "
                    f"failed (exit {r.exit_code})"
                )
                _log_hook_warning(msg)
                if self.last_error is None:
                    self.last_error = msg

    def _config(self) -> dict:
        return config_helpers._config(self)

    async def todos(self) -> list[dict]:
        """Current plan checklist from graph state (empty if none / no runtime)."""
        if self.runtime is None:
            return []
        state = await self.runtime.agent.aget_state(self._config())
        return list((getattr(state, "values", {}) or {}).get("todos", []) or [])

    async def history(self) -> list:
        """Messages in the current thread's checkpoint (for transcript replay)."""
        rt = await self.ensure_runtime()
        state = await rt.agent.aget_state(self._config())
        return list((getattr(state, "values", {}) or {}).get("messages", []) or [])

    async def compact_preview(self) -> str:
        """Generate the compaction summary for the current thread *without*
        applying it. Records the summarizer call's cost. Returns the summary
        text (``""`` when there's nothing to compact). The manual ``/compact``
        command renders this and asks the user before calling
        :meth:`compact_apply`."""
        rt = await self.ensure_runtime()
        state = await rt.agent.aget_state(self._config())
        messages = (getattr(state, "values", {}) or {}).get("messages", []) if state else []
        if not messages:
            return ""

        transcript = _render_transcript(messages)
        summarizer = rt.factory.build_summarizer() or rt.factory.build_main()
        # Resolve the model ref the summarizer actually runs on so its usage is
        # attributed correctly; fall back to the main model (which is what
        # build_summarizer() falls back to when no summarizer is configured).
        summarizer_ref = (
            self.config.resolved_summarizer_model()
            or rt.main_model_ref
            or self.config.resolved_main_model()
            or "unknown"
        )
        prompt = (
            "Summarize the following coding-assistant conversation so work can "
            "continue in a fresh context. Capture: the goal, decisions made, files "
            "changed, current state, and the next step. Be concise but complete.\n\n"
            + transcript
        )
        resp = await summarizer.ainvoke(prompt)
        summary = _content_text(resp)
        # Record the summarizer call's cost (it is a real model call). Guard
        # against providers that don't return usage metadata.
        usage = getattr(resp, "usage_metadata", None)
        if usage:
            self.tracker.record(
                summarizer_ref,
                int(usage.get("input_tokens", 0)),
                int(usage.get("output_tokens", 0)),
                tool="(compact)",
            )
        return summary

    async def compact_apply(self, summary: str) -> None:
        """Replace the conversation thread with ``summary``: start a fresh
        thread and seed it with the summary. The destructive half of compaction
        — call only after the user confirms (manual) or unconditionally (auto)."""
        rt = await self.ensure_runtime()
        self.new_thread()
        from langchain_core.messages import HumanMessage

        await rt.agent.aupdate_state(
            self._config(),
            {"messages": [HumanMessage(content=f"[Summary of prior conversation]\n{summary}")]},
        )

    async def compact(self) -> str:
        """One-shot summarize-and-fork primitive: generate a summary via
        :meth:`compact_preview` then immediately apply it via
        :meth:`compact_apply`, returning the summary text.

        The interactive ``/compact`` command in the REPL uses the split
        :meth:`compact_preview` + :meth:`compact_apply` pair so the user can
        review the summary before it is applied.  This method is the single-call
        variant exposed through the controller command registry for callers that
        do not need the preview step."""
        summary = await self.compact_preview()
        if not summary:
            return ""
        await self.compact_apply(summary)
        return summary

    async def human_turns(self) -> list[tuple[int, str]]:
        """Enumerate the user turns in the current thread for the ``/rewind``
        picker. Returns ``(message_index, preview)`` for each ``HumanMessage``,
        in conversation order. ``message_index`` is the offset into
        ``state.values["messages"]`` — the cut point that keeps everything
        *before* that turn. Previews are single-line and truncated.

        Enumerated straight from the messages list (not the checkpointer step
        history) so a turn is exactly one ``HumanMessage`` boundary — simple,
        deterministic, and decoupled from checkpointer internals."""
        rt = await self.ensure_runtime()
        state = await rt.agent.aget_state(self._config())
        messages = (getattr(state, "values", {}) or {}).get("messages", []) or []
        turns: list[tuple[int, str]] = []
        for idx, msg in enumerate(messages):
            if getattr(msg, "type", "") != "human":
                continue
            content = getattr(msg, "content", "")
            if isinstance(content, list):
                content = "".join(
                    b.get("text", "") if isinstance(b, dict) else str(b)
                    for b in content
                )
            preview = " ".join(str(content).split())
            if len(preview) > 80:
                preview = preview[:77] + "…"
            turns.append((idx, preview))
        return turns

    async def fork_to_turn(
        self, keep_count: int, *, restore_files: bool = False
    ) -> int | None:
        """Branch the conversation: keep ``messages[:keep_count]`` on a *new*
        thread and continue from there. The original thread is untouched and
        still resumable (this forks, it does not destroy).

        ``keep_count`` is the cut index from :meth:`human_turns` — it must land
        on a ``HumanMessage`` boundary so the re-run begins a clean turn. The
        kept prefix is everything strictly before the chosen turn.

        Mirrors :meth:`compact_apply`: ``new_thread()`` mints a fresh thread,
        then ``aupdate_state`` seeds it. We prepend ``RemoveMessage(
        REMOVE_ALL_MESSAGES)`` so the messages reducer starts from empty even if
        the fresh thread somehow carried state — the same reducer mechanism
        compaction relies on. Resets the context-token gauge like
        :meth:`new_thread`.

        When ``restore_files`` is True (slice 2), the working tree is reverted to
        the chosen turn's checkpoint BEFORE the thread is forked, so conversation
        and files rewind atomically. The checkpoint is resolved on the ORIGINAL
        thread (snapshots were recorded against it) by the human-turn count in the
        kept prefix — the same 0-based turn index the session driver records at
        snapshot time. Any in-flight snapshot is settled first (the /abort race
        guard) so the restore targets a consistent stack. A missing checkpoint
        (autocheckpoint off, turn never snapshotted) degrades to conversation-only
        — the fork still proceeds, the tree is left untouched. When
        ``restore_files`` is False the behavior is byte-identical to slice 1.

        Returns the cut index actually used, or ``None`` when there is nothing
        to rewind (empty thread or a negative ``keep_count``). ``keep_count == 0``
        is a *valid* rewind to before the very first turn: it seeds an empty
        branch (``RemoveMessage`` only) so the conversation restarts from
        scratch — not a no-op (that's the 2-turn / first-turn rewind case)."""
        rt = await self.ensure_runtime()
        state = await rt.agent.aget_state(self._config())
        messages = (getattr(state, "values", {}) or {}).get("messages", []) or []
        if not messages or keep_count < 0:
            return None

        import asyncio

        kept_prefix = list(messages[:keep_count])  # empty when keep_count == 0
        original_thread = self.thread_id

        # Slice 2: revert the working tree to the chosen turn's checkpoint before
        # forking. Resolve against the ORIGINAL thread; the turn index is the
        # human-turn count in the kept prefix (matches the driver's recording).
        if restore_files:
            turn_index = sum(
                1 for m in kept_prefix if getattr(m, "type", "") == "human"
            )
            ref = await asyncio.to_thread(
                self.checkpoint_manager.find_for_turn, self.thread_id, turn_index
            )
            if ref is not None:
                # Settle any in-flight snapshot first so restore_to's undo-stack
                # push targets a consistent stack (mirrors /abort, /undo, /redo).
                await self.settle_snapshot()
                await asyncio.to_thread(self.checkpoint_manager.restore_to, ref.sha)

        from langchain_core.messages import RemoveMessage
        from langgraph.graph.message import REMOVE_ALL_MESSAGES

        # Compute kept_turns before minting the new thread_id.
        _kept_turns = sum(
            1 for m in kept_prefix if getattr(m, "type", "") == "human"
        )
        self.new_thread()  # mint a fresh thread_id; resets tracker.context_tokens
        # Register alias so find_for_turn can walk back to the parent on a stacked
        # rewind within the same session (in-memory, not persisted).
        self.checkpoint_manager.register_thread_alias(
            self.thread_id, original_thread, _kept_turns
        )
        await rt.agent.aupdate_state(
            self._config(),
            {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *kept_prefix]},
        )
        return keep_count

    # TODO(rewind-conversation) slice 3: in-place/destructive rewind on the same
    # thread + true free message-editing.
    # TODO(rewind-conversation) slice 4: visual branch tree across forked threads
    # surfaced in /sessions.

    def terminate_shells(self) -> int:
        """Kill any shell command still running on the host (on turn cancel).

        The asyncio task cancel alone leaves a spawned process tree alive; this
        reaches the execution backend and kills it. Returns the number killed."""
        backend = getattr(self.runtime, "backend", None)
        terminate = getattr(backend, "terminate_all", None)
        if terminate is None:
            return 0
        try:
            return terminate()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            return 0

    def make_driver(self, approver: Approver) -> SessionDriver:
        assert self.runtime is not None, "call ensure_runtime() first"
        transcript = None
        if self.config.observability.transcript:
            from jarn.memory.sessions import make_transcript_writer

            transcript = make_transcript_writer(
                self.thread_id, project_root=self.project_root
            )
        driver = SessionDriver(
            agent=self.runtime.agent,
            engine=self.engine,
            tracker=self.tracker,
            thread_id=self.thread_id,
            main_model_ref=self.runtime.main_model_ref or "unknown",
            known_model_refs=self.runtime.known_model_refs,
            approver=approver,
            hooks=self._hook_runner(),
            transcript=transcript,
            checkpoint=self.checkpoint_manager,
            verify_gate=self.config.verify.gate,
            project_root=self.project_root,
            verify_executor=getattr(
                getattr(self.runtime, "backend", None), "execute", None
            ),
            diagnostics_mode=self.config.verify.diagnostics,
            diagnostics_max_rounds=self.config.verify.diagnostics_max_rounds,
            diagnostics_ts=self.config.verify.diagnostics_ts,
            _diag_round=self._diag_chain_round,
        )
        # Retain for settle_snapshot: the /undo, /redo, and /abort paths await this
        # driver's pending turn-start snapshot before mutating the checkpoint stack.
        self._active_driver = driver
        return driver

    async def settle_snapshot(self) -> None:
        """Await any in-flight checkpoint snapshot before a UI-driven ``/undo``,
        ``/redo``, or ``/abort`` rollback mutates the checkpoint stack, so the
        mutation never races a snapshot that is still building its tree and reverts
        an extra turn back (over-revert). Delegates to the active
        :class:`SessionDriver` (see :meth:`SessionDriver.settle_snapshot`); a no-op
        when no turn has run this session. Never raises."""
        driver = self._active_driver
        if driver is not None:
            await driver.settle_snapshot()

    def close(self) -> None:
        import contextlib

        with contextlib.suppress(Exception):
            self.telemetry.flush()
        # Tear down the execution backend deterministically (e.g. remove the
        # Docker sandbox container) instead of relying on GC / __del__.
        backend = getattr(self.runtime, "backend", None)
        backend_close = getattr(backend, "close", None)
        if callable(backend_close):
            with contextlib.suppress(Exception):
                backend_close()

    async def aclose(self) -> None:
        """Async cleanup: fire session_end, flush telemetry, close checkpointer."""
        import contextlib

        if self._session_started:
            self._fire_lifecycle("session_end")
            self._session_started = False
        self.close()
        if self._saver_cm is not None:
            with contextlib.suppress(Exception):
                await self._saver_cm.__aexit__(None, None, None)
            self._saver_cm = None
            self._saver = None

    # -- thread management --------------------------------------------------

    def new_thread(self) -> None:
        self.thread_id = new_thread_id()
        self.tracker.context_tokens = 0  # reset the context gauge on /clear /compact

    def resume_thread(self, thread_id: str) -> None:
        self.thread_id = thread_id

    def rotate_to_fallback(self) -> str | None:
        """Switch to the next model in the fallback chain and force a rebuild.

        Returns the new model ref, or ``None`` if the chain is exhausted. Used by
        the TUI to transparently recover from a model/provider failure at the
        start of a turn.
        """
        if self._candidate_idx + 1 >= len(self._candidates):
            return None
        self._candidate_idx += 1
        new_ref = self._candidates[self._candidate_idx]
        self.config.routing.main = new_ref
        self.config.default_model = new_ref
        self.runtime = None  # rebuild with the new model
        return new_ref

    def reset_model_rotation(self) -> None:
        """Return to the primary model (called when a turn succeeds)."""
        if self._candidate_idx != 0 and self._candidates:
            self._candidate_idx = 0
            self.config.routing.main = self._candidates[0]
            self.config.default_model = self._candidates[0]
            self.runtime = None

    def _ref_has_resolvable_key(self, ref: str) -> bool:
        """True if ``ref``'s provider has a usable (resolvable, non-empty) key.

        Used to decide whether rotating to a fallback on an *auth* failure is
        worthwhile: a fallback whose own key is missing/unresolvable would just
        401 again, so it isn't a viable target. Local providers (Ollama / LM
        Studio) need no key and always count as viable. Resolution failures
        (``${ENV}`` not set, no keychain entry) fail closed → not viable."""
        from jarn.config.schema import ProviderType
        from jarn.config.secrets import SecretResolutionError, resolve
        from jarn.providers import parse_model_ref

        try:
            parsed = parse_model_ref(ref, default_profile=self.config.default_profile)
        except Exception:  # noqa: BLE001 - malformed ref → not viable
            return False
        provider = self.config.providers.get(parsed.profile)
        if provider is None:
            return False
        if provider.type in (ProviderType.OLLAMA, ProviderType.LMSTUDIO):
            return True  # local endpoints need no key
        try:
            return bool(resolve(provider.api_key))
        except SecretResolutionError:
            return False

    def rotate_to_keyed_fallback(self) -> str | None:
        """Rotate to the next fallback on a *different* provider that has a key.

        Auth (401) failures are not retryable on the same provider — a rejected
        key won't fix itself by reusing it. But a configured ``routing.fallback``
        on a *different* provider with a resolvable key is exactly the case where
        switching providers helps. This advances the rotation cursor past any
        same-provider or keyless candidates to the first viable one, mutating the
        same routing state as :meth:`rotate_to_fallback` (so a later success
        ``reset_model_rotation`` still returns to the primary). Returns the new
        ref, or ``None`` when no viable fallback remains."""
        from jarn.providers import parse_model_ref

        def _profile(ref: str) -> str:
            try:
                return parse_model_ref(
                    ref, default_profile=self.config.default_profile
                ).profile
            except Exception:  # noqa: BLE001
                return ref

        current = self._candidates[self._candidate_idx] if self._candidates else ""
        current_profile = _profile(current)
        idx = self._candidate_idx + 1
        while idx < len(self._candidates):
            cand = self._candidates[idx]
            if _profile(cand) != current_profile and self._ref_has_resolvable_key(cand):
                self._candidate_idx = idx
                self.config.routing.main = cand
                self.config.default_model = cand
                self.runtime = None  # rebuild with the new model
                return cand
            idx += 1
        return None

    def record_session_title(self, title: str, *, when: float) -> None:
        self.sessions.touch(self.thread_id, title, when=when)

    def record_turn(self, *, when: float) -> None:
        """Record one completed turn to telemetry (no-op when disabled).

        Numeric-only props — nothing about prompts, files, or model output is
        sent (the Telemetry sink also drops non-numeric props defensively)."""
        t = self.tracker
        self.telemetry.record(
            "turn",
            when=when,
            context_tokens=t.context_tokens,
            total_tokens=t.total.total_tokens,
            cost_cents=round(t.total.cost_usd * 100, 4),
            calls=t.total.calls,
        )

    def enrich_turn_input(self, user_input: str) -> str:
        """Inject per-turn memory recall and shell-escape context ahead of the user's prompt.

        Shell context is cleared on the FIRST call regardless of turn outcome so
        there is no double-append if the turn is retried."""
        from jarn.memory import recall_block

        block = recall_block(
            user_input,
            k=3,
            project_root=self.project_root,
            include_project=self.project_trusted,
        )

        # Consume pending shell context once, regardless of whether the recall
        # block is present (clear first so a turn error never double-appends).
        shell_notes = self.pending_shell_context[:]
        self.pending_shell_context.clear()

        parts: list[str] = []
        if block:
            parts.append(block)
            parts.append("---")

        if shell_notes and self.config.execution.shell_escape_context:
            lines: list[str] = []
            for note in shell_notes:
                exit_str = str(note.exit_code) if note.exit_code is not None else "?"
                lines.append(f"$ {note.cmd}  (exit {exit_str})")
                if note.tail:
                    lines.append(note.tail)
            shell_block = (
                "<shell-escape context (ran by the user, not the agent)>\n"
                + "\n".join(lines)
                + "\n</shell-escape>"
            )
            parts.append(shell_block)
            parts.append("---")

        if not parts:
            return user_input

        return "\n\n".join(parts) + "\n\n" + user_input

    # -- selection choices (for arrow-key modals) ---------------------------

    def model_choices(self) -> list[tuple[str, str]]:
        """Return (model_ref, provider_hint) candidates for the /model picker."""
        from jarn.config.defaults import DEFAULT_MODELS

        seen: dict[str, str] = {}

        def add(ref: str | None, hint: str) -> None:
            if ref and ref not in seen:
                seen[ref] = hint

        add(self.config.resolved_main_model(), "current")
        for ref in self.config.routing.fallback:
            add(ref, "fallback")
        # Offer the default main/subagent models for every configured provider.
        for name, prov in self.config.providers.items():
            models = DEFAULT_MODELS.get(name) or DEFAULT_MODELS.get(prov.type.value)
            if models:
                add(models["main"], name)
                add(models["subagent"], name)
        return list(seen.items())

    def discover_models(self) -> list[tuple[str, str]]:
        """Probe configured local endpoints for their served models.

        Returns ``(qualified_ref, profile)`` for every model reported by a local
        provider (Ollama / LM Studio / openai_compatible). Fails open: providers
        that are unreachable contribute nothing, so the list is simply empty when
        no endpoint answers — the caller then falls back to manual entry.
        """
        from jarn.config.schema import ProviderType
        from jarn.providers import list_remote_models, qualify_model_ref

        local = {ProviderType.OLLAMA, ProviderType.LMSTUDIO, ProviderType.OPENAI_COMPATIBLE}
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for name, prov in self.config.providers.items():
            if prov.type not in local:
                continue
            for model_id in list_remote_models(prov):
                ref = qualify_model_ref(model_id, name)
                if ref not in seen:
                    seen.add(ref)
                    out.append((ref, name))
        return out

    def mode_choices(self) -> list[tuple[str, str]]:
        hints = {
            "plan": "read-only",
            "ask": "confirm writes/shell (default)",
            "auto-edit": "auto edits + web fetch/search, confirm shell",
            "yolo": "no prompts (danger-guard still applies)",
        }
        return [
            (m.value, f"{palette.MODE_GLYPH.get(m.value, '◆')} {hints[m.value]}")
            for m in PermissionMode
        ]

    def apply_model(self, ref: str) -> None:
        self.config.routing.main = ref
        self.config.default_model = ref
        self._candidates = [ref] + list(self.config.routing.fallback)
        self._candidate_idx = 0
        self.runtime = None

    def apply_mode(self, value: str) -> str:
        """Apply a permission mode, clamped to the untrusted floor.

        This is the single choke point for every mode change (``/mode``,
        Shift+Tab cycle, the mode picker), so the untrusted-project floor cannot
        be bypassed through any of them: an untrusted project can never be
        loosened past the review-only floor (``plan``). Returns the mode actually
        applied (which may be clamped below ``value``)."""
        target = PermissionMode(value)
        if not self.project_trusted and target.rank > PermissionMode.PLAN.rank:
            target = PermissionMode.PLAN
        self.config.permission_mode = target
        self.engine.mode = target
        self.runtime = None
        return target.value

    def peek_next_mode(self) -> str:
        """Return the mode that cycle_mode() *would* advance to, without applying it."""
        order = list(PermissionMode)
        idx = order.index(self.config.permission_mode)
        return order[(idx + 1) % len(order)].value

    def cycle_mode(self) -> str:
        """Advance to the next permission mode (plan→ask→auto-edit→yolo→plan).

        Returns the mode actually applied — on an untrusted project this stays
        ``plan`` (apply_mode clamps anything more permissive to the floor)."""
        order = list(PermissionMode)
        idx = order.index(self.config.permission_mode)
        nxt = order[(idx + 1) % len(order)]
        return self.apply_mode(nxt.value)

    # -- status -------------------------------------------------------------

    def validate(self) -> tuple[bool, str]:
        """Check the default provider key resolves and the main model builds.

        Cheap and offline (no network): catches missing keys / bad model refs.
        Sets ``self.health`` and returns ``(ok, message)``.
        """
        from jarn.providers import ModelFactory, ModelResolutionError

        try:
            ModelFactory(self.config).build_main()
            self.health = "ok"
            return True, "ready"
        except ModelResolutionError as exc:
            self.health = "error"
            self.last_error = str(exc)
            return False, str(exc)

    def _main_context_window(self) -> int:
        """The main model's context window in tokens (0 = unknown).

        Curated table / user override first; for a local model whose window isn't
        known (LM Studio / Ollama), query its endpoint once and cache the result
        so the toolbar can show a real context % instead of hiding the gauge."""
        ref = self.config.resolved_main_model() or ""
        from jarn.cost.pricing import context_window

        window = context_window(ref)
        if window > 0:
            return window
        if ref in self._ctx_window_cache:
            return self._ctx_window_cache[ref]
        window = 0
        from jarn.providers import parse_model_ref, remote_context_window

        parsed = parse_model_ref(ref, default_profile=self.config.default_profile)
        provider = self.config.providers.get(parsed.profile)
        if provider is not None:
            window = remote_context_window(provider, parsed.model_id) or 0
        self._ctx_window_cache[ref] = window
        return window

    def context_status(self) -> tuple[int, int, float] | None:
        """(tokens, window, fraction) of the main model's context, or None when
        unknown — no tokens recorded yet, or the model's window can't be resolved
        (so the toolbar hides the gauge rather than dividing by a guess)."""
        tokens = self.tracker.context_tokens
        if tokens <= 0:
            return None
        window = self._main_context_window()
        if window <= 0:
            return None
        return tokens, window, tokens / window

    def isolation_level(self) -> str:
        """Effective execution isolation level, for status display + doctor.

        Returns one of:
        - ``"docker"``         — commands run inside a Docker container.
        - ``"os-sandbox"``     — host shell wrapped by sandbox-exec/bwrap.
        - ``"remote-sandbox"`` — remote (LangSmith) sandbox runtime.
        - ``"host"``           — running directly on the host, NO isolation
          (the permission engine + danger-guard are the only authorizers).

        Reads the live backend when a runtime exists, otherwise infers from
        config so the bar/doctor are honest even before the first turn.
        """
        backend = getattr(self.runtime, "backend", None) if self.runtime else None
        cls = type(backend).__name__ if backend is not None else ""
        if cls == "CancellableDockerSandbox":
            return "docker"
        if cls == "CancellableLangSmithSandbox":
            return "remote-sandbox"
        if cls == "CancellableLocalShellBackend":
            if getattr(backend, "_sandbox_mode", "off") in ("auto", "require"):
                from jarn.agent import os_sandbox

                if os_sandbox.available():
                    return "os-sandbox"
            return "host"
        # No runtime yet — infer from config.
        ex = self.config.execution
        if ex.backend == "docker" or (
            ex.backend == "sandbox" and ex.sandbox_provider == "docker"
        ):
            return "docker"
        if ex.backend == "sandbox":
            return "remote-sandbox"
        if ex.local_sandbox in ("auto", "require"):
            from jarn.agent import os_sandbox

            if os_sandbox.available():
                return "os-sandbox"
        return "host"

    @property
    def status_line(self) -> str:
        model = (self.runtime.main_model_ref if self.runtime else None) or self.config.resolved_main_model()
        glyph = {
            "ok": f"[{palette.C_SUCCESS}]●[/{palette.C_SUCCESS}] ",
            "error": (
                f"[{palette.C_ERROR}]✗ key[/{palette.C_ERROR}]"
                f" [{palette.C_DIM}]·[/{palette.C_DIM}]"
                f" [{palette.C_ERROR}]/doctor[/{palette.C_ERROR}] "
            ),
            "degraded": f"[{palette.C_WARN}]⚠[/{palette.C_WARN}] ",
            "unknown": "",
        }.get(self.health, "")
        sep = f" [{palette.C_DIM}]·[/{palette.C_DIM}] "
        mode = palette.mode_label(self.config.permission_mode.value)
        # Make the isolation level visible: positively flag a real sandbox, and
        # never let the host (no-isolation) state hide — so nobody assumes they
        # are safe when the permission engine is the only thing standing guard.
        level = self.isolation_level()
        if level == "docker":
            backend = f"{sep}[{palette.C_SUCCESS}]docker[/{palette.C_SUCCESS}]"
        elif level == "os-sandbox":
            backend = f"{sep}[{palette.C_SUCCESS}]os-sandbox[/{palette.C_SUCCESS}]"
        elif level == "remote-sandbox":
            backend = f"{sep}[{palette.C_SUCCESS}]sandbox[/{palette.C_SUCCESS}]"
        elif self.health == "degraded":
            backend = f"{sep}[{palette.C_WARN}]host (no sandbox)[/{palette.C_WARN}]"
        else:
            backend = f"{sep}[{palette.C_DIM}]host[/{palette.C_DIM}]"
        return (
            f"{glyph}{model or 'unconfigured'}{sep}{mode}{backend}{sep}{self.tracker.summary_line()}"
        )

    # -- built-in commands --------------------------------------------------

    def handle_command(self, name: str, args: str) -> CommandResult:
        from jarn.controller.commands import REGISTRY

        handler = REGISTRY.get(name.replace("-", "_"))
        if handler is None:
            return CommandResult(f"Unknown command: /{name}. Try /help.")
        return handler(self, args)

    def current_provider(self) -> str | None:
        """Profile name of the provider serving the active main model.

        A model ref is ``<profile>/<model-id>``; the profile keys
        ``config.providers``. Returns ``None`` when no model is configured."""
        from jarn.providers.models import parse_model_ref

        ref = self.config.resolved_main_model()
        if not ref:
            return None
        return parse_model_ref(ref, default_profile=self.config.default_profile).profile

    def set_provider_key(self, raw_key: str, *, provider: str | None = None) -> CommandResult:
        return config_helpers.set_provider_key(self, raw_key, provider=provider)

    def _apply_reloaded_config(self) -> None:
        config_helpers._apply_reloaded_config(self)

    def _invalidate_model_cache(self) -> None:
        config_helpers._invalidate_model_cache(self)

    def set_setting(self, key: str, raw: str) -> tuple[bool, str]:
        return config_helpers.set_setting(self, key, raw)

    def _config_set(self, key: str, raw: str) -> CommandResult:
        return config_helpers._config_set(self, key, raw)

    def save_suggested_memory(self, suggestion: SuggestedMemory) -> tuple[bool, str]:
        return session_helpers.save_suggested_memory(self, suggestion)

    def abort_rollback(self) -> str:
        return session_helpers.abort_rollback(self)

    def can_rollback_turn(self) -> bool:
        return session_helpers.can_rollback_turn(self)

    def cancel_edit_note(self) -> str | None:
        return session_helpers.cancel_edit_note(self)

    def autocheckpoint_off_hint(self) -> str | None:
        return session_helpers.autocheckpoint_off_hint(self)

def _render_transcript(messages: list) -> str:
    """Render LangChain messages to a compact text transcript for summarization."""
    lines = []
    for msg in messages:
        role = getattr(msg, "type", None) or getattr(msg, "role", "?")
        content = _content_text(msg)
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)

def _content_text(msg) -> str:
    content = getattr(msg, "content", msg)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content
        )
    return str(content)
