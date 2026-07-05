"""Turn streaming renderer for the inline REPL."""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.status import Status
from rich.text import Text

from jarn.tui import palette

# Sentinel prefix the reasoning live-stream is pushed with, so the inline app can
# tell a thinking block from assistant prose in the shared live sink and render it
# as PLAIN dim text (markdown would collapse the "✻ thinking\n…" soft break).
REASONING_STREAM_PREFIX = "✻ thinking\n"


def _current_width() -> int:
    """Return the current terminal width, capped at 100.

    Called at render time (not at startup) so that committed text and the live
    region both wrap to the *current* terminal width after a resize.
    """
    return min(shutil.get_terminal_size((100, 24)).columns, 100)


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def stable_cut(buf: str) -> int:
    """Largest offset at a blank-line boundary outside a code fence."""
    last = -1
    idx = buf.find("\n\n")
    while idx != -1:
        end = idx + 2
        if buf.count("```", 0, end) % 2 == 0:
            last = end
        idx = buf.find("\n\n", idx + 1)
    return last


def fmt_args(args: dict) -> str:
    parts = []
    for k, v in list(args.items())[:3]:
        s = str(v)
        if len(s) > 60:
            s = s[:57] + "…"
        parts.append(s if k in ("command", "cmd") else f"{k}={s}")
    return "  ".join(parts)


def tool_signature(name: str, args: dict) -> str:
    return f"{name}:{args!r}"


@dataclass(slots=True)
class ToolRenderState:
    name: str
    args: dict
    started: float = field(default_factory=time.monotonic)
    ended: bool = False


