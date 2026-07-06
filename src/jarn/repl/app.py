"""The terminal front-end (:class:`InlineApp`) — layout, stream, and lifecycle."""
# mypy: ignore-errors

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

from prompt_toolkit.application import Application
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, HTML
from prompt_toolkit.history import FileHistory
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
from prompt_toolkit.layout.processors import (
    AppendAutoSuggestion,
    BeforeInput,
    Transformation,
)
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape as _rich_escape

from jarn.config.schema import Config
from jarn.extensibility.commands import completion_catalog, parse_input
from jarn.repl import turn as repl_turn
from jarn.repl.commands import CommandMixin, format_todos
from jarn.repl.completer import _ShellEscapeLexer, _SlashFileCompleter
from jarn.repl.keys import KeysMixin
from jarn.repl.overlays import OverlayMixin
from jarn.repl_renderer import REASONING_STREAM_PREFIX, _current_width
from jarn.repl_renderer import esc as _esc
from jarn.tui import palette
from jarn.tui.completion import CompletionProvider
from jarn.tui.controller import Controller
from jarn.tui.input_queue import InputQueue
from jarn.tui.logo import SHORTCUT_HINT, splash, splash_compact
from jarn.tui.notify import notify, set_title
from jarn.tui.toolbar import render_toolbar
from jarn.version import __version__

if TYPE_CHECKING:
    from jarn.config.settings import ConfigPanel

#: Max body lines of the LIVE plan checklist above the input, so a long plan can't
#: push the input/toolbar off-screen (the committed end-of-turn render is uncapped).
_LIVE_TODOS_CAP = 8


class _GhostAutoSuggestion(AppendAutoSuggestion):
    """AppendAutoSuggestion that hides ghost text when the completion dropdown is open.

    prompt_toolkit renders the CompletionsMenu float and the AppendAutoSuggestion
    processor independently; without this override both would appear simultaneously.
    Subclassing and returning an empty Transformation when complete_state is set
    keeps the accept-key rule (dropdown wins) consistent with the visual state."""

    def apply_transformation(self, ti):  # type: ignore[override]
        if ti.buffer_control.buffer.complete_state is not None:
            return Transformation(ti.fragments)
        return super().apply_transformation(ti)


