"""Key bindings for the REPL."""
# mypy: ignore-errors

from __future__ import annotations

import asyncio
import time

from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from rich.markup import escape as _rich_escape

from jarn.extensibility.commands import parse_input
from jarn.tui import palette


class KeysMixin:
    """prompt_toolkit key bindings for :class:`~jarn.repl.app.InlineApp`."""

    def _expand_pastes(self, text: str) -> str:
        """Restore collapsed ``[Pasted ...]`` tokens to their stored content."""
        for token, full in self._pastes.items():
            text = text.replace(token, full)
        return text

    def _build_keys(self) -> KeyBindings:
        kb = KeyBindings()
        # Input/editing keys only apply when neither overlay (pager / config
        # panel) is open; while one is open keys drive that overlay instead.
        live = Condition(lambda: not self._expanded and not self._config_open)
        cfg_open = Condition(lambda: self._config_open)
        cfg_edit = Condition(
            lambda: self._config_open
            and self._config_panel is not None
            and self._config_panel.editing
        )
        cfg_nav = Condition(
            lambda: self._config_open
            and (self._config_panel is None or not self._config_panel.editing)
        )

        @kb.add("enter", filter=live)
        def _submit(event) -> None:
            self._armed = False
            if self._menu_future is not None and not self._menu_future.done():
                label, value = self._menu_options[self._menu_index]
                self.console.print(
                    f"[{palette.C_DIM}]› {_rich_escape(label)}[/{palette.C_DIM}]"
                )
                self._menu_future.set_result(value)
                return
            text = self.input.text
            # Resolving an app-native ask (picker) takes priority.
            if self._line_future is not None and not self._line_future.done():
                self.input.reset()
                if text.strip():
                    self.console.print(f"[{palette.C_DIM}]› {_rich_escape(text.strip())}[/{palette.C_DIM}]")
                self._line_future.set_result(text)
                return
            stripped = text.strip()
            # /abort must reach the running turn, not wait behind it: dispatch it
            # immediately instead of queueing so it can cancel the in-flight turn
            # and roll the working tree back.
            if self._busy() and parse_input(stripped).name == "abort":
                self.input.append_to_history()
                self.input.reset()
                self.console.print(f"[{palette.C_USER}]›[/{palette.C_USER}] {_rich_escape(stripped)}")
                asyncio.create_task(self._command("abort", ""))
                return
            if self._busy():
                # A turn is running; queue the line (echoed dim) to run when it
                # finishes. Empty lines just clear the input.
                if stripped:
                    self.input.append_to_history()
                    self.input.reset()
                    # Store (collapsed display, expanded payload) so the dequeued
                    # echo stays tidy while the agent still receives the full paste.
                    expanded = self._expand_pastes(stripped)
                    self._pastes.clear()
                    self._input_queue.append(stripped, expanded)
                    self.console.print(f"[{palette.C_DIM}]» queued: {_rich_escape(stripped)}[/{palette.C_DIM}]")
                else:
                    self.input.reset()
                return
            self.input.append_to_history()
            self.input.reset()
            if stripped:
                # Echo the submitted line into the scrollback transcript (the
                # input buffer is cleared, so without this the message vanishes).
                send = self._expand_pastes(stripped)
                self._pastes.clear()
                if stripped.startswith("!"):
                    # Host shell escape — echo in red with a clear marker so it's
                    # obvious this ran outside the agent (no approval).
                    cmd = _rich_escape(stripped[1:].strip())
                    self.console.print(
                        f"[{palette.C_ERROR}]![/{palette.C_ERROR}] "
                        f"[{palette.C_ERROR}]{cmd}[/{palette.C_ERROR}] "
                        f"[{palette.C_DIM}](host shell)[/{palette.C_DIM}]"
                    )
                else:
                    self.console.print(f"[{palette.C_USER}]›[/{palette.C_USER}] {_rich_escape(stripped)}")
                self._turn_start = time.monotonic()
                self._turn_stream_chars = 0
                self._turn_base_output = self.controller.tracker.total.output_tokens
                self._turn_base_input = self.controller.tracker.total.input_tokens
                self._first_token_at = None
                self._turn_task = asyncio.create_task(self._handle(send))

        @kb.add("c-j", filter=live)
        def _newline(event) -> None:  # Shift+Enter usually arrives as Ctrl+J
            self.input.insert_text("\n")

        @kb.add(Keys.BracketedPaste, filter=live)
        def _paste(event) -> None:
            # Collapse large pastes to a placeholder token so the input stays
            # readable; the real text is restored on submit via _expand_pastes.
            data = event.data
            if data.count("\n") >= 3 or len(data) > 800:
                self._paste_n += 1
                lines = data.count("\n") + 1
                token = f"[Pasted #{self._paste_n}: {lines} lines]"
                self._pastes[token] = data
                self.input.insert_text(token)
            else:
                self.input.insert_text(data)

        @kb.add("tab", filter=live)
        def _complete(event) -> None:
            buf = self.input
            if buf.complete_state:
                buf.complete_next()
            else:
                buf.start_completion(select_first=True)

        @kb.add("up", filter=live)
        def _up(event) -> None:
            if self._menu_future is not None and not self._menu_future.done():
                n = len(self._menu_options)
                self._menu_index = (self._menu_index - 1) % n
                if self.app is not None:
                    self.app.invalidate()
                return
            buf = self.input
            buf.complete_previous() if buf.complete_state else buf.auto_up()

        @kb.add("down", filter=live)
        def _down(event) -> None:
            if self._menu_future is not None and not self._menu_future.done():
                n = len(self._menu_options)
                self._menu_index = (self._menu_index + 1) % n
                if self.app is not None:
                    self.app.invalidate()
                return
            buf = self.input
            buf.complete_next() if buf.complete_state else buf.auto_down()

        def _menu_fastkey(event) -> None:
            """Single-keypress accept/deny for the approval menu: a mapped char
            (y/a/n/d) resolves the picker instantly. When no fast-key menu is up
            — or the char isn't mapped — the key types normally into the input so
            ordinary editing is never swallowed."""
            char = event.data
            keys = self._menu_fastkeys
            if (
                self._menu_future is not None
                and not self._menu_future.done()
                and keys is not None
                and char in keys
            ):
                value = keys[char]
                label = next(
                    (lbl for lbl, v in self._menu_options if v is value),
                    char,
                )
                self.console.print(
                    f"[{palette.C_DIM}]› {_rich_escape(label)}[/{palette.C_DIM}]"
                )
                self._menu_future.set_result(value)
                return
            self.input.insert_text(char)

        for _fk in ("y", "a", "n", "d"):
            kb.add(_fk, filter=live)(_menu_fastkey)

        @kb.add("s-tab", filter=live)
        def _cycle_mode(event) -> None:
            next_mode = self.controller.peek_next_mode()
            if next_mode == "yolo":
                # Entering yolo requires async confirmation; hand off to a task,
                # de-duped so rapid repeat presses don't stack confirmations.
                self._request_yolo_confirm()
                event.app.invalidate()
                return
            new = self.controller.cycle_mode()
            self._armed = False
            color = palette.MODE_COLOR.get(new, "#22d3ee")
            glyph = palette.MODE_GLYPH.get(new, "◆")
            # transient flash above the input (not a permanent scrollback line);
            # the toolbar also reflects the new mode immediately.
            self._flash(HTML(
                f'<style fg="{color}"><b>{glyph} {new}</b></style> '
                f'<style fg="#7c8f94">mode</style>'
            ))
            event.app.invalidate()

        # -- interactive /config settings panel --------------------------------
        @kb.add("up", filter=cfg_open)
        def _cfg_up(event) -> None:
            if self._config_panel is not None:
                self._config_panel.move(-1)
                event.app.invalidate()

        @kb.add("down", filter=cfg_open)
        def _cfg_down(event) -> None:
            if self._config_panel is not None:
                self._config_panel.move(1)
                event.app.invalidate()

        @kb.add("left", filter=cfg_nav)
        @kb.add("s-tab", filter=cfg_nav)
        def _cfg_prev_cat(event) -> None:
            if self._config_panel is not None:
                self._config_panel.move_category(-1)
                event.app.invalidate()

        @kb.add("right", filter=cfg_nav)
        @kb.add("tab", filter=cfg_nav)
        def _cfg_next_cat(event) -> None:
            if self._config_panel is not None:
                self._config_panel.move_category(1)
                event.app.invalidate()

        @kb.add("enter", filter=cfg_open)
        def _cfg_enter(event) -> None:
            p = self._config_panel
            if p is not None:
                p.commit_edit() if p.editing else p.activate()
                event.app.invalidate()

        @kb.add("space", filter=cfg_nav)
        def _cfg_space(event) -> None:
            if self._config_panel is not None:
                self._config_panel.activate()
                event.app.invalidate()

        @kb.add("backspace", filter=cfg_edit)
        def _cfg_backspace(event) -> None:
            if self._config_panel is not None:
                self._config_panel.backspace()
                event.app.invalidate()

        @kb.add(Keys.Any, filter=cfg_edit)
        def _cfg_type(event) -> None:
            data = event.data
            if self._config_panel is not None and data and len(data) == 1 and data.isprintable():
                self._config_panel.type_text(data)
                event.app.invalidate()

        @kb.add("c-v", filter=live)
        def _paste_image_key(event) -> None:
            self._paste_clipboard_image()

        @kb.add("c-o")
        def _expand_key(event) -> None:
            self._armed = False
            if self._config_open:
                return
            self._collapse() if self._expanded else self._open_pager()

        @kb.add("q", filter=Condition(lambda: self._expanded))
        def _close_pager(event) -> None:
            self._collapse()

        @kb.add("escape")
        def _esc_key(event) -> None:
            if self._config_open:
                p = self._config_panel
                if p is not None and p.editing:
                    p.cancel_edit()      # leave edit mode, panel stays open
                else:
                    self._close_config()
                event.app.invalidate()
                return
            if self._menu_future is not None and not self._menu_future.done():
                self._menu_future.set_result(self._menu_cancel)
                return
            if self._expanded:
                self._collapse()
            elif self._busy():
                self._cancel_turn(note_edits=True)
            elif self.input.text:
                self.input.reset()

        @kb.add("c-c")
        def _interrupt(event) -> None:
            if self._config_open:
                p = self._config_panel
                if p is not None and p.editing:
                    p.cancel_edit()
                else:
                    self._close_config()
                event.app.invalidate()
                return
            if self._expanded:
                self._collapse()
                return
            if self._busy():
                self._cancel_turn(note_edits=True)  # cancel the running turn
                return
            if self.input.text:
                self.input.reset()  # first Ctrl+C clears the input
                return
            if self._armed:
                event.app.exit()  # second consecutive Ctrl+C exits
            else:
                self._armed = True
                self.console.print(f"[{palette.C_DIM}]press Ctrl+C again to exit[/{palette.C_DIM}]")

        return kb
