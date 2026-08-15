"""Session controller — the framework-agnostic brain behind the TUI.

Owns the runtime, permission engine, cost tracker, checkpointer, and the current
thread. Builds/rebuilds the deep agent, creates :class:`SessionDriver`s, and
handles built-in slash commands. Kept free of Textual imports so it is unit
testable on its own.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarn.agent.builder import JarnRuntime, build_runtime
from jarn.agent.checkpoint import CheckpointManager, RestorePreview
from jarn.agent.prompt_modules import (
    PromptAssembly,
    PromptModuleContext,
    PromptModuleRegistry,
    PromptModuleScope,
    PromptModuleStatus,
    RenderedPromptModule,
    create_prompt_module_registry,
    render_prompt_module,
    with_context_budgets,
)
from jarn.agent.prompt_modules import (
    prompt_module_diagnostics as build_prompt_module_diagnostics,
)
from jarn.agent.session import Approver, SessionDriver, SuggestedMemory, SuggestedSkill
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
from jarn.tui import grammar, layout, palette

if TYPE_CHECKING:
    from jarn.catalog import ModelCatalogEntry, ModelCatalogSnapshot
    from jarn.extensibility.mcp import MCPLoadResult

#: Async confirm callback for yolo escalation (Telegram card, REPL `_ask`, …).
YoloConfirm = Callable[[], Awaitable[bool]]
#: Async confirmation callback for a concrete, read-only undo preview.
UndoConfirm = Callable[[RestorePreview], Awaitable[bool]]

_log = logging.getLogger("jarn")


class TurnBusyError(RuntimeError):
    """A second turn tried to start while one is already in flight on this controller.

    **Policy: refuse (do not queue).** Concurrent turns on one ``thread_id`` must
    never silently interleave — ``make_driver`` would overwrite
    ``_active_driver`` and break ``settle_snapshot`` / undo. Gateway sessions
    already queue-at-chat in :mod:`jarn.gateway.sessions`; the controller /
    shared turn-runner boundary still hard-refuses so REPL, headless, and the
    gateway worker all inherit the same fail-loud guard.
    """


def _log_hook_warning(message: str) -> None:
    """Log a lifecycle-hook failure at WARNING (never silent, never fatal)."""
    _log.warning("hooks: %s", message)


@dataclass(slots=True)
class CommandResult:
    text: str
    rebuilt: bool = False
    clear_screen: bool = False
    quit: bool = False
    # When True, `text` is instructions the model should act on (e.g. /skill):
    # the REPL seeds an agent turn with it instead of just printing it.
    seed_turn: bool = False
    # Optional model-facing seed distinct from the user-visible command result.
    # /skill uses this to preserve its inspectable full-body result while the
    # bounded registry body is attached once as a system module.
    seed_input: str | None = None


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
        extra_roots: list[Path] | None = None,
    ) -> None:
        self.config = config
        self.project_root = project_root
        self.project_trusted = project_trusted
        # Added (secondary) roots from --add-dir / /add-dir. These widen the WRITE
        # scope only — the engine, the backend FS guard, and the sandboxes all
        # share this set (kept in sync in build_runtime). Context loading and
        # checkpoint/undo stay PRIMARY-ONLY (project_root). Resolved once so the
        # symlink-escape discipline and the scope check agree byte-for-byte.
        self.extra_roots: list[Path] = self._resolve_extra_roots(extra_roots)
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
            roots=tuple(self.extra_roots),
        )
        # Persist ALWAYS-scoped approvals to the project config so they survive
        # across processes (no-op outside a project).
        #
        # Resolve the *project* root rather than trusting project_root, which is
        # the scoping root and falls back to the cwd. Handing that straight to
        # the store broke the no-op above in both directions: outside a project
        # the first "always" grant created <cwd>/.jarn/, silently promoting an
        # ordinary folder to a project root for every later run; and a headless
        # run started in a subdirectory wrote its grants to <subdir>/.jarn/
        # instead of the project's own config.
        from jarn.config import paths
        from jarn.permissions.rule_store import PermissionRuleStore

        discovered = paths.find_project_root(project_root) if project_root is not None else None
        self.rule_store = PermissionRuleStore(
            paths.project_config_path(discovered) if discovered is not None else None
        )
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
        self.health = "unknown"  # unknown | ok | error | degraded
        self.last_error: str | None = None
        # Resolved context-window sizes per model ref (0 = unknown / gave up), so
        # a local-model endpoint (LM Studio / Ollama) is queried at most once.
        self._ctx_window_cache: dict[str, int] = {}
        # Live/cache catalog shown by /model and enforced before the first
        # runtime build.  Kept UI-independent so REPL, headless, and gateway all
        # share the same selected-model evidence.
        self._model_catalog_snapshot: ModelCatalogSnapshot | None = None
        self._model_catalog_snapshots: dict[str, ModelCatalogSnapshot] = {}
        self._model_catalog_service: Any | None = None
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
        # Cached MCP load result (each server is a process spawn + handshake).
        # Reused across runtime rebuilds so a rebuild triggered only by a
        # main-model / mode / backend change does NOT re-spawn every MCP server.
        # Invalidated (set None) wherever config reloads or MCP config mutates
        # (see _apply_reloaded_config and cmd_trust); the health-mirroring block
        # in ensure_runtime still runs on EVERY rebuild off this cached result.
        self._mcp_cache: MCPLoadResult | None = None
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
        # Single-flight guard for ensure_runtime: build_runtime mutates
        # process-global deepagents summarization-profile state, so concurrent
        # callers must not both build (double build, leaked runtime, registry
        # race). Plain attribute — a 3.12 asyncio.Lock needs no running loop at
        # construction and binds to the loop on first ``async with``.
        self._ensure_lock = asyncio.Lock()
        # Generation counter + single in-flight build task backing the
        # single-flight, cancellation-safe, invalidation-safe build (see
        # _invalidate_runtime, _run_build, and ensure_runtime). Every
        # invalidation bumps the generation; a build worker commits its result
        # only if the generation is unchanged when it finishes, so a config /
        # MCP / model / mode change mid-build disposes the stale runtime rather
        # than assigning one built with revoked tools. Plain attributes — the
        # task binds to the running loop when ensure_runtime first creates it.
        self._runtime_generation = 0
        self._build_task: asyncio.Task[JarnRuntime | None] | None = None
        # Set True by aclose(): shutdown is a generation boundary. Once closed,
        # ensure_runtime refuses to start or serve a build, and any in-flight
        # worker has been awaited there (disposed, never committed) so no backend
        # outlives the session.
        self._closed = False
        # Non-fatal lifecycle-hook notice to surface once (e.g. a failed
        # session_start hook, or the global-hooks-trust gate refusing to run).
        self._lifecycle_notice: str | None = None
        # Shell-escape output captured by ! commands; injected once into the next
        # turn via enrich_turn_input and then cleared.
        self.pending_shell_context: list[ShellNote] = []
        # Display SSOT: session-scoped tool-progress / focus, seeded from config.
        from jarn.tui.grammar import TOOL_PROGRESS_DEFAULT

        self.session_started_at = time.monotonic()
        self.tool_progress = getattr(config.ui, "tool_progress", TOOL_PROGRESS_DEFAULT)
        self.focus_mode = False
        #: Session-local Enter-while-busy mode. Seeded from config; /busy does
        #: not write YAML (persist with /config set ui.busy_input_mode).
        self.busy_input_mode = getattr(config.ui, "busy_input_mode", "queue")
        #: Session-local ``/compact`` apply count for the toolbar badge. In-graph
        #: auto-compact is not observed without a new engine API, so only
        #: :meth:`compact_apply` increments this.
        self.compact_count = 0
        self._focus_saved_progress: str | None = None
        # Diagnostics auto-fix chain round (T-3-3): 0 for a real user turn,
        # incremented when an auto-fix round is queued, reset on real user
        # input. Passed to each per-turn SessionDriver so the round cap holds
        # across the driver instances that make up one auto-fix chain.
        self._diag_chain_round: int = 0
        # T-3-7: set True after a provider rejects inlined images, so `auto` behaves
        # like `off` for the rest of the session (session-lifetime, not per-turn —
        # drivers are recreated each turn and would reset a per-driver flag).
        self.inline_images_disabled: bool = False
        # T-4-6 mid-turn steering: a single pending-steer slot the REPL writes (via
        # [s] / /queue steer) and each per-turn SessionDriver pulls at a settled
        # boundary. Controller-held (not per-driver) so it survives across the
        # drivers minted per turn/retry; single-shot (cleared on pull) so a steer is
        # never double-applied across a model-rotation retry.
        self._steer_slot: str | None = None
        # Session-lifetime holder for the last injected date block. Drivers are
        # recreated per turn, so this ONE shared dict is passed to every per-turn
        # SessionDriver (see make_driver) — that is what makes the date system
        # message injected once per local day rather than re-injected every turn.
        self._date_state: dict = {}
        # Explicit prompt-body activation is thread-scoped. Session modules are
        # compiled into the runtime prompt; turn modules are consumed by the next
        # driver. Replacing the thread clears both so prompt text cannot leak into
        # /clear, /compact, /rewind, or /resume targets.
        self._prompt_module_activations: dict[str, PromptModuleScope] = {}
        # Front-end-owned in-flight turn task (REPL / gateway). ``abort()``
        # cancels and *awaits* it before settle+rollback so cancel never races
        # the turn's finally / detached snapshot (T-CTRL-1 / #59).
        self._turn_task: asyncio.Task[Any] | None = None
        self._abort_lock = asyncio.Lock()
        # Turn re-entrancy guard (T-QA-1): exclusive owner of the active turn on
        # this controller / thread_id. Policy is REFUSE — see TurnBusyError.
        # ``_turn_held`` distinguishes "we own the slot" from a stale done task
        # still referenced in ``_turn_owner`` (steal-on-done healing).
        self._turn_owner: asyncio.Task[Any] | None = None
        self._turn_held: bool = False

    # -- multi-root scope ---------------------------------------------------

    @staticmethod
    def _resolve_extra_roots(raw: list[Path] | None) -> list[Path]:
        """Normalize added roots to resolved, de-duplicated ``Path`` objects.

        Resolving here (once) is what keeps the engine's scope check, the backend
        FS guard, and the sandboxes agreeing on the exact same paths — including
        the symlink-escape realpath — with no per-layer drift.
        """
        out: list[Path] = []
        for p in raw or []:
            try:
                resolved = Path(p).expanduser().resolve()
            except (OSError, RuntimeError, ValueError):
                continue
            if resolved not in out:
                out.append(resolved)
        return out

    def add_root(self, raw: str) -> tuple[bool, str]:
        """Add a directory to the session's WRITE scope (``/add-dir`` core logic).

        Refuses on an untrusted project (a scope-widening capability must not be
        grantable to a repo whose config we don't trust). Validates the path
        (exists, is a directory), de-dupes against the primary + existing roots,
        then extends the engine's roots and drops the runtime so the next turn
        rebuilds the backend (FS guard + sandbox) with the new root bound. Returns
        ``(ok, message)``; the message always states the checkpoint/undo
        primary-only limitation on success.
        """
        if not self.project_trusted:
            return (
                False,
                "/add-dir is refused on an untrusted project — run /trust here "
                "first (an untrusted repo may not widen the agent's write scope).",
            )
        try:
            path = Path(raw).expanduser().resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            return False, f"/add-dir: cannot resolve {raw!r}: {exc}"
        if not path.exists():
            return False, f"/add-dir: {path} does not exist."
        if not path.is_dir():
            return False, f"/add-dir: {path} is not a directory."
        primary = self.project_root.resolve() if self.project_root else None
        if path == primary or path in self.extra_roots:
            return False, f"/add-dir: {path} is already an active root."
        self.extra_roots.append(path)
        self.engine.roots = tuple(self.extra_roots)
        # Rebuild the runtime on the next turn so the backend FS guard + sandbox
        # bind/writable set pick up the new root (kept in sync with the engine).
        self._invalidate_runtime()
        return (
            True,
            f"Added {path} to this session's write scope "
            f"({len(self.extra_roots) + (1 if primary else 0)} active roots). "
            "Note: checkpoint/undo still snapshots the primary project root ONLY "
            "— edits in added roots are NOT captured by /undo or /rewind, and "
            "project context (JARN.md) is loaded from the primary root only.",
        )

    # -- lifecycle ----------------------------------------------------------

    async def ensure_runtime(self) -> JarnRuntime:
        """Build (or return) the session runtime, single-flighted per controller.

        Raises ``RuntimeError`` once the controller is closed: aclose() is a
        generation boundary, so no build is started or served after shutdown.
        """
        # Single-flight: build_runtime mutates process-global deepagents profile
        # state, so builds must run one-at-a-time per controller. Holding the lock
        # across the whole body makes the None checks below a double-check —
        # concurrent callers serialize and reuse the first build's runtime.
        async with self._ensure_lock:
            # Refuse after shutdown: aclose() bumped the generation and awaited any
            # in-flight worker, so starting or serving a build now would resurrect
            # a backend past teardown.
            if self._closed:
                raise RuntimeError("controller is closed")
            return await self._ensure_runtime_locked()

    async def _ensure_runtime_locked(self) -> JarnRuntime:
        if self.runtime is None:
            ok, message = await asyncio.to_thread(self.validate_selected_model_catalog)
            if not ok:
                from jarn.errors import ErrorCode, JarnUserError, error_detail

                self.health = "error"
                self.last_error = message
                raise JarnUserError(
                    error_detail(
                        ErrorCode.MODEL_UNAVAILABLE,
                        "Selected model routing is unavailable",
                        cause=message,
                        component="model catalog",
                        retryable=True,
                        action=(
                            "Run `/model refresh` or `jarn setup`; for Ollama, "
                            "pull a model whose `/api/show` capabilities include "
                            "`tools`, then retry."
                        ),
                    )
                )
        if self._saver is None:
            self._saver, self._saver_cm = await create_async_checkpointer(self._db_path)
        # Build via a controller-owned, generation-tagged worker task. Invariants:
        # (i) at most one build worker exists at a time — a caller that arrives
        # while a build is in flight (even after a previous caller was cancelled)
        # reuses the stored task via asyncio.shield instead of starting a SECOND
        # build (build_runtime mutates process-global deepagents profile state);
        # (ii) a result is committed only if no invalidation happened since the
        # build started (_run_build's generation check); (iii) caller cancellation
        # (a cancelled turn) neither cancels the shared worker — an OS thread
        # can't be stopped anyway — nor orphans an uncommitted runtime. A stale
        # build leaves runtime None, so this loop rebuilds fresh (re-loading MCP,
        # which the invalidator dropped).
        while self.runtime is None:
            # Belt-and-suspenders: aclose() holds _ensure_lock across its whole
            # teardown, so _closed cannot flip mid-loop today — but re-check each
            # iteration so a future refactor that weakens that mutual exclusion
            # still can't resurrect a build (and its backend) after shutdown.
            if self._closed:
                raise RuntimeError("controller is closed")
            task = self._build_task
            if task is None:
                gen = self._runtime_generation
                task = asyncio.create_task(self._run_build(gen))
                self._build_task = task
            # shield: cancelling this caller must not cancel the shared worker,
            # which commits-or-disposes exactly once regardless.
            await asyncio.shield(task)
        if not self._session_started:
            self._fire_lifecycle("session_start")
            self._session_started = True
        return self.runtime

    async def _run_build(self, gen: int) -> JarnRuntime | None:
        """The single build worker: load MCP + build the runtime off-thread, then
        commit the result ONLY if the generation is unchanged since ``gen`` was
        captured. Invariants: (i) at most one worker runs at a time (ensure_runtime
        reuses the stored task via shield); (ii) a result is committed only when
        no invalidation (generation bump) happened mid-build — otherwise the fresh
        runtime is disposed (backend closed, best-effort) and NOT assigned, and
        the MCP cache is left untouched so a cache the invalidator dropped is never
        resurrected with revoked tools; (iii) caller cancellation never orphans an
        uncommitted runtime — the worker still commits-or-disposes exactly once.
        Clears its own task slot last so the next ensure_runtime rebuilds fresh."""
        from jarn.agent.builder import AmbientKeyLeakError, SandboxUnavailable

        mcp = None
        loaded_mcp = False
        retained_mcp = False
        module_kwargs = (
            {"prompt_module_activations": self._prompt_module_activations}
            if self._prompt_module_activations
            else {}
        )
        try:
            # Load MCP servers once per session and reuse across rebuilds: a
            # rebuild triggered only by a main-model / mode / backend change must
            # not re-spawn every MCP server. Loaded into a LOCAL and committed to
            # self._mcp_cache only on a matching-generation commit, so an
            # invalidation that dropped the cache mid-build can't be undone by a
            # stale worker re-caching the old (revoked-tool) result.
            mcp = self._mcp_cache
            if mcp is None:
                mcp = await load_mcp_tools(self.config.mcp_servers, self.config.permissions.network)
                loaded_mcp = True
            tools = mcp.tools
            try:
                # build_runtime is pure-sync and does O(repo) file I/O (skills /
                # commands / subagents scan, JARN.md, wiki index, capability
                # detection, repo-map). Run it off the event loop so a rebuild
                # (model rotation, mode change, /add-dir) never freezes the TUI.
                # Built into a LOCAL; committed to self.runtime only under the
                # generation check below. functools.partial resolves the
                # module-level build_runtime name at call time, so tests
                # monkeypatching it keep working. `engine=self.engine` threads the
                # session's AUTHORITATIVE, session-persistent permission engine (the
                # same instance interrupts.py records deny_session/remember on) into
                # the result-filter middleware so a runtime read-denial is honored by
                # the filter on every stack (BUG A); it survives runtime rebuilds
                # because only self.runtime is invalidated, never self.engine.
                runtime = await asyncio.to_thread(
                    functools.partial(
                        build_runtime,
                        self.config,
                        project_root=self.project_root,
                        project_trusted=self.project_trusted,
                        checkpointer=self._saver,
                        extra_tools=tools,
                        system_prompt_override=self.system_prompt_override,
                        response_format=self.response_format,
                        extra_roots=self.extra_roots,
                        cost_tracker=self.tracker,
                        engine=self.engine,
                        **module_kwargs,
                    )
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
                # Off the event loop for the same reason as the primary build above.
                runtime = await asyncio.to_thread(
                    functools.partial(
                        build_runtime,
                        self.config,
                        project_root=self.project_root,
                        project_trusted=self.project_trusted,
                        checkpointer=self._saver,
                        extra_tools=tools,
                        system_prompt_override=self.system_prompt_override,
                        response_format=self.response_format,
                        extra_roots=self.extra_roots,
                        cost_tracker=self.tracker,
                        engine=self.engine,
                        **module_kwargs,
                    )
                )
            # Commit-or-dispose exactly once, gated on the generation. The commit
            # section has NO await, so no invalidation can interleave between the
            # check and the assignment.
            if self._runtime_generation != gen:
                # An invalidation superseded this build (config / MCP / model /
                # mode changed mid-build): dispose the fresh runtime so its
                # backend isn't leaked, and leave runtime None (and the MCP cache
                # untouched) so ensure_runtime rebuilds fresh from the new config.
                self._dispose_runtime(runtime)
                return None
            self._mcp_cache = mcp
            retained_mcp = True
            self._mirror_mcp_health(mcp)
            self.runtime = runtime
            return runtime
        finally:
            # A failed or generation-stale build must not orphan the persistent
            # MCP sessions it opened locally. Cached sessions are owned by the
            # controller and are closed by cache invalidation / aclose instead.
            if loaded_mcp and not retained_mcp and mcp is not None:
                await mcp.aclose()
            # Free the slot last so the next ensure_runtime starts a new worker
            # for a stale (uncommitted) or failed build. Safe to clear
            # unconditionally: the commit/dispose above ran without awaiting, so
            # no concurrent caller could have installed a different task.
            self._build_task = None

    def _mirror_mcp_health(self, mcp: MCPLoadResult) -> None:
        """Mirror an MCP load result's per-server health onto the config entries
        and session state (called at build commit so status/UI can read it
        without re-loading). Invariant: MCP may never clobber or clear another
        subsystem's degradation reason — it owns ``last_error`` only when that is
        empty or already an MCP reason, so a live sandbox/other reason survives."""
        self.mcp_health = dict(mcp.health)
        self.mcp_errors = dict(mcp.errors)
        for server in self.config.mcp_servers:
            if server.name in self.mcp_health:
                server.health = self.mcp_health[server.name]
        _MCP_PREFIX = "MCP server(s) failed:"
        if mcp.degraded:
            self.health = "degraded"
            if not self.last_error or self.last_error.startswith(_MCP_PREFIX):
                failed = ", ".join(sorted(mcp.errors))
                first = next(iter(sorted(mcp.errors)))
                self.last_error = f"{_MCP_PREFIX} {failed} ({mcp.errors[first]})"
        elif self.health == "degraded" and (self.last_error or "").startswith(_MCP_PREFIX):
            # A prior MCP failure degraded the session; a now-healthy cache
            # (e.g. after `/mcp refresh` recovered the server) clears it on
            # rebuild — symmetric with the degrade above, so recovery sticks
            # instead of the session staying degraded forever. Scoped to the
            # MCP last_error so we never stomp a sandbox/other degradation.
            self.health = "ok"
            self.last_error = None

    @staticmethod
    def _dispose_runtime(runtime: JarnRuntime | None) -> None:
        """Best-effort teardown of a built-but-not-committed runtime (a stale
        build superseded by an invalidation) so its backend — e.g. a Docker
        sandbox container — is not leaked. Mirrors :meth:`close`'s teardown."""
        import contextlib

        backend = getattr(runtime, "backend", None)
        backend_close = getattr(backend, "close", None)
        if callable(backend_close):
            with contextlib.suppress(Exception):
                backend_close()

    def _invalidate_runtime(self, *, drop_mcp_cache: bool = False) -> None:
        """The single choke point that invalidates the built runtime.

        Bumps the build generation so any worker still in flight disposes (rather
        than commits) its result, drops the runtime so the next ensure_runtime
        rebuilds, and — when the MCP config itself changed — drops the MCP tool
        cache so the rebuild re-loads servers (a stale worker must never resurrect
        revoked MCP tools). Every raw ``runtime = None`` invalidation routes
        through here so the generation guard can't be bypassed."""
        self._runtime_generation += 1
        self.runtime = None
        if drop_mcp_cache:
            stale_mcp = self._mcp_cache
            self._mcp_cache = None
            if stale_mcp is not None:
                stale_mcp.close_soon()

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

    # -- prompt modules -----------------------------------------------------

    def _prompt_module_snapshot(
        self,
    ) -> tuple[PromptModuleRegistry, PromptModuleContext, PromptAssembly]:
        """Return registry, live activation context, and actual static assembly."""
        runtime = self.runtime
        runtime_registry = getattr(runtime, "prompt_registry", None)
        if isinstance(runtime_registry, PromptModuleRegistry):
            registry = runtime_registry
            base_context = getattr(runtime, "prompt_context", None)
            assert isinstance(base_context, PromptModuleContext)
            context = replace(
                base_context,
                config=self.config,
                project_trusted=self.project_trusted,
                explicit_scopes=dict(self._prompt_module_activations),
                prompt_override=self.system_prompt_override is not None,
            )
        else:
            from jarn.extensibility.skills import load_skills

            skills = (
                runtime.skills
                if runtime is not None
                else load_skills(
                    self.project_root,
                    project_trusted=self.project_trusted,
                    read_claude_dir=self.config.compat.read_claude_dir,
                )
            )
            context = PromptModuleContext(
                config=self.config,
                project_root=self.project_root,
                project_trusted=self.project_trusted,
                skills=skills,
                explicit_scopes=dict(self._prompt_module_activations),
                prompt_override=self.system_prompt_override is not None,
            )
            registry = with_context_budgets(
                create_prompt_module_registry(skills), context
            )

        runtime_assembly = getattr(runtime, "prompt_assembly", None)
        if isinstance(runtime_assembly, PromptAssembly):
            assembly = runtime_assembly
        elif self.system_prompt_override is not None:
            from jarn.memory.tokens import count_tokens

            assembly = PromptAssembly(
                text=self.system_prompt_override,
                modules=(),
                token_count=count_tokens(self.system_prompt_override),
            )
        else:
            assembly = registry.assemble(context)
        return registry, context, assembly

    def prompt_module_statuses(
        self,
    ) -> tuple[tuple[PromptModuleStatus, ...], PromptAssembly]:
        """Observable module state used by /modules, /cost, and doctor."""
        registry, context, assembly = self._prompt_module_snapshot()
        return registry.statuses(context, assembly=assembly), assembly

    def prompt_module_diagnostics(self) -> dict[str, Any]:
        registry, context, assembly = self._prompt_module_snapshot()
        return build_prompt_module_diagnostics(registry, context, assembly)

    def activate_prompt_module(
        self, name: str, scope: PromptModuleScope
    ) -> CommandResult:
        """Queue a user-activatable body for one turn or this thread session."""
        if scope not in ("turn", "session"):
            return CommandResult("Module scope must be 'turn' or 'session'.")
        if self.system_prompt_override is not None:
            return CommandResult(
                "Prompt modules are disabled by the wholesale system-prompt override."
            )
        registry, _context, _assembly = self._prompt_module_snapshot()
        module = registry.resolve(name)
        if module is None:
            return CommandResult(
                f"Unknown module: {name!r}. Run /modules or /skills to list choices."
            )
        if not module.user_activatable:
            return CommandResult(
                f"{module.name} is controlled by runtime state/config and cannot "
                "be manually activated or suppressed."
            )

        previous = self._prompt_module_activations.get(module.name)
        self._prompt_module_activations[module.name] = scope
        if previous == "session" or scope == "session":
            self._invalidate_runtime()
        return CommandResult(
            f"Activated {module.name} for the next turn."
            if scope == "turn"
            else f"Activated {module.name} for thread {self.thread_id[:8]}."
        )

    def deactivate_prompt_module(self, name: str) -> CommandResult:
        """Remove an explicit activation without disabling deterministic modules."""
        registry, _context, _assembly = self._prompt_module_snapshot()
        module = registry.resolve(name)
        if module is None:
            return CommandResult(
                f"Unknown module: {name!r}. Run /modules or /skills to list choices."
            )
        previous = self._prompt_module_activations.pop(module.name, None)
        if previous is None:
            if module.user_activatable:
                return CommandResult(f"{module.name} is not explicitly active.")
            return CommandResult(
                f"{module.name} is controlled by runtime state/config and cannot "
                "be manually suppressed."
            )
        if previous == "session":
            self._invalidate_runtime()
        return CommandResult(f"Deactivated {module.name}.")

    def _consume_turn_prompt_modules(self) -> tuple[RenderedPromptModule, ...]:
        """Render and consume queued one-turn bodies for the next fresh driver."""
        names = sorted(
            name
            for name, scope in self._prompt_module_activations.items()
            if scope == "turn"
        )
        if not names:
            return ()
        registry, context, _assembly = self._prompt_module_snapshot()
        rendered = []
        for name in names:
            module = registry.resolve(name)
            if module is not None and module.user_activatable:
                item = render_prompt_module(module, context)
                if item.content:
                    rendered.append(item)
            self._prompt_module_activations.pop(name, None)
        return tuple(sorted(rendered, key=lambda item: (item.priority, item.name)))

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
        # Bound the transcript to the summarizer's context so /compact does not
        # overflow the summarizer exactly when the thread is largest (the moment
        # /compact is most needed). Per-message capping stops one huge tool result
        # from dominating; the window trim keeps head (goal) + tail (latest state)
        # within budget. 0.6 leaves room for the instruction prompt and the summary
        # output; 60_000 is a conservative floor when the window is unknown.
        from jarn.cost.pricing import context_window

        window = context_window(summarizer_ref)
        # Clamp to at least 1 token: a tiny known window can make int(window * 0.6)
        # round to 0, and _trim_to_window must never be handed a zero budget (it
        # would otherwise have nothing it may keep). 60_000 is the unknown-window floor.
        budget = max(1, int(window * 0.6)) if window > 0 else 60_000
        transcript = _trim_to_window(_render_transcript(messages), budget)
        prompt = (
            "Summarize the following coding-assistant conversation so work can "
            "continue in a fresh context. Capture: the goal, decisions made, files "
            "changed, current state, and the next step. Be concise but complete.\n\n" + transcript
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
        — call only after the user confirms (manual) or unconditionally (auto).

        The structured ``todos`` plan is a separate graph-state channel from
        ``messages`` (see :meth:`todos`), and the fresh thread starts with an
        empty one. Capture the current plan BEFORE minting the new thread and
        seed it back, or the agent's checklist silently resets to empty on every
        compaction — losing its plan on a long task exactly when compaction
        fires mid-work."""
        rt = await self.ensure_runtime()
        # Read the plan off the OLD thread while its config is still current.
        todos = await self.todos()
        self.new_thread()
        from langchain_core.messages import HumanMessage

        seed: dict[str, Any] = {
            "messages": [HumanMessage(content=f"[Summary of prior conversation]\n{summary}")]
        }
        # Only carry a non-empty plan across: an empty list has nothing to
        # preserve, so keep the seed messages-only (byte-identical to before)
        # rather than writing an empty channel value.
        if todos:
            seed["todos"] = todos
        await rt.agent.aupdate_state(self._config(), seed)
        self.compact_count += 1

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
                    b.get("text", "") if isinstance(b, dict) else str(b) for b in content
                )
            preview = " ".join(str(content).split())
            if len(preview) > 80:
                preview = preview[:77] + "…"
            turns.append((idx, preview))
        return turns

    async def fork_to_turn(self, keep_count: int, *, restore_files: bool = False) -> int | None:
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
        values = getattr(state, "values", {}) or {}
        messages = values.get("messages", []) or []
        if not messages or keep_count < 0:
            return None

        import asyncio

        kept_prefix = list(messages[:keep_count])  # empty when keep_count == 0
        # Carry the structured plan onto the new branch: ``todos`` is a distinct
        # graph-state channel from ``messages``, and the forked thread starts with
        # an empty one, so a rewind would otherwise wipe the agent's checklist.
        todos = list(values.get("todos", []) or [])
        original_thread = self.thread_id

        # Slice 2: revert the working tree to the chosen turn's checkpoint before
        # forking. Resolve against the ORIGINAL thread; the turn index is the
        # human-turn count in the kept prefix (matches the driver's recording).
        if restore_files:
            turn_index = sum(1 for m in kept_prefix if getattr(m, "type", "") == "human")
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
        _kept_turns = sum(1 for m in kept_prefix if getattr(m, "type", "") == "human")
        self.new_thread()  # mint a fresh thread_id; resets tracker.context_tokens
        # Register alias so find_for_turn can walk back to the parent on a stacked
        # rewind within the same session (in-memory, not persisted).
        self.checkpoint_manager.register_thread_alias(self.thread_id, original_thread, _kept_turns)
        seed: dict[str, Any] = {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *kept_prefix]}
        # Only seed a non-empty plan (an empty channel value is a no-op we skip
        # to keep the payload byte-identical to the pre-fix behavior).
        if todos:
            seed["todos"] = todos
        await rt.agent.aupdate_state(self._config(), seed)
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

    def _pop_steer_slot(self) -> str | None:
        """Return the pending steer text and clear the slot (single-shot pull).

        Wired as each per-turn :class:`SessionDriver`'s ``steer_source`` so the
        driver reads the slot at a settled boundary; the REPL writes it at any time
        via ``[s]`` / ``/queue steer``. Clearing on read is what prevents a steer
        from being applied twice across a model-rotation retry (each retry builds a
        fresh driver, but the slot is emptied the first time it is consumed)."""
        text = self._steer_slot
        self._steer_slot = None
        return text

    def prepare_media(self, text: str, media: Any = None) -> Any:
        """Gate/stage inbound media via the core ingest API (#54 / T-MEDIA-1).

        Thin controller facade over :func:`jarn.agent.media_ingest.prepare_media`
        so Telegram/REPL share the same size/type gates and document staging.
        Pass the result's images/text into :meth:`SessionDriver.run_turn`, or call
        ``run_turn(..., media=…)`` directly (preferred — handles cleanup).
        """
        from jarn.agent.media_ingest import prepare_media

        return prepare_media(text, media, project_root=self.project_root)

    def acquire_turn(self) -> bool:
        """Claim exclusive turn ownership for the current asyncio task.

        Returns ``True`` when this call newly acquired the slot (caller must
        :meth:`release_turn`), or ``False`` when the current task already holds
        it (re-entrant ``make_driver`` during model-fallback retries).

        Raises :class:`TurnBusyError` when another live task holds the slot —
        **refuse, never queue** (T-QA-1). A finished owner is stolen so a
        leaked hold (e.g. ``make_driver`` without ``run_turn``) cannot wedge
        the controller forever.

        Sync callers (no running event loop — e.g. unit tests calling
        ``make_driver`` directly) still work: they take the slot with
        ``owner=None`` and refuse only when a live asyncio task already holds
        it.
        """
        try:
            me = asyncio.current_task()
        except RuntimeError:
            # No running loop — sync ``make_driver`` / non-async call sites.
            me = None
        owner = self._turn_owner
        if owner is not None and not owner.done() and owner is not me:
            raise TurnBusyError(
                f"turn already in progress on thread {self.thread_id!r} "
                "(refusing concurrent run; gateway queues at chat layer)"
            )
        if me is not None and owner is me and self._turn_held:
            return False
        if me is None and self._turn_held and owner is None:
            # Sync re-entry while a previous sync hold is still open.
            return False
        self._turn_owner = me
        self._turn_held = True
        return True

    def release_turn(self) -> None:
        """Release exclusive turn ownership if held by the current task."""
        try:
            me = asyncio.current_task()
        except RuntimeError:
            me = None
        if self._turn_owner is me:
            self._turn_held = False
            self._turn_owner = None

    def make_driver(self, approver: Approver) -> SessionDriver:
        assert self.runtime is not None, "call ensure_runtime() first"
        # T-QA-1: refuse a second concurrent driver so ``_active_driver`` is never
        # overwritten mid-turn (breaks settle_snapshot / undo). Same-task retries
        # inside an already-held turn (run_agent_turn fallback loop) re-enter.
        newly_acquired = self.acquire_turn()
        transcript = None
        if self.config.observability.transcript:
            from jarn.memory.sessions import make_transcript_writer

            transcript = make_transcript_writer(self.thread_id, project_root=self.project_root)
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
            verify_max_repair_rounds=self.config.verify.max_repair_rounds,
            project_root=self.project_root,
            verify_executor=getattr(getattr(self.runtime, "backend", None), "execute", None),
            diagnostics_mode=self.config.verify.diagnostics,
            diagnostics_max_rounds=self.config.verify.diagnostics_max_rounds,
            diagnostics_ts=self.config.verify.diagnostics_ts,
            _diag_round=self._diag_chain_round,
            steer_source=self._pop_steer_slot,
            date_state=self._date_state,
            turn_prompt_module_source=self._consume_turn_prompt_modules,
            inject_prompt_modules=getattr(
                self.runtime, "prompt_modules_enabled", True
            ),
            # Live tool-output streaming: the runtime's backend feeds this queue with
            # ToolProgress from execute's worker thread; the driver drains it and
            # interleaves TOOL_PROGRESS events. None for non-local backends.
            progress_queue=getattr(self.runtime, "progress_queue", None),
            # Document staging (#54): expose mkdtemp dirs to virtual-mode read_file.
            fs_backend=getattr(self.runtime, "backend", None),
        )
        # When this make_driver opened the slot (approval-resume and other
        # make_driver→run_turn callers outside run_agent_turn), release when the
        # driver's turn finishes so the hold cannot outlive the stream.
        if newly_acquired:
            driver._turn_release = self.release_turn
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

    # -- turn binding + async mutation APIs (T-CTRL-1 / #59) ----------------

    def bind_turn_task(self, task: asyncio.Task[Any] | None) -> None:
        """Register (or clear) the front end's in-flight turn task."""
        from jarn.controller import async_ops

        async_ops.bind_turn_task(self, task)

    @property
    def turn_running(self) -> bool:
        """True while a front-end-bound turn task is still in flight."""
        from jarn.controller import async_ops

        return async_ops.turn_running(self)

    async def undo(
        self,
        *,
        confirm: UndoConfirm | None = None,
    ) -> CommandResult:
        """Preview, confirm, then revert the last turn's file edits."""
        from jarn.controller import async_ops

        return await async_ops.undo(self, confirm=confirm)

    async def redo(self) -> CommandResult:
        """Settle any in-flight snapshot, then re-apply the most recent undo."""
        from jarn.controller import async_ops

        return await async_ops.redo(self)

    async def abort(self) -> CommandResult:
        """Cancel the in-flight turn, settle its snapshot, then roll back edits."""
        from jarn.controller import async_ops

        return await async_ops.abort(self)

    async def set_permission_mode(
        self,
        value: str,
        *,
        confirm: YoloConfirm | None = None,
    ) -> CommandResult:
        """Set permission mode; yolo escalate requires a successful ``confirm``."""
        from jarn.controller import async_ops

        return await async_ops.set_permission_mode(self, value, confirm=confirm)

    def close(self) -> None:
        import contextlib

        # ``close`` is synchronous for legacy callers; signal the same-task MCP
        # session owners here and let ``aclose`` await them when available.
        if self._mcp_cache is not None:
            self._mcp_cache.close_soon()
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
        """Async cleanup: settle any in-flight build, fire session_end, flush
        telemetry, close checkpointer. Safe after a cancelled turn."""
        import contextlib

        # Serialize shutdown with the build machinery: hold _ensure_lock across the
        # entire teardown so aclose and ensure_runtime are MUTUALLY EXCLUSIVE. A
        # live caller holds this lock across its whole build loop, so aclose runs
        # only AFTER that caller finishes — possibly having committed a runtime,
        # which the committed-runtime capture below then disposes. Without this,
        # aclose could bump the generation mid-loop, the in-flight build would
        # dispose, and the still-live loop would start a replacement build that
        # commits (leaking its backend) after aclose returned.
        async with self._ensure_lock:
            # Shutdown is a generation boundary. Flip the closed flag so
            # ensure_runtime refuses further builds, and bump the generation (via
            # the single invalidation choke point) so a build worker still in flight
            # disposes its result instead of committing self.runtime after close.
            # Capture any already-committed runtime FIRST: the invalidation nulls
            # self.runtime, so without this its backend would slip past self.close()
            # and leak.
            self._closed = True
            committed = self.runtime
            committed_mcp = self._mcp_cache
            self._invalidate_runtime(drop_mcp_cache=True)
            # Await the in-flight worker so it has committed-or-disposed before
            # teardown. The generation bump above forces its dispose path; shield
            # keeps our await from cancelling the shared worker (which does NOT take
            # _ensure_lock, so awaiting it while holding the lock cannot deadlock),
            # suppress swallows its build error. This guarantees no backend (e.g. a
            # Docker sandbox container) outlives aclose.
            task = self._build_task
            if task is not None and not task.done():
                with contextlib.suppress(Exception):
                    await asyncio.shield(task)
            if self._session_started:
                self._fire_lifecycle("session_end")
                self._session_started = False
            transcript = getattr(self._active_driver, "transcript", None)
            if transcript is not None:
                with contextlib.suppress(Exception):
                    transcript.close()
            self.close()
            # self.close() saw the invalidated None; tear down the captured
            # committed runtime's backend too (best-effort, no double close — a
            # committed runtime and an in-flight build never coexist).
            self._dispose_runtime(committed)
            if committed_mcp is not None:
                with contextlib.suppress(Exception):
                    await committed_mcp.aclose()
            if self._saver_cm is not None:
                with contextlib.suppress(Exception):
                    await self._saver_cm.__aexit__(None, None, None)
                self._saver_cm = None
                self._saver = None

    # -- thread management --------------------------------------------------

    def new_thread(self) -> None:
        had_session_module = any(
            scope == "session"
            for scope in self._prompt_module_activations.values()
        )
        self._prompt_module_activations.clear()
        self.thread_id = new_thread_id()
        self.tracker.context_tokens = 0  # reset the context gauge on /clear /compact
        if had_session_module:
            self._invalidate_runtime()

    def resume_thread(self, thread_id: str) -> None:
        had_session_module = any(
            scope == "session"
            for scope in self._prompt_module_activations.values()
        )
        self._prompt_module_activations.clear()
        self.thread_id = thread_id
        if had_session_module:
            self._invalidate_runtime()

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
        self._invalidate_runtime()  # rebuild with the new model
        return new_ref

    def reset_model_rotation(self) -> None:
        """Return to the primary model (called when a turn succeeds)."""
        if self._candidate_idx != 0 and self._candidates:
            self._candidate_idx = 0
            self.config.routing.main = self._candidates[0]
            self.config.default_model = self._candidates[0]
            self._invalidate_runtime()

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
        if provider.type in (
            ProviderType.OLLAMA,
            ProviderType.LMSTUDIO,
            ProviderType.CODEX_SUBSCRIPTION,
        ):
            return True  # local endpoints / managed Codex auth need no API key
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
                return parse_model_ref(ref, default_profile=self.config.default_profile).profile
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
                self._invalidate_runtime()  # rebuild with the new model
                return cand
            idx += 1
        return None

    async def drop_pending_image_message(self) -> bool:
        """Remove the last checkpointed human message from the current thread IFF it
        carries inline image blocks (T-3-7 image fallback).

        After a provider rejects inlined images, the front-end re-sends the turn
        text-only. LangGraph has already checkpointed the image-laden human message
        (that is why model-rotation *resumes* rather than re-sends), so a plain
        re-send would leave the rejected image in state and the model would see it
        again. Stripping only a message that actually contains image blocks makes
        this safe whether or not the failed call persisted the message: if it did we
        remove it; if it did not, the last human message is a prior text turn we must
        not touch. Best-effort — returns ``False`` (no-op) when the runtime/agent
        can't update state (e.g. a fake agent in tests) or on any error."""
        agent = getattr(self.runtime, "agent", None)
        aget = getattr(agent, "aget_state", None)
        aupdate = getattr(agent, "aupdate_state", None)
        if aget is None or aupdate is None:
            return False
        try:
            state = await aget(self._config())
            messages = (getattr(state, "values", {}) or {}).get("messages", []) or []
            last_human = next(
                (m for m in reversed(messages) if getattr(m, "type", "") == "human"),
                None,
            )
            if last_human is None:
                return False
            mid = getattr(last_human, "id", None)
            content = getattr(last_human, "content", None)
            has_image = isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "image" for b in content
            )
            if mid is None or not has_image:
                return False
            from langchain_core.messages import RemoveMessage

            await aupdate(self._config(), {"messages": [RemoveMessage(id=mid)]})
            return True
        except Exception:  # noqa: BLE001 - best-effort; never abort the fallback retry
            return False

    def record_session_title(self, title: str, *, when: float) -> None:
        self.sessions.touch(
            self.thread_id,
            title,
            when=when,
            project_root=self.project_root,
            model=self.config.resolved_main_model(),
            state="incomplete",
        )

    def mark_session_complete(self, *, when: float) -> None:
        self.sessions.mark_complete(self.thread_id, when=when)

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
        from jarn.catalog import ModelCatalogService
        from jarn.providers import parse_model_ref

        seen: dict[str, str] = {}

        def add(ref: str | None, hint: str) -> None:
            if ref and ref not in seen:
                seen[ref] = hint

        snapshots = dict(self._model_catalog_snapshots)
        if self._model_catalog_snapshot is not None:
            snapshots.setdefault(
                self._model_catalog_snapshot.provider_profile,
                self._model_catalog_snapshot,
            )

        for route, ref, _effort in ModelCatalogService.configured_routes(self.config):
            parsed = parse_model_ref(ref, default_profile=self.config.default_profile)
            snapshot = snapshots.get(parsed.profile)
            verified_refs = (
                {
                    entry.ref
                    for entry in snapshot.visible_models()
                    if not entry.deprecated
                    and entry.account_available is not False
                    and not (
                        snapshot.provider_type == "ollama"
                        and entry.supports_tools is not True
                    )
                }
                if snapshot is not None and snapshot.availability_verified
                else set()
            )
            # A verified provider response is authoritative: a remembered route
            # removed by that provider must not remain selectable as "current".
            if (
                snapshot is not None
                and snapshot.availability_verified
                and parsed.qualified not in verified_refs
            ):
                continue
            if snapshot is not None and not snapshot.availability_verified:
                continue
            provenance = (
                snapshot.provenance_label
                if snapshot is not None
                else "catalog not refreshed; availability unverified"
            )
            add(parsed.qualified, f"{route} · {provenance}")

        for snapshot in snapshots.values():
            if not snapshot.availability_verified:
                continue
            for entry in snapshot.visible_models():
                if (
                    entry.deprecated
                    or entry.account_available is False
                    or (
                        snapshot.provider_type == "ollama"
                        and entry.supports_tools is not True
                    )
                ):
                    continue
                flags: list[str] = [entry.display_name]
                if entry.is_default:
                    flags.append("account default")
                if entry.preview:
                    flags.append("preview")
                if entry.supported_reasoning_efforts:
                    flags.append(
                        "reasoning "
                        + "/".join(effort.value for effort in entry.supported_reasoning_efforts)
                    )
                if entry.context_window:
                    flags.append(f"{entry.context_window:,} token context")
                if entry.input_modalities:
                    flags.append("input " + "/".join(entry.input_modalities))
                if entry.supports_tools is True:
                    flags.append("tools verified")
                if entry.service_tiers:
                    flags.append("tiers " + "/".join(entry.service_tiers))
                if entry.description:
                    flags.append(entry.description)
                flags.append(snapshot.provenance_label)
                add(entry.ref, " · ".join(flags))
        return list(seen.items())

    def _active_catalog_target(self) -> tuple[str, Any, str] | None:
        """Return ``(profile, provider, selected_ref)`` for the active model."""

        from jarn.providers import parse_model_ref

        ref = self.config.resolved_main_model()
        if not ref:
            return None
        parsed = parse_model_ref(ref, default_profile=self.config.default_profile)
        provider = self.config.providers.get(parsed.profile)
        if provider is None:
            return None
        return parsed.profile, provider, ref

    def model_catalog_supported(self) -> bool:
        """Whether the active target can use the unified catalog abstraction."""

        return self._active_catalog_target() is not None

    def model_catalog_requires_verification(self) -> bool:
        """Every runtime target needs live/fresh-cache/exact-call evidence."""

        return self._active_catalog_target() is not None

    def refresh_model_catalog(self, *, include_hidden: bool = False) -> ModelCatalogSnapshot:
        """Refresh every configured provider, returning the active snapshot."""

        from jarn.catalog import ModelCatalogService

        target = self._active_catalog_target()
        if target is None:
            raise RuntimeError("No active model/provider is configured.")
        profile, provider, _ref = target
        if self._model_catalog_service is None:
            self._model_catalog_service = ModelCatalogService()
        service = self._model_catalog_service

        # A multi-provider picker should not make N serial timeout waits.  Each
        # provider has independent state/cache files, so bounded parallel reads
        # are safe and keep `/model` responsive.
        from concurrent.futures import ThreadPoolExecutor

        items = list(self.config.providers.items())

        def fetch(item: tuple[str, Any]) -> tuple[str, ModelCatalogSnapshot]:
            name, configured = item
            return name, service.get_catalog(
                name,
                configured,
                include_hidden=include_hidden,
                allow_stale_cache=True,
                cwd=self.project_root,
            )

        snapshots: dict[str, ModelCatalogSnapshot] = {}
        with ThreadPoolExecutor(max_workers=min(4, max(1, len(items)))) as pool:
            for name, fetched in pool.map(fetch, items):
                snapshots[name] = fetched
        snapshot = snapshots.get(profile)
        if snapshot is None:  # pragma: no cover - target derives from providers
            raise RuntimeError(f"No catalog could be loaded for {profile}.")
        self._model_catalog_snapshots = snapshots
        self._model_catalog_snapshot = snapshot
        return snapshot

    def catalog_models(self) -> tuple[ModelCatalogEntry, ...]:
        """Visible entries from the latest shared catalog snapshot."""

        snapshot = self._model_catalog_snapshot
        return snapshot.visible_models() if snapshot is not None else ()

    def verified_catalog_models(self) -> tuple[ModelCatalogEntry, ...]:
        """Visible selectable models from every currently verified provider."""

        entries: list[ModelCatalogEntry] = []
        seen: set[str] = set()
        for snapshot in self._model_catalog_snapshots.values():
            if not snapshot.availability_verified:
                continue
            for entry in snapshot.visible_models():
                if (
                    entry.ref in seen
                    or entry.deprecated
                    or entry.account_available is False
                    or (
                        snapshot.provider_type == "ollama"
                        and entry.supports_tools is not True
                    )
                ):
                    continue
                seen.add(entry.ref)
                entries.append(entry)
        return tuple(entries)

    def reasoning_choices(self, model_ref: str) -> list[tuple[str, str]]:
        """Reasoning efforts for ``model_ref`` from the rendered catalog."""

        snapshots = list(self._model_catalog_snapshots.values())
        if self._model_catalog_snapshot is not None:
            snapshots.append(self._model_catalog_snapshot)
        entry = next(
            (item for snapshot in snapshots for item in snapshot.models if item.ref == model_ref),
            None,
        )
        if entry is None:
            return []
        return [
            (
                effort.value,
                effort.description
                or ("recommended default" if effort.value == entry.default_reasoning_effort else ""),
            )
            for effort in entry.supported_reasoning_efforts
        ]

    def default_reasoning_effort(self, model_ref: str) -> str | None:
        snapshots = list(self._model_catalog_snapshots.values())
        if self._model_catalog_snapshot is not None:
            snapshots.append(self._model_catalog_snapshot)
        entry = next(
            (item for snapshot in snapshots for item in snapshot.models if item.ref == model_ref),
            None,
        )
        return entry.default_reasoning_effort if entry is not None else None

    def validate_selected_model_catalog(self) -> tuple[bool, str]:
        """Validate all routes before a turn through one catalog service."""

        from jarn.catalog import ModelCatalogService

        if not ModelCatalogService.configured_routes(self.config):
            return True, "catalog preflight not applicable"
        if self._model_catalog_service is None:
            self._model_catalog_service = ModelCatalogService()
        snapshots = self._model_catalog_service.get_catalogs_for_routes(
            self.config,
            include_hidden=False,
            allow_stale_cache=True,
            cwd=self.project_root,
        )
        self._model_catalog_snapshots.update(snapshots)
        target = self._active_catalog_target()
        if target is not None:
            self._model_catalog_snapshot = snapshots.get(target[0])
        ok, errors = ModelCatalogService.validate_routes(self.config, snapshots)
        if not ok:
            return (
                False,
                "Selected model routing is unavailable: "
                + "; ".join(errors)
                + ". Run `/model refresh` or `jarn setup`, or update every route "
                "in Advanced config.",
            )
        return True, "all selected model routes verified"

    def discover_models(self) -> list[tuple[str, str]]:
        """Return selectable models from configured endpoint catalogs.

        This compatibility API now delegates to :class:`ModelCatalogService`, so
        command completion cannot bypass the provenance/account-scoping rules
        used by the picker and pre-turn gate. Unverified endpoints contribute no
        entries; Advanced manual selection remains available elsewhere.
        """
        from jarn.catalog import ModelCatalogService
        from jarn.config.schema import ProviderType

        local = {ProviderType.OLLAMA, ProviderType.LMSTUDIO, ProviderType.OPENAI_COMPATIBLE}
        if self._model_catalog_service is None:
            self._model_catalog_service = ModelCatalogService()
        service = self._model_catalog_service
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for name, prov in self.config.providers.items():
            if prov.type not in local:
                continue
            snapshot = service.get_catalog(
                name,
                prov,
                allow_stale_cache=True,
                cwd=self.project_root,
            )
            self._model_catalog_snapshots[name] = snapshot
            if not snapshot.availability_verified:
                continue
            for entry in snapshot.visible_models():
                if (
                    entry.ref not in seen
                    and not entry.deprecated
                    and entry.account_available is not False
                    and not (
                        snapshot.provider_type == "ollama"
                        and entry.supports_tools is not True
                    )
                ):
                    seen.add(entry.ref)
                    out.append((entry.ref, name))
        return out

    def mode_choices(self) -> list[tuple[str, str]]:
        hints = {
            "plan": "read-only",
            "ask": "confirm writes/shell (default)",
            "auto-edit": "auto edits + web fetch/search, confirm shell",
            "yolo": "no prompts (danger-guard still applies)",
        }
        fallback = grammar.MODE_GLYPH["ask"]
        return [
            (m.value, f"{palette.MODE_GLYPH.get(m.value, fallback)} {hints[m.value]}")
            for m in PermissionMode
        ]

    def apply_model(self, ref: str, *, reasoning_effort: str | None = None) -> None:
        from jarn.providers import parse_model_ref

        self.config.routing.main = ref
        self.config.default_model = ref
        parsed = parse_model_ref(ref, default_profile=self.config.default_profile)
        provider = self.config.providers.get(parsed.profile)
        if provider is not None:
            selected_effort = reasoning_effort or self.default_reasoning_effort(ref)
            if selected_effort:
                provider.extra["reasoning_effort"] = selected_effort
        self._candidates = [ref] + list(self.config.routing.fallback)
        self._candidate_idx = 0
        self._invalidate_runtime()

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
        self._invalidate_runtime()
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
        if ex.backend == "docker" or (ex.backend == "sandbox" and ex.sandbox_provider == "docker"):
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
        model = (
            self.runtime.main_model_ref if self.runtime else None
        ) or self.config.resolved_main_model()
        glyph = {
            "ok": f"{layout.ok(grammar.GLYPH_KEY_OK)} ",
            "error": (
                f"{layout.err(f'{grammar.GLYPH_FAIL} key')}"
                f"{layout.sep()}"
                f"{layout.err('/doctor')} "
            ),
            "degraded": f"{layout.warn(grammar.GLYPH_WARN)} ",
            "unknown": "",
        }.get(self.health, "")
        sep = layout.sep()
        mode = palette.mode_label(self.config.permission_mode.value)
        # Make the isolation level visible: positively flag a real sandbox, and
        # never let the host (no-isolation) state hide — so nobody assumes they
        # are safe when the permission engine is the only thing standing guard.
        level = self.isolation_level()
        if level == "docker":
            backend = f"{sep}{layout.ok('docker')}"
        elif level == "os-sandbox":
            backend = f"{sep}{layout.ok('os-sandbox')}"
        elif level == "remote-sandbox":
            backend = f"{sep}{layout.ok('sandbox')}"
        elif self.health == "degraded":
            backend = f"{sep}{layout.warn('host (no sandbox)')}"
        else:
            backend = f"{sep}{layout.muted('host')}"
        return f"{glyph}{model or 'unconfigured'}{sep}{mode}{backend}{sep}{self.tracker.summary_line()}"

    # -- built-in commands --------------------------------------------------

    def handle_command(self, name: str, args: str) -> CommandResult:
        from jarn.commands.help import unknown_command
        from jarn.commands.registry import canonical_name, spec_by_name
        from jarn.controller.commands import REGISTRY
        from jarn.extensibility.skills import find_skill

        spec = spec_by_name(name)
        if spec is None:
            skill = None
            if self.runtime and self.runtime.skills:
                skill = find_skill(self.runtime.skills, name)
            if skill is not None:
                from jarn.controller.commands.meta import cmd_skill

                return cmd_skill(self, skill.name)
            return CommandResult(unknown_command(name))
        key = canonical_name(name) or spec.name
        handler = REGISTRY.get(key) or REGISTRY.get(spec.name)
        if handler is None:
            return CommandResult(unknown_command(name))
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

    def save_suggested_skill(self, suggestion: SuggestedSkill) -> tuple[bool, str]:
        return session_helpers.save_suggested_skill(self, suggestion)

    def abort_rollback(self) -> str:
        return session_helpers.abort_rollback(self)

    def can_rollback_turn(self) -> bool:
        return session_helpers.can_rollback_turn(self)

    def cancel_edit_note(self) -> str | None:
        return session_helpers.cancel_edit_note(self)

    def autocheckpoint_off_hint(self) -> str | None:
        return session_helpers.autocheckpoint_off_hint(self)


def _render_transcript(messages: list, *, max_msg_chars: int = 4_000) -> str:
    """Render LangChain messages to a compact text transcript for summarization.

    Each message's content is capped at ``max_msg_chars`` (suffixing
    ``" …[+N chars]"`` with the truncated count) so one oversized tool result can
    neither dominate nor overflow the summarizer prompt."""
    lines = []
    for msg in messages:
        role = getattr(msg, "type", None) or getattr(msg, "role", "?")
        content = _content_text(msg)
        if not content:
            continue
        if len(content) > max_msg_chars:
            dropped = len(content) - max_msg_chars
            content = f"{content[:max_msg_chars]} …[+{dropped} chars]"
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _trim_to_window(text: str, token_budget: int) -> str:
    """Trim a rendered transcript to ``token_budget`` tokens for the summarizer.

    Returns ``text`` unchanged when it already fits. Otherwise keeps head lines up
    to 30% of the budget (the original goal) and tail lines up to 70% (the latest
    state, which matters most), joined by a marker line carrying the count of
    dropped lines — so the summarizer still sees where the work began and where it
    stands even when the middle is too large to fit.

    Invariant: ``count_tokens(result) <= max(1, token_budget)`` for ANY input,
    and a non-empty ``text`` never trims to empty. A ``token_budget <= 0`` is
    treated as 1 (the caller clamps too, but this is the hard floor). Per-line
    accounting includes each line's ``"\n"`` separator and reserves the marker
    cost up front, but tokenization is not additive, so the assembled result is
    re-measured and shrunk in batches until it provably fits."""
    from jarn.memory.tokens import count_tokens

    # Hard floor: a zero/negative budget still has to keep at least 1 token of the
    # newest content, so a non-empty transcript never trims to nothing.
    budget = max(1, token_budget)
    if count_tokens(text) <= budget:
        return text
    lines = text.splitlines()

    def _marker(n: int) -> str:
        return f"…[trimmed {n} lines]…"

    # Reserve the marker's cost (worst-case dropped count) so it can never itself
    # push the result over budget; split the remainder 30% head / 70% tail.
    marker_reserve = count_tokens(_marker(len(lines)) + "\n")
    body_budget = max(0, budget - marker_reserve)
    head_budget = int(body_budget * 0.3)
    head: list[str] = []
    used = 0
    for line in lines:
        c = count_tokens(line + "\n")  # cost the separator, not just the line
        if used + c > head_budget:
            break
        head.append(line)
        used += c
    tail: list[str] = []
    used = 0
    for line in reversed(lines[len(head) :]):
        c = count_tokens(line + "\n")
        if used + c > body_budget - head_budget:
            break
        tail.append(line)
        used += c
    tail.reverse()

    def _assemble() -> str:
        dropped = len(lines) - len(head) - len(tail)
        return "\n".join([*head, _marker(dropped), *tail])

    # Re-measure the joined result and drop whole batches (tail first, then head)
    # until it fits — closes the gap left by non-additive tokenization.
    # Invariant: preserve the NEWEST state. Shrink the tail from its BEGINNING
    # (oldest tail lines) so the latest transcript lines — the whole point of the
    # 70% tail allocation — survive; only once the tail is empty do we shrink the
    # head from its END, keeping the earliest goal lines at the front.
    candidate = _assemble()
    while count_tokens(candidate) > budget and (head or tail):
        batch = 32
        if tail:
            del tail[:batch]
        else:
            del head[max(0, len(head) - batch) :]
        candidate = _assemble()
    if (head or tail) and count_tokens(candidate) <= budget:
        return candidate
    # No whole line fit head or tail (a single oversized line), or the budget is
    # below the marker: hard-cut by characters, keeping the NEWEST SUFFIX (the
    # newest-tail contract) — NOT the oldest prefix. Prefer prepending the trim
    # marker; drop the marker (never the content) if not even marker + 1 char fits,
    # so a non-empty input still yields non-empty content within budget.
    marker = "…[trimmed tail]…"
    if count_tokens(marker + text[-1:]) <= budget:
        lo, hi = 1, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if count_tokens(marker + text[-mid:]) <= budget:
                lo = mid
            else:
                hi = mid - 1
        return marker + text[-lo:]
    # Even marker + 1 char overflows: drop the marker and keep the largest newest
    # suffix that fits (at least 1 char, so non-empty stays non-empty).
    lo, hi = 1, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if count_tokens(text[-mid:]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return text[-lo:]


def _content_text(msg) -> str:
    content = getattr(msg, "content", msg)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content)
