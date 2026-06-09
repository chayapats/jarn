"""Session controller — the framework-agnostic brain behind the TUI.

Owns the runtime, permission engine, cost tracker, checkpointer, and the current
thread. Builds/rebuilds the deep agent, creates :class:`SessionDriver`s, and
handles built-in slash commands. Kept free of Textual imports so it is unit
testable on its own.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rich.markup import escape as _escape_markup

from jarn.agent.builder import JarnRuntime, build_runtime
from jarn.agent.checkpoint import CheckpointManager
from jarn.agent.session import Approver, SessionDriver
from jarn.config.schema import Config, PermissionMode
from jarn.cost import CostTracker
from jarn.extensibility.commands import format_help
from jarn.extensibility.mcp import load_mcp_tools
from jarn.memory import (
    SessionIndex,
    create_async_checkpointer,
    default_db_path,
    new_thread_id,
    write_jarn_md,
)
from jarn.permissions import PermissionEngine
from jarn.tui import palette

if TYPE_CHECKING:
    from jarn.memory import MemoryStore, RecallHit


@dataclass(slots=True)
class CommandResult:
    text: str
    rebuilt: bool = False
    clear_screen: bool = False
    quit: bool = False


class Controller:
    def __init__(
        self,
        config: Config,
        project_root: Path | None,
        *,
        project_trusted: bool = True,
    ) -> None:
        self.config = config
        self.project_root = project_root
        self.project_trusted = project_trusted
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
        # Per-server MCP health, populated by ensure_runtime: name -> "ok"/"error".
        self.mcp_health: dict[str, str] = {}
        self.mcp_errors: dict[str, str] = {}
        # Ordered model candidates for turn-level fallback: main + configured chain.
        main = config.resolved_main_model()
        self._candidates = ([main] if main else []) + list(config.routing.fallback)
        self._candidate_idx = 0
        self.runtime: JarnRuntime | None = None
        # Lifecycle hooks (built lazily from config); session_start fires once
        # the runtime is first ready, session_end on close.
        self._hooks_runner = None
        self._session_started = False

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
                )
            if self.runtime.warnings:
                if self.health not in ("error", "degraded"):
                    self.health = "degraded"
                if self.last_error is None:
                    self.last_error = self.runtime.warnings[0]
        if not self._session_started:
            self._fire_lifecycle("session_start")
            self._session_started = True
        return self.runtime

    def _hook_runner(self):
        """Lazily build the lifecycle :class:`HookRunner` (None if no hooks)."""
        if self._hooks_runner is None and self.config.hooks:
            from jarn.extensibility.hooks import HookRunner

            self._hooks_runner = HookRunner(
                hooks=self.config.hooks, cwd=self.project_root or Path.cwd()
            )
        return self._hooks_runner

    def _fire_lifecycle(self, event_name: str) -> None:
        runner = self._hook_runner()
        if runner is None:
            return
        import contextlib

        from jarn.extensibility.hooks import HookEvent

        with contextlib.suppress(Exception):
            runner.run(HookEvent(event_name))

    def _config(self) -> dict:
        return {"configurable": {"thread_id": self.thread_id}}

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

    async def compact(self) -> str:
        """Summarize the current thread and continue in a fresh thread seeded
        with the summary (the richer ``/compact``). Returns the summary text."""
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
            )

        self.new_thread()
        from langchain_core.messages import HumanMessage

        await rt.agent.aupdate_state(
            self._config(),
            {"messages": [HumanMessage(content=f"[Summary of prior conversation]\n{summary}")]},
        )
        return summary

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
        return SessionDriver(
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
        )

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
        """Inject per-turn memory recall ahead of the user's visible prompt."""
        from jarn.memory import recall_block

        block = recall_block(
            user_input,
            k=3,
            project_root=self.project_root,
            include_project=self.project_trusted,
        )
        if not block:
            return user_input
        return f"{block}\n\n---\n\n{user_input}"

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

    def context_status(self) -> tuple[int, int, float] | None:
        """(tokens, window, fraction) of the main model's context, or None when
        unknown — no tokens recorded yet, or the model's window can't be resolved
        (so the toolbar hides the gauge rather than dividing by a guess)."""
        tokens = self.tracker.context_tokens
        if tokens <= 0:
            return None
        from jarn.cost.pricing import context_window

        window = context_window(self.config.resolved_main_model() or "")
        if window <= 0:
            return None
        return tokens, window, tokens / window

    def should_auto_compact(self) -> bool:
        """True when auto-compaction is on and the context gauge has crossed the
        configured threshold, so the turn loop can compact transparently."""
        if not self.config.context.auto_compact:
            return False
        status = self.context_status()
        if status is None:
            return False
        _tokens, _window, fraction = status
        return fraction * 100 >= self.config.context.compact_at_pct

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
            "error": f"[{palette.C_ERROR}]✗[/{palette.C_ERROR}] ",
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
        handler = getattr(self, f"_cmd_{name.replace('-', '_')}", None)
        if handler is None:
            return CommandResult(f"Unknown command: /{name}. Try /help.")
        return handler(args)

    def _cmd_help(self, args: str) -> CommandResult:
        custom = self.runtime.commands if self.runtime else None
        return CommandResult(
            format_help(
                custom,
                custom_description=lambda c: getattr(c, "description", ""),
            )
        )

    def _cmd_model(self, args: str) -> CommandResult:
        if not args.strip():
            return CommandResult(f"Current model: {self.config.resolved_main_model()}")
        self.config.routing.main = args.strip()
        self.config.default_model = args.strip()
        self.runtime = None  # force rebuild on next turn
        return CommandResult(f"Model set to {args.strip()} (rebuilding).", rebuilt=True)

    def _cmd_mode(self, args: str) -> CommandResult:
        if not args.strip():
            return CommandResult(f"Current mode: {self.config.permission_mode.value}")
        try:
            mode = PermissionMode(args.strip())
        except ValueError:
            valid = ", ".join(m.value for m in PermissionMode)
            return CommandResult(f"Unknown mode. Choose one of: {valid}")
        # Route through apply_mode so the untrusted-floor clamp applies here too.
        applied = self.apply_mode(mode.value)
        if applied != mode.value:
            return CommandResult(
                f"Project untrusted — mode clamped to {applied}. "
                "Run `jarn trust` to unlock other modes. (rebuilding)",
                rebuilt=True,
            )
        return CommandResult(f"Permission mode set to {applied} (rebuilding).", rebuilt=True)

    def _cmd_sandbox(self, args: str) -> CommandResult:
        current = self.config.execution.backend
        if not args.strip():
            return CommandResult(
                f"Execution backend: {current} · isolation: {self.isolation_level()}. "
                "Use /sandbox docker|on|off."
            )
        # Untrusted projects can't weaken isolation at runtime (defence in depth
        # alongside the untrusted-mode floor); viewing it (no-arg) stays allowed.
        if not self.project_trusted:
            return CommandResult(
                "Project untrusted — execution backend is locked. "
                "Run `jarn trust` to change it."
            )
        choice = args.strip().lower()
        if choice == "docker":
            self.config.execution.backend = "docker"
        elif choice in ("on", "sandbox"):
            self.config.execution.backend = "sandbox"
        elif choice in ("off", "local"):
            self.config.execution.backend = "local"
        else:
            return CommandResult("Usage: /sandbox docker|on|off")
        self.runtime = None  # backend changes require a rebuild
        return CommandResult(
            f"Execution backend set to {self.config.execution.backend} (rebuilding). "
            "A sandbox/docker backend requires an available runtime; fails closed "
            "unless execution.allow_local_fallback is true.",
            rebuilt=True,
        )

    def _cmd_profile(self, args: str) -> CommandResult:
        from jarn.config.loader import ConfigError
        from jarn.config.profiles import PROFILE_NAMES, resolve_effective_profile

        available = ", ".join(sorted(PROFILE_NAMES))
        if not args.strip():
            current = self.config.policy.profile or "none"
            return CommandResult(
                f"Current policy profile: {current}. Available: {available}."
            )
        choice = args.strip()
        # resolve_effective_profile applies the chosen profile (raising on an
        # unknown name) AND clamps untrusted sessions to the floor — a single
        # apply path, so the REPL can never loosen an untrusted session.
        try:
            effective = resolve_effective_profile(
                self.config, project_trusted=self.project_trusted, cli_profile=choice
            )
        except ConfigError:
            return CommandResult(f"Unknown profile {choice!r}. Choose one of: {available}")
        self.config.policy.profile = effective or ""
        self.engine.mode = self.config.permission_mode
        self.runtime = None  # mode/sandbox/web-tools changes require a rebuild
        suffix = ""
        if effective != choice:
            suffix = f" (clamped to {effective} — project untrusted)"
        return CommandResult(
            f"Policy profile set to {effective}{suffix} (rebuilding).", rebuilt=True
        )

    def _cmd_mcp(self, args: str) -> CommandResult:
        """Show configured MCP servers with per-server health + last error.

        Usage: ``/mcp`` or ``/mcp status``. Reads the live health/error maps
        populated by :meth:`ensure_runtime` (falling back to each server's
        ``health`` field) so the user can see at a glance which stdio/HTTP MCP
        servers came up and which failed (with the reason)."""
        sub = args.strip().lower()
        if sub and sub != "status":
            return CommandResult("Usage: /mcp [status]")
        servers = self.config.mcp_servers
        if not servers:
            return CommandResult("No MCP servers configured.")
        glyph = {
            "ok": f"[{palette.C_SUCCESS}]●[/{palette.C_SUCCESS}]",
            "error": f"[{palette.C_ERROR}]✗[/{palette.C_ERROR}]",
        }
        lines = ["[b]MCP servers[/b]"]
        for server in servers:
            health = self.mcp_health.get(server.name, server.health or "unknown")
            mark = glyph.get(health, f"[{palette.C_DIM}]○[/{palette.C_DIM}]")
            transport = getattr(server, "transport", "") or ""
            detail = f" [dim]({_escape_markup(transport)})[/dim]" if transport else ""
            line = f"  {mark} [cyan]{_escape_markup(server.name)}[/cyan]{detail} — {health}"
            err = self.mcp_errors.get(server.name)
            if err:
                line += f"\n      [dim]last error: {_escape_markup(err)}[/dim]"
            lines.append(line)
        if not self.runtime:
            lines.append(
                f"[{palette.C_DIM}]Health is populated after the first turn "
                f"loads the servers.[/{palette.C_DIM}]"
            )
        return CommandResult("\n".join(lines))

    def _cmd_trust(self, args: str) -> CommandResult:
        """Trust the current project root and lift the untrusted review-only floor.

        Persists the trust grant via :class:`~jarn.config.trust.TrustStore`,
        flips ``project_trusted`` on, re-resolves the effective policy profile so
        the review-only clamp no longer applies, and forces a runtime rebuild.

        Honesty note: capability-granting keys (``hooks``/``mcp_servers``/
        ``providers``/…) were stripped from the in-memory config at LOAD time for
        an untrusted project; lifting the floor here cannot retroactively
        re-inject them, so we tell the user they take effect on the next launch.
        """
        if self.project_root is None:
            return CommandResult("No project root — nothing to trust.")
        if self.project_trusted:
            return CommandResult("This project is already trusted.")

        from jarn.config import paths
        from jarn.config.loader import _read_yaml
        from jarn.config.trust import (
            TrustStore,
            fingerprint,
            project_dangerous,
        )

        ppath = paths.project_config_path(self.project_root)
        danger = project_dangerous(_read_yaml(ppath)) if ppath else {}
        store = TrustStore.load()
        store.trust(self.project_root, fingerprint(danger))
        store.save()

        self.project_trusted = True
        # RELOAD the config from disk now that the project is trusted. A simple
        # re-resolve can't fix this: the launch-time untrusted floor already
        # OVERWROTE config.permission_mode with the review-only clamp (plan) and
        # the loader had stripped the project's capability keys. Reloading with
        # project_trusted=True restores both the configured mode and the project
        # hooks / MCP / providers, so the rebuilt runtime is genuinely unlocked.
        from jarn.config.loader import load_config
        from jarn.config.profiles import resolve_effective_profile

        self.config = load_config(project_root=self.project_root, project_trusted=True)
        resolve_effective_profile(self.config, project_trusted=True, cli_profile=None)
        self.engine.mode = self.config.permission_mode
        self.engine.rules = self.config.permissions
        self.runtime = None  # rebuild so trust-gated state is reapplied

        note = ""
        if danger:
            note = (
                "\n[dim]Project hooks / MCP servers / providers from "
                ".jarn/config.yaml are now active.[/dim]"
            )
        return CommandResult(
            f"Trusted {self.project_root}. Review-only floor lifted; "
            f"mode is now {self.config.permission_mode.value} (rebuilding).{note}",
            rebuilt=True,
        )

    def _cmd_cost(self, args: str) -> CommandResult:
        t = self.tracker
        lines = [f"[b]Session usage[/b] — {t.summary_line()}", f"status: {t.status().value}"]
        for model, usage in t.per_model.items():
            lines.append(
                f"  {_escape_markup(model)}: ${usage.cost_usd:.4f} · {usage.total_tokens:,} tok"
            )
        return CommandResult("\n".join(lines))

    def _cmd_compact(self, args: str) -> CommandResult:
        return CommandResult(
            "Context auto-compaction is "
            + ("on" if self.config.context.auto_compact else "off")
            + f" (at {self.config.context.compact_at_pct}% full). "
            "Use /clear to start a fresh thread."
        )

    def _cmd_clear(self, args: str) -> CommandResult:
        self.new_thread()
        return CommandResult("Started a fresh conversation.", clear_screen=True)

    def _cmd_sessions(self, args: str) -> CommandResult:
        sessions = self.sessions.list()
        if not sessions:
            return CommandResult("No previous sessions.")
        lines = ["[b]Recent sessions[/b] [dim](use /resume to pick one)[/dim]"]
        for s in sessions:
            marker = "→ " if s.thread_id == self.thread_id else "  "
            lines.append(
                f"{marker}{s.updated_human}  {_escape_markup(s.title)}  "
                f"[dim]{s.thread_id[:8]}[/dim]"
            )
        return CommandResult("\n".join(lines))

    def _cmd_skills(self, args: str) -> CommandResult:
        if not self.runtime or not self.runtime.skills:
            return CommandResult("No skills loaded.")
        lines = ["[b]Skills[/b]"]
        for s in self.runtime.skills.values():
            trig = "manual" if s.is_manual else "auto"
            lines.append(
                f"  [cyan]{_escape_markup(s.name)}[/cyan] "
                f"([dim]{trig}, {s.scope}[/dim]) — {_escape_markup(s.description)}"
            )
        return CommandResult("\n".join(lines))

    def _cmd_memory(self, args: str) -> CommandResult:
        raw = args.strip()
        if not raw:
            return self._memory_list()
        try:
            parts = shlex.split(raw)
        except ValueError as exc:
            return CommandResult(f"Could not parse /memory: {exc}")
        if not parts:
            return self._memory_list()

        subcommand = parts[0].lower()
        if subcommand == "search":
            query = raw[len(parts[0]):].strip()
            return self._memory_search(query)
        if subcommand == "show":
            return self._memory_show(parts[1:])
        if subcommand == "add":
            return self._memory_add(parts[1:])
        if subcommand == "update":
            return self._memory_update(parts[1:])
        if subcommand == "delete":
            return self._memory_delete(parts[1:])
        return CommandResult(
            "Usage: /memory [search|show|add|update|delete] ...\n"
            "Examples:\n"
            "  /memory search pytest\n"
            "  /memory add project project \"Test style\" \"Use pytest\" \"Prefer parametrized tests.\"\n"
            "  /memory show project test-style\n"
            "  /memory delete global test-style"
        )

    def _memory_list(self) -> CommandResult:
        stores = self._memory_read_stores()
        lines = ["[b]Long-term memory[/b] [dim](use /memory search <query>)[/dim]"]
        found = False
        for scope, store in stores:
            memories = store.load_all()
            if not memories:
                continue
            found = True
            lines.append(f"\n[b]{scope}[/b]")
            for memory in memories:
                lines.append(
                    f"  [cyan]{_escape_markup(memory.name)}[/cyan] "
                    f"([dim]{_escape_markup(memory.type)}[/dim]) — "
                    f"{_escape_markup(memory.description)}"
                )
        if not self.project_trusted and self.project_root is not None:
            lines.append("\n[dim]Project memory skipped until this project is trusted (`jarn trust`).[/dim]")
        if not found:
            return CommandResult(
                "No long-term memories yet. Try /memory add project project "
                '"Name" "Description" "Body".'
            )
        return CommandResult("\n".join(lines))

    def _memory_search(self, query: str) -> CommandResult:
        if not query:
            return CommandResult("Usage: /memory search <query>")
        hits = self._memory_search_hits(query, k=5)
        if not hits:
            return CommandResult(f"No memories matched: {query!r}")
        lines = [f"[b]Recall for[/b] {_escape_markup(query)!r}"]
        for scope, hit in hits:
            lines.append(
                f"  [cyan]{_escape_markup(hit.memory.name)}[/cyan] "
                f"[dim]({scope}, {hit.score:.2f})[/dim] — "
                f"{_escape_markup(hit.memory.description)}"
            )
            if hit.memory.body:
                lines.append(_format_memory_body(hit.memory.body))
        return CommandResult("\n".join(lines))

    def _memory_show(self, parts: list[str]) -> CommandResult:
        if not parts:
            return CommandResult("Usage: /memory show [global|project] <name-or-slug>")
        explicit_scope = parts[0].lower() in ("global", "project")
        if explicit_scope:
            scope = parts[0].lower()
            name = " ".join(parts[1:]).strip()
            store, error = self._memory_store_for_scope(scope, write=False)
            if error or store is None:
                return CommandResult(error or "Memory store unavailable.")
            candidates = [(scope, store)]
        else:
            name = " ".join(parts).strip()
            candidates = list(reversed(self._memory_read_stores()))
        if not name:
            return CommandResult("Usage: /memory show [global|project] <name-or-slug>")
        for scope, store in candidates:
            memory = store.get(name)
            if memory is None:
                continue
            lines = [
                f"[b]{_escape_markup(memory.name)}[/b] [dim]({scope}, {memory.type})[/dim]",
                _escape_markup(memory.description),
            ]
            if memory.body:
                lines.append(_format_memory_body(memory.body))
            return CommandResult("\n".join(lines))
        return CommandResult(f"No memory found: {name!r}")

    def _memory_add(self, parts: list[str]) -> CommandResult:
        from jarn.memory.store import MEMORY_TYPES, Memory

        scope, idx = self._memory_scope_from_parts(parts)
        remaining = parts[idx:]
        if len(remaining) < 3:
            return CommandResult(
                "Usage: /memory add [global|project] <type> <name> <description> [body]"
            )
        mem_type, name, description = remaining[:3]
        if mem_type not in MEMORY_TYPES:
            return CommandResult(f"Unknown memory type {mem_type!r}; choose one of: {', '.join(MEMORY_TYPES)}")
        store, error = self._memory_store_for_scope(scope, write=True)
        if error or store is None:
            return CommandResult(error or "Memory store unavailable.")
        body = " ".join(remaining[3:]).strip() or description
        path = store.save(Memory(name=name, description=description, body=body, type=mem_type))
        return CommandResult(f"Saved {scope} memory: {path.name}", rebuilt=False)

    def _memory_update(self, parts: list[str]) -> CommandResult:
        scope, idx = self._memory_scope_from_parts(parts)
        remaining = parts[idx:]
        if len(remaining) < 2:
            return CommandResult("Usage: /memory update [global|project] <name-or-slug> <description> [body]")
        name, description = remaining[:2]
        store, error = self._memory_store_for_scope(scope, write=True)
        if error or store is None:
            return CommandResult(error or "Memory store unavailable.")
        memory = store.get(name)
        if memory is None:
            return CommandResult(f"No {scope} memory found: {name!r}")
        body = " ".join(remaining[2:]).strip() or memory.body
        memory.description = description
        memory.body = body
        path = store.save(memory)
        return CommandResult(f"Updated {scope} memory: {path.name}")

    def _memory_delete(self, parts: list[str]) -> CommandResult:
        scope, idx = self._memory_scope_from_parts(parts)
        name = " ".join(parts[idx:]).strip()
        if not name:
            return CommandResult("Usage: /memory delete [global|project] <name-or-slug>")
        store, error = self._memory_store_for_scope(scope, write=True)
        if error or store is None:
            return CommandResult(error or "Memory store unavailable.")
        if not store.delete(name):
            return CommandResult(f"No {scope} memory found: {name!r}")
        return CommandResult(f"Deleted {scope} memory: {name}")

    def _memory_read_stores(self) -> list[tuple[str, MemoryStore]]:
        from jarn.memory import MemoryStore

        stores = [("global", MemoryStore.global_store())]
        project = MemoryStore.project_store(self.project_root)
        if project and self.project_trusted:
            stores.append(("project", project))
        return stores

    def _memory_search_hits(self, query: str, *, k: int) -> list[tuple[str, RecallHit]]:
        from jarn.memory import VectorIndex
        from jarn.memory.store import slugify

        deduped: dict[str, tuple[str, RecallHit]] = {}
        for scope, store in self._memory_read_stores():
            if not store.root.is_dir():
                continue
            for hit in VectorIndex(store).search(query, k=k):
                key = slugify(hit.memory.name)
                existing = deduped.get(key)
                if existing is None or hit.score > existing[1].score:
                    deduped[key] = (scope, hit)
        return sorted(deduped.values(), key=lambda item: item[1].score, reverse=True)[:k]

    def _memory_scope_from_parts(self, parts: list[str]) -> tuple[str, int]:
        if parts and parts[0].lower() in ("global", "project"):
            return parts[0].lower(), 1
        return ("project" if self.project_root is not None else "global"), 0

    def _memory_store_for_scope(
        self,
        scope: str,
        *,
        write: bool,
    ) -> tuple[MemoryStore | None, str | None]:
        from jarn.memory import MemoryStore

        if scope == "global":
            return MemoryStore.global_store(), None
        if scope != "project":
            return None, "Scope must be 'global' or 'project'."
        if not self.project_trusted:
            return None, "Project memory is disabled until this project is trusted (`jarn trust`)."
        store = MemoryStore.project_store(self.project_root)
        if store is None:
            target = "write" if write else "read"
            return None, f"No project root found; cannot {target} project memory."
        return store, None

    def _cmd_permissions(self, args: str) -> CommandResult:
        r = self.config.permissions
        session_allow = self.engine._all_allow()[len(r.allow) :]
        lines = [
            f"[b]Mode[/b]: {self.config.permission_mode.value}",
            f"[b]Allow[/b]: {', '.join(_escape_markup(p) for p in r.allow) or '(none)'}",
            f"[b]Deny[/b]: {', '.join(_escape_markup(p) for p in r.deny) or '(none)'}",
            f"[b]Session-allow[/b]: "
            f"{', '.join(_escape_markup(p) for p in session_allow) or '(none)'}",
        ]
        return CommandResult("\n".join(lines))

    def _cmd_init(self, args: str) -> CommandResult:
        try:
            path = write_jarn_md(self.project_root, overwrite=args.strip() == "--force")
        except FileExistsError as exc:
            return CommandResult(f"{exc} (use /init --force to overwrite)")
        return CommandResult(f"Created {path}. Edit it to give J.A.R.N. project context.")

    def _cmd_undo(self, args: str) -> CommandResult:
        """Revert the last agent turn's file changes via the checkpoint stack.

        Capturing the current state as a redo-point first guarantees that undo
        is itself reversible: the user can always /redo to get back here.
        """
        result = self.checkpoint_manager.undo()
        if result.ok:
            return CommandResult(f"Undone. {result.message}")
        return CommandResult(f"Cannot undo: {result.message}")

    def _cmd_redo(self, args: str) -> CommandResult:
        """Re-apply the most recently undone agent turn's file changes."""
        result = self.checkpoint_manager.redo()
        if result.ok:
            return CommandResult(f"Redone. {result.message}")
        return CommandResult(f"Cannot redo: {result.message}")

    def _cmd_checkpoints(self, args: str) -> CommandResult:
        """List recent auto-checkpoints available for /undo."""
        if not self.checkpoint_manager.enabled:
            return CommandResult(
                "Autocheckpoint is disabled. "
                "Set git.autocheckpoint: true in your config to enable /undo."
            )
        if not self.checkpoint_manager.is_repo:
            return CommandResult("Not a git repository — checkpoints are unavailable.")
        entries = self.checkpoint_manager.list()
        if not entries:
            return CommandResult(
                "No checkpoints yet. "
                "Checkpoints are taken automatically at the start of each agent turn."
            )
        lines = ["[b]Checkpoints[/b] [dim](most recent first)[/dim]"]
        for i, entry in enumerate(entries):
            marker = "→ " if i == 0 else "  "
            lines.append(
                f"{marker}[dim]{entry.sha[:12]}[/dim] {_escape_markup(entry.label)}"
            )
        return CommandResult("\n".join(lines))

    def _cmd_map(self, args: str) -> CommandResult:
        """Build and display the ranked repo map.

        Supports an optional focus substring to bias ranking, and the keyword
        ``--refresh`` to bypass the cache and recompute.

        Usage: /map [focus] [--refresh]
        """
        from jarn.agent.repomap import build_repo_map

        raw = args.strip()
        refresh = False
        if "--refresh" in raw:
            raw = raw.replace("--refresh", "").strip()
            refresh = True

        focus = raw.strip()
        root = self.project_root or Path.cwd()
        budget = self.config.context.repo_map_tokens

        if refresh:
            # Bust the cache by removing any matching cache files for this root.
            _bust_repomap_cache(root)

        try:
            text = build_repo_map(root, token_budget=budget, focus=focus)
        except Exception as exc:  # noqa: BLE001
            return CommandResult(f"Error building repo map: {exc}")
        if not text.strip():
            return CommandResult("No source files found in the project.")
        return CommandResult(text)

    def _cmd_wiki(self, args: str) -> CommandResult:
        """List or search the wiki knowledge base.

        Usage::

            /wiki              — list all pages in the index
            /wiki list         — same as above
            /wiki search <q>   — grep pages for <q> (case-insensitive)
        """
        from jarn.memory.wiki import WikiStore

        store = WikiStore.build(self.project_root)

        raw = args.strip()
        if not raw or raw.lower() == "list":
            index = store.index_text()
            if not index.strip():
                return CommandResult(
                    "No wiki pages yet. "
                    "Enable wiki with `wiki: {enabled: true}` in your config, "
                    "then let the agent write pages with `wiki_write`."
                )
            return CommandResult(index)

        parts = raw.split(None, 1)
        subcmd = parts[0].lower()
        if subcmd == "search":
            query = parts[1].strip() if len(parts) > 1 else ""
            if not query:
                return CommandResult("Usage: /wiki search <query>")
            results = store.search(query)
            if not results:
                return CommandResult(f"No wiki pages matched {query!r}.")
            lines = [f"[b]Wiki search:[/b] {_escape_markup(query)!r}"]
            for slug, matched in results:
                lines.append(f"\n  [cyan]{_escape_markup(slug)}[/cyan]")
                for line in matched[:5]:
                    lines.append(f"    {_escape_markup(line)}")
                if len(matched) > 5:
                    lines.append(f"    [dim]… ({len(matched) - 5} more)[/dim]")
            return CommandResult("\n".join(lines))

        return CommandResult("Usage: /wiki [search <q>|list]")

    def _cmd_quit(self, args: str) -> CommandResult:
        return CommandResult("Bye.", quit=True)


def _bust_repomap_cache(root: Path) -> None:
    """Remove cached repo-map files for *root* (best-effort, never raises).

    Since the cache key embeds both root and the file-set signature, the
    simplest invalidation strategy is to wipe all .json files in the repomap
    cache dir — it's cheap to rebuild and avoids reimplementing the key
    derivation here.
    """
    import contextlib

    from jarn.config import paths as _paths

    cache_dir = _paths.cachedir() / "repomap"
    if not cache_dir.is_dir():
        return
    with contextlib.suppress(Exception):
        for f in cache_dir.iterdir():
            if f.suffix == ".json":
                with contextlib.suppress(Exception):
                    f.unlink()


def _format_memory_body(body: str) -> str:
    escaped = _escape_markup(body.strip())
    return "\n".join(f"    {line}" for line in escaped.splitlines())


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
