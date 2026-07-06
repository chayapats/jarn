"""REPL command dispatch and REPL-only handlers."""
# mypy: ignore-errors

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from rich.markdown import Markdown
from rich.markup import escape as _rich_escape

from jarn.agent.local_backend import CancellableLocalShellBackend
from jarn.repl import turn as repl_turn
from jarn.repl.turn import _apply_mode_ref, _apply_model_ref
from jarn.tui import palette

#: Plan-checklist glyphs — the SINGLE source shared by the live in-turn region
#: (``app._live_todos_ansi``) and the committed end-of-turn render
#: (``_render_todos``) so the styling never drifts between the two.
_TODO_GLYPHS = {
    "completed": "[#3ee07a]✔[/#3ee07a]",
    "in_progress": "[#22d3ee]◐[/#22d3ee]",
    "pending": "[#7c8f94]☐[/#7c8f94]",
}


def _todo_item_line(todo: dict, truncate: int | None) -> str:
    """One Rich-markup checklist line: ``  <glyph> <content>`` (completed dimmed).

    ``truncate`` (the live region's terminal width) bounds the content to a single
    line so a long todo can't wrap and blow the height budget; ``None`` (committed
    render) leaves it to wrap freely, preserving the pre-existing behaviour."""
    status = todo.get("status", "pending")
    glyph = _TODO_GLYPHS.get(status, _TODO_GLYPHS["pending"])
    content = str(todo.get("content", ""))
    if truncate is not None:
        limit = max(8, truncate - 4)  # 2-space indent + glyph + space
        if len(content) > limit:
            content = content[: limit - 1] + "…"
    content = _rich_escape(content)
    if status == "completed":
        content = f"[{palette.C_DIM}]{content}[/{palette.C_DIM}]"
    return f"  {glyph} {content}"


def format_todos(todos: list[dict], width: int, *, cap: int | None = None) -> list[str]:
    """Render a plan checklist to Rich-markup lines: ``["⏺ Todos", <item>, …]``.

    Shared by BOTH the live in-turn region and the committed end-of-turn render so
    glyphs and layout stay identical.

    ``cap`` (live region only) bounds the body to ``cap`` lines so a long plan
    can't push the input off-screen: completed items collapse to one ``✔ N done``
    summary, the in-progress + upcoming items fill the remaining budget, and any
    overflow is elided behind a ``… +N more`` line. ``cap is None`` (committed
    render) shows every item, unwrapped, exactly as before.
    """
    header = f"[{palette.C_TOOL}]⏺[/{palette.C_TOOL}] [bold]Todos[/bold]"
    lines = [header]
    trunc = width if cap is not None else None
    if cap is None or len(todos) <= cap:
        lines.extend(_todo_item_line(t, trunc) for t in todos)
        return lines
    # Windowed live block: keep it focused on what is happening *now*.
    done = [t for t in todos if t.get("status") == "completed"]
    tail = [t for t in todos if t.get("status") != "completed"]  # in-progress + pending
    budget = cap
    if done:
        lines.append(
            f"  {_TODO_GLYPHS['completed']} [{palette.C_DIM}]{len(done)} done[/{palette.C_DIM}]"
        )
        budget -= 1
    if len(tail) > budget:
        show = max(1, budget - 1)  # reserve a line for the "… +N more" summary
        lines.extend(_todo_item_line(t, trunc) for t in tail[:show])
        hidden = len(tail) - show
        lines.append(f"  [{palette.C_DIM}]… +{hidden} more[/{palette.C_DIM}]")
    else:
        lines.extend(_todo_item_line(t, trunc) for t in tail)
    return lines