class InlineApp(OverlayMixin, KeysMixin, CommandMixin):
    def __init__(
        self,
        config: Config,
        project_root: Path | None,
        *,
        resume: bool = False,
        project_trusted: bool = True,
        detected_theme: str | None = None,
    ) -> None:
        self.config = config
        # Terminal background resolved ONCE at startup (light/dark) when
        # ui.theme is "auto"; a runtime `/theme auto` reuses this instead of
        # re-probing (a runtime OSC-11 probe races prompt_toolkit's input reader).
        self._detected_theme = detected_theme
        self.controller = Controller(
            config, project_root, project_trusted=project_trusted
        )
        # Derive project name once (used in OSC 2 title strings).
        self._proj_name: str = project_root.name if project_root is not None else "jarn"
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
            auto_suggest=AutoSuggestFromHistory(),
        )
        self._kb = self._build_keys()
        self._armed = False                       # ctrl+c double-press to exit
        self._last_esc_ts: float | None = None    # Esc-Esc chord: timestamp of last idle Esc
        self._hinted: bool = False                # empty-Enter hint shown once per session
        self._last_tool_outputs: list[tuple[str, str]] = []  # for Ctrl+O expand
        self._turn_task: asyncio.Task | None = None
        self._pastes: dict[str, str] = {}          # collapsed token -> full paste
        self._paste_n = 0
        self._input_queue = InputQueue()
        self._resume_task: asyncio.Task | None = None
        self._line_future: asyncio.Future | None = None       # app-native asks
        self._yolo_confirm_inflight = False    # de-dupe rapid Shift+Tab→yolo presses
        self._menu_future: asyncio.Future | None = None     # arrow-key pickers
        self._menu_options: list[tuple[str, object]] = []
        self._menu_index = 0
        self._menu_header = ""
        self._menu_cancel: object | None = None
        # Single-keypress fast-path for the *approval* menu only: maps a typed
        # char (e.g. "y"/"a" → allow, "n"/"d" → deny) to the option value it
        # resolves to. None for every other picker, so letter keys type normally.
        self._menu_fastkeys: dict[str, object] | None = None
        # History picker filter: non-None while the Ctrl+R picker is open.
        # Empty string = filter active but no chars typed yet.  Set by
        # _history_picker; updated by _history_type_filter / _history_backspace_filter.
        self._menu_filter: str | None = None
        # Full unfiltered list of (display-label, full-text) for the history
        # picker, held here so the key handler can recompute matches on every char.
        self._history_all_options: list[tuple[str, str]] = []
        self._stream_text = ""                    # in-progress region above input
        # Cache for the live markdown->ANSI render: _stream_control runs on every
        # prompt_toolkit redraw (refresh_interval + invalidate per delta), so cache
        # (source, rendered_ansi) and only re-render when the buffer actually grew.
        self._stream_md_cache: tuple[str, str, int] | None = None
        # True while the live region holds a reasoning block (render it plain dim,
        # not markdown — see _set_stream / _stream_control).
        self._stream_is_reasoning = False
        self._turn_start: float | None = None     # for the elapsed timer
        # One stable thinking word per session (don't re-roll every turn — the
        # churning label reads as noise). Shared with the renderer spinner.
        self._thinking_word = palette.session_thinking_word()
        self._turn_stream_chars = 0   # streamed output chars this turn (live tok estimate)
        self._turn_base_output = 0    # tracker output tokens at turn start (real-usage delta)
        self._turn_base_input = 0     # tracker input tokens at turn start (prompt-size delta)
        self._first_token_at: float | None = None   # first streamed delta (for tok/s)
        self._flash_html: HTML | None = None       # transient region message
        self._flash_until = 0.0
        self._expanded = False                     # pager overlay open?
        self._last_todos_sig: str | None = None    # de-dupe the todo checklist
        # Live plan checklist shown above the input DURING a turn (updated in place
        # on each write_todos; None = nothing to show). Cleared at turn end, where
        # the committed _render_todos replaces it — see _on_todos_live.
        self._live_todos: list[dict] | None = None
        self._pager_buffer = Buffer(read_only=True)
        self._pager_window: Window | None = None
        #: Resolved when the pager closes — lets an approval prompt block on the
        #: user finishing a "view full diff" before it re-shows the menu.
        self._pager_closed: asyncio.Future[None] | None = None
        self._config_open = False                  # interactive /config panel open?
        self._config_panel: ConfigPanel | None = None
        self._config_window: Window | None = None
        self._resume_on_start = resume               # show the /resume picker on launch
        self.app: Application | None = None

    async def run(self) -> None:
        c = self.console
        _splash_cfg = self.config.ui.splash
        from jarn.config.paths import global_home
        _first_run_marker = global_home() / "state" / "first_run_done"
        _is_first_run = not _first_run_marker.exists()
        if _is_first_run:
            # Recording the marker is best-effort: a read-only home must re-show
            # the full splash next time, never crash startup.
            with contextlib.suppress(OSError):
                _first_run_marker.parent.mkdir(parents=True, exist_ok=True)
                _first_run_marker.touch()
            c.print(splash(__version__))
        elif _splash_cfg == "full":
            c.print(splash(__version__))
        elif _splash_cfg == "compact":
            c.print(splash_compact(__version__))
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
        # Startup notice: name the context file loaded into the system prompt.
        if self.controller.project_trusted and self.controller.project_root is not None:
            from jarn.memory.context import resolve_context_file
            _ctx_path = resolve_context_file(
                self.controller.project_root,
                context_files=self.config.compat.context_files,
            )
            if _ctx_path is not None:
                c.print(
                    f"[{palette.C_DIM}]context: {_ctx_path.name}[/{palette.C_DIM}]"
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
        self._title_hook("idle")   # Set idle title on app start
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
            self._title_hook("quit")   # Reset terminal title to plain "jarn" on exit

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
        from jarn.cost.pricing import network_fetch_enabled

        if not any(p.type is ProviderType.OPENROUTER for p in self.config.providers.values()):
            return
        if not network_fetch_enabled(config_network=self.config.pricing.network):
            self.console.print(
                f"[{palette.C_DIM}]Network pricing catalog disabled "
                f"— using bundled/override prices.[/{palette.C_DIM}]"
            )
            return
        import threading

        from jarn.cost import pricing

        threading.Thread(
            target=pricing.warm_catalog,
            kwargs={"network": self.config.pricing.network},
            daemon=True,
        ).start()
    def _build_app(self) -> Application:
        # dont_extend_height so each window sizes to its CONTENT (not the screen):
        # otherwise the windows expand to their max and the input floats up the
        # screen with a gap above the toolbar.
        # The live block is a transient FORMATTED markdown render of the in-progress
        # run (prose commits to scrollback once at the run seam), so an unclosed
        # fence / long paragraph is no longer clipped at a hard 8 lines.
        # dont_extend_height keeps it content-sized; _stream_height adds a
        # terminal-height-aware cap (rows - reserve) so a very tall live block clips
        # instead of pushing the input + toolbar off the bottom of the screen.
        stream = Window(
            FormattedTextControl(self._stream_control),
            height=self._stream_height, wrap_lines=True,
            dont_extend_height=True, style=f"fg:{palette.C_DIM}",
        )
        prompt = Window(
            BufferControl(
                self.input,
                input_processors=[
                    BeforeInput("› ", style="bold"),
                    # Ghost autosuggest: renders the upcoming suggestion suffix in
                    # a dim colour after the cursor.  Hidden when the completion
                    # dropdown is open so both UI layers never appear together.
                    _GhostAutoSuggestion(style=f"fg:{palette.C_DIM}"),
                ],
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
        from prompt_toolkit.output import DummyOutput, create_output

        try:
            output = create_output()
        except Exception:  # noqa: BLE001 — headless Windows CI has no console buffer
            output = DummyOutput()
        return Application(
            layout=Layout(root, focused_element=prompt),
            key_bindings=self._kb,
            style=Style.from_dict(palette.toolbar_style_dict()),
            full_screen=False,
            mouse_support=False,
            refresh_interval=0.2,  # animate the thinking spinner / elapsed timer
            output=output,
        )

    def _title_isatty(self) -> bool:
        """Whether the app's output stream is a TTY — monkeypatchable in tests."""
        f = self.console.file
        return bool(getattr(f, "isatty", lambda: False)())

    def _title_hook(self, state: str) -> None:
        """Emit an OSC 2 terminal title for the given lifecycle state.

        States: ``"working"`` (✳), ``"approval"`` (⏸), ``"idle"``, ``"quit"``.
        Respects ``ui.terminal_title`` and the TTY guard in :func:`set_title`.
        """
        if state == "working":
            text = f"✳ jarn — {self._proj_name}"
        elif state == "approval":
            text = f"⏸ jarn — {self._proj_name}"
        elif state == "idle":
            text = f"jarn — {self._proj_name}"
        elif state == "quit":
            text = "jarn"
        set_title(text, settings=self.config.ui, write=self.console.file.write, isatty=self._title_isatty)

    def _busy(self) -> bool:
        return self._turn_task is not None and not self._turn_task.done()

    def _cancel_turn(self, *, note_edits: bool = False) -> None:
        """Cancel the running turn AND kill any shell process it spawned.

        Cancelling the asyncio task alone leaves a host subprocess (e.g. a long
        ``sleep``/build) running; terminating the backend's process tree makes
        Esc/Ctrl+C actually stop the work.

        ``note_edits`` is set by the Esc/Ctrl+C path (not ``/abort``, which
        rolls back itself): when the cancelled turn already applied file edits,
        those edits stay on disk, so we print a clear note that they remain and
        how to revert them (``/abort`` rolls them back; offered when a
        turn-start checkpoint exists)."""
        if self._turn_task is not None:
            self._turn_task.cancel()
        killed = self.controller.terminate_shells()
        if killed:
            self.console.print(f"[{palette.C_DIM}]stopped {killed} running command(s)[/{palette.C_DIM}]")
        if note_edits and self._turn_made_edits():
            note = self.controller.cancel_edit_note()
            if note:
                self.console.print(f"[{palette.C_DIM}]{_rich_escape(note)}[/{palette.C_DIM}]")

    def _turn_made_edits(self) -> bool:
        """Whether the just-cancelled turn applied a file edit (write/edit) —
        detected from the live tool-output sink, same signal as the
        /undo-unavailable hint."""
        return any(n in ("write_file", "edit_file") for n, _ in self._last_tool_outputs)

    def _set_stream(self, text: str) -> None:
        """Show ``text`` in the live region above the input (in-progress prose or
        an approval/picker prompt). Empty string collapses the region."""
        self._stream_text = text
        # Reasoning blocks arrive through this same sink with a sentinel prefix;
        # flag them so _stream_control renders them plain (not markdown-collapsed).
        self._stream_is_reasoning = text.startswith(REASONING_STREAM_PREFIX)
        if self.app is not None:
            self.app.invalidate()

    def _render_stream_md(self, source: str) -> str:
        """Render the growing markdown buffer to an ANSI string for the live region.

        Uses a force_terminal capture Console at the SAME width as ``self.console``
        (the scrollback console) so the live block wraps identically to the
        committed block — no reflow jump when the run finally commits. Cached on
        (source, width): _stream_control runs on every redraw, so we only re-run
        the Rich render when the buffer or the terminal width actually changed
        (O(buffer) per change, not per frame). Width is refreshed at render time
        so live and committed renders stay in lockstep across a terminal resize."""
        width = _current_width()
        self.console.width = width
        if (self._stream_md_cache is not None
                and self._stream_md_cache[0] == source
                and self._stream_md_cache[2] == width):
            return self._stream_md_cache[1]
        buf = io.StringIO()
        cap = Console(force_terminal=True, width=width, file=buf)
        cap.print(Markdown(source.strip(), code_theme=palette.CODE_THEME), end="")
        rendered = buf.getvalue().rstrip("\n")
        self._stream_md_cache = (source, rendered, width)
        return rendered

    def _render_dim_ansi(self, text: str) -> str:
        """Render a one-line dim footer to an ANSI string (palette colour may be a
        name or hex, so let Rich emit the escape rather than hand-building it).
        Width is refreshed at render time to match the current terminal."""
        width = _current_width()
        self.console.width = width
        buf = io.StringIO()
        cap = Console(force_terminal=True, width=width, file=buf)
        cap.print(f"[{palette.C_DIM}]{_rich_escape(text)}[/{palette.C_DIM}]", end="")
        return buf.getvalue().rstrip("\n")

    def _stream_height(self) -> Dimension:
        """Height for the live region: content-sized, but capped so a very tall
        in-progress block (long paragraph / big code block) can't push the input
        and toolbar off the bottom of the screen. Reserve a few rows for the input
        + toolbar; the live block clips at that cap, the input stays pinned."""
        rows = shutil.get_terminal_size((80, 24)).lines
        return Dimension(min=0, max=max(4, rows - 4))

    def _stream_control(self):
        """Region above the input: the live plan checklist (when a turn has written
        todos), in-progress text, an animated thinking indicator while the model
        works, a transient flash (mode change, etc.), else nothing."""
        # Render the menu whenever a picker is active — even with zero options: a
        # history filter (``_menu_filter is not None``) that matched nothing must
        # still show its "(no matches)" modal, not collapse into an invisible one.
        if self._menu_future is not None and (
            self._menu_options or self._menu_filter is not None
        ):
            return self._menu_html()
        # The live todos block is a turn-time thing: only composed while busy, and
        # only above the prose/thinking body — never over a picker/_ask prompt.
        todos = self._live_todos_ansi() if self._busy() else ""
        if self._stream_text:
            if self._busy():
                # The streamed assistant prose renders LIVE as one growing
                # FORMATTED markdown block (Rich -> ANSI), not dim escaped raw
                # source — that kills the grey-raw-then-recommit double render and
                # the mid-construct literal markup. The dim gen-stat footer (live
                # token/rate) stays, since streamed text replaces the spinner.
                # Reasoning is the exception: render it plain dim so the multi-line
                # "✻ thinking\n…" block keeps its line breaks (markdown collapses
                # the soft break onto one line).
                if self._stream_is_reasoning:
                    rendered = self._render_dim_ansi(self._stream_text)
                else:
                    rendered = self._render_stream_md(self._stream_text)
                footer = self._render_dim_ansi(
                    f"{self._gen_stat()} · esc to interrupt"
                )
                # Drop the leading blank line when the buffer is still empty (pure
                # newlines streamed before the first real prose) — show just the footer.
                body = f"{rendered}\n{footer}" if rendered else footer
                # Checklist ABOVE the streaming prose, so prose keeps flowing below.
                return ANSI(f"{todos}\n{body}" if todos else body)
            # Not busy: a picker / _ask prompt lives here as PLAIN text — never
            # markdown-rendered (it isn't markdown and must show verbatim).
            return self._stream_text
        if self._busy():
            # No prose yet — show the checklist above the animated thinking line.
            if todos:
                return ANSI(f"{todos}\n{self._thinking_ansi()}")
            return self._thinking_line()
        if self._flash_html is not None and time.monotonic() < self._flash_until:
            return self._flash_html
        return ""

    async def _on_todos_live(self) -> None:
        """On a ``write_todos`` completion: pull the current plan checklist and show
        it live above the input, re-rendering in place as items flip. A state read
        must never kill the turn, so failures degrade to leaving the block as-is.
        Cleared at turn end by _render_todos (the committed render replaces it)."""
        try:
            todos = await self.controller.todos()
        except Exception:  # noqa: BLE001 — a live-region refresh must not break the turn
            return
        self._live_todos = todos or None
        if self.app is not None:
            self.app.invalidate()

    def _live_todos_ansi(self) -> str:
        """Render the live plan checklist (capped) to an ANSI string for the region,
        or ``""`` when there is nothing to show. Width is refreshed at render time so
        the block wraps to the CURRENT terminal (same discipline as the prose)."""
        if not self._live_todos:
            return ""
        width = _current_width()
        self.console.width = width
        lines = format_todos(self._live_todos, width, cap=_LIVE_TODOS_CAP)
        buf = io.StringIO()
        cap = Console(force_terminal=True, width=width, file=buf)
        cap.print("\n".join(lines), end="")
        return buf.getvalue().rstrip("\n")

    def _thinking_ansi(self) -> str:
        """The animated thinking line rendered to ANSI, for composing BELOW the live
        todos block (the plain-HTML _thinking_line can't be concatenated with the
        ANSI checklist in a single control value)."""
        start = self._turn_start or time.monotonic()
        elapsed = int(time.monotonic() - start)
        frame = palette.SPINNER_FRAMES[int(time.monotonic() * 5) % len(palette.SPINNER_FRAMES)]
        word = self._thinking_word or "Working"
        text = f"{word}… ({elapsed}s · {self._gen_stat()} · esc to interrupt)"
        width = _current_width()
        self.console.width = width
        buf = io.StringIO()
        cap = Console(force_terminal=True, width=width, file=buf)
        cap.print(
            f"[{palette.C_TOOL}]{frame}[/{palette.C_TOOL}] "
            f"[{palette.C_DIM}]{_rich_escape(text)}[/{palette.C_DIM}]",
            end="",
        )
        return buf.getvalue().rstrip("\n")

    def _flash(self, html: HTML, secs: float = 2.0) -> None:
        """Show a transient one-line message above the input (auto-clears) — used
        for ephemeral feedback (mode switch, etc.) so nothing lands in scrollback."""
        self._flash_html = html
        self._flash_until = time.monotonic() + secs
        if self.app is not None:
            self.app.invalidate()

    def _clear_scrollback(self) -> None:
        """Clear terminal scrollback and reset the live region above the input."""
        self._stream_text = ""
        self._stream_md_cache = None
        self._stream_is_reasoning = False
        self._last_tool_outputs = []
        self._last_todos_sig = None
        self._live_todos = None
        if self.app is not None:
            self.app.invalidate()
        f = self.console.file
        if hasattr(f, "truncate") and hasattr(f, "seek"):
            # Headless / test consoles (StringIO) — reset the buffer in place.
            f.truncate(0)
            f.seek(0)
        else:
            # Real TTY: erase scrollback + visible screen (DEC reset + home + erase).
            self.console.file.write("\x1b[3J\x1b[H\x1b[2J")
            self.console.file.flush()

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
        # Footer: when history filter is active show filter-specific hints;
        # otherwise show the standard approval-menu nav hint.
        if self._menu_filter is not None:
            if not self._menu_options:
                lines.append(
                    f'<style fg="{palette.C_DIM}">(no matches) · Backspace to clear</style>'
                )
            else:
                lines.append(
                    f'<style fg="{palette.C_DIM}">↑/↓ · Enter prefill · Backspace · Esc cancel</style>'
                )
        else:
            lines.append(
                f'<style fg="{palette.C_DIM}">↑/↓ select · Enter confirm</style>'
            )
        return HTML("\n".join(lines))

    def _gen_stat(self) -> str:
        """Live turn stat for the spinner / stream footer.

        Two phases: while still processing the prompt (no token streamed yet) show
        the PROMPT size; once output is generating show OUTPUT tokens + tok/s.

        Output tokens use the provider's real per-chunk usage when it reports it
        (e.g. Anthropic); otherwise a ``~`` estimate from the streamed text
        (~4 chars/token), so a local model (LM Studio) — which streams without
        per-chunk usage — still shows the counter moving. The rate is measured from
        the first streamed token, so it reflects generation speed, not the
        prompt-processing (prefill) wait."""
        if self._first_token_at is None:
            # Still thinking / prefilling — there is no generation rate yet, so
            # show the prompt size instead (real input delta if the provider has
            # reported it, else the prior context size as a proxy).
            prompt = self.controller.tracker.total.input_tokens - self._turn_base_input
            if prompt > 0:
                return f"prompt {prompt} tok"
            ctx = self.controller.tracker.context_tokens
            return f"prompt ~{ctx} tok" if ctx > 0 else ""
        est = self._turn_stream_chars // 4
        real = self.controller.tracker.total.output_tokens - self._turn_base_output
        gen, approx = (real, "") if real > 0 else (est, "~")
        out = f"{approx}{gen} tok"
        if gen > 0:
            dur = time.monotonic() - self._first_token_at
            if dur >= 0.5:
                out += f" · {gen / dur:.0f} tok/s"
        return out

    def _thinking_line(self) -> HTML:
        start = self._turn_start or time.monotonic()
        elapsed = int(time.monotonic() - start)
        frame = palette.SPINNER_FRAMES[int(time.monotonic() * 5) % len(palette.SPINNER_FRAMES)]
        word = self._thinking_word or "Working"
        return HTML(
            f'{palette.styled_fg(palette.C_TOOL, frame)} '
            f'{palette.styled_fg(palette.C_DIM, f"{word}… ({elapsed}s · {self._gen_stat()} · esc to interrupt)")}'
        )

    def _count_stream_chars(self, delta: str) -> None:
        """Accumulate streamed output chars this turn (for the live token estimate),
        stamping the first delta so tok/s measures generation, not prefill."""
        if self._first_token_at is None:
            self._first_token_at = time.monotonic()
        self._turn_stream_chars += len(delta)
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
            trusted=self.controller.project_trusted,
            queue_count=len(self._input_queue),
            context_frac=ctx_frac,
            width=width,
        )

    def _completer(self) -> CompletionProvider:
        custom = self.controller.runtime.commands if self.controller.runtime else None
        model_refs = [ref for ref, _ in self.controller.model_choices()]
        for ref, _ in self.controller.discover_models():
            if ref not in model_refs:
                model_refs.append(ref)
        sessions = self.controller.sessions.list()
        session_titles = [s.thread_id for s in sessions]
        mcp_servers = [
            s.name for s in self.config.mcp_servers if s.enabled
        ]
        return CompletionProvider(
            command_catalog=completion_catalog(custom),
            project_root=self.controller.project_root,
            model_refs=model_refs or None,
            session_titles=session_titles or None,
            mcp_servers=mcp_servers or None,
        )

    def _paste_clipboard_image(self) -> None:
        """Ctrl+V: grab a clipboard image and insert it as an ``@path`` reference."""
        async def _job() -> None:
            from jarn.tui.clipboard import grab_error_message, save_clipboard_image

            root = self.controller.project_root or Path(".")
            path = await asyncio.to_thread(save_clipboard_image, root)
            if path is None:
                hint = grab_error_message() or (
                    "no image on the clipboard — copy a screenshot, or save it and use @path"
                )
                self._set_stream(hint)
            else:
                try:
                    rel = path.relative_to(root)
                except ValueError:
                    rel = path
                at_ref = rel.as_posix() if hasattr(rel, "as_posix") else str(rel).replace("\\", "/")
                self.input.insert_text(f"@{at_ref} ")
                self.console.print(
                    f"[{palette.C_NOTICE}]📎 attached {_rich_escape(at_ref)}[/{palette.C_NOTICE}]"
                )
            if self.app is not None:
                self.app.invalidate()

        asyncio.create_task(_job())

    def _drain_queue(self) -> None:
        """Start the next queued line as a new turn (mirrors the submit path)."""
        if not self._busy():
            item = self._input_queue.pop_next()
            if item is None:
                return
            # No prompt echo here: the line was already echoed once with the
            # `» queued: …` marker at submit time (see _submit) — and INTERNAL
            # items (diagnostics auto-fix rounds) are never echoed at all, by
            # design. Re-echoing on drain would double a queued input's lines
            # (or fake a user line for an internal one).
            if not item.internal:
                # A real user line starts a fresh turn-chain: reset the
                # diagnostics auto-fix round counter (T-3-3 loop guard).
                self.controller._diag_chain_round = 0
                # A real user line supersedes pending auto-diagnostics rounds.
                self._input_queue.drop_internal()
            self._turn_start = time.monotonic()
            self._turn_stream_chars = 0
            self._turn_base_output = self.controller.tracker.total.output_tokens
            self._turn_base_input = self.controller.tracker.total.input_tokens
            self._first_token_at = None
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
                self._title_hook("working")   # Set working title when agent turn starts
                await repl_turn._run_turn(
                    self.console, self.controller, text, self._ask,
                    pick=self._pick_approval, view=self._view_full_diff,
                    edit=self._edit_before_apply,
                    live_sink=self._set_stream, spinner=False,
                    tool_sink=self._last_tool_outputs,
                    token_sink=self._count_stream_chars,
                    todos_sink=self._on_todos_live,
                    title_hook=self._title_hook,
                    queue_sink=self._input_queue.append,
                )
                # Turn completed normally (not cancelled — CancelledError would
                # have bypassed this line).  Fire the turn-end notification.
                _elapsed = (
                    time.monotonic() - self._turn_start
                    if self._turn_start is not None
                    else 0.0
                )
                notify(
                    "turn_done",
                    self.config.ui,
                    elapsed=_elapsed,
                    write=self.console.file.write,
                )
                await self._render_todos()
                self._maybe_autocheckpoint_hint()
        except asyncio.CancelledError:
            # renderer.cancel() already printed "cancelled" for agent turns;
            # for command/shell turns (no renderer) the cancel is silent.
            pass
        except Exception as exc:  # noqa: BLE001
            # The TUI must not print a traceback (it corrupts the display), so log
            # the full one to the file logger and point the user at it — an
            # otherwise-opaque mid-turn failure (e.g. a langgraph error) is then
            # diagnosable instead of a bare one-line message.
            logging.getLogger("jarn").error("turn failed", exc_info=exc)
            from jarn.config import paths

            self.console.print(
                f"[{palette.C_ERROR}]{_rich_escape(str(exc))}[/{palette.C_ERROR}]"
            )
            # soft_wrap so a long log path isn't word-wrapped mid-token (that split
            # ".../jarn.log" across a line on narrow / CI-width terminals).
            self.console.print(
                f"[{palette.C_DIM}]full traceback → "
                f"{paths.global_logs_dir() / 'jarn.log'}[/{palette.C_DIM}]",
                soft_wrap=True,
            )
        finally:
            self._turn_task = None
            self._turn_start = None
            self._title_hook("idle")   # Restore idle title for all exit paths (success / cancel / error)
            self._set_stream("")
            # Drop the live checklist on EVERY exit (cancel/error included, where
            # _render_todos never runs) so no stale block lingers above the input.
            self._live_todos = None
            if self.app is not None:
                self.app.invalidate()
            self._drain_queue()
