"""Pager, config panel, and menu overlays for the REPL."""
# mypy: ignore-errors

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TypeVar, cast

from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import HTML
from rich.markup import escape as _rich_escape

from jarn.agent.session import ApprovalReply, ApprovalRequest
from jarn.permissions import RememberScope
from jarn.repl import turn as repl_turn
from jarn.repl.turn import _editable_field
from jarn.tui import palette

_MenuT = TypeVar("_MenuT")


class OverlayMixin:
    """Pager, config panel, and interactive pickers."""

    async def _ask(self, prompt: str, *, prefill: str = "") -> str:
        """App-native line capture for pickers: show the prompt in the region above
        the input and resolve on the next Enter-submitted line.

        ``prefill`` pre-populates the editable input with text the user can edit
        in place (used by ``/rewind`` to offer the chosen turn's prompt) — the
        cursor lands at the end so they can tweak or accept it as-is."""
        self._set_stream(prompt)
        if prefill:
            self.input.reset()
            self.input.insert_text(prefill)
            if self.app is not None:
                self.app.invalidate()
        self._line_future = asyncio.get_running_loop().create_future()
        try:
            text = await self._line_future
        finally:
            self._line_future = None
            self._set_stream("")
        return text.strip()

    async def _pick_menu(
        self,
        options: list[tuple[str, _MenuT]],
        *,
        header: str = "",
        cancel_returns: _MenuT | None = None,
        fastkeys: dict[str, _MenuT] | None = None,
    ) -> _MenuT | None:
        """Claude Code-style menu: ↑/↓ to highlight, Enter to confirm, Esc cancel.

        ``fastkeys`` maps a single typed char to the option value it resolves to
        immediately (no arrow+Enter) — used by the approval menu for y/a/n/d."""
        self._menu_options = cast(list[tuple[str, object]], options)
        self._menu_index = 0
        self._menu_header = header
        self._menu_cancel = cancel_returns
        self._menu_fastkeys = cast("dict[str, object] | None", fastkeys)
        self._menu_future = asyncio.get_running_loop().create_future()
        if self.app is not None:
            self.app.invalidate()
        try:
            return await self._menu_future
        finally:
            self._menu_future = None
            self._menu_options = []
            self._menu_header = ""
            self._menu_cancel = None
            self._menu_fastkeys = None
            self._set_stream("")

    async def _pick_approval(self, options: list[tuple[str, object]]) -> object:
        # Cancel → deny. The "View full diff" sentinel may sit last, so find the
        # deny reply by value rather than assuming it's options[-1].
        deny = next(
            v for _, v in options
            if isinstance(v, ApprovalReply) and not v.approved
        )
        # Allow-once is the first allow reply (options[0] by construction).
        allow = next(
            v for _, v in options
            if isinstance(v, ApprovalReply) and v.approved
        )
        # Single-keypress fast-path: y/a approve (allow once), n/d deny — no
        # arrow+Enter needed across a multi-edit turn. Arrow nav + Enter and the
        # "View full diff"/"Edit before apply" options keep working unchanged.
        fastkeys: dict[str, object] = {"y": allow, "a": allow, "n": deny, "d": deny}
        picked = await self._pick_menu(
            options,
            header="Approve · y/a allow · n/d deny · ↑/↓ · Enter · Esc cancel",
            cancel_returns=deny,
            fastkeys=fastkeys,
        )
        return deny if picked is None else picked

    async def _confirm_yolo(self) -> bool:
        """Prompt the user to confirm entering yolo mode.  Returns True iff confirmed."""
        # Print a prominent banner into the visible scrollback first — the inline
        # ask renders in the faint region above the input, which is easy to miss;
        # the user must clearly see this is a y/N decision.
        self.console.print(
            f"[{palette.C_ERROR}]⚠  Entering YOLO mode[/{palette.C_ERROR}] "
            f"[{palette.C_DIM}]— no approval prompts; the danger-guard still blocks "
            f"catastrophic actions.[/{palette.C_DIM}]"
        )
        answer = await self._ask("Type 'y' to confirm yolo, anything else to cancel [y/N]: ")
        return answer.strip().lower() in ("y", "yes")

    def _request_yolo_confirm(self) -> None:
        """Spawn the yolo confirmation, dropping re-entrant requests.

        Shift+Tab only *peeks* the next mode, so rapid presses while a
        confirmation is pending would each spawn a task — all sharing the single
        ``_line_future``, leaving every prompt but the last hung forever. The flag
        is set synchronously here (before the task is scheduled) so repeat presses
        are ignored until the in-flight confirmation resolves."""
        if self._yolo_confirm_inflight:
            return
        self._yolo_confirm_inflight = True

        async def _run() -> None:
            try:
                await self._confirm_and_cycle_yolo()
            finally:
                self._yolo_confirm_inflight = False

        asyncio.get_running_loop().create_task(_run())

    async def _confirm_and_cycle_yolo(self) -> None:
        """Async helper for Shift+Tab: confirm yolo, then apply it (or skip)."""
        if not await self._confirm_yolo():
            self.console.print(f"[{palette.C_DIM}]yolo cancelled — mode unchanged.[/{palette.C_DIM}]")
            return
        new = self.controller.cycle_mode()
        self._armed = False
        color = palette.MODE_COLOR.get(new, "#22d3ee")
        glyph = palette.MODE_GLYPH.get(new, "◆")
        self._flash(HTML(
            f'<style fg="{color}"><b>{glyph} {new}</b></style> '
            f'<style fg="#7c8f94">mode</style>'
        ))
        if self.app is not None:
            self.app.invalidate()

    def _expanded_text(self) -> str | None:
        """The full output of the last turn's tool calls, joined for the pager —
        or ``None`` when there is nothing to expand."""
        if not self._last_tool_outputs:
            return None
        return "\n\n".join(
            f"⏺ {name}\n{'─' * 40}\n{full or '(empty)'}"
            for name, full in self._last_tool_outputs
        )

    def _open_pager(self) -> None:
        """Open the in-app pager overlay on the last turn's full output. Works
        mid-turn (reads whatever has accumulated so far). Ctrl+O toggles it shut."""
        text = self._expanded_text()
        if text is None:
            self._flash(HTML('<style fg="#7c8f94">nothing to expand yet</style>'))
            return
        self._pager_buffer.set_document(Document(text, 0), bypass_readonly=True)
        self._expanded = True
        if self.app is not None and self._pager_window is not None:
            self.app.layout.focus(self._pager_window)
            self.app.invalidate()

    def _collapse(self) -> None:
        if not self._expanded:
            return
        self._expanded = False
        if self.app is not None:
            self.app.layout.focus(self.input)  # back to the conversation
            self.app.invalidate()
        # Unblock a "view full diff" approval prompt that's waiting on the close.
        if self._pager_closed is not None and not self._pager_closed.done():
            self._pager_closed.set_result(None)

    async def _view_full_diff(self, text: str) -> None:
        """Show the COMPLETE approval diff in the pager and wait for the user to
        close it (q / Ctrl+O / Esc). Returns when the pager is dismissed so the
        caller can re-show the SAME approve/deny prompt — viewing never decides."""
        self._pager_buffer.set_document(Document(text, 0), bypass_readonly=True)
        self._expanded = True
        self._pager_closed = asyncio.get_running_loop().create_future()
        if self.app is not None and self._pager_window is not None:
            self.app.layout.focus(self._pager_window)
            self.app.invalidate()
        try:
            await self._pager_closed
        finally:
            self._pager_closed = None

    async def _edit_before_apply(self, request: ApprovalRequest) -> ApprovalReply | None:
        """Open the proposed new content in ``$EDITOR``; apply the edited result.

        Returns an approve-:class:`ApprovalReply` carrying ``edited_args`` (the
        original tool args with the new content swapped for the user's edit), or
        ``None`` when the editor is aborted so the caller cancels without applying.
        """
        args = request.args or {}
        field = _editable_field(args)
        if field is None:  # nothing editable — should not happen (menu gated on it)
            return None
        original = str(args.get(field, ""))
        # Suspend the app so $EDITOR owns the terminal, run it off-thread so the
        # event loop stays live, then restore the app.
        from prompt_toolkit.application import run_in_terminal

        suffix = Path(str(args.get("file_path") or args.get("path") or "")).suffix or ".txt"
        edited = await run_in_terminal(
            lambda: repl_turn._edit_text_in_editor(original, suffix=suffix)
        )
        if edited is None:
            return None  # editor aborted — cancel cleanly, apply nothing
        # Validate: the edited content must still apply. We only ever replace the
        # *new* content (``content`` for a write, ``new_string`` for an edit) and
        # never touch ``old_string``, so an edit_file's anchor is unchanged and the
        # replacement still applies; a write_file overwrites wholesale. Carry the
        # edited args back so the turn resumes with a LangGraph ``edit`` decision.
        edited_args = {**args, field: edited}
        return ApprovalReply(True, RememberScope.ONCE, edited_args=edited_args)

    async def _cmd_compact(self) -> None:
        """Manual ``/compact``: generate the summary, render it for review, then
        ask before replacing the thread. ``y`` applies, ``edit`` opens the
        summary in ``$EDITOR`` first, anything else declines and keeps the
        original context fully intact. (Automatic compaction is separate and
        non-interactive — the in-graph summarization middleware handles it; see
        ``build_runtime``.)"""
        c = self.console
        c.print(f"[{palette.C_DIM}]compacting…[/{palette.C_DIM}]")
        try:
            summary = await self.controller.compact_preview()
        except Exception as exc:  # noqa: BLE001
            c.print(
                f"[{palette.C_ERROR}]compact failed: {_rich_escape(str(exc))}"
                f"[/{palette.C_ERROR}]"
            )
            return
        if not summary:
            c.print("Nothing to compact yet.")
            return
        c.print(f"[{palette.C_NOTICE}]Proposed compaction:[/{palette.C_NOTICE}]")
        c.print(_rich_escape(summary))
        answer = (await self._ask("Apply this compaction? [y/N/edit] ")).strip().lower()
        if answer in ("e", "edit"):
            from prompt_toolkit.application import run_in_terminal

            edited = await run_in_terminal(
                lambda: repl_turn._edit_text_in_editor(summary, suffix=".md")
            )
            if edited is None:  # editor aborted — keep the original context intact
                c.print(f"[{palette.C_DIM}]Compaction cancelled.[/{palette.C_DIM}]")
                return
            summary = edited
        elif answer not in ("y", "yes"):
            c.print(f"[{palette.C_DIM}]Compaction cancelled.[/{palette.C_DIM}]")
            return
        try:
            await self.controller.compact_apply(summary)
        except Exception as exc:  # noqa: BLE001
            c.print(
                f"[{palette.C_ERROR}]compact failed: {_rich_escape(str(exc))}"
                f"[/{palette.C_ERROR}]"
            )
            return
        c.print(f"[{palette.C_NOTICE}]Compacted.[/{palette.C_NOTICE}]")

    def _config_render(self):
        """FormattedTextControl source for the settings panel."""
        if self._config_panel is None:
            return []
        return self._config_panel.render_lines()

    def _open_config(self) -> None:
        """Open the arrow-key settings panel. Saves persist to global config."""
        from jarn.config.settings import ConfigPanel

        self._config_panel = ConfigPanel(
            get_config=lambda: self.controller.config,
            apply=self.controller.set_setting,
        )
        self._config_open = True
        # The input keeps focus; global bindings (gated on _config_open) drive
        # the panel, so input keys stay inert while it is open.
        if self.app is not None:
            self.app.invalidate()

    def _close_config(self) -> None:
        self._config_open = False
        self._config_panel = None
        if self.app is not None:
            self.app.invalidate()

    def _pager_header(self) -> HTML:
        base = (' <b>full output</b> '
                '<style fg="#7c8f94">— ↑/↓ PgUp/PgDn scroll · Ctrl+O / q / Esc close</style>')
        if self._busy():  # the turn keeps running behind the overlay — show it
            frame = palette.SPINNER_FRAMES[int(time.monotonic() * 5) % len(palette.SPINNER_FRAMES)]
            elapsed = int(time.monotonic() - (self._turn_start or time.monotonic()))
            base += f' <style fg="#5fb8d8">{frame} still working… ({elapsed}s)</style>'
        return HTML(base)
