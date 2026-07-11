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
    return max(1, min(shutil.get_terminal_size((100, 24)).columns, 100))


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
        # T-3-5 subagent tagging (display-only). Per-turn state: tool-call count and
        # accumulated (collapsed) prose per subagent name, plus the pager index that
        # prose is streamed into so Ctrl+O sees the full text mid-turn.
        self._subagent_tools: dict[str, int] = {}
        self._subagent_prose: dict[str, str] = {}
        self._subagent_pager_idx: dict[str, int] = {}
        self._spin()

    def _refresh_width(self) -> None:
        """Sync self.console.width to the current terminal width (capped at 100).

        Called at the top of every commit and live-render entry point so that
        both committed scrollback and the live region always wrap to the terminal
        width that is current *at render time*, not the width captured at startup.
        Rich Console.width is a settable property, so no reconstruction needed.
        """
        # Rich's ``Console.size`` returns a hard-coded 80x25 for a dumb terminal
        # before consulting a width-only override. Pinning the current height via
        # the public setter makes the width override authoritative in redirected
        # output/CI as well as a real TTY.
        current_height = self.console.height
        self.console.width = _current_width()
        self.console.height = current_height

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
        self._refresh_width()
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
        self._refresh_width()
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
        # Emit a blank line only when the kind changes (or on the very first
        # commit when _prev is None).  Suppressing same-kind repeats prevents
        # consecutive text paragraphs (or tools) from stacking double-blanks
        # on top of the blank line that Rich's Markdown already adds after each
        # paragraph in terminal mode.
        if self._prev != kind:
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

    def on_text(self, text: str, *, agent: str | None = None) -> None:
        # A subagent's streamed prose collapses to a single live status line rather
        # than flooding scrollback; the full text stays available in the Ctrl+O pager.
        if agent:
            self._on_subagent_text(agent, text)
            return
        self._unspin()
        self._commit_reasoning()
        self._buf += text
        self._flush_stable()
        self._live_show()

    # -- T-3-5 subagent progress labels -------------------------------------

    def _agent_prefix(self, agent: str | None) -> str:
        """Dim ``┊ <name> `` prefix marking a line as a subagent's, or ``""``."""
        if not agent:
            return ""
        return f"[{palette.C_DIM}]┊ {esc(agent)} [/{palette.C_DIM}]"

    def _on_subagent_text(self, agent: str, text: str) -> None:
        """Collapse subagent prose: accumulate it into the Ctrl+O pager (in place) and
        refresh the live status line instead of committing it to scrollback."""
        full = self._subagent_prose.get(agent, "") + text
        self._subagent_prose[agent] = full
        label = f"{agent} (subagent)"
        idx = self._subagent_pager_idx.get(agent)
        if idx is None:
            self.tool_outputs.append((label, full))
            self._subagent_pager_idx[agent] = len(self.tool_outputs) - 1
        else:
            self.tool_outputs[idx] = (label, full)
        self._show_subagent_status()

    def _subagent_names(self) -> list[str]:
        """Active subagents this turn, in first-seen order (tools and/or prose)."""
        return list(dict.fromkeys([*self._subagent_tools, *self._subagent_prose]))

    def _show_subagent_status(self) -> None:
        """Render the live ``└ <name>: working… (N tool calls)`` status for every
        active subagent (one line each) into the shared live region."""
        self._refresh_width()
        agents = self._subagent_names()
        if not agents:
            return
        body = "\n".join(
            f"└ {a}: working… ({self._subagent_tools.get(a, 0)} tool calls)"
            for a in agents
        )
        if self._live_sink is not None:
            self._live_sink(body)
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
        self._live.update(Text(body, style=palette.C_DIM))

    def _commit_subagent_summaries(self) -> None:
        """At turn end, commit one compact ``┊ <name> ⎿ done · N tool calls`` line per
        subagent to scrollback (the collapsed live status disappears with the turn)."""
        agents = self._subagent_names()
        if not agents:
            return
        self._refresh_width()
        self._live_clear()
        for a in agents:
            n = self._subagent_tools.get(a, 0)
            hint = " · ctrl+o" if self._subagent_prose.get(a, "").strip() else ""
            self.console.print(
                f"[{palette.C_DIM}]┊ {esc(a)} ⎿ done · {n} tool calls{hint}"
                f"[/{palette.C_DIM}]"
            )
        # One-shot: clear so a defensive second finish()/cancel() can't double-print.
        self._subagent_tools = {}
        self._subagent_prose = {}
        self._subagent_pager_idx = {}

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

    def on_tool(
        self, name: str, args: dict, *,
        tool_call_id: str | None = None, agent: str | None = None,
    ) -> None:
        key = self._tool_key(name, args, tool_call_id)
        if key in self._seen_starts:
            return
        self._seen_starts.add(key)
        self._commit_reasoning()
        self._commit_text()
        self._unspin()
        self._refresh_width()
        self._sep("tool")
        if agent:
            self._subagent_tools[agent] = self._subagent_tools.get(agent, 0) + 1
        prefix = self._agent_prefix(agent)
        line = f"{prefix}[{palette.C_TOOL}]⏺[/{palette.C_TOOL}] [bold]{esc(name)}[/bold]"
        arg_s = fmt_args(args)
        if arg_s:
            line += f"  [{palette.C_DIM}]{esc(arg_s)}[/{palette.C_DIM}]"
        self.console.print(line)
        self._tools[key] = ToolRenderState(name=name, args=args)
        if agent:
            self._show_subagent_status()
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
        agent: str | None = None,
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
        # A subagent's result line carries the same dim ┊ <name> prefix; the leading
        # indent is folded into the prefix so tagged lines stay left-aligned with it.
        prefix = self._agent_prefix(agent)
        indent = "" if agent else "  "
        self.console.print(
            f"{prefix}{indent}[{palette.C_DIM}]⎿ {esc(summary)}{dur}[/{palette.C_DIM}]{hint}"
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

    def on_verify_badge(self, verify_data: dict) -> None:
        """Render the structured verify result as a badge line."""
        from jarn.tui import palette as _p

        self._commit_reasoning()
        self._commit_text()
        self._unspin()
        self._refresh_width()

        cmd = esc(verify_data.get("cmd", ""))
        mode = verify_data.get("mode")

        if mode == "suggest":
            self.console.print(
                f"  [{_p.C_DIM}]⎿ verify: run {cmd} to confirm "
                f"(verify.gate: auto to automate)[/{_p.C_DIM}]"
            )
            return

        ok = verify_data.get("ok")
        summary = esc(verify_data.get("summary", ""))
        secs: float = float(verify_data.get("secs", 0.0))
        full_output: str = verify_data.get("full_output", "")

        if ok:
            self.console.print(
                f"  [{_p.C_DIM}]⎿ verified: {cmd} [/{_p.C_DIM}]"
                f"[{_p.C_SUCCESS}]✓[/{_p.C_SUCCESS}]"
                f"[{_p.C_DIM}] {summary} · {secs:.1f}s[/{_p.C_DIM}]"
            )
        else:
            self.console.print(
                f"  [{_p.C_DIM}]⎿ verify: {cmd} [/{_p.C_DIM}]"
                f"[{_p.C_ERROR}]✗[/{_p.C_ERROR}]"
                f"[{_p.C_DIM}] {summary} · details ctrl+o[/{_p.C_DIM}]"
            )
            if full_output:
                self.tool_outputs.append(("verify", full_output))

    def finish(self) -> None:
        self._commit_reasoning()
        self._commit_text()
        self._commit_subagent_summaries()
        self._unspin()

    def cancel(self) -> None:
        self._commit_reasoning()
        self._commit_text()
        self._commit_subagent_summaries()
        self._unspin()
        self._refresh_width()
        self.console.print(f"\n[{palette.C_DIM}]cancelled[/{palette.C_DIM}]")


# Backward-compatible alias used in tests.
_TurnRenderer = TurnRenderer
