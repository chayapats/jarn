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

from jarn.tui import grammar, layout, palette, tool_labels

# Sentinel prefix the reasoning live-stream is pushed with, so the inline app can
# tell a thinking block from assistant prose in the shared live sink and render it
# as PLAIN dim text (markdown would collapse the "✻ thinking\n…" soft break).
REASONING_STREAM_PREFIX = f"{grammar.GLYPH_THINKING} thinking\n"

# Sentinel prefix a live tool-output tail is pushed with (same shared live sink),
# so the inline app renders it as PLAIN dim text — raw command output is not
# markdown and must show verbatim. Stripped by the app before display.
TOOL_PROGRESS_STREAM_PREFIX = "\x00tool-progress\x00"

# How many trailing lines of a running tool's output to show in the live tail.
_PROGRESS_TAIL_LINES = 10


def _current_width(wrap_at: int = grammar.WRAP_AT) -> int:
    """Return the current terminal width, optionally capped.

    Called at render time (not at startup) so that committed text and the live
    region both wrap to the *current* terminal width after a resize.
    """
    cols = shutil.get_terminal_size((100, 24)).columns
    cap = wrap_at if wrap_at > 0 else cols
    return max(1, min(cols, cap))


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
        tool_progress: str = "all",
        wrap_at: int = grammar.WRAP_AT,
        show_reasoning: str = "collapsed",
        locale: str = "en",
        thinking_style: str = "plain",
    ) -> None:
        self.console = console
        self._tokens = tokens or (lambda: 0)
        self._live_sink = live_sink
        self._spinner_enabled = spinner and live_sink is None
        self._tool_progress = tool_progress
        self._wrap_at = wrap_at
        self._show_reasoning = show_reasoning
        self._locale = locale
        self._thinking_style = thinking_style
        self._buf = ""
        self._rbuf = ""
        self._live: Live | None = None
        self._status: Status | None = None
        self._prev: str | None = None
        self._seen_starts: set[str] = set()
        self._tools: dict[str, ToolRenderState] = {}
        # Tool keys (tool_call_id, else name) currently showing a live output tail in
        # the live region, so on_tool_end knows to clear that tail before committing
        # the final result line.
        self._progress_active: set[str] = set()
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
        self.console.width = _current_width(self._wrap_at)
        self.console.height = current_height

    def _spin(self) -> None:
        if not self._spinner_enabled:
            return
        if self._status is None and self._live is None:
            label = palette.thinking_label(
                style=self._thinking_style, locale=self._locale
            )
            n = self._tokens()
            if self._tool_progress == "verbose" and n:
                label = f"{label} {n} tok"
            self._status = self.console.status(
                layout.muted(label), spinner="dots"
            )
            self._status.start()

    def _thinking_caption(self) -> str:
        return palette.thinking_label(
            style=self._thinking_style, locale=self._locale
        )

    def _thinking_heading(self) -> str:
        return f"{grammar.GLYPH_THINKING} {self._thinking_caption()}"

    def _human_activity(self) -> bool:
        return self._tool_progress == "new"

    def _hide_checklist_line(self, name: str) -> bool:
        return (
            tool_labels.is_checklist_tool(name)
            and self._tool_progress != "verbose"
        )

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
        preview = Text(f"{self._thinking_heading()}\n", style=palette.C_DIM)
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
        if self._show_reasoning == "off":
            return
        self._rbuf += text
        self._unspin()
        if self._show_reasoning == "full":
            self._live_show_reasoning()

    def _commit_reasoning(self) -> None:
        if self._rbuf.strip():
            self._refresh_width()
            self._live_clear()
            self._unspin()
            self._sep("reasoning")
            self.console.print(layout.thinking(label=self._thinking_caption()))
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
        return layout.subagent_prefix(agent)

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
                layout.subagent_done(a, n, hint="ctrl+o" if hint else ""),
                highlight=False,
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
            layout.print_assistant_markdown(self.console, stable)

    def _commit_text(self) -> None:
        self._flush_stable()
        if self._buf.strip():
            self._refresh_width()
            self._live_clear()
            self._sep("text")
            layout.print_assistant_markdown(self.console, self._buf)
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
        if agent:
            self._subagent_tools[agent] = self._subagent_tools.get(agent, 0) + 1
        self._tools[key] = ToolRenderState(name=name, args=args)
        hide = self._hide_checklist_line(name)
        if self._tool_progress == "off" or hide:
            if agent:
                self._show_subagent_status()
            self._spin()
            return
        self._sep("tool")
        prefix = self._agent_prefix(agent)
        if self._human_activity():
            shown_name, arg_s = tool_labels.activity_open(
                name, args, locale=self._locale
            )
        else:
            shown_name, arg_s = name, fmt_args(args)
        self.console.print(prefix + layout.tool_open(shown_name, arg_s), highlight=False)
        if agent:
            self._show_subagent_status()
        self._spin()

    def _format_tool_progress(
        self, name: str, tail: str, elapsed: float, *, heartbeat: bool
    ) -> str:
        """Compose the live tail body: the last few output lines (width-capped) above
        a ``still running… Ns`` heartbeat footer. Returns RAW text — the live sink /
        Rich Live path both render it dim without markup interpretation."""
        width = _current_width(self._wrap_at)
        lines = tail.splitlines()[-_PROGRESS_TAIL_LINES:]
        capped = [ln if len(ln) <= width else ln[: width - 1] + "…" for ln in lines]
        footer = f"{grammar.GLYPH_RESULT} {name}: still running… {int(elapsed)}s"
        return "\n".join([*capped, footer]) if capped else footer

    def on_tool_progress(
        self,
        name: str,
        tail: str,
        elapsed: float,
        *,
        tool_call_id: str | None = None,
        heartbeat: bool = False,
        agent: str | None = None,
    ) -> None:
        """Show a live-updating tail of a still-running tool under its ⏺ line.

        Renders into the shared live region (the same transient region the in-progress
        prose uses) so it never touches scrollback; ``on_tool_end`` clears it and
        commits the final result exactly as today. A no-op body only ever REPLACES the
        live region — it is never committed — so it can't double-render."""
        if self._tool_progress not in ("all", "verbose"):
            return
        key = tool_call_id or name
        self._progress_active.add(key)
        body = self._format_tool_progress(name, tail, elapsed, heartbeat=heartbeat)
        self._refresh_width()
        if self._live_sink is not None:
            self._live_sink(f"{TOOL_PROGRESS_STREAM_PREFIX}{body}")
            return
        if not self.console.is_terminal:
            return
        # Rich Live fallback (no inline app): swap the spinner for the tail.
        self._unspin()
        if self._live is None:
            self._live = Live(
                console=self.console,
                transient=True,
                refresh_per_second=12,
                vertical_overflow="visible",
            )
            self._live.start()
        self._live.update(Text(body, style=palette.C_DIM))

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
        # A tool that streamed a live tail: clear that transient region before the
        # final result lands in scrollback, so the tail is REPLACED (not stacked).
        if self._progress_active:
            self._progress_active.discard(tool_call_id or name)
            self._live_clear()
        hint = "· ctrl+o" if full and self._tool_progress == "verbose" else ""
        dur = ""
        _key, state = self._resolve_tool_state(name, tool_call_id)
        if state is not None:
            dt = time.monotonic() - state.started
            if self._tool_progress == "verbose":
                dur = f" · {dt:.1f}s"
            state.ended = True
        # A subagent's result line carries the same dim ┊ <name> prefix; the leading
        # indent is folded into the prefix so tagged lines stay left-aligned with it.
        prefix = self._agent_prefix(agent)
        indent = "" if agent else "  "
        shown = (
            tool_labels.activity_result(summary, locale=self._locale)
            if self._human_activity()
            else summary
        )
        hide = self._hide_checklist_line(name)
        if self._tool_progress != "off" and not hide:
            self.console.print(
                prefix
                + layout.tool_result(
                    shown, duration=dur, hint=hint, indent=indent
                ),
                highlight=False,
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
        self.console.print(markup, highlight=False)

    def on_verify_badge(self, verify_data: dict) -> None:
        """Render the structured verify result as a badge line."""
        self._commit_reasoning()
        self._commit_text()
        self._unspin()
        self._refresh_width()

        cmd = verify_data.get("cmd", "")
        mode = verify_data.get("mode")

        if mode == "suggest":
            self.console.print(
                layout.tool_result(
                    f"verify: run {cmd} to confirm (verify.gate: auto to automate)"
                ),
                highlight=False,
            )
            return

        ok = verify_data.get("ok")
        summary = str(verify_data.get("summary", ""))
        secs: float = float(verify_data.get("secs", 0.0))
        full_output: str = verify_data.get("full_output", "")

        if ok:
            self.console.print(
                f"{layout.tool_result(f'verified: {cmd} ')}"
                f"{layout.ok(grammar.GLYPH_OK)}"
                f"{layout.muted(f' {summary} · {secs:.1f}s')}",
                highlight=False,
            )
        else:
            self.console.print(
                f"{layout.tool_result(f'verify: {cmd} ')}"
                f"{layout.err(grammar.GLYPH_FAIL)}"
                f"{layout.muted(f' {summary} · details ctrl+o')}",
                highlight=False,
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
        self.console.print("\n" + layout.cancelled(), highlight=False)


# Backward-compatible alias used in tests.
_TurnRenderer = TurnRenderer