class TurnRenderer:
    """Renders one streamed turn into the native scrollback."""

    def __init__(
        self,
        console: Console,
        tokens: Callable[[], int] | None = None,
        *,
        live_sink: Callable[[str], None] | None = None,
        spinner: bool = True,
        tool_sink: list[tuple[str, str]] | None = None,
    ) -> None:
        self.console = console
        self._tokens = tokens or (lambda: 0)
        self._live_sink = live_sink
        self._spinner_enabled = spinner and live_sink is None
        self._buf = ""
        self._rbuf = ""
        self._live: Live | None = None
        self._status: Status | None = None
        self._prev: str | None = None
        self._seen_starts: set[str] = set()
        self._tools: dict[str, ToolRenderState] = {}
        self.tool_outputs: list[tuple[str, str]] = tool_sink if tool_sink is not None else []
        self._spin()

    def _refresh_width(self) -> None:
        """Sync self.console.width to the current terminal width (capped at 100).

        Called at the top of every commit and live-render entry point so that
        both committed scrollback and the live region always wrap to the terminal
        width that is current *at render time*, not the width captured at startup.
        Rich Console.width is a settable property, so no reconstruction needed.
        """
        self.console.width = _current_width()

    def _spin(self) -> None:
        if not self._spinner_enabled:
            return
        if self._status is None and self._live is None:
            word = palette.session_thinking_word()
            n = self._tokens()
            label = f"{word}… {n} tok" if n else f"{word}…"
            self._status = self.console.status(
                f"[{palette.C_DIM}]{label}[/{palette.C_DIM}]", spinner="dots"
            )
            self._status.start()

    def _unspin(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None

    def _live_show(self) -> None:
        if self._live_sink is not None:
            self._live_sink(self._buf)
            return
        if not (self.console.is_terminal and self._buf.strip()):
            return
        if self._live is None:
            self._live = Live(
                console=self.console,
                transient=True,
                refresh_per_second=12,
                vertical_overflow="visible",
            )
            self._live.start()
        self._live.update(Markdown(self._buf, code_theme=palette.CODE_THEME))

    def _live_show_reasoning(self) -> None:
        """Stream the in-progress thinking text into the live region so it appears
        as it arrives, instead of dumping the whole block when the phase ends."""
        body = self._rbuf.strip()
        if not body:
            return
        if self._live_sink is not None:
            self._live_sink(f"{REASONING_STREAM_PREFIX}{body}")
            return
        if not self.console.is_terminal:
            return
        if self._live is None:
            self._live = Live(
                console=self.console,
                transient=True,
                refresh_per_second=12,
                vertical_overflow="visible",
            )
            self._live.start()
        preview = Text("✻ thinking\n", style=palette.C_DIM)
        preview.append(body, style=palette.C_DIM)
        self._live.update(preview)

    def _live_clear(self) -> None:
        if self._live_sink is not None:
            self._live_sink("")
            return
        if self._live is not None:
            self._live.stop()
            self._live = None

    def _sep(self, kind: str) -> None:
        if not (self._prev == "tool" and kind == "tool"):
            self.console.print()
        self._prev = kind

    def on_reasoning(self, text: str) -> None:
        self._rbuf += text
        self._unspin()
        self._live_show_reasoning()

    def _commit_reasoning(self) -> None:
        if self._rbuf.strip():
            self._refresh_width()
            self._live_clear()
            self._unspin()
            self._sep("reasoning")
            self.console.print(f"[{palette.C_DIM}]✻ thinking[/{palette.C_DIM}]")
            self.console.print(Text(self._rbuf.strip(), style=palette.C_DIM))
        self._rbuf = ""

    def on_text(self, text: str) -> None:
        self._unspin()
        self._commit_reasoning()
        self._buf += text
        self._flush_stable()
        self._live_show()

    def _flush_stable(self) -> None:
        # With a live_sink (inline REPL) the live preview renders the whole growing
        # buffer as one FORMATTED markdown block, so do NOT recommit per blank line
        # — that double-renders (live preview + scrollback) and shows raw markup
        # mid-construct. The whole run commits to scrollback exactly once via the
        # existing _commit_text() seams (on_tool / on_notice / finish / cancel).
        # The terminal-fallback Rich Live path (no sink) keeps the per-paragraph cut.
        if self._live_sink is not None:
            return
        cut = stable_cut(self._buf)
        if cut <= 0:
            return
        stable, self._buf = self._buf[:cut], self._buf[cut:]
        if stable.strip():
            self._refresh_width()
            self._live_clear()
            self._sep("text")
            self.console.print(Markdown(stable.strip(), code_theme=palette.CODE_THEME))

    def _commit_text(self) -> None:
        self._flush_stable()
        if self._buf.strip():
            self._refresh_width()
            self._live_clear()
            self._sep("text")
            self.console.print(Markdown(self._buf.strip(), code_theme=palette.CODE_THEME))
        self._buf = ""
        self._live_clear()

    def _tool_key(self, name: str, args: dict, tool_call_id: str | None) -> str:
        if tool_call_id:
            return tool_call_id
        return tool_signature(name, args)

    def on_tool(self, name: str, args: dict, *, tool_call_id: str | None = None) -> None:
        key = self._tool_key(name, args, tool_call_id)
        if key in self._seen_starts:
            return
        self._seen_starts.add(key)
        self._commit_reasoning()
        self._commit_text()
        self._unspin()
        self._refresh_width()
        self._sep("tool")
        line = f"[{palette.C_TOOL}]⏺[/{palette.C_TOOL}] [bold]{esc(name)}[/bold]"
        arg_s = fmt_args(args)
        if arg_s:
            line += f"  [{palette.C_DIM}]{esc(arg_s)}[/{palette.C_DIM}]"
        self.console.print(line)
        self._tools[key] = ToolRenderState(name=name, args=args)
        self._spin()

    def _resolve_tool_state(
        self, name: str, tool_call_id: str | None
    ) -> tuple[str, ToolRenderState | None]:
        if tool_call_id and tool_call_id in self._tools:
            return tool_call_id, self._tools[tool_call_id]
        if tool_call_id:
            return tool_call_id, None
        for key, state in self._tools.items():
            if state.name == name and not state.ended:
                return key, state
        return tool_signature(name, {}), None

    def on_tool_end(
        self,
        name: str,
        summary: str,
        full: str = "",
        *,
        tool_call_id: str | None = None,
    ) -> None:
        if not summary:
            return
        self._unspin()
        self._refresh_width()
        hint = f" [{palette.C_DIM}]· ctrl+o[/{palette.C_DIM}]" if full else ""
        dur = ""
        key, state = self._resolve_tool_state(name, tool_call_id)
        if state is not None:
            dt = time.monotonic() - state.started
            dur = f" · {dt:.1f}s"
            state.ended = True
        self.console.print(
            f"  [{palette.C_DIM}]⎿ {esc(summary)}{dur}[/{palette.C_DIM}]{hint}"
        )
        if full:
            self.tool_outputs.append((name, full))
        self._spin()

    def on_notice(self, markup: str) -> None:
        self._commit_reasoning()
        self._commit_text()
        self._unspin()
        self._refresh_width()
        self._sep("notice")
        self.console.print(markup)

    def finish(self) -> None:
        self._commit_reasoning()
        self._commit_text()
        self._unspin()

    def cancel(self) -> None:
        self._commit_reasoning()
        self._commit_text()
        self._unspin()
        self._refresh_width()
        self.console.print(f"\n[{palette.C_DIM}]cancelled[/{palette.C_DIM}]")


# Backward-compatible alias used in tests.
_TurnRenderer = TurnRenderer
