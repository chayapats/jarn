"""REPL command dispatch and REPL-only handlers."""
# mypy: ignore-errors

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from rich.markdown import Markdown

from jarn.agent.checkpoint import RestorePreview
from jarn.agent.local_backend import CancellableLocalShellBackend
from jarn.commands.help import usage_error
from jarn.controller.commands.session import format_undo_preview
from jarn.repl import turn as repl_turn
from jarn.repl.turn import _apply_model_ref
from jarn.tui import grammar, layout, palette
from jarn.tui.layout import format_todos  # re-export for tests / live checklist


class CommandMixin:
    """Slash-command dispatch and REPL-only command handlers."""

    async def _command(self, name: str, args: str) -> None:
        c = self.console
        # `/config` with no args opens the interactive arrow-key settings panel;
        # `/config get|set …` still routes to the controller as text below.
        if name == "config" and not args.strip():
            self._open_config()
            return
        # `/modules` (and the singular `/module`) with no arguments opens the
        # interactive picker. Text/status and scripted on/off forms keep routing
        # through the framework-agnostic controller below.
        if name in ("modules", "module") and not args.strip():
            self._open_modules()
            return
        await self._ensure_extensions()
        rt = self.controller.runtime
        if rt and name in rt.commands:
            self._last_tool_outputs = []
            try:
                rendered = rt.commands[name].render(args)
            except Exception as exc:  # noqa: BLE001 - defensive boundary
                # A custom/MCP command's render() must NEVER dump a raw,
                # potentially secret-bearing exception here: this direct dispatch
                # path (unlike the /mcp handler) has no other boundary, so an MCP
                # prompt whose lazy fetch raised (redirect egress block, transport
                # error) would show the raw text — a repro leaked a Bearer token.
                # Redact and surface a stable one-liner instead. CancelledError is
                # a BaseException, so a real turn-cancellation still propagates.
                from jarn.config.secrets import redact_secrets

                c.print(layout.err(redact_secrets(str(exc))))
                return
            await repl_turn._run_turn(
                c, self.controller, rendered, self._ask,
                pick=self._pick_approval, view=self._view_full_diff,
                edit=self._edit_before_apply,
                live_sink=self._set_stream, spinner=False,
                tool_sink=self._last_tool_outputs,
                token_sink=self._count_stream_chars,
                todos_sink=self._on_todos_live,
                queue_sink=self._input_queue.append,
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
        if name in ("resume", "sessions"):
            await self._resume_picker(query=args.strip())
            return
        if name == "diff":
            result = self.controller.handle_command("diff", args)
            raw = result.text
            if raw.startswith(("diff ", "--- ", "+++ ")) or "\n@@" in raw:
                from jarn.tui.widgets.diff import colorize_unified_diff

                c.print(colorize_unified_diff(raw))
            else:
                c.print(raw, highlight=False)
            return
        if name == "rewind":
            await self._rewind_picker()
            return
        if name == "queue":
            await self._cmd_queue(args)
            return
        if name == "abort":
            # Controller.abort() owns cancel → await turn → settle → rollback
            # (T-CTRL-1). Idle is a no-op that does not undo the previous turn.
            result = await self.controller.abort()
            c.print(
                layout.muted(result.text)
                if result.text.lower().startswith("nothing to abort")
                else result.text,
                highlight=False,
            )
            return
        if name == "key":
            await self._cmd_key(args)
            return
        if name == "add-dir":
            await self._cmd_add_dir(args)
            return
        if name == "model" and args.strip() in ("refresh", "list", "ref"):
            await self._refresh_models()
            return
        if name == "theme":
            await self._cmd_theme(args)
            return
        if name in ("model", "mode") and not args.strip():
            await self._pick_model_or_mode(name)
            return
        if name == "mode" and args.strip():
            # Permission-mode changes (including yolo escalate) go through the
            # controller-owned async API so silent yolo escalate is impossible.
            result = await self.controller.set_permission_mode(
                args.strip(),
                confirm=self._confirm_yolo,
            )
            if result.rebuilt:
                self.controller._invalidate_runtime()
            c.print(result.text)
            return
        if name in ("undo", "redo"):
            # Async APIs embed settle_snapshot — one path for REPL and remote.
            result = (
                await self.controller.undo(confirm=self._confirm_undo)
                if name == "undo"
                else await self.controller.redo()
            )
            c.print(result.text)
            return
        result = self.controller.handle_command(name, args)
        if result.seed_turn:
            # A command (e.g. /skill) whose text is instructions the model should
            # act on: seed an agent turn with it — same path as custom commands
            # and /commit,/review — rather than just printing it.
            self._last_tool_outputs = []
            await repl_turn._run_turn(
                c, self.controller, result.seed_input or result.text, self._ask,
                pick=self._pick_approval, view=self._view_full_diff,
                edit=self._edit_before_apply,
                live_sink=self._set_stream, spinner=False,
                tool_sink=self._last_tool_outputs,
                token_sink=self._count_stream_chars,
                todos_sink=self._on_todos_live,
                queue_sink=self._input_queue.append,
            )
            await self._render_todos()
            self._maybe_autocheckpoint_hint()
            return
        if result.clear_screen:
            self._clear_scrollback()
        c.print(result.text)
        if result.rebuilt:
            # Route through the generation-bumping choke point: a raw
            # ``runtime = None`` would let a build worker still in flight
            # commit a stale runtime after this invalidation.
            self.controller._invalidate_runtime()
        if result.quit and self.app is not None:
            self.app.exit()

    async def _confirm_undo(self, preview: RestorePreview) -> bool:
        """Show the exact restore scope, then require an explicit yes."""
        self.console.print(format_undo_preview(preview), highlight=False)
        self.console.print(
            layout.warn(
                "Current content in the affected files will be restored to this checkpoint."
            ),
            highlight=False,
        )
        # Own line so wrap cannot split the "/redo can recover" phrase the
        # undo confirmation test (and users) scan for.
        self.console.print(
            layout.muted("/redo can recover the current state."),
            highlight=False,
        )
        answer = await self._ask(
            "Restore these file changes? Type 'y' to confirm; "
            "anything else cancels [y/N]: "
        )
        return answer.strip().lower() in ("y", "yes")

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
            c.print(layout.err("Not a git repository."))
            return
        prompt = commit_prompt(diff) if which == "commit" else review_prompt(diff)
        if prompt is None:
            what = "commit" if which == "commit" else "review"
            c.print(layout.muted(f"Nothing to {what} — the working tree is clean."))
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
            queue_sink=self._input_queue.append,
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
                layout.err(
                    "No active provider — configure a model first with /model or run jarn setup."
                )
            )
            return
        provider_config = self.controller.config.providers.get(provider)
        if (
            provider_config is not None
            and provider_config.type.value == "codex_subscription"
        ):
            c.print(
                layout.notice(
                    "Codex subscription uses managed ChatGPT authentication, not an API key. "
                    "Run `jarn codex login`, then verify it with `jarn codex status`."
                )
            )
            return
        inline = args.strip()
        if inline:
            # Inline keys are convenient but land in shell/REPL history — warn.
            c.print(
                layout.warn(
                    "Heads up: an inline key is visible in your scrollback/history. "
                    "Prefer /key with no argument next time."
                )
            )
            secret = inline
        else:
            secret = await self._ask(f"Paste the {provider} API key (Enter to cancel): ")
        if not secret.strip():
            c.print(layout.muted("No key entered — unchanged."))
            return
        result = self.controller.set_provider_key(secret, provider=provider)
        c.print(result.text)
        if result.rebuilt:
            # Same generation-bump routing as _command's rebuilt path.
            self.controller._invalidate_runtime()

    async def _cmd_add_dir(self, args: str) -> None:
        """`/add-dir <path>`: add a directory to this session's write scope.

        Security gating:
        - REFUSED outright on an untrusted project (a scope-widening capability
          must not be grantable to a repo whose config we don't trust) — no
          prompt, no change.
        - In ``ask`` AND ``plan`` modes it REQUIRES explicit approval before
          widening scope. ``plan`` must confirm too: a root added in plan
          persists into a later Shift+Tab escalation to auto-edit, so it must
          not slip in unconfirmed. ``auto-edit``/``yolo`` add directly (the user
          already opted into the looser mode).

        The added root extends the engine's WRITE scope AND the backend FS guard
        + sandbox bind/writable set (the runtime rebuilds on the next turn).
        Checkpoint/undo and project context stay PRIMARY-ONLY — the success
        message states that limitation explicitly.
        """
        from jarn.config.schema import PermissionMode

        c = self.console
        raw = args.strip()
        if not raw:
            c.print(
                layout.muted(
                    "/add-dir <path> — add a directory to this session's write scope"
                )
            )
            return
        if not self.controller.project_trusted:
            c.print(
                layout.err(
                    "/add-dir is refused on an untrusted project — run /trust here first "
                    "(an untrusted repo may not widen the agent's write scope)."
                )
            )
            return
        if self.controller.config.permission_mode in (
            PermissionMode.ASK,
            PermissionMode.PLAN,
        ):
            answer = (
                await self._ask(
                    f"Add '{raw}' as a writable root for this session? [y/N]: "
                )
            ).strip().lower()
            if answer not in ("y", "yes"):
                c.print(layout.muted("/add-dir cancelled — scope unchanged."))
                return
        ok, msg = self.controller.add_root(raw)
        printer = layout.ok if ok else layout.err
        c.print(printer(msg))

    # -- queue --------------------------------------------------------------

    async def _cmd_queue(self, args: str) -> None:
        parts = args.split()
        sub = parts[0].lower() if parts else ""
        c = self.console
        q = self._input_queue
        if not sub:
            items = q.list()
            if not items:
                c.print(layout.muted("Queue empty."))
                return
            for i, item in enumerate(items, 1):
                c.print(f"  {i}. {layout.escape(item.display)}", highlight=False)
            return
        if sub == "clear":
            n = q.clear()
            c.print(layout.notice(f"Cleared {n} queued line(s)."))
            return
        if sub == "cancel" and len(parts) >= 2:
            try:
                idx = int(parts[1])
            except ValueError:
                c.print(usage_error("queue"), highlight=False)
                return
            removed = q.cancel(idx)
            if removed is None:
                c.print(layout.err(f"No item at {idx}."))
            else:
                c.print(layout.notice(f"Removed: {removed.display}"))
            return
        if sub == "move" and len(parts) >= 3:
            try:
                fr, to = int(parts[1]), int(parts[2])
            except ValueError:
                c.print(usage_error("queue"), highlight=False)
                return
            if not q.move(fr, to):
                c.print(layout.err("Invalid queue indices."))
            else:
                c.print(layout.notice(f"Moved item {fr} → {to}."))
            return
        if sub == "steer" and len(parts) >= 2:
            # Mid-turn steering (T-4-6): route the 1-based line into the steer slot
            # so the running turn sees it before its next tool call.
            try:
                idx = int(parts[1])
            except ValueError:
                c.print(usage_error("queue"), highlight=False)
                return
            ok, msg = self._steer_index(idx)
            printer = layout.notice if ok else layout.err
            c.print(printer(msg))
            return
        c.print(usage_error("queue"), highlight=False)

    # -- resume -------------------------------------------------------------

    async def _resume_picker(self, query: str = "") -> None:
        from jarn.controller.commands.session import filter_sessions
        from jarn.memory.sessions import SessionInfo, session_label

        sessions = filter_sessions(self.controller.sessions.list(), query)
        if not sessions:
            if query:
                self.console.print(layout.muted(f"No sessions matching {query!r}."))
            else:
                self.console.print(layout.muted("No previous sessions."))
            return
        options: list[tuple[str, SessionInfo | None]] = [
            (session_label(s), s)
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
        from jarn.controller.commands.diagnostics import format_resume_recap

        self.console.print(format_resume_recap(self.controller), highlight=False)
        # A selected thread may be parked on a checkpointed approval from a
        # cancelled or crashed process. Ask for the verdict immediately and
        # resume with a Command; ``pending_only`` is a no-op for settled threads,
        # so merely browsing history never creates an empty user turn.
        self._last_tool_outputs = []
        await repl_turn._run_turn(
            self.console,
            self.controller,
            "",
            self._ask,
            pick=self._pick_approval,
            view=self._view_full_diff,
            edit=self._edit_before_apply,
            live_sink=self._set_stream,
            spinner=False,
            tool_sink=self._last_tool_outputs,
            token_sink=self._count_stream_chars,
            todos_sink=self._on_todos_live,
            queue_sink=self._input_queue.append,
            pending_only=True,
        )
        await self._render_todos()
        self._maybe_autocheckpoint_hint()

    async def _rewind_picker(self) -> None:
        """`/rewind`: pick an earlier user turn, fork onto a NEW thread keeping
        everything before it, optionally edit that turn's prompt, then continue.

        The original thread is left intact (still in /sessions for /resume) — this
        branches, it does not destroy. After the turn is chosen a second confirm
        (see :meth:`_confirm_rewind_restore`) can also restore the working tree to
        that turn's git checkpoint, so conversation and files rewind atomically
        (slice 2). Declining it — or having autocheckpoint off — leaves files as-is
        and points the user at /undo, exactly as slice 1 did.

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
            c.print(layout.err(f"could not load conversation: {exc}"))
            return
        # Rewinding to the LAST user turn is a no-op (you'd keep everything and
        # re-ask the same thing), so it's not offered — need at least two turns
        # for an earlier one to exist.
        if len(turns) < 2:
            c.print(
                layout.muted(
                    "Nothing to rewind — need an earlier user turn to branch from."
                )
            )
            return
        # Drop the last turn: forking at it keeps the whole conversation, which
        # is a no-op. The picker only offers turns you can meaningfully branch
        # before continuing again.
        options: list[tuple[str, tuple[int, str] | None]] = [
            (f"turn {n} · {preview}", (idx, preview))
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
        # Slice 2: second confirm — restore files too, or conversation only?
        # Returns True (restore) / False (conversation only) / None (cancel).
        decision = await self._confirm_rewind_restore(cut_index, turns)
        if decision is None:
            c.print(layout.muted("Rewind cancelled."))
            return
        restore_files = decision
        # Optional prompt edit: pre-fill the input with the chosen turn's text so
        # the user can tweak it before re-running (blank keeps the original).
        edited = await self._ask(
            "Edit the prompt (Enter to keep it as-is):", prefill=original_prompt
        )
        prompt = edited if edited else original_prompt

        cut = await self.controller.fork_to_turn(cut_index, restore_files=restore_files)
        if cut is None:
            c.print(layout.muted("Nothing to rewind."))
            return
        self._last_todos_sig = None
        await self._replay_transcript()
        if restore_files:
            c.print(
                layout.notice(
                    "↩ rewound to a new branch — conversation and files restored to this turn"
                )
                + " "
                + layout.muted(
                    "— the original session is still in /resume; /undo reverts this file restore."
                )
            )
        else:
            c.print(
                layout.notice("↩ rewound to a new branch")
                + " "
                + layout.muted(
                    "— the original session is still in /resume. "
                    "File edits made after this point are NOT reverted — /undo rolls back "
                    "file changes one turn at a time."
                )
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
        c.print(layout.prompt(prompt))
        self._last_tool_outputs = []
        # Match the main submit path (repl/app.py): pass queue_sink so a
        # diagnostics auto-fix round on the rewound/edited prompt is queued, and
        # inline any @image mention in that prompt (both no-op unless enabled).
        await repl_turn._run_turn(
            c, self.controller, prompt, self._ask,
            pick=self._pick_approval, view=self._view_full_diff,
            edit=self._edit_before_apply,
            live_sink=self._set_stream, spinner=False,
            tool_sink=self._last_tool_outputs,
            token_sink=self._count_stream_chars,
            todos_sink=self._on_todos_live,
            queue_sink=self._input_queue.append,
            images=repl_turn.select_inline_images(self.controller, prompt),
        )
        await self._render_todos()
        self._maybe_autocheckpoint_hint()

    async def _confirm_rewind_restore(
        self, cut_index: int, turns: list[tuple[int, str]]
    ) -> bool | None:
        """`/rewind` second confirm: restore the working tree to the chosen turn too,
        or rewind the conversation only?

        Returns ``True`` (restore files), ``False`` (conversation only), or ``None``
        (cancel the whole rewind). When autocheckpoint is off or no checkpoint
        captured the chosen turn, there is nothing to restore — returns ``False``
        WITHOUT showing a menu, so /rewind stays byte-identical to slice 1 for the
        default (autocheckpoint-off) config. Otherwise it previews the revert
        (``git diff --stat``, capped) and defaults the highlight to restore.
        """
        c = self.console
        cpm = self.controller.checkpoint_manager
        if not cpm.enabled or not cpm.is_repo:
            return False  # no checkpoints — slice-1 conversation-only, no extra menu
        # 0-based turn index of the chosen cut = human turns strictly before it —
        # the same quantity the session driver records on each turn-start snapshot.
        turn_index = sum(1 for idx, _ in turns if idx < cut_index)
        ref = await asyncio.to_thread(
            cpm.find_for_turn, self.controller.thread_id, turn_index
        )
        if ref is None:
            c.print(
                layout.muted(
                    "No checkpoint captured for that turn "
                    "(autocheckpoint off, no edits that turn, or the thread was forked "
                    "in an earlier session) — reverting the conversation only."
                )
            )
            return False
        # Preview what the restore would revert (git diff --stat, ≤10 lines).
        stat = await asyncio.to_thread(cpm.diff_stat, ref.sha)
        if stat:
            c.print(
                layout.muted(
                    "Tracked changes vs that snapshot "
                    "(untracked files created since will also be removed):"
                )
            )
            for line in stat[:10]:
                c.print(f"  {layout.muted(line)}")
            if len(stat) > 10:
                c.print(f"  {layout.muted(f'… +{len(stat) - 10} more')}")
        # Warn when the tree has hand-edits no checkpoint captured — the restore
        # would roll them back (they stay recoverable via /undo, but flag it).
        if await asyncio.to_thread(cpm.has_uncheckpointed_changes):
            c.print(
                layout.warn(
                    f"{grammar.GLYPH_WARN} Uncommitted changes not captured by any "
                    "checkpoint will be rolled back by the restore "
                    "(/undo can recover them)."
                )
            )
        options: list[tuple[str, bool | None]] = [
            ("Restore files too (recommended)", True),
            ("Conversation only", False),
            ("Cancel", None),
        ]
        return await self._pick_menu(
            options,
            header="Restore files? · ↑/↓ · Enter · Esc cancel",
            cancel_returns=None,
        )

    async def _pick_model_or_mode(self, what: str) -> None:
        c = self.console
        if what == "model":
            if self.controller.model_catalog_supported():
                c.print(layout.muted("Checking live model catalogs…"))
                snapshot = await asyncio.to_thread(self.controller.refresh_model_catalog)
                if (
                    self.controller.model_catalog_requires_verification()
                    and not snapshot.availability_verified
                ):
                    detail = snapshot.error.message if snapshot.error else snapshot.provenance_label
                    c.print(
                        layout.warn(f"active-provider catalog unverified: {detail}")
                        + layout.sep()
                        + layout.muted(
                            "verified models from other configured providers remain selectable"
                        )
                    )
                else:
                    c.print(layout.muted(snapshot.provenance_label))
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
            ref = str(chosen)
            effort = await self._pick_reasoning_effort(ref)
            _apply_model_ref(self.controller, c, ref, reasoning_effort=effort)
        else:
            result = await self.controller.set_permission_mode(
                str(chosen),
                confirm=self._confirm_yolo,
            )
            if "cancelled" in result.text.lower():
                c.print(layout.muted(result.text))
                return
            applied = self.controller.config.permission_mode.value
            if "clamped" in result.text.lower():
                c.print(
                    layout.notice(f"mode → {applied}")
                    + " "
                    + layout.muted("(clamped — project untrusted)")
                )
            elif result.text.startswith("Permission mode set"):
                c.print(layout.notice(f"mode → {applied}"))
            else:
                c.print(result.text)

    async def _refresh_models(self) -> None:
        """Refresh every configured provider and pick only verified entries."""
        c = self.console
        if self.controller.model_catalog_supported():
            c.print(layout.muted("Refreshing live model catalogs…"))
            snapshot = await asyncio.to_thread(self.controller.refresh_model_catalog)
            c.print(layout.muted(snapshot.provenance_label))
            if (
                self.controller.model_catalog_requires_verification()
                and not snapshot.availability_verified
            ):
                detail = snapshot.error.message if snapshot.error else snapshot.provenance_label
                c.print(
                    layout.warn(f"Could not verify the active provider: {detail}")
                    + layout.sep()
                    + layout.muted("checking other configured providers")
                )
            entries = self.controller.verified_catalog_models()
            if entries:
                options: list[tuple[str, str | None]] = []
                for entry in entries:
                    markers: list[str] = []
                    if entry.is_default:
                        markers.append("account default")
                    if entry.default_reasoning_effort:
                        markers.append(f"reasoning {entry.default_reasoning_effort}")
                    suffix = f"  ({', '.join(markers)})" if markers else ""
                    options.append((f"{entry.display_name}{suffix}", entry.ref))
                options.append(("Cancel", None))
                chosen = await self._pick_menu(
                    options,
                    header="Pick model · ↑/↓ · Enter · Esc cancel",
                    cancel_returns=None,
                )
                if chosen is None:
                    return
                ref = str(chosen)
                effort = await self._pick_reasoning_effort(ref)
                _apply_model_ref(self.controller, c, ref, reasoning_effort=effort)
                return

        c.print(
            layout.muted(
                "No verified models were reported by the configured providers. "
                "Check credentials/endpoints, or use Advanced manual entry."
            )
        )
        custom = (await self._ask("Paste model ref (blank to cancel): ")).strip()
        if custom:
            _apply_model_ref(self.controller, c, custom)

    async def _pick_reasoning_effort(self, model_ref: str) -> str | None:
        """Pick only efforts the selected live catalog entry supports."""

        choices = self.controller.reasoning_choices(model_ref)
        default = self.controller.default_reasoning_effort(model_ref)
        if not choices:
            return default
        if len(choices) == 1:
            return choices[0][0]
        options: list[tuple[str, str | None]] = []
        for value, description in choices:
            marker = " · default" if value == default else ""
            detail = f" — {description}" if description else ""
            options.append((f"{value}{marker}{detail}", value))
        options.append(("Cancel", None))
        return await self._pick_menu(
            options,
            header="Reasoning effort · ↑/↓ · Enter · Esc cancel",
            cancel_returns=default,
        )

    async def _replay_transcript(self) -> None:
        try:
            messages = await self.controller.history()
        except Exception as exc:  # noqa: BLE001
            self.console.print(layout.err(f"could not load session: {exc}"))
            return
        self.console.print(layout.muted(f"── resumed: {len(messages)} messages ──"))
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
            self.console.print(layout.prompt(text))
        elif mtype == "ai" and text:
            self.console.print(Markdown(text, code_theme=palette.CODE_THEME))
        elif mtype == "tool" and text:
            first = text.splitlines()[0] if text else ""
            self.console.print(layout.tool_result(first[:80]))

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
        c = self.console
        _VALID = ("dark", "light", "high-contrast", "auto")

        # Resolve "auto" to an actual palette name for display / apply.  The
        # terminal-background detection runs ONCE at startup (while we still own
        # the tty) and is cached on the app as ``_detected_theme``; probing again
        # at runtime would race prompt_toolkit's input reader (junk keystrokes +
        # wrong fallback), so /theme reuses the cached value instead.
        def _resolve(name: str) -> str:
            if name == "auto":
                return self._detected_theme or "dark"
            return name

        chosen: str | None = args.strip().lower() if args.strip() else None

        if chosen is None:
            # Open the arrow-key picker.
            current = self.controller.config.ui.theme
            resolved = _resolve(current)
            if current == "auto":
                header = (
                    f"Pick theme (currently: auto → {resolved} (detected at startup)) · "
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
                    layout.err(
                        f"Unknown theme {chosen!r}. Valid: dark, light, high-contrast, auto."
                    )
                )
                return

        # Apply: resolve auto → actual palette name (cached, sync), then configure.
        palette_name = _resolve(str(chosen))
        palette.configure_ui(theme=palette_name, accent=self.controller.config.ui.accent)

        # Persist via the standard config-set path.
        ok, msg = self.controller.set_setting("ui.theme", str(chosen))
        if ok:
            suffix = f" (→ {palette_name})" if chosen == "auto" else ""
            c.print(layout.ok(f"Theme set to {chosen!r}{suffix}."))
        else:
            c.print(layout.err(msg))

    def _maybe_autocheckpoint_hint(self) -> None:
        """After a turn that wrote a file, show the one-time /undo-unavailable
        hint when autocheckpoint is off (no-op otherwise; self-gates per session)."""
        if self._turn_made_edits():
            hint = self.controller.autocheckpoint_off_hint()
            if hint:
                self.console.print(layout.muted(hint), highlight=False)

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
            c.print(layout.muted("! <cmd>  — run a shell command directly"))
            return
        # Make it unmistakable this runs on the host, outside the agent: no
        # permission engine, no danger-guard, no sandbox. The ``!`` prefix is an
        # intentional bypass the user typed themselves, so we still print a
        # one-line reminder that the danger-guard is skipped for it.
        c.print(layout.host_shell_banner())
        cwd = self.controller.project_root or Path(".")
        backend = CancellableLocalShellBackend(str(cwd))
        # execute is blocking; offload to a thread so the event-loop stays live
        # (Esc can still fire while the command runs).
        try:
            response = await asyncio.to_thread(backend.execute, command)
        except asyncio.CancelledError:
            # Esc/cancel hit while the shell command was running.  Print feedback
            # (the renderer owns "cancelled" for agent turns; the shell path has no
            # renderer, so we own the message here) then re-raise so the event-loop
            # sees the cancellation.
            c.print(layout.muted("interrupted"))
            raise
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