class CommandMixin:
    """Slash-command dispatch and REPL-only command handlers."""

    async def _command(self, name: str, args: str) -> None:
        c = self.console
        # `/config` with no args opens the interactive arrow-key settings panel;
        # `/config get|set …` still routes to the controller as text below.
        if name == "config" and not args.strip():
            self._open_config()
            return
        await self._ensure_extensions()
        rt = self.controller.runtime
        if rt and name in rt.commands:
            self._last_tool_outputs = []
            await repl_turn._run_turn(
                c, self.controller, rt.commands[name].render(args), self._ask,
                pick=self._pick_approval, view=self._view_full_diff,
                edit=self._edit_before_apply,
                live_sink=self._set_stream, spinner=False,
                tool_sink=self._last_tool_outputs,
                token_sink=self._count_stream_chars,
                todos_sink=self._on_todos_live,
            )
            await self._render_todos()
            self._maybe_autocheckpoint_hint()
            return
        if name == "compact":
            sub = args.strip().lower()
            if sub == "status" or sub:
                result = self.controller.handle_command(name, args)
                c.print(result.text)
                return
            await self._cmd_compact()
            return
        if name in ("commit", "review"):
            await self._cmd_git_seed(name)
            return
        if name == "expand":  # same as Ctrl+O — reliable even if the key is eaten
            self._open_pager()
            return
        if name == "resume":
            await self._resume_picker()
            return
        if name == "rewind":
            await self._rewind_picker()
            return
        if name == "queue":
            await self._cmd_queue(args)
            return
        if name == "abort":
            # Stop the running turn AND revert its edits in one action. When idle
            # there is no turn to abort — don't silently undo the last one; point
            # at /undo instead.
            if not self._busy():
                c.print(
                    f"[{palette.C_DIM}]Nothing to abort — no turn is running. "
                    f"Use /undo to revert the last turn's edits.[/{palette.C_DIM}]"
                )
                return
            self._cancel_turn()
            # Settle any in-flight turn-start snapshot BEFORE rolling back. The
            # cancelled turn detaches its snapshot fire-and-forget (session.py
            # _detach_pending_snapshot), and on a large repo it may still be
            # building its tree OFF the checkpoint lock. Without this await,
            # abort_rollback's undo() would take the lock first and pop the PREVIOUS
            # turn's checkpoint (this turn's isn't pushed yet) — an extra-turn-back
            # over-revert. Awaiting guarantees this turn's checkpoint is on the
            # stack, so the rollback reverts exactly this turn.
            await self.controller.settle_snapshot()
            # abort_rollback runs a blocking git restore; offload it so the event
            # loop stays responsive.
            c.print(await asyncio.to_thread(self.controller.abort_rollback))
            return
        if name == "key":
            await self._cmd_key(args)
            return
        if name == "model" and args.strip() in ("refresh", "list"):
            await self._refresh_models()
            return
        if name == "theme":
            await self._cmd_theme(args)
            return
        if name in ("model", "mode") and not args.strip():
            await self._pick_model_or_mode(name)
            return
        if (
            name == "mode"
            and args.strip() == "yolo"
            and self.controller.config.permission_mode.value != "yolo"
            and not await self._confirm_yolo()
        ):
            c.print(f"[{palette.C_DIM}]yolo cancelled — mode unchanged.[/{palette.C_DIM}]")
            return
        if name in ("undo", "redo"):
            # A snapshot detached by an Esc-cancelled turn may still be building its
            # tree off the checkpoint lock; settle it so undo()/redo() mutate a
            # consistent stack and don't revert an extra turn back (same race the
            # /abort path guards above). Cheap no-op when nothing is pending.
            await self.controller.settle_snapshot()
        result = self.controller.handle_command(name, args)
        if result.clear_screen:
            self._clear_scrollback()
        c.print(result.text)
        if result.rebuilt:
            self.controller.runtime = None
        if result.quit and self.app is not None:
            self.app.exit()

    async def _cmd_git_seed(self, which: str) -> None:
        """`/commit` and `/review`: gather the diff and seed an agent turn.

        The diff is embedded in the prompt so the agent skips a tool round-trip.
        ``/commit`` then drives a real ``git commit`` through the normal approval
        path; ``/review`` is a read-only review.
        """
        from jarn.agent.git_commands import commit_prompt, gather_diff, review_prompt

        c = self.console
        root = self.controller.project_root or Path(".")
        diff = await asyncio.to_thread(gather_diff, root)
        if not diff.is_repo:
            c.print(f"[{palette.C_ERROR}]Not a git repository.[/{palette.C_ERROR}]")
            return
        prompt = commit_prompt(diff) if which == "commit" else review_prompt(diff)
        if prompt is None:
            what = "commit" if which == "commit" else "review"
            c.print(
                f"[{palette.C_DIM}]Nothing to {what} — the working tree is clean."
                f"[/{palette.C_DIM}]"
            )
            return
        self._last_tool_outputs = []
        await repl_turn._run_turn(
            c, self.controller, prompt, self._ask,
            pick=self._pick_approval, view=self._view_full_diff,
            edit=self._edit_before_apply,
            live_sink=self._set_stream, spinner=False,
            tool_sink=self._last_tool_outputs,
            token_sink=self._count_stream_chars,
            todos_sink=self._on_todos_live,
        )
        await self._render_todos()
        self._maybe_autocheckpoint_hint()

    async def _cmd_key(self, args: str) -> None:
        """`/key`: set/replace the API key for the current provider in-session.

        With no argument we prompt for the key (kept off the input history /
        scrollback by capturing it through the region prompt rather than the
        echoed command line). The secret goes to the OS keychain and the
        provider's config is pointed at a ``keychain:jarn/<provider>`` reference;
        the runtime is dropped so the next turn rebuilds with the new key."""
        c = self.console
        provider = self.controller.current_provider()
        if not provider:
            c.print(
                f"[{palette.C_ERROR}]No active provider — configure a model first "
                f"with /model or run jarn setup.[/{palette.C_ERROR}]"
            )
            return
        inline = args.strip()
        if inline:
            # Inline keys are convenient but land in shell/REPL history — warn.
            c.print(
                f"[{palette.C_WARN}]Heads up: an inline key is visible in your "
                f"scrollback/history. Prefer /key with no argument next time."
                f"[/{palette.C_WARN}]"
            )
            secret = inline
        else:
            secret = await self._ask(f"Paste the {provider} API key (Enter to cancel): ")
        if not secret.strip():
            c.print(f"[{palette.C_DIM}]No key entered — unchanged.[/{palette.C_DIM}]")
            return
        result = self.controller.set_provider_key(secret, provider=provider)
        c.print(result.text)
        if result.rebuilt:
            self.controller.runtime = None

    # -- queue --------------------------------------------------------------

    async def _cmd_queue(self, args: str) -> None:
        parts = args.split()
        sub = parts[0].lower() if parts else ""
        c = self.console
        q = self._input_queue
        if not sub:
            items = q.list()
            if not items:
                c.print(f"[{palette.C_DIM}]Queue empty.[/{palette.C_DIM}]")
                return
            for i, item in enumerate(items, 1):
                c.print(f"  {i}. {_rich_escape(item.display)}")
            return
        if sub == "clear":
            n = q.clear()
            c.print(f"[{palette.C_NOTICE}]Cleared {n} queued line(s).[/{palette.C_NOTICE}]")
            return
        if sub == "cancel" and len(parts) >= 2:
            try:
                idx = int(parts[1])
            except ValueError:
                c.print(f"[{palette.C_ERROR}]Usage: /queue cancel <n>[/{palette.C_ERROR}]")
                return
            removed = q.cancel(idx)
            if removed is None:
                c.print(f"[{palette.C_ERROR}]No item at {idx}.[/{palette.C_ERROR}]")
            else:
                c.print(
                    f"[{palette.C_NOTICE}]Removed: {_rich_escape(removed.display)}"
                    f"[/{palette.C_NOTICE}]"
                )
            return
        if sub == "move" and len(parts) >= 3:
            try:
                fr, to = int(parts[1]), int(parts[2])
            except ValueError:
                c.print(
                    f"[{palette.C_ERROR}]Usage: /queue move <from> <to>[/{palette.C_ERROR}]"
                )
                return
            if not q.move(fr, to):
                c.print(f"[{palette.C_ERROR}]Invalid queue indices.[/{palette.C_ERROR}]")
            else:
                c.print(f"[{palette.C_NOTICE}]Moved item {fr} → {to}.[/{palette.C_NOTICE}]")
            return
        c.print(
            f"[{palette.C_ERROR}]Usage: /queue [clear|cancel <n>|move <from> <to>]"
            f"[/{palette.C_ERROR}]"
        )

    # -- resume -------------------------------------------------------------

    async def _resume_picker(self) -> None:
        from jarn.memory.sessions import SessionInfo

        sessions = self.controller.sessions.list()
        if not sessions:
            self.console.print(f"[{palette.C_DIM}]No previous sessions.[/{palette.C_DIM}]")
            return
        options: list[tuple[str, SessionInfo | None]] = [
            (
                f"{s.updated_human}  {s.title}  {s.thread_id[:8]}",
                s,
            )
            for s in sessions
        ]
        options.append(("Cancel", None))
        chosen = await self._pick_menu(
            options,
            header="Resume session · ↑/↓ · Enter · Esc cancel",
            cancel_returns=None,
        )
        if chosen is None:
            return
        self.controller.resume_thread(chosen.thread_id)
        self._last_todos_sig = None
        await self._replay_transcript()

    async def _rewind_picker(self) -> None:
        """`/rewind`: pick an earlier user turn, fork onto a NEW thread keeping
        everything before it, optionally edit that turn's prompt, then continue.

        The original thread is left intact (still in /sessions for /resume) — this
        branches, it does not destroy. This rewinds the CONVERSATION only: file
        edits made after the chosen turn are NOT reverted; the notice points the
        user at /undo for those (git-checkpoint linkage is slice 2).

        Runs through the normal queue, so it never fires mid-turn: a `/rewind`
        typed while a turn is running is queued and only runs once that turn
        (and any HITL interrupt) has settled — no fork of a hanging thread.
        """
        if self._menu_future is not None and not self._menu_future.done():
            return
        c = self.console
        try:
            turns = await self.controller.human_turns()
        except Exception as exc:  # noqa: BLE001
            c.print(
                f"[{palette.C_ERROR}]could not load conversation: "
                f"{_rich_escape(str(exc))}[/{palette.C_ERROR}]"
            )
            return
        # Rewinding to the LAST user turn is a no-op (you'd keep everything and
        # re-ask the same thing), so it's not offered — need at least two turns
        # for an earlier one to exist.
        if len(turns) < 2:
            c.print(
                f"[{palette.C_DIM}]Nothing to rewind — need an earlier user "
                f"turn to branch from.[/{palette.C_DIM}]"
            )
            return
        # Drop the last turn: forking at it keeps the whole conversation, which
        # is a no-op. The picker only offers turns you can meaningfully branch
        # before continuing again.
        options: list[tuple[str, tuple[int, str] | None]] = [
            (f"turn {n} · {_rich_escape(preview)}", (idx, preview))
            for n, (idx, preview) in enumerate(turns[:-1], start=1)
        ]
        options.append(("Cancel", None))
        chosen = await self._pick_menu(
            options,
            header="Rewind to turn · ↑/↓ · Enter · Esc cancel",
            cancel_returns=None,
        )
        if chosen is None:
            return
        cut_index, original_prompt = chosen
        # Optional prompt edit: pre-fill the input with the chosen turn's text so
        # the user can tweak it before re-running (blank keeps the original).
        edited = await self._ask(
            "Edit the prompt (Enter to keep it as-is):", prefill=original_prompt
        )
        prompt = edited if edited else original_prompt

        cut = await self.controller.fork_to_turn(cut_index)
        if cut is None:
            c.print(f"[{palette.C_DIM}]Nothing to rewind.[/{palette.C_DIM}]")
            return
        self._last_todos_sig = None
        await self._replay_transcript()
        c.print(
            f"[{palette.C_NOTICE}]↩ rewound to a new branch[/{palette.C_NOTICE}] "
            f"[{palette.C_DIM}]— the original session is still in /resume. "
            f"File edits made after this point are NOT reverted — /undo rolls back "
            f"file changes one turn at a time.[/{palette.C_DIM}]"
        )
        if not prompt:
            # No continuation: still index the new branch so it survives in /resume
            # (otherwise it's an orphan checkpoint with no sessions row). Title it by
            # the turn we forked at.
            self.controller.record_session_title(
                original_prompt or "↩ rewound branch", when=time.time()
            )
            return
        # Continue from the fork through the normal turn path (we're already the
        # active turn task, so call _run_turn directly — same as _handle does).
        c.print(f"[{palette.C_USER}]›[/{palette.C_USER}] {_rich_escape(prompt)}")
        self._last_tool_outputs = []
        await repl_turn._run_turn(
            c, self.controller, prompt, self._ask,
            pick=self._pick_approval, view=self._view_full_diff,
            edit=self._edit_before_apply,
            live_sink=self._set_stream, spinner=False,
            tool_sink=self._last_tool_outputs,
            token_sink=self._count_stream_chars,
            todos_sink=self._on_todos_live,
        )
        await self._render_todos()
        self._maybe_autocheckpoint_hint()

    async def _pick_model_or_mode(self, what: str) -> None:
        c = self.console
        if what == "model":
            choices = self.controller.model_choices()
            options: list[tuple[str, str | None]] = [
                (f"{key}  ({hint})", key) for key, hint in choices
            ]
            options.append(("Custom model…", "__custom__"))
            options.append(("Cancel", None))
            header = "Pick model · ↑/↓ · Enter · Esc cancel"
        else:
            choices = self.controller.mode_choices()
            options = [(f"{key}  ({hint})", key) for key, hint in choices]
            options.append(("Cancel", None))
            header = "Pick mode · ↑/↓ · Enter · Esc cancel"

        chosen = await self._pick_menu(options, header=header, cancel_returns=None)
        if chosen is None:
            return
        if chosen == "__custom__":
            custom = (await self._ask("Paste model ref: ")).strip()
            if not custom:
                return
            chosen = custom
        if what == "model":
            _apply_model_ref(self.controller, c, str(chosen))
        else:
            if (
                chosen == "yolo"
                and self.controller.config.permission_mode.value != "yolo"
                and not await self._confirm_yolo()
            ):
                c.print(f"[{palette.C_DIM}]yolo cancelled — mode unchanged.[/{palette.C_DIM}]")
                return
            _apply_mode_ref(self.controller, c, str(chosen))

    async def _refresh_models(self) -> None:
        """Re-query local endpoints (Ollama / LM Studio) and pick from the result.

        Degrades to manual entry with a note when no endpoint answers, so it never
        leaves the user stuck.
        """
        c = self.console
        # Probing can block briefly on the network; keep the event loop live.
        discovered = await asyncio.to_thread(self.controller.discover_models)
        if not discovered:
            c.print(
                f"[{palette.C_DIM}]No local models found — is Ollama/LM Studio running? "
                f"Use /model to pick a configured model or paste a ref.[/{palette.C_DIM}]"
            )
            custom = (await self._ask("Paste model ref (blank to cancel): ")).strip()
            if custom:
                _apply_model_ref(self.controller, c, custom)
            return
        options: list[tuple[str, str | None]] = [
            (f"{ref}  ({profile})", ref) for ref, profile in discovered
        ]
        options.append(("Cancel", None))
        chosen = await self._pick_menu(
            options,
            header="Pick model · ↑/↓ · Enter · Esc cancel",
            cancel_returns=None,
        )
        if chosen is None:
            return
        _apply_model_ref(self.controller, c, str(chosen))

    async def _replay_transcript(self) -> None:
        try:
            messages = await self.controller.history()
        except Exception as exc:  # noqa: BLE001
            self.console.print(
                f"[{palette.C_ERROR}]could not load session: {_rich_escape(str(exc))}"
                f"[/{palette.C_ERROR}]"
            )
            return
        self.console.print(f"[{palette.C_DIM}]── resumed: {len(messages)} messages ──[/{palette.C_DIM}]")
        for msg in messages:
            self._replay_message(msg)

    def _replay_message(self, msg) -> None:
        mtype = getattr(msg, "type", "")
        content = getattr(msg, "content", "")
        if isinstance(content, list):
            content = "".join(
                b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
        text = str(content).strip()
        if mtype == "human" and text:
            self.console.print(f"[{palette.C_USER}]›[/{palette.C_USER}] {_rich_escape(text)}")
        elif mtype == "ai" and text:
            self.console.print(Markdown(text, code_theme=palette.CODE_THEME))
        elif mtype == "tool" and text:
            first = text.splitlines()[0] if text else ""
            self.console.print(f"  [{palette.C_DIM}]⎿ {_rich_escape(first[:80])}[/{palette.C_DIM}]")

    async def _cmd_theme(self, args: str) -> None:
        """`/theme [dark|light|high-contrast|auto]`: switch the color theme.

        With no argument: opens an arrow-key picker listing all four options;
        the title shows which theme is currently resolved (for ``auto``, the
        detected light/dark value is shown in parentheses).
        With an argument: applies directly (same as ``/config set ui.theme``).

        Applying a theme:
        1. Re-runs ``palette.configure_ui`` so the toolbar/live region picks up
           the new colors immediately (already-committed scrollback stays as-is).
        2. Persists ``ui.theme`` via ``controller.set_setting`` so the choice
           survives a restart.
        """
        from jarn.tui import termbg

        c = self.console
        _VALID = ("dark", "light", "high-contrast", "auto")

        # Resolve "auto" to an actual palette name for display / apply.
        def _resolve(name: str) -> str:
            if name == "auto":
                detected = termbg.detect()
                return detected if detected in ("light", "dark") else "dark"
            return name

        chosen: str | None = args.strip().lower() if args.strip() else None

        if chosen is None:
            # Open the arrow-key picker.
            current = self.controller.config.ui.theme
            resolved = _resolve(current)
            if current == "auto":
                header = (
                    f"Pick theme (currently: auto → {resolved}) · "
                    "↑/↓ · Enter · Esc cancel"
                )
            else:
                header = f"Pick theme (currently: {current}) · ↑/↓ · Enter · Esc cancel"
            options: list[tuple[str, str | None]] = [
                ("dark", "dark"),
                ("light", "light"),
                ("high-contrast", "high-contrast"),
                ("auto  (detect from terminal background)", "auto"),
                ("Cancel", None),
            ]
            chosen = await self._pick_menu(options, header=header, cancel_returns=None)
            if chosen is None:
                return
        else:
            if chosen not in _VALID:
                c.print(
                    f"[{palette.C_ERROR}]Unknown theme {chosen!r}. "
                    f"Valid: dark, light, high-contrast, auto.[/{palette.C_ERROR}]"
                )
                return

        # Apply: resolve auto → actual palette name, then configure.
        palette_name = _resolve(str(chosen))
        palette.configure_ui(theme=palette_name, accent=self.controller.config.ui.accent)

        # Persist via the standard config-set path.
        ok, msg = self.controller.set_setting("ui.theme", str(chosen))
        if ok:
            c.print(
                f"[{palette.C_SUCCESS}]Theme set to {chosen!r}"
                f"{' (→ ' + palette_name + ')' if chosen == 'auto' else ''}."
                f"[/{palette.C_SUCCESS}]"
            )
        else:
            c.print(f"[{palette.C_ERROR}]{msg}[/{palette.C_ERROR}]")

    def _maybe_autocheckpoint_hint(self) -> None:
        """After a turn that wrote a file, show the one-time /undo-unavailable
        hint when autocheckpoint is off (no-op otherwise; self-gates per session)."""
        if self._turn_made_edits():
            hint = self.controller.autocheckpoint_off_hint()
            if hint:
                self.console.print(f"[{palette.C_DIM}]{_rich_escape(hint)}[/{palette.C_DIM}]")

    async def _render_todos(self) -> None:
        """Print the current plan checklist into scrollback after a turn, de-duped
        so an unchanged list is never reprinted. This committed render REPLACES the
        transient live block, so the live todos are cleared here (even when there is
        nothing new to commit) — no duplicate lingering checklist."""
        self._live_todos = None
        todos = await self.controller.todos()
        sig = repr([(t.get("content"), t.get("status")) for t in todos])
        if not todos or sig == self._last_todos_sig:
            return
        self._last_todos_sig = sig
        self.console.print()
        for line in format_todos(todos, self.console.width):
            self.console.print(line)

    async def _shell_escape(self, command: str) -> None:
        """Run a ``! <cmd>`` shell escape directly — no agent round-trip, no tokens.

        The user typed the ``!`` prefix themselves, so the permission engine is
        bypassed entirely (same trust model as the user's own terminal).  Output
        is printed to the scrollback console.  Reuses
        :class:`~jarn.agent.local_backend.CancellableLocalShellBackend` so
        truncation and Esc/cancel behaviour match the agent's Bash tool.

        When ``execution.shell_escape_context`` is on (default), the tail of the
        output (last 50 lines / 2,000 chars, whichever is smaller) is also
        secret-redacted and stored on the controller so the next agent turn sees
        what the user ran (see :meth:`Controller.enrich_turn_input`).
        """
        c = self.console
        if not command:
            c.print(f"[{palette.C_DIM}]! <cmd>  — run a shell command directly[/{palette.C_DIM}]")
            return
        # Make it unmistakable this runs on the host, outside the agent: no
        # permission engine, no danger-guard, no sandbox. The ``!`` prefix is an
        # intentional bypass the user typed themselves, so we still print a
        # one-line reminder that the danger-guard is skipped for it.
        c.print(
            f"[{palette.C_ERROR}]⚡ host shell[/{palette.C_ERROR}] "
            f"[{palette.C_DIM}]— runs on your machine directly; no agent, no "
            f"approval, danger-guard skipped[/{palette.C_DIM}]"
        )
        cwd = self.controller.project_root or Path(".")
        backend = CancellableLocalShellBackend(str(cwd))
        # execute is blocking; offload to a thread so the event-loop stays live
        # (Esc can still fire while the command runs).
        response = await asyncio.to_thread(backend.execute, command)
        c.print(response.output)
        if self.controller.config.execution.shell_escape_context:
            raw = response.output or ""
            lines = raw.splitlines()[-50:]
            tail = "\n".join(lines)[-2000:]
            from jarn.config.secrets import redact_secrets
            from jarn.controller.core import ShellNote
            self.controller.pending_shell_context.append(
                ShellNote(cmd=command, exit_code=response.exit_code, tail=redact_secrets(tail))
            )
