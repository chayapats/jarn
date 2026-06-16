"""The terminal front-end (``jarn``) — a Claude-Code-style persistent app.

A **prompt_toolkit** :class:`Application` (``full_screen=False``) pins the input
box at the bottom of the *normal* terminal buffer while all conversation output
is printed *above* it — through :func:`patch_stdout` — into the terminal's native
scrollback (one scroll for everything; native selection/copy). The in-progress
assistant paragraph previews in a small region just above the input.

The agent turn runs as a **cancellable task**, so **Esc** (or Ctrl+C) interrupts
mid-stream while the input stays live. Enter sends, Shift+Enter (Ctrl+J) inserts a
newline, Shift+Tab cycles the permission mode, Tab completes ``/commands`` and
``@files``, ↑/↓ navigate history, Ctrl+O expands the last tool output. Approvals
and pickers are app-native (captured through the same input). It reuses the
UI-agnostic :class:`~jarn.tui.controller.Controller` and
:class:`~jarn.agent.session.SessionDriver`.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import shutil
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar, cast

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    Float,
    FloatContainer,
    HSplit,
    Window,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import BeforeInput
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape as _rich_escape

from jarn.agent.local_backend import CancellableLocalShellBackend
from jarn.agent.session import ApprovalReply, ApprovalRequest, Event, EventKind
from jarn.config.schema import Config, PermissionMode
from jarn.extensibility.commands import completion_catalog, parse_input
from jarn.permissions import ActionKind, RememberScope
from jarn.repl_renderer import TurnRenderer
from jarn.repl_renderer import esc as _esc
from jarn.tui import palette
from jarn.tui.completion import CompletionProvider
from jarn.tui.controller import Controller
from jarn.tui.input_queue import InputQueue
from jarn.tui.logo import SHORTCUT_HINT, splash, splash_compact
from jarn.tui.toolbar import render_toolbar
from jarn.version import __version__

if TYPE_CHECKING:
    from jarn.config.settings import ConfigPanel

Ask = Callable[[str], Awaitable[str]]
Pick = Callable[[list[tuple[str, ApprovalReply]]], Awaitable[ApprovalReply]]
_MenuT = TypeVar("_MenuT")

#: Max diff lines shown in a write/edit approval prompt before collapsing the
#: rest to a "… (+N more lines)" footer, so a large file doesn't flood the TUI.
_APPROVAL_DIFF_MAX_LINES = 40

def run_inline(
    config: Config,
    project_root: Path | None,
    *,
    resume: bool = False,
    project_trusted: bool = True,
) -> int:
    """CLI entry point for native inline mode."""
    palette.configure_ui(theme=config.ui.theme, accent=config.ui.accent)
    with contextlib.suppress(KeyboardInterrupt, EOFError):
        asyncio.run(
            InlineApp(
                config, project_root, resume=resume, project_trusted=project_trusted
            ).run()
        )
    return 0


class _ShellEscapeLexer(Lexer):
    """Colour the input red while it is a ``!`` shell escape.

    A ``!``-prefixed line runs directly on the host shell — no agent, no
    permission engine, no danger-guard — so the live input is rendered in the
    ``shell-escape`` style (red + bold) to make that unmistakable as the user
    types, distinct from a normal agent prompt.
    """

    def lex_document(self, document):  # noqa: ANN001 - prompt_toolkit Document
        is_shell = document.text.lstrip().startswith("!")

        def get_line(lineno: int):
            text = document.lines[lineno]
            return [("class:shell-escape" if is_shell else "", text)]

        return get_line


class InlineApp:
    def __init__(
        self,
        config: Config,
        project_root: Path | None,
        *,
        resume: bool = False,
        project_trusted: bool = True,
    ) -> None:
        self.config = config
        self.controller = Controller(
            config, project_root, project_trusted=project_trusted
        )
        # force_terminal so Rich still emits colour through prompt_toolkit's
        # patch_stdout proxy (which isn't a real TTY). Cap the width to a readable
        # measure (~100 cols) so prose/markdown wrap nicely on wide terminals
        # instead of running long horizontal lines.
        width = min(shutil.get_terminal_size((100, 24)).columns, 100)
        self.console = Console(force_terminal=True, width=width)
        from jarn.config.paths import global_home

        hist = global_home() / "history"
        hist.parent.mkdir(parents=True, exist_ok=True)
        self.input = Buffer(
            multiline=True,
            completer=_SlashFileCompleter(self._completer),
            complete_while_typing=True,
            history=FileHistory(str(hist)),
        )
        self._kb = self._build_keys()
        self._armed = False                       # ctrl+c double-press to exit
        self._last_tool_outputs: list[tuple[str, str]] = []  # for Ctrl+O expand
        self._turn_task: asyncio.Task | None = None
        self._pastes: dict[str, str] = {}          # collapsed token -> full paste
        self._paste_n = 0
        self._input_queue = InputQueue()
        self._resume_task: asyncio.Task | None = None
        self._line_future: asyncio.Future | None = None       # app-native asks
        self._menu_future: asyncio.Future | None = None     # arrow-key pickers
        self._menu_options: list[tuple[str, object]] = []
        self._menu_index = 0
        self._menu_header = ""
        self._menu_cancel: object | None = None
        self._stream_text = ""                    # in-progress region above input
        self._turn_start: float | None = None     # for the elapsed timer
        self._thinking_word = ""
        self._flash_html: HTML | None = None       # transient region message
        self._flash_until = 0.0
        self._expanded = False                     # pager overlay open?
        self._last_todos_sig: str | None = None    # de-dupe the todo checklist
        self._pager_buffer = Buffer(read_only=True)
        self._pager_window: Window | None = None
        self._config_open = False                  # interactive /config panel open?
        self._config_panel: ConfigPanel | None = None
        self._config_window: Window | None = None
        self._resume_on_start = resume               # show the /resume picker on launch
        self.app: Application | None = None

    # -- lifecycle ----------------------------------------------------------

    async def run(self) -> None:
        c = self.console
        _model = self.config.resolved_main_model()
        _mode = self.config.permission_mode.value
        _splash_cfg = self.config.ui.splash
        from jarn.config.paths import global_home
        _first_run_marker = global_home() / "state" / "first_run_done"
        _is_first_run = not _first_run_marker.exists()
        if _is_first_run:
            _first_run_marker.parent.mkdir(parents=True, exist_ok=True)
            _first_run_marker.touch()
            c.print(splash(__version__, _model, _mode))
        elif _splash_cfg == "full":
            c.print(splash(__version__, _model, _mode))
        elif _splash_cfg == "compact":
            c.print(splash_compact(__version__, _model, _mode))
        else:  # off
            c.print(SHORTCUT_HINT)
        c.print(f"[{palette.C_DIM}]terminal mode · Enter send · Shift+Enter newline · "
                f"Shift+Tab mode · Esc interrupt · Ctrl+O or /expand · Ctrl+C exit[/{palette.C_DIM}]")
        ok, message = self.controller.validate()
        if not ok:
            c.print(
                f"[{palette.C_ERROR}]Provider not ready: {_rich_escape(message)}"
                f"[/{palette.C_ERROR}]  [{palette.C_DIM}]run `jarn setup`[/{palette.C_DIM}]"
            )
        # One-time untrusted-project notice: the review-only floor is active and
        # capability keys were stripped. Surfaced once in scrollback (not per turn)
        # so the user knows why modes are clamped and how to unlock.
        if not self.controller.project_trusted and self.controller.project_root is not None:
            c.print(
                f"[{palette.C_WARN}]⚠ This project is untrusted[/{palette.C_WARN}] "
                f"[{palette.C_DIM}]— review-only floor active (modes clamped to plan; "
                f"project hooks/MCP/providers ignored). Run [/{palette.C_DIM}]"
                f"[{palette.C_NOTICE}]/trust[/{palette.C_NOTICE}]"
                f"[{palette.C_DIM}] or [/{palette.C_DIM}]"
                f"[{palette.C_NOTICE}]jarn trust[/{palette.C_NOTICE}]"
                f"[{palette.C_DIM}] to unlock.[/{palette.C_DIM}]"
            )
        self._warm_pricing_catalog()
        await self._ensure_extensions()
        self.app = self._build_app()
        try:
            # patch_stdout routes all printed output above the pinned input, into
            # the terminal's native scrollback — the Claude-Code layout.
            with patch_stdout(raw=True):
                # Schedule the resume picker as a background task so it runs once
                # the loop is live (_ask needs the running app/event loop).
                if self._resume_on_start:
                    # Keep a strong reference — the loop only weakly holds tasks.
                    self._resume_task = asyncio.create_task(self._resume_picker())
                await self.app.run_async()
        finally:
            await self.controller.aclose()

    async def _ensure_extensions(self) -> None:
        """Load skills/commands/MCP before the first turn so /skills and custom
        slash commands work immediately after launch."""
        try:
            await self.controller.ensure_runtime()
        except Exception as exc:  # noqa: BLE001
            self.console.print(
                f"[{palette.C_WARN}]extensions not loaded:[/{palette.C_WARN}] {exc}  "
                f"[{palette.C_DIM}]· send a message or run jarn setup[/{palette.C_DIM}]"
            )

    def _warm_pricing_catalog(self) -> None:
        """Refresh the OpenRouter price/context-window catalog in the background
        (only when an OpenRouter provider is configured). Network-safe and
        non-blocking — the gauge/cost just use the prior cache until it lands."""
        from jarn.config.schema import ProviderType

        if not any(p.type is ProviderType.OPENROUTER for p in self.config.providers.values()):
            return
        import threading

        from jarn.cost import pricing

        threading.Thread(target=pricing.warm_catalog, daemon=True).start()

    # -- layout -------------------------------------------------------------

    def _build_app(self) -> Application:
        # dont_extend_height so each window sizes to its CONTENT (not the screen):
        # otherwise the windows expand to their max and the input floats up the
        # screen with a gap above the toolbar.
        stream = Window(
            FormattedTextControl(self._stream_control),
            height=Dimension(min=0, max=8), wrap_lines=True,
            dont_extend_height=True, style=f"fg:{palette.C_DIM}",
        )
        prompt = Window(
            BufferControl(
                self.input,
                input_processors=[BeforeInput("› ", style="bold")],
                lexer=_ShellEscapeLexer(),
            ),
            height=Dimension(min=1, max=10), wrap_lines=True, dont_extend_height=True,
        )
        toolbar = Window(
            FormattedTextControl(self._toolbar), height=1, style="class:bottom-toolbar",
        )
        # In-app pager overlay (Ctrl+O / /expand): a scrollable read-only view of
        # the last turn's full tool output, toggled with Ctrl+O. Rendered by the
        # app so the turn keeps running behind it; the input + toolbar stay PINNED
        # at the bottom (only the region above swaps stream ↔ pager).
        self._pager_window = Window(
            BufferControl(self._pager_buffer, focusable=True), wrap_lines=True,
        )
        pager_header = Window(
            FormattedTextControl(self._pager_header), height=1, style="class:bottom-toolbar",
        )
        # Interactive settings panel overlay (/config). FormattedTextControl is
        # non-focusable; the global key bindings (gated on _config_open) drive it.
        self._config_window = Window(
            FormattedTextControl(self._config_render), wrap_lines=True,
        )
        top = HSplit([
            ConditionalContainer(
                stream,
                filter=Condition(lambda: not self._expanded and not self._config_open),
            ),
            ConditionalContainer(
                HSplit([pager_header, self._pager_window]),
                filter=Condition(lambda: self._expanded),
            ),
            ConditionalContainer(
                self._config_window, filter=Condition(lambda: self._config_open)
            ),
        ])
        root = FloatContainer(
            HSplit([top, prompt, toolbar]),
            floats=[Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=8))],
        )
        return Application(
            layout=Layout(root, focused_element=prompt),
            key_bindings=self._kb,
            style=Style.from_dict(palette.toolbar_style_dict()),
            full_screen=False,
            mouse_support=False,
            refresh_interval=0.2,  # animate the thinking spinner / elapsed timer
        )

    def _busy(self) -> bool:
        return self._turn_task is not None and not self._turn_task.done()

    def _cancel_turn(self) -> None:
        """Cancel the running turn AND kill any shell process it spawned.

        Cancelling the asyncio task alone leaves a host subprocess (e.g. a long
        ``sleep``/build) running; terminating the backend's process tree makes
        Esc/Ctrl+C actually stop the work."""
        if self._turn_task is not None:
            self._turn_task.cancel()
        killed = self.controller.terminate_shells()
        if killed:
            self.console.print(f"[{palette.C_DIM}]stopped {killed} running command(s)[/{palette.C_DIM}]")

    def _set_stream(self, text: str) -> None:
        """Show ``text`` in the live region above the input (in-progress prose or
        an approval/picker prompt). Empty string collapses the region."""
        self._stream_text = text
        if self.app is not None:
            self.app.invalidate()

    def _stream_control(self):
        """Region above the input: in-progress text, an animated thinking
        indicator while the model works, a transient flash (mode change, etc.),
        else nothing."""
        if self._menu_future is not None and self._menu_options:
            return self._menu_html()
        if self._stream_text:
            return self._stream_text
        if self._busy():
            return self._thinking_line()
        if self._flash_html is not None and time.monotonic() < self._flash_until:
            return self._flash_html
        return ""

    def _flash(self, html: HTML, secs: float = 2.0) -> None:
        """Show a transient one-line message above the input (auto-clears) — used
        for ephemeral feedback (mode switch, etc.) so nothing lands in scrollback."""
        self._flash_html = html
        self._flash_until = time.monotonic() + secs
        if self.app is not None:
            self.app.invalidate()

    def _menu_html(self) -> HTML:
        lines: list[str] = []
        if self._menu_header:
            lines.append(f'<style fg="{palette.C_DIM}">{_esc(self._menu_header)}</style>')
        for i, (label, _) in enumerate(self._menu_options):
            if i == self._menu_index:
                lines.append(
                    f'<style fg="{palette.C_USER}"><b>› {_esc(label)}</b></style>'
                )
            else:
                lines.append(f'<style fg="{palette.C_DIM}">  {_esc(label)}</style>')
        lines.append(
            f'<style fg="{palette.C_DIM}">↑/↓ select · Enter confirm</style>'
        )
        return HTML("\n".join(lines))

    def _thinking_line(self) -> HTML:
        start = self._turn_start or time.monotonic()
        elapsed = int(time.monotonic() - start)
        frame = palette.SPINNER_FRAMES[int(time.monotonic() * 5) % len(palette.SPINNER_FRAMES)]
        toks = self.controller.tracker.total.total_tokens
        word = self._thinking_word or "Working"
        return HTML(
            f'{palette.styled_fg(palette.C_TOOL, frame)} '
            f'{palette.styled_fg(palette.C_DIM, f"{word}… ({elapsed}s · {toks} tok · esc to interrupt)")}'
        )

    # -- prompt chrome ------------------------------------------------------

    def _toolbar(self):
        from jarn.providers import strip_profile

        cfg = self.controller.config
        model = strip_profile(cfg.resolved_main_model() or "unconfigured", cfg.default_profile)
        tracker = self.controller.tracker
        ctx_frac: float | None = None
        ctx = self.controller.context_status()
        if ctx is not None:
            _tokens, _window, ctx_frac = ctx
        width = shutil.get_terminal_size((100, 24)).columns
        return render_toolbar(
            model=model,
            mode=cfg.permission_mode.value,
            cost_line=tracker.summary_line(),
            cost_status=tracker.status(),
            queue_count=len(self._input_queue),
            context_frac=ctx_frac,
            width=width,
        )

    def _completer(self) -> CompletionProvider:
        custom = self.controller.runtime.commands if self.controller.runtime else None
        return CompletionProvider(
            command_catalog=completion_catalog(custom),
            project_root=self.controller.project_root,
        )

    async def _ask(self, prompt: str) -> str:
        """App-native line capture for pickers: show the prompt in the region above
        the input and resolve on the next Enter-submitted line."""
        self._set_stream(prompt)
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
    ) -> _MenuT | None:
        """Claude Code-style menu: ↑/↓ to highlight, Enter to confirm, Esc cancel."""
        self._menu_options = cast(list[tuple[str, object]], options)
        self._menu_index = 0
        self._menu_header = header
        self._menu_cancel = cancel_returns
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
            self._set_stream("")

    async def _pick_approval(self, options: list[tuple[str, ApprovalReply]]) -> ApprovalReply:
        deny = options[-1][1]
        picked = await self._pick_menu(
            options,
            header="Approve · ↑/↓ · Enter · Esc cancel",
            cancel_returns=deny,
        )
        return deny if picked is None else picked

    async def _render_todos(self) -> None:
        """Print the current plan checklist into scrollback after a turn, de-duped
        so an unchanged list is never reprinted."""
        todos = await self.controller.todos()
        sig = repr([(t.get("content"), t.get("status")) for t in todos])
        if not todos or sig == self._last_todos_sig:
            return
        self._last_todos_sig = sig
        glyphs = {"completed": "[#3ee07a]✔[/#3ee07a]",
                  "in_progress": "[#22d3ee]◐[/#22d3ee]",
                  "pending": "[#7c8f94]☐[/#7c8f94]"}
        self.console.print()
        self.console.print(f"[{palette.C_TOOL}]⏺[/{palette.C_TOOL}] [bold]Todos[/bold]")
        for t in todos:
            status = t.get("status", "pending")
            g = glyphs.get(status, glyphs["pending"])
            content = _rich_escape(str(t.get("content", "")))
            body = f"[{palette.C_DIM}]{content}[/{palette.C_DIM}]" if status == "completed" else content
            self.console.print(f"  {g} {body}")

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

    # -- interactive settings panel (/config) -------------------------------

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

    # -- key bindings -------------------------------------------------------

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
                self._thinking_word = random.choice(palette.THINKING_WORDS)
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

        @kb.add("s-tab", filter=live)
        def _cycle_mode(event) -> None:
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
                self._cancel_turn()
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
                self._cancel_turn()  # cancel the running turn
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

    # -- turn / command dispatch -------------------------------------------

    def _drain_queue(self) -> None:
        """Start the next queued line as a new turn (mirrors the submit path)."""
        if not self._busy():
            item = self._input_queue.pop_next()
            if item is None:
                return
            self.console.print(
                f"[{palette.C_USER}]›[/{palette.C_USER}] {_rich_escape(item.display)}"
            )
            self._turn_start = time.monotonic()
            self._thinking_word = random.choice(palette.THINKING_WORDS)
            self._turn_task = asyncio.create_task(self._handle(item.payload))

    async def _handle(self, text: str) -> None:
        """Run one submitted line (a turn, a command, or a shell escape) as a cancellable task."""
        try:
            parsed = parse_input(text)
            if parsed.is_command:
                await self._command(parsed.name, parsed.args)
            elif parsed.is_shell:
                await self._shell_escape(parsed.shell_command)
            else:
                # Reset + pass the list as a sink so tool outputs accumulate LIVE
                # — Ctrl+O works mid-turn, not only after the answer completes.
                self._last_tool_outputs = []
                await _run_turn(
                    self.console, self.controller, text, self._ask,
                    pick=self._pick_approval,
                    live_sink=self._set_stream, spinner=False,
                    tool_sink=self._last_tool_outputs,
                )
                await self._render_todos()
        except asyncio.CancelledError:
            self.console.print(f"\n[{palette.C_DIM}]interrupted[/{palette.C_DIM}]")
        except Exception as exc:  # noqa: BLE001
            self.console.print(
                f"[{palette.C_ERROR}]{_rich_escape(str(exc))}[/{palette.C_ERROR}]"
            )
        finally:
            self._turn_task = None
            self._turn_start = None
            self._set_stream("")
            if self.app is not None:
                self.app.invalidate()
            self._drain_queue()

    async def _shell_escape(self, command: str) -> None:
        """Run a ``! <cmd>`` shell escape directly — no agent round-trip, no tokens.

        The user typed the ``!`` prefix themselves, so the permission engine is
        bypassed entirely (same trust model as the user's own terminal).  Output
        is printed to the scrollback console.  Reuses
        :class:`~jarn.agent.local_backend.CancellableLocalShellBackend` so
        truncation and Esc/cancel behaviour match the agent's Bash tool.
        """
        c = self.console
        if not command:
            c.print(f"[{palette.C_DIM}]! <cmd>  — run a shell command directly[/{palette.C_DIM}]")
            return
        # Make it unmistakable this runs on the host, outside the agent: no
        # permission engine, no danger-guard, no sandbox.
        c.print(
            f"[{palette.C_ERROR}]⚡ host shell[/{palette.C_ERROR}] "
            f"[{palette.C_DIM}]— runs on your machine directly; no agent, no approval[/{palette.C_DIM}]"
        )
        cwd = self.controller.project_root or Path(".")
        backend = CancellableLocalShellBackend(str(cwd))
        # execute is blocking; offload to a thread so the event-loop stays live
        # (Esc can still fire while the command runs).
        response = await asyncio.to_thread(backend.execute, command)
        c.print(response.output)

    # -- commands -----------------------------------------------------------

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
            await _run_turn(
                c, self.controller, rt.commands[name].render(args), self._ask,
                pick=self._pick_approval,
                live_sink=self._set_stream, spinner=False,
                tool_sink=self._last_tool_outputs,
            )
            await self._render_todos()
            return
        if name == "compact":
            c.print(f"[{palette.C_DIM}]compacting…[/{palette.C_DIM}]")
            try:
                summary = await self.controller.compact()
                c.print(
                    f"[{palette.C_NOTICE}]Compacted.[/{palette.C_NOTICE}] {_rich_escape(summary)}"
                    if summary
                    else "Nothing to compact yet."
                )
            except Exception as exc:  # noqa: BLE001
                c.print(
                    f"[{palette.C_ERROR}]compact failed: {_rich_escape(str(exc))}"
                    f"[/{palette.C_ERROR}]"
                )
            return
        if name == "expand":  # same as Ctrl+O — reliable even if the key is eaten
            self._open_pager()
            return
        if name == "resume":
            await self._resume_picker()
            return
        if name == "queue":
            await self._cmd_queue(args)
            return
        if name in ("model", "mode") and not args.strip():
            await self._pick_model_or_mode(name)
            return
        result = self.controller.handle_command(name, args)
        c.print(result.text)
        if result.rebuilt:
            self.controller.runtime = None
        if result.quit and self.app is not None:
            self.app.exit()

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
            _apply_mode_ref(self.controller, c, str(chosen))

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


# -- prompt_toolkit completer adapter --------------------------------------

class _SlashFileCompleter(Completer):
    """Bridges :class:`CompletionProvider` to prompt_toolkit completions."""

    def __init__(self, provider_factory: Callable[[], CompletionProvider]) -> None:
        self._factory = provider_factory

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if "\n" in text:
            return
        for cand in self._factory().complete(text):
            yield Completion(
                cand.replacement,
                start_position=-len(text),
                display=cand.label,
                display_meta=cand.description or None,
            )


# -- turn streaming (module-level so it's unit-testable) -------------------

async def _run_turn(
    console: Console,
    controller: Controller,
    text: str,
    ask: Ask,
    *,
    pick: Pick | None = None,
    live_sink: Callable[[str], None] | None = None,
    spinner: bool = True,
    tool_sink: list[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """Stream a turn; return the turn's expandable ``(tool, full output)`` pairs.

    If ``tool_sink`` is given, tool outputs are appended to it live (so a pager
    can read them mid-turn)."""
    try:
        await controller.ensure_runtime()
    except Exception as exc:  # noqa: BLE001
        console.print(
            f"[{palette.C_ERROR}]agent not ready: {_rich_escape(str(exc))}[/{palette.C_ERROR}]  "
            f"[{palette.C_DIM}]· /help or jarn setup[/{palette.C_DIM}]"
        )
        return []

    # Surface a degraded/error runtime state once per session (MCP server down,
    # sandbox fell back to host, or an ambient key would leak to a non-local
    # async-subagent url). Without this it lands only in the rotating log file.
    if (
        not controller.health_notice_shown
        and controller.last_error
        and controller.health in ("degraded", "error")
    ):
        controller.health_notice_shown = True
        _warn_color, _glyph = (
            (palette.C_ERROR, "✗") if controller.health == "error" else (palette.C_WARN, "⚠")
        )
        _doctor_hint = (
            f" [{palette.C_DIM}]— run /doctor[/{palette.C_DIM}]"
            if controller.health == "error"
            else ""
        )
        console.print(
            f"[{_warn_color}]{_glyph} {_rich_escape(controller.last_error)}[/{_warn_color}]{_doctor_hint}"
        )

    controller.record_session_title(text, when=time.time())
    turn_text = controller.enrich_turn_input(text)

    async def approver(req: ApprovalRequest) -> ApprovalReply:
        return await _approve(console, controller, req, ask=ask, pick=pick)

    renderer = TurnRenderer(
        console, lambda: controller.tracker.total.total_tokens,
        live_sink=live_sink, spinner=spinner, tool_sink=tool_sink,
    )
    try:
        # Turn loop with transparent model fallback: if a turn fails with a
        # retryable provider error *before* emitting any visible output, rotate
        # to the next fallback model and retry; otherwise surface the error.
        # ``produced`` gates on emitted UI events. On retry we resume from the
        # thread's existing state (LangGraph already checkpointed the user
        # message before the failed model call) so the user turn isn't duplicated.
        resume = False
        while True:
            driver = controller.make_driver(approver)
            produced = False
            pending_error: Event | None = None
            payload = "" if resume else turn_text
            async for event in driver.run_turn(payload, resume=resume):
                if event.kind is EventKind.TEXT:
                    renderer.on_text(event.text)
                    produced = True
                elif event.kind is EventKind.REASONING:
                    renderer.on_reasoning(event.text)
                    produced = True
                elif event.kind is EventKind.TOOL_START:
                    renderer.on_tool(
                        event.text,
                        event.data.get("args", {}),
                        tool_call_id=event.data.get("tool_call_id"),
                    )
                    produced = True
                elif event.kind is EventKind.TOOL_END:
                    renderer.on_tool_end(
                        event.text,
                        event.data.get("summary", ""),
                        event.data.get("full", ""),
                        tool_call_id=event.data.get("tool_call_id"),
                    )
                    produced = True
                elif event.kind is EventKind.NOTICE or (
                    event.kind is EventKind.APPROVAL
                    and event.text.startswith(("blocked", "rejected"))
                ):
                    renderer.on_notice(f"[{palette.C_NOTICE}]{event.text}[/{palette.C_NOTICE}]")
                    produced = True
                elif event.kind is EventKind.APPROVAL:
                    produced = True  # a tool was authorized → has a side effect
                elif event.kind is EventKind.ERROR:
                    pending_error = event

            if pending_error is None:
                controller.reset_model_rotation()  # back to primary on success
                break
            if pending_error.data.get("retryable") and not produced:
                new_ref = controller.rotate_to_fallback()
                if new_ref:
                    try:
                        await controller.ensure_runtime()
                    except Exception as exc:  # noqa: BLE001
                        renderer.on_notice(f"[{palette.C_ERROR}]fallback unavailable: {exc}[/{palette.C_ERROR}]")
                        break
                    renderer.on_notice(
                        f"[{palette.C_NOTICE}]model error, retrying with {new_ref}…[/{palette.C_NOTICE}]"
                    )
                    resume = True  # user message is already in state; don't re-send
                    continue
            renderer.on_notice(f"[{palette.C_ERROR}]{pending_error.text}[/{palette.C_ERROR}]")
            break
    except KeyboardInterrupt:
        renderer.cancel()
    finally:
        renderer.finish()

    controller.record_turn(when=time.time())
    if controller.should_auto_compact():
        console.print(f"[{palette.C_DIM}]context full — auto-compacting…[/{palette.C_DIM}]")
        try:
            summary = await controller.compact()
            if summary:
                console.print(f"[{palette.C_NOTICE}]Auto-compacted; continuing in a fresh thread.[/{palette.C_NOTICE}]")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[{palette.C_ERROR}]auto-compact failed: {exc}[/{palette.C_ERROR}]")
    return renderer.tool_outputs


def _approval_options(request: ApprovalRequest) -> list[tuple[str, ApprovalReply]]:
    """Build the Claude Code-style approval menu for a gated action."""
    options: list[tuple[str, ApprovalReply]] = [
        ("Allow once", ApprovalReply(True, RememberScope.ONCE)),
    ]
    if request.result.block_remember_always:
        options.append(("Allow for session", ApprovalReply(True, RememberScope.SESSION)))
    else:
        options.append(("Allow always", ApprovalReply(True, RememberScope.ALWAYS)))
    options.append(("Deny", ApprovalReply(False, message="rejected by user")))
    return options


async def _approve(
    console: Console,
    controller: Controller,
    request: ApprovalRequest,
    *,
    ask: Ask | None = None,
    pick: Pick | None = None,
) -> ApprovalReply:
    a = request.action
    what = (f"run: {a.target}" if a.kind is ActionKind.SHELL
            else f"write: {a.target}" if a.kind is ActionKind.WRITE
            else f"{a.kind.value}: {a.target}")
    danger = "[red]⚠ DANGEROUS — [/red]" if request.result.dangerous else ""
    console.print(f"\n{danger}[bold]Approve?[/bold] {what}  [{palette.C_DIM}]({request.result.reason})[/{palette.C_DIM}]")
    if a.kind is ActionKind.WRITE:
        from jarn.tui.widgets.diff import diff_from_edit_args

        # Cap the diff so writing a large file doesn't flood the prompt; the
        # full content is what's being approved, not what needs to be read.
        diff = diff_from_edit_args(request.args or {}, max_lines=_APPROVAL_DIFF_MAX_LINES)
        if diff is not None:
            console.print(diff)
    options = _approval_options(request)
    if pick is not None:
        return await pick(options)
    # Text fallback for headless tests / non-interactive callers.
    if ask is None:
        return options[-1][1]
    choices = ("[a]llow once / [s]ession / [r]eject" if request.result.block_remember_always
               else "[a]llow once / [s]ession / [w] always / [r]eject")
    ans = (await ask(f"  {choices}: ")).strip().lower()
    if ans in ("a", "allow", "y", "yes"):
        return options[0][1]
    if ans in ("s", "session"):
        return ApprovalReply(True, RememberScope.SESSION)
    if ans in ("w", "always") and not request.result.block_remember_always:
        return ApprovalReply(True, RememberScope.ALWAYS)
    return options[-1][1]


def _apply_model_ref(controller: Controller, console: Console, chosen: str) -> None:
    from jarn.providers import qualify_model_ref

    # Treat the ref as already-qualified only when its first segment names a
    # configured provider profile. Otherwise it's a bare model id whose own
    # vendor prefix happens to contain a "/" (e.g. "deepseek/deepseek-chat")
    # — qualify it under the default profile so it routes correctly.
    first = chosen.split("/", 1)[0]
    ref = chosen if first in controller.config.providers else qualify_model_ref(
        chosen, controller.config.default_profile
    )
    controller.apply_model(ref)
    console.print(
        f"[{palette.C_NOTICE}]model → {controller.config.resolved_main_model()}[/{palette.C_NOTICE}]"
    )


def _apply_mode_ref(controller: Controller, console: Console, chosen: str) -> None:
    try:
        controller.apply_mode(PermissionMode(chosen).value)
        console.print(f"[{palette.C_NOTICE}]mode → {chosen}[/{palette.C_NOTICE}]")
    except ValueError:
        console.print(f"[{palette.C_ERROR}]unknown mode {chosen!r}[/{palette.C_ERROR}]")


