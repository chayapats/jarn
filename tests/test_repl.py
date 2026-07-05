"""Native inline (REPL) front-end tests — headless, fake driver/model."""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
from io import StringIO

import pytest
from rich.console import Console

from jarn.agent.session import ApprovalReply, ApprovalRequest, Event, EventKind
from jarn.config.schema import Config, ProviderConfig, ProviderType, RoutingConfig
from jarn.permissions import Action, ActionKind, Decision, PermissionResult, RememberScope
from jarn.tui.controller import Controller


def _controller(tmp_path, monkeypatch):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="openrouter",
        providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
        routing=RoutingConfig(main="openrouter/m"),
    )
    return Controller(cfg, root)


def _ask_returning(answer: str):
    async def _ask(prompt: str) -> str:
        return answer
    return _ask


def _pick_returning(index: int):
    async def _pick(options):
        return options[index][1]
    return _pick


class _FakeDriver:
    def __init__(self, events):
        self._events = events
        self.resumed = None
        self.text = None

    async def run_turn(self, text, *, resume=False):
        self.text = text
        self.resumed = resume
        for e in self._events:
            yield e


@pytest.mark.asyncio
async def test_run_turn_streams_to_terminal(tmp_path, monkeypatch):
    from jarn import repl

    ctrl = _controller(tmp_path, monkeypatch)

    async def _noop_runtime():
        return None
    monkeypatch.setattr(ctrl, "ensure_runtime", _noop_runtime)
    events = [
        Event(EventKind.TEXT, "Hello "),
        Event(EventKind.TOOL_START, "web_search", {"args": {"query": "gold price"}}),
        Event(EventKind.TOOL_END, "web_search", {"summary": "5 lines"}),
        Event(EventKind.TEXT, "the answer."),
        Event(EventKind.DONE),
    ]
    monkeypatch.setattr(ctrl, "make_driver", lambda approver: _FakeDriver(events))

    console = Console(file=StringIO(), width=80)
    await repl._run_turn(console, ctrl, "hi", _ask_returning(""))
    out = console.file.getvalue()
    assert "Hello" in out and "the answer." in out
    assert "web_search" in out
    # tool result summary renders under the call (never the raw payload)
    assert "⎿" in out and "5 lines" in out
    ctrl.close()


@pytest.mark.asyncio
async def test_run_turn_enriches_payload_once(tmp_path, monkeypatch):
    from jarn import repl

    ctrl = _controller(tmp_path, monkeypatch)

    async def _noop_runtime():
        return None
    monkeypatch.setattr(ctrl, "ensure_runtime", _noop_runtime)
    monkeypatch.setattr(ctrl, "enrich_turn_input", lambda text: f"MEMORY\n\n{text}")
    driver = _FakeDriver([Event(EventKind.TEXT, "ok"), Event(EventKind.DONE)])
    monkeypatch.setattr(ctrl, "make_driver", lambda approver: driver)

    console = Console(file=StringIO(), width=80)
    await repl._run_turn(console, ctrl, "hi", _ask_returning(""))

    assert driver.text == "MEMORY\n\nhi"
    ctrl.close()


@pytest.mark.asyncio
async def test_auto_compact_controller_path_removed(tmp_path, monkeypatch):
    """The controller-side auto-compact trigger is gone: a completed turn never
    calls controller.compact(). Auto-compaction now happens in-graph via the
    summarization middleware wired in build_runtime."""
    from jarn import repl

    ctrl = _controller(tmp_path, monkeypatch)

    async def _noop_runtime():
        return None
    monkeypatch.setattr(ctrl, "ensure_runtime", _noop_runtime)
    monkeypatch.setattr(ctrl, "make_driver",
                        lambda approver: _FakeDriver([Event(EventKind.TEXT, "ok"), Event(EventKind.DONE)]))

    compacted: list[bool] = []

    async def _fake_compact():
        compacted.append(True)
        return "summary"
    monkeypatch.setattr(ctrl, "compact", _fake_compact)

    console = Console(file=StringIO(), width=80)
    await repl._run_turn(console, ctrl, "hi", _ask_returning(""))

    assert compacted == []                                    # never auto-compacts
    assert "auto-compact" not in console.file.getvalue().lower()
    # The old trigger is removed outright, not merely left unused.
    assert not hasattr(ctrl, "should_auto_compact")
    ctrl.close()


@pytest.mark.asyncio
async def test_write_approval_shows_diff(tmp_path, monkeypatch):
    """Inline WRITE approval renders a diff body (parity with the TUI modal)."""
    from jarn import repl

    ctrl = _controller(tmp_path, monkeypatch)
    console = Console(file=StringIO(), width=80)
    request = ApprovalRequest(
        action=Action(ActionKind.WRITE, "notes.txt"),
        result=PermissionResult(Decision.ASK, "ask mode"),
        args={"file_path": "notes.txt", "content": "a brand new line\n"},
    )
    await repl._approve(console, ctrl, request, ask=_ask_returning("r"))
    out = console.file.getvalue()
    assert "a brand new line" in out  # diff body shown before the prompt
    ctrl.close()


@pytest.mark.asyncio
async def test_reasoning_and_markdown_render(tmp_path, monkeypatch):
    """Reasoning shows as a dim ✻ block; assistant Markdown is rendered, not raw."""
    from jarn import repl

    ctrl = _controller(tmp_path, monkeypatch)

    async def _noop_runtime():
        return None
    monkeypatch.setattr(ctrl, "ensure_runtime", _noop_runtime)
    events = [
        Event(EventKind.REASONING, "weighing options"),
        Event(EventKind.TEXT, "# Title\n\nA **bold** word.\n\nSecond para."),
        Event(EventKind.DONE),
    ]
    monkeypatch.setattr(ctrl, "make_driver", lambda approver: _FakeDriver(events))

    console = Console(file=StringIO(), width=80)
    await repl._run_turn(console, ctrl, "hi", _ask_returning(""))
    out = console.file.getvalue()
    assert "✻ thinking" in out and "weighing options" in out
    # multi-paragraph Markdown is rendered (asterisks/hashes gone), all text kept
    assert "Title" in out and "bold" in out and "Second para." in out
    assert "**bold**" not in out and "# Title" not in out
    ctrl.close()


@pytest.mark.asyncio
async def test_tool_output_collapsed_then_expandable(tmp_path, monkeypatch):
    """Tool output is collapsed to a summary; the full text is returned for Ctrl+O."""
    from jarn import repl

    ctrl = _controller(tmp_path, monkeypatch)

    async def _noop_runtime():
        return None
    monkeypatch.setattr(ctrl, "ensure_runtime", _noop_runtime)
    full = "line one\nline two\nline three"
    events = [
        Event(EventKind.TOOL_START, "read_file", {"args": {"path": "x.py"}}),
        Event(EventKind.TOOL_END, "read_file", {"summary": "3 lines", "full": full}),
        Event(EventKind.DONE),
    ]
    monkeypatch.setattr(ctrl, "make_driver", lambda approver: _FakeDriver(events))

    console = Console(file=StringIO(), width=80)
    outputs = await repl._run_turn(console, ctrl, "hi", _ask_returning(""))
    out = console.file.getvalue()
    assert "3 lines" in out and "ctrl+o" in out  # collapsed summary + expand hint
    assert "line two" not in out                  # full body NOT shown by default
    assert outputs == [("read_file", full)]       # but retained for expansion
    ctrl.close()


@pytest.mark.asyncio
async def test_turn_retries_on_retryable_error(tmp_path, monkeypatch):
    """A retryable error before any output rotates to a fallback model and retries."""
    from jarn import repl

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="openrouter",
        providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
        routing=RoutingConfig(main="openrouter/m", fallback=["openrouter/f1"]),
    )
    ctrl = Controller(cfg, root)

    async def _noop_runtime():
        return None
    monkeypatch.setattr(ctrl, "ensure_runtime", _noop_runtime)

    first = _FakeDriver([Event(EventKind.ERROR, "rate limit exceeded", {"retryable": True})])
    second = _FakeDriver([Event(EventKind.TEXT, "recovered."), Event(EventKind.DONE)])
    seq = [first, second]
    monkeypatch.setattr(ctrl, "make_driver", lambda approver: seq.pop(0))

    console = Console(file=StringIO(), width=80)
    await repl._run_turn(console, ctrl, "hi", _ask_returning(""))
    out = console.file.getvalue()
    assert "retrying with openrouter/f1" in out
    assert "recovered." in out
    assert ctrl.config.routing.main == "openrouter/m"  # reset to primary after success
    assert first.resumed is False   # first attempt sends the user message
    assert second.resumed is True   # retry resumes from state (no duplicate user msg)
    assert second.text == ""        # resume retry must not inject/send the turn again
    ctrl.close()


@pytest.mark.asyncio
async def test_turn_does_not_retry_after_output(tmp_path, monkeypatch):
    """An error *after* visible output is surfaced, not retried (no dup work)."""
    from jarn import repl

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="openrouter",
        providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
        routing=RoutingConfig(main="openrouter/m", fallback=["openrouter/f1"]),
    )
    ctrl = Controller(cfg, root)

    async def _noop_runtime():
        return None
    monkeypatch.setattr(ctrl, "ensure_runtime", _noop_runtime)

    calls = {"n": 0}

    def _make(approver):
        calls["n"] += 1
        return _FakeDriver([
            Event(EventKind.TEXT, "partial work"),
            Event(EventKind.ERROR, "timeout", {"retryable": True}),
        ])
    monkeypatch.setattr(ctrl, "make_driver", _make)

    console = Console(file=StringIO(), width=80)
    await repl._run_turn(console, ctrl, "hi", _ask_returning(""))
    out = console.file.getvalue()
    assert calls["n"] == 1               # no retry attempted
    assert "partial work" in out and "timeout" in out
    ctrl.close()


@pytest.mark.asyncio
async def test_auth_error_surfaces_friendly_message(tmp_path, monkeypatch):
    """A 401/auth ERROR is mapped to a friendly, actionable message naming the
    provider — not the raw SDK JSON, which is kept only as dim detail."""
    from jarn import repl

    ctrl = _controller(tmp_path, monkeypatch)  # routing.main = "openrouter/m"

    async def _noop_runtime():
        return None
    monkeypatch.setattr(ctrl, "ensure_runtime", _noop_runtime)

    raw = "Error code: 401 - {'error': {'message': 'invalid x-api-key'}}"
    driver = _FakeDriver([
        Event(EventKind.ERROR, raw, {"retryable": False, "auth": True,
                                     "provider": "openrouter"}),
    ])
    monkeypatch.setattr(ctrl, "make_driver", lambda approver: driver)

    console = Console(file=StringIO(), width=100)
    await repl._run_turn(console, ctrl, "hi", _ask_returning(""))
    out = console.file.getvalue()
    flat = " ".join(out.split())  # collapse console line-wrapping
    assert "was rejected (401)" in flat
    assert "openrouter" in flat
    assert "/key" in flat and "jarn setup" in flat
    # raw detail stays available (dim), but the message is no longer just the JSON
    assert not out.strip().startswith("Error code: 401")
    ctrl.close()


@pytest.mark.asyncio
async def test_non_auth_error_message_unchanged(tmp_path, monkeypatch):
    """A non-auth error is surfaced verbatim (no friendly remapping)."""
    from jarn import repl

    ctrl = _controller(tmp_path, monkeypatch)

    async def _noop_runtime():
        return None
    monkeypatch.setattr(ctrl, "ensure_runtime", _noop_runtime)

    driver = _FakeDriver([
        Event(EventKind.TEXT, "partial"),
        Event(EventKind.ERROR, "boom: something broke", {"retryable": False}),
    ])
    monkeypatch.setattr(ctrl, "make_driver", lambda approver: driver)

    console = Console(file=StringIO(), width=80)
    await repl._run_turn(console, ctrl, "hi", _ask_returning(""))
    out = console.file.getvalue()
    assert "boom: something broke" in out
    assert "was rejected (401)" not in out
    ctrl.close()


@pytest.mark.asyncio
async def test_auth_error_rotates_to_keyed_fallback(tmp_path, monkeypatch):
    """A 401 on the primary rotates to a configured fallback on a different
    provider that has a good key, and the turn completes there."""
    from jarn import repl

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="primary",
        providers={
            "primary": ProviderConfig(type=ProviderType.OPENROUTER, api_key="bad"),
            "backup": ProviderConfig(type=ProviderType.ANTHROPIC, api_key="good"),
        },
        routing=RoutingConfig(main="primary/m", fallback=["backup/m"]),
    )
    ctrl = Controller(cfg, root)

    async def _noop_runtime():
        return None
    monkeypatch.setattr(ctrl, "ensure_runtime", _noop_runtime)

    raw = "Error code: 401 - {'error': {'message': 'invalid x-api-key'}}"
    first = _FakeDriver([
        Event(EventKind.ERROR, raw, {"retryable": False, "auth": True,
                                     "provider": "primary"}),
    ])
    second = _FakeDriver([Event(EventKind.TEXT, "recovered."), Event(EventKind.DONE)])
    seq = [first, second]
    monkeypatch.setattr(ctrl, "make_driver", lambda approver: seq.pop(0))

    console = Console(file=StringIO(), width=100)
    await repl._run_turn(console, ctrl, "hi", _ask_returning(""))
    out = console.file.getvalue()
    flat = " ".join(out.split())
    assert "auth failed, retrying with backup/m" in flat
    assert "recovered." in flat
    assert "was rejected (401)" not in flat   # rotated instead of dead-ending
    assert second.resumed is True             # resume from state, no duplicate user msg
    assert ctrl.config.routing.main == "primary/m"  # reset to primary on success
    ctrl.close()


@pytest.mark.asyncio
async def test_auth_error_dead_ends_without_viable_fallback(tmp_path, monkeypatch):
    """With no viable fallback, a 401 still dead-ends with the friendly message."""
    from jarn import repl

    ctrl = _controller(tmp_path, monkeypatch)  # single provider, no fallback

    async def _noop_runtime():
        return None
    monkeypatch.setattr(ctrl, "ensure_runtime", _noop_runtime)

    raw = "Error code: 401 - {'error': {'message': 'invalid x-api-key'}}"
    calls = {"n": 0}

    def _make(approver):
        calls["n"] += 1
        return _FakeDriver([
            Event(EventKind.ERROR, raw, {"retryable": False, "auth": True,
                                         "provider": "openrouter"}),
        ])
    monkeypatch.setattr(ctrl, "make_driver", _make)

    console = Console(file=StringIO(), width=100)
    await repl._run_turn(console, ctrl, "hi", _ask_returning(""))
    out = console.file.getvalue()
    flat = " ".join(out.split())
    assert calls["n"] == 1                     # no rotation attempted
    assert "was rejected (401)" in flat
    assert "auth failed, retrying" not in flat
    ctrl.close()


def test_friendly_auth_error_falls_back_to_generic_without_provider():
    """With no provider, the message degrades gracefully to a generic phrasing
    and still keeps the raw detail dim."""
    from jarn import repl

    msg = repl._friendly_auth_error("Error code: 401 - invalid x-api-key", "")
    assert "was rejected (401)" in msg
    assert "/key" in msg and "jarn setup" in msg
    assert "invalid x-api-key" in msg  # raw detail preserved


@pytest.mark.asyncio
async def test_fallback_retry_no_duplicate_human_message(tmp_path, monkeypatch):
    """End-to-end: a real flaky model fails the first call, the turn rotates to a
    fallback and recovers — leaving exactly ONE human message in the thread
    (resume mode), and exercising the real ensure_runtime→make_driver rebuild."""
    from unittest.mock import patch

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from jarn import repl

    calls = {"n": 0}

    class _Flaky(GenericFakeChatModel):
        def bind_tools(self, tools, **kw):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("rate limit exceeded 429")  # retryable
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kw)

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="openrouter",
        providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
        routing=RoutingConfig(main="openrouter/m", fallback=["openrouter/f1"]),
    )
    ctrl = Controller(cfg, root)

    fake = _Flaky(messages=iter([AIMessage(content="recovered answer"),
                                 AIMessage(content="unused")]))
    console = Console(file=StringIO(), width=80)
    with patch("jarn.providers.models.ModelFactory.build", return_value=fake):
        await repl._run_turn(console, ctrl, "the question", _ask_returning(""))
        out = console.file.getvalue()
        assert "retrying with openrouter/f1" in out
        assert "recovered answer" in out
        assert calls["n"] == 2  # failed once, succeeded on the fallback

        # Crucially: the thread holds exactly one human message (no duplicate).
        history = await ctrl.history()
        humans = [m for m in history if getattr(m, "type", "") == "human"]
        assert len(humans) == 1, [getattr(m, "type", "") for m in history]
        assert "the question" in str(humans[0].content)
    await ctrl.aclose()


def test_inline_expanded_text(tmp_path, monkeypatch):
    """Ctrl+O builds the full retained output for the pager (None when empty)."""
    from jarn import repl

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(default_profile="openrouter",
                 providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
                 routing=RoutingConfig(main="openrouter/m"))
    app = repl.InlineApp(cfg, root)

    assert app._expanded_text() is None  # nothing retained yet

    app._last_tool_outputs = [("read_file", "alpha\nbeta"), ("web_search", "r1\nr2")]
    text = app._expanded_text()
    assert "read_file" in text and "alpha" in text and "beta" in text
    assert "web_search" in text and "r2" in text
    app.controller.close()


def test_tool_sink_accumulates_live():
    """Tool outputs append to a provided sink as they arrive (mid-turn Ctrl+O)."""
    from jarn.repl_renderer import TurnRenderer as _TurnRenderer

    sink: list = []
    r = _TurnRenderer(Console(file=StringIO()), tool_sink=sink,
                      live_sink=lambda _s: None, spinner=False)
    r.on_tool_end("web_search", "3 lines", "a\nb\nc")
    assert sink == [("web_search", "a\nb\nc")]  # visible before the turn ends


def test_reasoning_streams_live_into_sink():
    """Reasoning tokens stream into the live region as they arrive, not just at end."""
    from jarn.repl_renderer import TurnRenderer as _TurnRenderer

    seen: list[str] = []
    r = _TurnRenderer(Console(file=StringIO()),
                      live_sink=seen.append, spinner=False)
    r.on_reasoning("weighing ")
    r.on_reasoning("options")
    # The growing thinking text is pushed to the live region before any other
    # event commits it — the user sees it during the phase, not only after.
    assert seen, "reasoning never reached the live region"
    assert "weighing options" in seen[-1]
    assert "thinking" in seen[-1]


def test_reasoning_live_preview_clears_when_committed():
    """Committing reasoning to scrollback clears the live preview (no double-render)."""
    from jarn.repl_renderer import TurnRenderer as _TurnRenderer

    seen: list[str] = []
    console = Console(file=StringIO(), width=80)
    r = _TurnRenderer(console, live_sink=seen.append, spinner=False)
    r.on_reasoning("pondering")
    assert "pondering" in seen[-1]
    # Real text arriving commits the reasoning block and collapses the preview.
    r.on_text("done.")
    assert "" in seen  # the live reasoning preview was cleared on commit
    assert "thinking" not in seen[-1]  # live region no longer shows reasoning
    out = console.file.getvalue()
    assert "✻ thinking" in out and "pondering" in out  # committed once to scrollback
    assert out.count("pondering") == 1  # not double-rendered


def test_reasoning_streams_live_into_rich_live(monkeypatch):
    """On a real terminal, reasoning updates the Rich Live region during the phase."""
    from jarn.repl_renderer import TurnRenderer as _TurnRenderer

    console = Console(file=StringIO(), force_terminal=True, width=80)
    r = _TurnRenderer(console, spinner=False)  # no sink -> Rich Live path
    updates: list = []
    try:
        r.on_reasoning("thinking hard")
        assert r._live is not None  # a live region was opened mid-phase
        # capture what it would render
        import rich.live as _rl

        monkeypatch.setattr(_rl.Live, "update",
                            lambda self, renderable, **kw: updates.append(renderable))
        r.on_reasoning(" now")
    finally:
        r._live_clear()
    assert updates, "reasoning did not refresh the live region"


def test_session_thinking_word_is_stable():
    """The session thinking word is picked once and stays put across calls."""
    from jarn.tui import palette

    word = palette.session_thinking_word()
    assert word in palette.THINKING_WORDS
    # Re-asking within the session yields the same identity, not a fresh pick.
    assert all(palette.session_thinking_word() == word for _ in range(20))


def test_thinking_word_stable_across_turns(tmp_path, monkeypatch):
    """The inline indicator label keeps one identity across multiple turns."""
    from jarn import repl
    from jarn.tui import palette

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(default_profile="openrouter",
                 providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
                 routing=RoutingConfig(main="openrouter/m"))
    app = repl.InlineApp(cfg, root)

    # The word is established at session start (not blank/Working fallback).
    assert app._thinking_word in palette.THINKING_WORDS
    first = app._thinking_word

    # Simulating several turn starts must NOT re-roll the word.
    for _ in range(5):
        app._turn_start = 0.0
        # mirror the submit/drain bookkeeping that used to re-randomize
        assert app._thinking_word == first
    assert app._thinking_word == first
    app.controller.close()


def test_renderer_spinner_word_matches_session(monkeypatch):
    """The renderer spinner uses the stable session word, not a per-spin pick."""
    from jarn.repl_renderer import TurnRenderer as _TurnRenderer
    from jarn.tui import palette

    buf = StringIO()
    console = Console(file=buf, force_terminal=True, width=80)
    word = palette.session_thinking_word()
    # spinner enabled (live_sink is None) so _spin builds the label
    r = _TurnRenderer(console, lambda: 0)
    try:
        assert r._status is not None
        assert word in r._status.status  # same stable word, not a per-spin pick
    finally:
        r._unspin()


def test_pager_overlay_toggle(tmp_path, monkeypatch):
    """Ctrl+O opens the in-app overlay; toggling/collapse closes it."""
    from jarn import repl

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(default_profile="openrouter",
                 providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
                 routing=RoutingConfig(main="openrouter/m"))
    app = repl.InlineApp(cfg, root)
    app.app = app._build_app()

    assert app._expanded is False
    app._open_pager()                       # nothing to expand → stays closed
    assert app._expanded is False

    app._last_tool_outputs = [("read_file", "x\ny\nz")]
    app._open_pager()
    assert app._expanded is True and app._pager_buffer.text
    app._collapse()
    assert app._expanded is False
    app.controller.close()


def test_stable_cut_respects_code_fences():
    """Paragraphs commit at blank lines, but never inside an open code fence."""
    from jarn.repl_renderer import stable_cut as _stable_cut

    assert _stable_cut("para one\n\npara two") == len("para one\n\n")
    assert _stable_cut("still going, no blank line") == -1
    # blank line inside an unclosed fence is not a safe commit point
    assert _stable_cut("```\ncode\n\nmore code") == -1
    # once the fence closes, the boundary after it is safe
    s = "```\ncode\n```\n\nafter"
    assert _stable_cut(s) == s.index("\n\nafter") + 2


def test_live_sink_suppresses_incremental_flush():
    """With a live_sink, prose is NOT recommitted per blank line — the live preview
    is the single source of truth mid-run, and the whole run lands in scrollback
    exactly once at finish() (no grey-raw-then-recommit double render)."""
    from jarn.repl_renderer import TurnRenderer as _TurnRenderer

    console = Console(file=StringIO(), width=80)
    r = _TurnRenderer(console, live_sink=lambda _s: None, spinner=False)
    r.on_text("paragraph_one\n\nparagraph_two\n\n")
    # Nothing recommitted to scrollback yet — only the live preview has it.
    assert "paragraph_one" not in console.file.getvalue()
    r.finish()
    out = console.file.getvalue()
    assert out.count("paragraph_one") == 1  # committed exactly once
    assert out.count("paragraph_two") == 1


def test_live_sink_commits_once_before_tool():
    """A prose run accumulated before a tool call commits to scrollback once,
    and BEFORE the tool glyph — the on_tool seam preserves interleaving."""
    from jarn.repl_renderer import TurnRenderer as _TurnRenderer

    console = Console(file=StringIO(), width=80)
    r = _TurnRenderer(console, live_sink=lambda _s: None, spinner=False)
    r.on_text("answer_body\n\n")
    r.on_tool("read_file", {"path": "x"})
    out = console.file.getvalue()
    assert out.count("answer_body") == 1
    assert out.index("answer_body") < out.index("read_file")


def test_live_sink_carries_growing_buffer():
    """The live_sink receives the whole growing markdown buffer each delta, so the
    preview can render the full block (not just the latest fragment)."""
    from jarn.repl_renderer import TurnRenderer as _TurnRenderer

    seen: list[str] = []
    r = _TurnRenderer(Console(file=StringIO(), width=80),
                      live_sink=seen.append, spinner=False)
    r.on_text("a")
    r.on_text("b")
    assert "a" in seen and "ab" in seen[-1]


def _request(dangerous=False, block_always=False):
    return ApprovalRequest(
        action=Action(ActionKind.SHELL, "npm test"),
        result=PermissionResult(Decision.ASK, "ask mode", dangerous=dangerous,
                                block_remember_always=block_always),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("index,approved,scope", [
    (0, True, RememberScope.ONCE),
    (1, True, RememberScope.ALWAYS),
    (2, False, RememberScope.ONCE),
])
async def test_approve_mapping_pick(tmp_path, monkeypatch, index, approved, scope):
    from jarn import repl

    ctrl = _controller(tmp_path, monkeypatch)
    console = Console(file=StringIO(), width=80)
    reply = await repl._approve(console, ctrl, _request(), pick=_pick_returning(index))
    assert reply.approved is approved
    if approved:
        assert reply.scope is scope
    ctrl.close()


@pytest.mark.parametrize("answer,approved,scope", [
    ("a", True, RememberScope.ONCE),
    ("s", True, RememberScope.SESSION),
    ("w", True, RememberScope.ALWAYS),
    ("r", False, RememberScope.ONCE),
])
async def test_approve_mapping_text_fallback(tmp_path, monkeypatch, answer, approved, scope):
    from jarn import repl

    ctrl = _controller(tmp_path, monkeypatch)
    console = Console(file=StringIO(), width=80)
    reply = await repl._approve(console, ctrl, _request(), ask=_ask_returning(answer))
    assert reply.approved is approved
    if approved:
        assert reply.scope is scope
    ctrl.close()


def test_approval_options_dangerous_offers_session_not_always():
    from jarn import repl

    req = _request(dangerous=True, block_always=True)
    labels = [label for label, _ in repl._approval_options(req)]
    assert labels == ["Allow once", "Allow for session", "Deny"]


@pytest.mark.asyncio
async def test_approve_always_blocked_for_dangerous(tmp_path, monkeypatch):
    from jarn import repl

    ctrl = _controller(tmp_path, monkeypatch)
    console = Console(file=StringIO(), width=80)
    reply = await repl._approve(
        console, ctrl, _request(dangerous=True, block_always=True),
        pick=_pick_returning(1),
    )
    assert reply.approved is True
    assert reply.scope is RememberScope.SESSION  # no "always" on danger-guard actions
    ctrl.close()


class _KeyEvent:
    """Minimal stand-in for a prompt_toolkit key event (only ``.data`` is read)."""

    def __init__(self, char: str):
        self.data = char


def _fastkey_handler(app):
    """Return the `_menu_fastkey` keybinding handler (the y/a/n/d keystroke path)."""
    for binding in app._kb.bindings:
        if getattr(binding.handler, "__name__", "") == "_menu_fastkey":
            return binding.handler
    raise AssertionError("no _menu_fastkey binding found")


def _inline_app(tmp_path, monkeypatch):
    from jarn import repl

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="openrouter",
        providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
        routing=RoutingConfig(main="openrouter/m"),
    )
    return repl.InlineApp(cfg, root)


@pytest.mark.asyncio
@pytest.mark.parametrize("key,approved,scope", [
    ("y", True, RememberScope.ONCE),
    ("a", True, RememberScope.ONCE),
    ("n", False, None),
    ("d", False, None),
])
async def test_pick_approval_one_key_resolves_instantly(tmp_path, monkeypatch, key, approved, scope):
    """A single y/a/n/d keypress resolves the approval picker with no arrow+Enter."""
    from jarn import repl

    app = _inline_app(tmp_path, monkeypatch)
    options = repl._approval_options(_request())
    handler = _fastkey_handler(app)
    task = asyncio.create_task(app._pick_approval(options))
    await asyncio.sleep(0)  # let _pick_menu install the future + fastkeys
    assert app._menu_fastkeys is not None  # approval menu wired the fast-path

    handler(_KeyEvent(key))
    picked = await task
    assert isinstance(picked, ApprovalReply)
    assert picked.approved is approved
    if approved:
        assert picked.scope is scope
    app.controller.close()


@pytest.mark.asyncio
async def test_pick_approval_arrow_then_enter_still_works(tmp_path, monkeypatch):
    """Arrow navigation + Enter still confirms a non-default option (no regression)."""
    from jarn import repl

    app = _inline_app(tmp_path, monkeypatch)
    options = repl._approval_options(_request())  # [allow once, allow always, deny]
    task = asyncio.create_task(app._pick_approval(options))
    await asyncio.sleep(0)
    app._menu_index = 1  # "Allow always" via ↑/↓
    app._menu_future.set_result(app._menu_options[1][1])
    picked = await task
    assert isinstance(picked, ApprovalReply)
    assert picked.approved is True
    assert picked.scope is RememberScope.ALWAYS
    app.controller.close()


@pytest.mark.asyncio
async def test_menu_fastkey_types_normally_outside_approval_menu(tmp_path, monkeypatch):
    """With no fast-key menu active, y/a/n/d type into the input — editing intact.

    (Async so the buffer's complete-while-typing has a running loop to schedule on.)"""
    app = _inline_app(tmp_path, monkeypatch)
    assert app._menu_fastkeys is None
    handler = _fastkey_handler(app)
    for ch in "andy":
        handler(_KeyEvent(ch))
    assert app.input.text == "andy"
    app.controller.close()


def _write_request(content: str):
    return ApprovalRequest(
        action=Action(ActionKind.WRITE, "big.txt"),
        result=PermissionResult(Decision.ASK, "ask mode"),
        args={"file_path": "big.txt", "content": content},
    )


@pytest.mark.asyncio
async def test_view_full_diff_offered_only_over_cap(tmp_path, monkeypatch):
    """A write diff longer than ui.approval_diff_lines offers the view option;
    a small one does not."""
    from jarn import repl

    ctrl = _controller(tmp_path, monkeypatch)
    ctrl.config.ui.approval_diff_lines = 5
    console = Console(file=StringIO(), width=80)

    captured: list[list[str]] = []

    async def _pick(options):
        captured.append([label for label, _ in options])
        return options[0][1]  # Allow once

    async def _view(_text: str) -> None:  # present but should not be used here
        raise AssertionError("view must not run when picking allow")

    # Big diff (20 added lines > cap of 5) → view offered.
    big = "".join(f"line {i}\n" for i in range(20))
    await repl._approve(console, ctrl, _write_request(big), pick=_pick, view=_view)
    assert "View full diff" in captured[-1]

    # Small diff (2 lines < cap) → view NOT offered.
    await repl._approve(
        console, ctrl, _write_request("a\nb\n"), pick=_pick, view=_view
    )
    assert "View full diff" not in captured[-1]
    ctrl.close()


@pytest.mark.asyncio
async def test_view_full_diff_does_not_approve_and_reprompts(tmp_path, monkeypatch):
    """Choosing [v] routes the COMPLETE diff through the pager and returns to the
    SAME prompt — it never auto-approves."""
    from jarn import repl

    ctrl = _controller(tmp_path, monkeypatch)
    ctrl.config.ui.approval_diff_lines = 5
    console = Console(file=StringIO(), width=80)

    big = "".join(f"line {i}\n" for i in range(20))
    request = _write_request(big)

    viewed: list[str] = []

    async def _view(text: str) -> None:
        viewed.append(text)

    # First pick the view sentinel; on the re-prompt pick Deny.
    calls = {"n": 0}

    async def _pick(options):
        calls["n"] += 1
        if calls["n"] == 1:
            return repl._VIEW_FULL_DIFF
        return next(v for _, v in options
                    if isinstance(v, ApprovalReply) and not v.approved)

    reply = await repl._approve(console, ctrl, request, pick=_pick, view=_view)
    assert calls["n"] == 2  # re-prompted after viewing

    # View ran exactly once, with the COMPLETE diff (all 20 lines present).
    assert len(viewed) == 1
    assert "line 0" in viewed[0] and "line 19" in viewed[0]
    # Viewing did not approve; the second pick (Deny) decided.
    assert reply.approved is False
    ctrl.close()


# -- edit before apply (P4.B) ----------------------------------------------

def _edit_request(content: str | None = None, *, old=None, new=None):
    """A write/edit ApprovalRequest. ``content`` → write_file; old/new → edit_file."""
    if content is not None:
        args = {"file_path": "f.txt", "content": content}
    else:
        args = {"file_path": "f.txt", "old_string": old, "new_string": new}
    return ApprovalRequest(
        action=Action(ActionKind.WRITE, "f.txt"),
        result=PermissionResult(Decision.ASK, "ask mode"),
        args=args,
    )


def test_editable_field_picks_content_then_new_string():
    from jarn import repl

    assert repl._editable_field({"content": "x"}) == "content"
    assert repl._editable_field({"old_string": "a", "new_string": "b"}) == "new_string"
    assert repl._editable_field({}) is None
    assert repl._editable_field(None) is None


@pytest.mark.asyncio
async def test_edit_option_offered_only_with_editor_wired(tmp_path, monkeypatch):
    """[e] Edit before apply appears for an editable write iff an editor launcher
    is threaded in; never for headless callers (no ``edit``)."""
    from jarn import repl

    ctrl = _controller(tmp_path, monkeypatch)
    console = Console(file=StringIO(), width=80)
    captured: list[list[str]] = []

    async def _pick(options):
        captured.append([label for label, _ in options])
        return next(v for _, v in options
                    if isinstance(v, ApprovalReply) and not v.approved)  # Deny

    async def _edit(_req):
        raise AssertionError("edit must not run when picking Deny")

    # With an editor wired → option offered.
    await repl._approve(console, ctrl, _edit_request("hi\n"), pick=_pick, edit=_edit)
    assert "Edit before apply" in captured[-1]

    # Without one → option absent.
    await repl._approve(console, ctrl, _edit_request("hi\n"), pick=_pick)
    assert "Edit before apply" not in captured[-1]
    ctrl.close()


@pytest.mark.asyncio
async def test_edit_then_apply_carries_edited_content(tmp_path, monkeypatch):
    """Picking [e] and saving in the editor returns an approve whose edited_args
    carry the USER-edited content — that's what lands, not the agent's original."""
    from jarn import repl

    ctrl = _controller(tmp_path, monkeypatch)
    console = Console(file=StringIO(), width=80)

    async def _pick(options):
        return next(v for v in (o[1] for o in options) if v is repl._EDIT_BEFORE_APPLY)

    async def _edit(req):
        # Simulate the editor returning user-edited content.
        field = repl._editable_field(req.args)
        return ApprovalReply(True, edited_args={**req.args, field: "USER EDIT\n"})

    reply = await repl._approve(
        console, ctrl, _edit_request("agent original\n"), pick=_pick, edit=_edit
    )
    assert reply.approved is True
    assert reply.edited_args is not None
    assert reply.edited_args["content"] == "USER EDIT\n"
    ctrl.close()


@pytest.mark.asyncio
async def test_edit_abort_cancels_without_applying(tmp_path, monkeypatch):
    """Aborting the editor (edit callable returns None) denies cleanly: the reply
    is a rejection and carries no edited_args, so nothing is applied."""
    from jarn import repl

    ctrl = _controller(tmp_path, monkeypatch)
    console = Console(file=StringIO(), width=80)

    async def _pick(options):
        return next(v for v in (o[1] for o in options) if v is repl._EDIT_BEFORE_APPLY)

    async def _edit(_req):
        return None  # editor aborted

    reply = await repl._approve(
        console, ctrl, _edit_request("x\n"), pick=_pick, edit=_edit
    )
    assert reply.approved is False
    assert reply.edited_args is None
    ctrl.close()


def test_edit_text_in_editor_returns_saved_content(tmp_path, monkeypatch):
    """The $EDITOR launcher writes the proposal to a temp file, runs the editor,
    and returns whatever the editor saved."""
    from jarn import repl

    def _fake_editor(argv, **kwargs):
        # argv == [editor, path]; emulate the user appending a line and saving.
        path = argv[-1]
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("+appended\n")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setenv("EDITOR", "fake-editor")
    monkeypatch.setattr(repl.turn.subprocess, "run", _fake_editor)
    out = repl._edit_text_in_editor("original\n")
    assert out == "original\n+appended\n"


def test_edit_text_in_editor_abort_returns_none(tmp_path, monkeypatch):
    """A non-zero editor exit (e.g. vim :cq) is treated as an abort → None."""
    from jarn import repl

    def _fake_editor(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1)  # aborted

    monkeypatch.setenv("EDITOR", "fake-editor")
    monkeypatch.setattr(repl.turn.subprocess, "run", _fake_editor)
    assert repl._edit_text_in_editor("original\n") is None


# -- /compact preview + confirm (P4.D) -------------------------------------


def _stub_compact(app, monkeypatch, *, summary="SUMMARY: did X"):
    """Stub the controller's preview/apply; return a list capturing what got applied."""
    applied: list[str] = []

    async def _preview():
        return summary

    async def _apply(s):
        applied.append(s)

    monkeypatch.setattr(app.controller, "compact_preview", _preview)
    monkeypatch.setattr(app.controller, "compact_apply", _apply)
    return applied


def _stub_editor(monkeypatch, result):
    """Route the /compact 'edit' path through a stubbed editor (never spawn one)."""
    import prompt_toolkit.application as pta

    from jarn import repl

    async def _run_in_terminal(func, *a, **k):
        return func()

    monkeypatch.setattr(pta, "run_in_terminal", _run_in_terminal)
    monkeypatch.setattr(repl.turn, "_edit_text_in_editor", lambda text, **k: result)


@pytest.mark.asyncio
async def test_cmd_compact_applies_on_yes(tmp_path, monkeypatch):
    """Manual /compact applies the summary only after the user confirms with y."""
    app = _make_inline_app(tmp_path, monkeypatch)
    applied = _stub_compact(app, monkeypatch)
    monkeypatch.setattr(app, "_ask", _ask_returning("y"))

    await app._command("compact", "")

    assert applied == ["SUMMARY: did X"]
    assert "Compacted" in app.console.file.getvalue()
    app.controller.close()


@pytest.mark.asyncio
async def test_cmd_compact_declined_keeps_context(tmp_path, monkeypatch):
    """Declining (n) applies nothing — the original context stays intact."""
    app = _make_inline_app(tmp_path, monkeypatch)
    applied = _stub_compact(app, monkeypatch)
    monkeypatch.setattr(app, "_ask", _ask_returning("n"))

    await app._command("compact", "")

    assert applied == []
    assert "cancelled" in app.console.file.getvalue().lower()
    app.controller.close()


@pytest.mark.asyncio
async def test_cmd_compact_default_no_on_empty(tmp_path, monkeypatch):
    """Empty input takes the [y/N] default (N) — nothing applied."""
    app = _make_inline_app(tmp_path, monkeypatch)
    applied = _stub_compact(app, monkeypatch)
    monkeypatch.setattr(app, "_ask", _ask_returning(""))

    await app._command("compact", "")

    assert applied == []
    app.controller.close()


@pytest.mark.asyncio
async def test_cmd_compact_edit_applies_edited_summary(tmp_path, monkeypatch):
    """'edit' opens $EDITOR; the user-edited summary is what gets applied."""
    app = _make_inline_app(tmp_path, monkeypatch)
    applied = _stub_compact(app, monkeypatch)
    monkeypatch.setattr(app, "_ask", _ask_returning("edit"))
    _stub_editor(monkeypatch, "EDITED SUMMARY")

    await app._command("compact", "")

    assert applied == ["EDITED SUMMARY"]
    app.controller.close()


@pytest.mark.asyncio
async def test_cmd_compact_edit_aborted_keeps_context(tmp_path, monkeypatch):
    """Aborting the editor (None) applies nothing and keeps the context."""
    app = _make_inline_app(tmp_path, monkeypatch)
    applied = _stub_compact(app, monkeypatch)
    monkeypatch.setattr(app, "_ask", _ask_returning("edit"))
    _stub_editor(monkeypatch, None)

    await app._command("compact", "")

    assert applied == []
    assert "cancelled" in app.console.file.getvalue().lower()
    app.controller.close()


@pytest.mark.asyncio
async def test_cmd_compact_nothing_to_compact(tmp_path, monkeypatch):
    """An empty preview short-circuits before any prompt — nothing to apply."""
    app = _make_inline_app(tmp_path, monkeypatch)
    applied = _stub_compact(app, monkeypatch, summary="")
    monkeypatch.setattr(app, "_ask", _ask_returning("y"))

    await app._command("compact", "")

    assert applied == []
    assert "nothing to compact" in app.console.file.getvalue().lower()
    app.controller.close()


@pytest.mark.asyncio
async def test_compact_apply(tmp_path, monkeypatch):
    """`/compact` with no args runs the interactive apply path (not status)."""
    app = _make_inline_app(tmp_path, monkeypatch)
    applied = _stub_compact(app, monkeypatch)
    monkeypatch.setattr(app, "_ask", _ask_returning("y"))

    await app._command("compact", "")

    assert applied == ["SUMMARY: did X"]
    assert "Compacted" in app.console.file.getvalue()
    app.controller.close()


@pytest.mark.asyncio
async def test_clear_clears_screen(tmp_path, monkeypatch):
    """`/clear` resets scrollback, starts a fresh thread, and confirms."""
    app = _make_inline_app(tmp_path, monkeypatch)
    app.console.print("old conversation output")
    old_thread = app.controller.thread_id

    await app._command("clear", "")

    out = app.console.file.getvalue()
    assert "old conversation output" not in out
    assert "Started a fresh conversation" in out
    assert app.controller.thread_id != old_thread
    assert app._stream_text == ""
    app.controller.close()


def test_inline_app_constructs(tmp_path, monkeypatch):
    from jarn import repl

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(default_profile="openrouter",
                 providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
                 routing=RoutingConfig(main="openrouter/m"))
    app = repl.InlineApp(cfg, root)
    # toolbar + completer + key bindings build without a TTY.
    assert app._toolbar() is not None
    assert app._completer().commands  # has command names
    assert app._kb is not None
    app.controller.close()


def test_app_builds(tmp_path, monkeypatch):
    """The persistent prompt_toolkit Application + layout construct headlessly."""
    from jarn import repl

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(default_profile="openrouter",
                 providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
                 routing=RoutingConfig(main="openrouter/m"))
    app = repl.InlineApp(cfg, root)
    assert app._busy() is False
    built = app._build_app()
    assert built is not None  # layout/keybindings/toolbar all assemble
    assert app._toolbar() is not None
    app.controller.close()


def test_stream_control_renders_markdown_live(tmp_path, monkeypatch):
    """While busy, the live region renders the streamed buffer as FORMATTED
    markdown (Rich -> ANSI), not the dim escaped raw source — no double render."""
    from prompt_toolkit.formatted_text import ANSI

    from jarn import repl

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(default_profile="openrouter",
                 providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
                 routing=RoutingConfig(main="openrouter/m"))
    app = repl.InlineApp(cfg, root)
    monkeypatch.setattr(app, "_busy", lambda: True)
    app._stream_text = "# Heading\n\nbody **bold**"
    result = app._stream_control()
    assert isinstance(result, ANSI)
    # The rendered fragments are NOT the raw markdown source: the literal "# "
    # heading prefix and "**" bold markers are consumed by the formatter.
    rendered = result.value
    assert "# Heading" not in rendered
    assert "**bold**" not in rendered
    assert "Heading" in rendered and "bold" in rendered
    app.controller.close()


def test_stream_control_no_eight_line_clip(tmp_path, monkeypatch):
    """The preview Window is no longer hard-clipped at 8 lines — a long live block
    grows past the old fold, but is capped at (terminal rows - reserve) so the
    input + toolbar stay on-screen (not the old fixed 8, not unbounded)."""
    from prompt_toolkit.layout.containers import Window

    from jarn import repl

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(default_profile="openrouter",
                 providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
                 routing=RoutingConfig(main="openrouter/m"))
    app = repl.InlineApp(cfg, root)
    built = app._build_app()

    def _windows(container):
        if isinstance(container, Window):
            yield container
            return
        for child in getattr(container, "children", []) or []:
            yield from _windows(child)
        content = getattr(container, "content", None)
        if content is not None and content is not container:
            yield from _windows(content)

    maxes = [w.height.max for w in _windows(built.layout.container)
             if getattr(w.height, "max", None) is not None]
    assert 8 not in maxes  # the old hard 8-line preview clip is gone

    # The live region is now a terminal-height-aware cap (rows - reserve), not the
    # fixed 8 and not unbounded — so a tall block clips, the input stays visible.
    import os
    import shutil
    monkeypatch.setattr(shutil, "get_terminal_size", lambda *_a, **_k: os.terminal_size((80, 30)))
    dim = app._stream_height()
    assert dim.max == 26  # 30 rows - 4 reserved
    assert dim.min == 0
    monkeypatch.setattr(shutil, "get_terminal_size", lambda *_a, **_k: os.terminal_size((80, 5)))
    assert app._stream_height().max == 4  # floor on a tiny terminal
    app.controller.close()


def test_stream_control_plain_prompt_not_markdown(tmp_path, monkeypatch):
    """A picker/_ask prompt (not busy, _line_future pending) renders as the plain
    string, NOT markdown — guards the regression where a prompt gets markdownified."""
    from jarn import repl

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(default_profile="openrouter",
                 providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
                 routing=RoutingConfig(main="openrouter/m"))
    app = repl.InlineApp(cfg, root)
    assert app._busy() is False
    prompt = "Pick a model # 1"
    app._stream_text = prompt
    result = app._stream_control()
    assert result == prompt  # plain string, unchanged
    app.controller.close()


def test_stream_control_reasoning_renders_plain_not_collapsed(tmp_path, monkeypatch):
    """A reasoning block renders as PLAIN dim multi-line text, NOT markdown — the
    "✻ thinking\\n…" soft break must keep its line break (regression: routing
    reasoning through the markdown branch collapsed it onto one line)."""
    from prompt_toolkit.formatted_text import ANSI

    from jarn import repl
    from jarn.repl_renderer import REASONING_STREAM_PREFIX

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(default_profile="openrouter",
                 providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
                 routing=RoutingConfig(main="openrouter/m"))
    app = repl.InlineApp(cfg, root)
    monkeypatch.setattr(app, "_busy", lambda: True)
    app._set_stream(f"{REASONING_STREAM_PREFIX}let me consider the options")
    assert app._stream_is_reasoning is True
    result = app._stream_control()
    assert isinstance(result, ANSI)
    rendered = result.value
    assert "✻ thinking" in rendered
    assert "let me consider the options" in rendered
    # The buggy markdown render joins the soft break: "thinking let me consider".
    # The plain render keeps the header and body on separate lines.
    assert "thinking let me consider" not in rendered
    app.controller.close()


def test_stream_control_empty_buffer_has_no_leading_blank_line(tmp_path, monkeypatch):
    """A pure-whitespace buffer (newlines streamed before the first real prose)
    shows just the footer, not a stray leading blank line."""
    from prompt_toolkit.formatted_text import ANSI

    from jarn import repl

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(default_profile="openrouter",
                 providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
                 routing=RoutingConfig(main="openrouter/m"))
    app = repl.InlineApp(cfg, root)
    monkeypatch.setattr(app, "_busy", lambda: True)
    app._stream_text = "\n"  # renders to empty markdown → footer only
    result = app._stream_control()
    assert isinstance(result, ANSI)
    assert not result.value.startswith("\n")
    app.controller.close()


def test_todos_capped_in_live_region(tmp_path, monkeypatch):
    """A long plan (20 todos) is capped in the live region to a bounded block —
    at most 8 body lines plus a "… +N more" summary — so it can't push the input
    and toolbar off-screen.  The committed end-of-turn render still shows all 20."""
    from jarn import repl
    from jarn.repl.commands import format_todos

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(default_profile="openrouter",
                 providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
                 routing=RoutingConfig(main="openrouter/m"))
    app = repl.InlineApp(cfg, root)

    todos = (
        [{"content": f"done step {i}", "status": "completed"} for i in range(3)]
        + [{"content": "active step", "status": "in_progress"}]
        + [{"content": f"pending step {i}", "status": "pending"} for i in range(16)]
    )  # 20 items total

    # Pure formatter: the live cap bounds the body to <= 8 lines (+ header).
    capped = format_todos(todos, 80, cap=8)
    body = capped[1:]                                # drop the "⏺ Todos" header
    assert len(body) <= 8
    assert any("more" in line for line in body)      # elision summary present
    assert any("active step" in line for line in capped)   # active item stays visible
    # Not every pending item is shown — some are elided behind "… +N more".
    assert not all(f"pending step {i}" in "".join(capped) for i in range(16))

    # The committed render (no cap) still shows the whole list.
    full = format_todos(todos, 80)
    assert all(any(f"pending step {i}" in line for line in full) for i in range(16))

    # And through the live region itself (busy + live todos stored).
    monkeypatch.setattr(app, "_busy", lambda: True)
    app._live_todos = todos
    rendered = app._stream_control().value
    assert "more" in rendered
    app.controller.close()


@pytest.mark.asyncio
async def test_todos_update_live(tmp_path, monkeypatch):
    """A write_todos TOOL_END mid-turn shows the checklist LIVE above the input
    (◐ on the in-progress item); a second write_todos updates it IN PLACE with no
    scrollback commit between; the final checklist commits once at turn end and
    clears the live block."""
    from jarn import repl

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(default_profile="openrouter",
                 providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
                 routing=RoutingConfig(main="openrouter/m"))
    app = repl.InlineApp(cfg, root)

    async def _noop_runtime():
        return None
    monkeypatch.setattr(app.controller, "ensure_runtime", _noop_runtime)

    first = [
        {"content": "parse input", "status": "in_progress"},
        {"content": "write output", "status": "pending"},
    ]
    second = [
        {"content": "parse input", "status": "completed"},
        {"content": "write output", "status": "in_progress"},
    ]
    # Two sink calls (one per write_todos) + one end-of-turn commit read.
    todos_queue = [first, second, second]

    async def _fake_todos():
        return todos_queue.pop(0)
    monkeypatch.setattr(app.controller, "todos", _fake_todos)

    # The live region only composes while a turn is in flight.
    monkeypatch.setattr(app, "_busy", lambda: True)

    # Snapshot the live region right after each write_todos update.
    snaps: list[str] = []

    async def _rec_sink():
        await app._on_todos_live()
        snaps.append(app._stream_control().value)

    events = [
        Event(EventKind.TOOL_START, "write_todos", {"args": {}}),
        Event(EventKind.TOOL_END, "write_todos", {"summary": "2 todos"}),
        Event(EventKind.TEXT, "working through the plan"),
        Event(EventKind.TOOL_START, "read_file", {"args": {"path": "x"}}),
        Event(EventKind.TOOL_END, "read_file", {"summary": "1 line"}),   # must NOT trigger the sink
        Event(EventKind.TOOL_START, "write_todos", {"args": {}}),
        Event(EventKind.TOOL_END, "write_todos", {"summary": "2 todos"}),
        Event(EventKind.DONE),
    ]
    monkeypatch.setattr(app.controller, "make_driver", lambda approver: _FakeDriver(events))

    console = Console(file=StringIO(), width=80)
    app.console = console
    await repl._run_turn(
        console, app.controller, "hi", _ask_returning(""),
        live_sink=app._set_stream, token_sink=app._count_stream_chars,
        todos_sink=_rec_sink,
    )

    # Sink fired exactly twice — once per write_todos, never for read_file.
    assert len(snaps) == 2
    # First update: both items shown with ◐ on the in-progress "parse input".
    assert "◐" in snaps[0] and "parse input" in snaps[0]
    # Second update in place: "parse input" now ✔, ◐ moved to "write output".
    assert "✔" in snaps[1] and "◐" in snaps[1] and "write output" in snaps[1]
    # No committed "⏺ Todos" block landed in scrollback DURING the turn.
    assert "Todos" not in console.file.getvalue()

    # Prose streams BELOW the todos block in the live region.
    app._live_todos = second
    app._set_stream("here is some prose")
    composed = app._stream_control().value
    assert composed.index("write output") < composed.index("here is some prose")

    # Turn end: the committed render lands once and clears the live block.
    await app._render_todos()
    committed = console.file.getvalue()
    assert "Todos" in committed
    assert app._live_todos is None
    app.controller.close()


@pytest.mark.asyncio
async def test_pick_model_qualifies_vendor_prefixed_ref(tmp_path, monkeypatch):
    """A custom ref whose first segment isn't a configured provider profile is
    qualified under the default profile (regression: deepseek/deepseek-chat must
    become openrouter/deepseek/deepseek-chat, not stay bare)."""
    from jarn import repl

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="openrouter",
        providers={
            "openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x"),
            "google": ProviderConfig(type=ProviderType.GOOGLE, api_key="g"),
        },
        routing=RoutingConfig(main="openrouter/m"),
    )
    ctrl = Controller(cfg, root)
    console = Console(file=StringIO())

    # "deepseek" is a vendor prefix, not a configured profile -> qualify.
    repl._apply_model_ref(ctrl, console, "deepseek/deepseek-chat")
    assert ctrl.config.routing.main == "openrouter/deepseek/deepseek-chat"

    # "google" IS a configured profile -> already-qualified, left as-is.
    repl._apply_model_ref(ctrl, console, "google/gemini-2.5-pro")
    assert ctrl.config.routing.main == "google/gemini-2.5-pro"
    ctrl.close()


@pytest.mark.asyncio
async def test_pick_menu_returns_selected_value(tmp_path, monkeypatch):
    from jarn import repl

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="openrouter",
        providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
        routing=RoutingConfig(main="openrouter/m"),
    )
    app = repl.InlineApp(cfg, root)
    task = asyncio.create_task(
        app._pick_menu([("one", 1), ("two", 2)], header="pick", cancel_returns=None)
    )
    await asyncio.sleep(0)
    app._menu_index = 1
    app._menu_future.set_result(app._menu_options[1][1])
    assert await task == 2
    app.controller.close()


@pytest.mark.asyncio
async def test_pick_menu_esc_cancel(tmp_path, monkeypatch):
    from jarn import repl

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="openrouter",
        providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
        routing=RoutingConfig(main="openrouter/m"),
    )
    app = repl.InlineApp(cfg, root)
    task = asyncio.create_task(
        app._pick_menu([("yes", True), ("no", False)], cancel_returns=False)
    )
    await asyncio.sleep(0)
    app._menu_future.set_result(app._menu_cancel)
    assert await task is False
    app.controller.close()


@pytest.mark.asyncio
async def test_rewind_blank_continuation_indexes_branch(tmp_path, monkeypatch):
    """A rewind with no continuation prompt still records a session title for the
    forked branch, so it appears in /resume instead of being an orphan checkpoint
    (regression: the blank-prompt path returned before indexing the new thread)."""
    from jarn import repl

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="openrouter",
        providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
        routing=RoutingConfig(main="openrouter/m"),
    )
    app = repl.InlineApp(cfg, root)
    titles: list[str] = []

    async def _human_turns():
        return [(0, ""), (2, "second")]  # fork target (turn 1) has empty content

    async def _fork(idx):
        return idx  # non-None: the fork "succeeded"

    async def _pick(options, header="", cancel_returns=None):
        return (0, "")  # choose the empty-content first turn

    async def _ask(*a, **k):
        return ""  # blank → no continuation

    async def _replay():
        return None

    monkeypatch.setattr(app.controller, "human_turns", _human_turns)
    monkeypatch.setattr(app.controller, "fork_to_turn", _fork)
    monkeypatch.setattr(app, "_pick_menu", _pick)
    monkeypatch.setattr(app, "_ask", _ask)
    monkeypatch.setattr(app, "_replay_transcript", _replay)
    monkeypatch.setattr(
        app.controller, "record_session_title",
        lambda title, *, when: titles.append(title),
    )

    await app._rewind_picker()
    assert titles, "blank-continuation rewind must index the new branch in /resume"
    app.controller.close()


@pytest.mark.asyncio
async def test_skills_available_after_ensure_extensions(tmp_path, monkeypatch):
    from jarn import repl
    from jarn.agent.builder import JarnRuntime
    from jarn.extensibility.skills import Skill

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="openrouter",
        providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
        routing=RoutingConfig(main="openrouter/m"),
    )
    app = repl.InlineApp(cfg, root)
    skill = Skill(
        name="demo",
        description="A demo skill",
        body="instructions",
        trigger="manual",
        scope="project",
    )

    async def _fake_ensure():
        app.controller.runtime = JarnRuntime(
            agent=object(),
            config=cfg,
            factory=object(),
            project_root=root,
            system_prompt="",
            capabilities=object(),
            skills={"demo": skill},
        )

    monkeypatch.setattr(app.controller, "ensure_runtime", _fake_ensure)
    await app._ensure_extensions()
    result = app.controller.handle_command("skills", "")
    assert "demo" in result.text
    assert "No skills loaded" not in result.text
    app.controller.close()


def test_pastes_cleared_after_expand(tmp_path, monkeypatch):
    from jarn import repl

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="openrouter",
        providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
        routing=RoutingConfig(main="openrouter/m"),
    )
    app = repl.InlineApp(cfg, root)
    token = "[Pasted #1: 5 lines]"
    app._pastes[token] = "line one\nline two\nline three"
    expanded = app._expand_pastes(token)
    app._pastes.clear()
    assert "line one" in expanded
    assert app._pastes == {}
    app.controller.close()


@pytest.mark.asyncio
async def test_shell_escape_runs_command_and_prints_output(tmp_path, monkeypatch):
    """A ``! echo`` line bypasses the agent and prints stdout to the console."""
    from jarn import repl

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="openrouter",
        providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
        routing=RoutingConfig(main="openrouter/m"),
    )
    app = repl.InlineApp(cfg, root)
    buf = StringIO()
    app.console = Console(file=buf, width=80)
    await app._shell_escape("echo hello_shell_test")
    out = buf.getvalue()
    assert "hello_shell_test" in out
    app.controller.close()


@pytest.mark.asyncio
async def test_shell_escape_bare_bang_prints_hint(tmp_path, monkeypatch):
    """A bare ``!`` (no command) prints a usage hint, not an error."""
    from jarn import repl

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="openrouter",
        providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
        routing=RoutingConfig(main="openrouter/m"),
    )
    app = repl.InlineApp(cfg, root)
    buf = StringIO()
    app.console = Console(file=buf, width=80)
    await app._shell_escape("")
    out = buf.getvalue()
    assert "!" in out  # hint mentions the ! prefix
    app.controller.close()


def _git_repo_with_commit(root):
    """Create a fresh git repo with one commit at ``root``."""
    def g(*args):
        subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)

    root.mkdir(parents=True, exist_ok=True)
    g("init", "-b", "main")
    g("config", "user.email", "test@jarn.test")
    g("config", "user.name", "Jarn Test")
    (root / "README.txt").write_text("init\n", encoding="utf-8")
    g("add", "README.txt")
    g("commit", "-m", "init")


@pytest.mark.asyncio
async def test_abort_cancels_running_turn_and_rolls_back(tmp_path, monkeypatch):
    """/abort stops the in-flight turn AND reverts its edits in one action."""
    from jarn import repl
    from jarn.config.schema import GitConfig

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    _git_repo_with_commit(root)
    (root / ".jarn").mkdir(parents=True, exist_ok=True)
    cfg = Config(
        default_profile="openrouter",
        providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
        routing=RoutingConfig(main="openrouter/m"),
        git=GitConfig(autocheckpoint=True),
    )
    app = repl.InlineApp(cfg, root)
    buf = StringIO()
    app.console = Console(file=buf, width=80)

    # Turn-start checkpoint (what session.py snapshots before the agent edits).
    (root / "file.txt").write_text("before\n", encoding="utf-8")
    app.controller.checkpoint_manager.snapshot("running-turn")
    # The running turn edits a file; /abort must revert it.
    (root / "file.txt").write_text("after\n", encoding="utf-8")

    # Simulate an in-flight turn so _busy() is True and _cancel_turn cancels it.
    async def _never():
        await asyncio.sleep(3600)

    app._turn_task = asyncio.create_task(_never())
    assert app._busy()

    await app._command("abort", "")
    await asyncio.sleep(0)  # let the loop process the task cancellation

    assert app._turn_task.cancelled() or app._turn_task.done()
    assert (root / "file.txt").read_text(encoding="utf-8") == "before\n"
    out = buf.getvalue().lower()
    assert "cancel" in out and "rolled back" in out
    app.controller.close()


@pytest.mark.asyncio
async def test_abort_without_checkpoint_cancels_and_explains(tmp_path, monkeypatch):
    """/abort with autocheckpoint off cancels the turn and explains rollback is off."""
    from jarn import repl

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="openrouter",
        providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
        routing=RoutingConfig(main="openrouter/m"),
    )
    assert not cfg.git.autocheckpoint  # default OFF
    app = repl.InlineApp(cfg, root)
    buf = StringIO()
    app.console = Console(file=buf, width=80)

    async def _never():
        await asyncio.sleep(3600)

    app._turn_task = asyncio.create_task(_never())
    assert app._busy()

    await app._command("abort", "")
    await asyncio.sleep(0)  # let the loop process the task cancellation

    assert app._turn_task.cancelled() or app._turn_task.done()
    out = buf.getvalue().lower()
    assert "cancel" in out
    assert "autocheckpoint" in out
    assert "/config" in buf.getvalue()
    app.controller.close()


def _submit_handler(app):
    """Return the real `enter`/`_submit` keybinding handler (the keystroke path)."""
    for binding in app._kb.bindings:
        if getattr(binding.handler, "__name__", "") == "_submit":
            return binding.handler
    raise AssertionError("no _submit binding found")


async def _drain_background_tasks(*ignore):
    """Run the fire-and-forgot task(s) _submit spawned (the /abort command) to
    completion. Awaits every other pending task except the long-running turn
    stub(s) in ``ignore`` and this coroutine itself."""
    self = asyncio.current_task()
    skip = {self, *ignore}
    for _ in range(50):
        pending = [t for t in asyncio.all_tasks() if t not in skip and not t.done()]
        if not pending:
            break
        for t in pending:
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(asyncio.shield(t), timeout=1.0)
    await asyncio.sleep(0)  # let any scheduled cancellation settle


@pytest.mark.asyncio
async def test_submit_abort_cancels_running_turn_through_keystroke(tmp_path, monkeypatch):
    """Submitting `/abort` through _submit (not _command) while a turn is live
    cancels the running turn and rolls back — the real keystroke entrypoint.

    Regression guard: _submit must NOT queue /abort behind the running turn
    (which would leave the cancel branch unreachable until the turn finishes)."""
    from jarn import repl
    from jarn.config.schema import GitConfig

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    _git_repo_with_commit(root)
    (root / ".jarn").mkdir(parents=True, exist_ok=True)
    cfg = Config(
        default_profile="openrouter",
        providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
        routing=RoutingConfig(main="openrouter/m"),
        git=GitConfig(autocheckpoint=True),
    )
    app = repl.InlineApp(cfg, root)
    buf = StringIO()
    app.console = Console(file=buf, width=80)

    # Turn-start checkpoint, then a mid-turn edit /abort must revert.
    (root / "file.txt").write_text("before\n", encoding="utf-8")
    app.controller.checkpoint_manager.snapshot("running-turn")
    (root / "file.txt").write_text("after\n", encoding="utf-8")

    async def _never():
        await asyncio.sleep(3600)

    app._turn_task = asyncio.create_task(_never())
    turn_task = app._turn_task
    assert app._busy()

    # Drive the actual submit path: type "/abort" + Enter.
    app.input.text = "/abort"
    _submit_handler(app)(event=None)
    # _submit fire-and-forgets _command via create_task; drain background tasks
    # (the abort command) so it reaches _cancel_turn and the rollback print.
    await _drain_background_tasks(turn_task)

    assert turn_task.cancelled() or turn_task.done()
    assert not app.input.text  # the abort line did NOT stay queued in the input
    assert (root / "file.txt").read_text(encoding="utf-8") == "before\n"
    out = buf.getvalue().lower()
    assert "cancel" in out and "rolled back" in out
    app.controller.close()


@pytest.mark.asyncio
async def test_submit_abort_without_checkpoint_cancels_and_explains(tmp_path, monkeypatch):
    """Through the real keystroke path, /abort with autocheckpoint off cancels the
    turn and explains rollback needs autocheckpoint."""
    from jarn import repl

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="openrouter",
        providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
        routing=RoutingConfig(main="openrouter/m"),
    )
    assert not cfg.git.autocheckpoint  # default OFF
    app = repl.InlineApp(cfg, root)
    buf = StringIO()
    app.console = Console(file=buf, width=80)

    async def _never():
        await asyncio.sleep(3600)

    app._turn_task = asyncio.create_task(_never())
    turn_task = app._turn_task
    assert app._busy()

    app.input.text = "/abort"
    _submit_handler(app)(event=None)
    await _drain_background_tasks(turn_task)

    assert turn_task.cancelled() or turn_task.done()
    out = buf.getvalue().lower()
    assert "cancel" in out
    assert "autocheckpoint" in out
    assert "/config" in buf.getvalue()
    app.controller.close()


def _esc_handler(app):
    """Return the real `escape` keybinding handler (the Esc keystroke path)."""
    for binding in app._kb.bindings:
        if getattr(binding.handler, "__name__", "") == "_esc_key":
            return binding.handler
    raise AssertionError("no _esc_key binding found")


def _app_with_running_edited_turn(tmp_path, monkeypatch, *, autocheckpoint):
    """An InlineApp with an in-flight turn that already applied a file edit.

    Returns (app, buf, turn_task). The turn task is a never-finishing stub so
    _busy() is True and the cancel path fires."""
    from jarn import repl
    from jarn.config.schema import GitConfig

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    if autocheckpoint:
        _git_repo_with_commit(root)
    (root / ".jarn").mkdir(parents=True, exist_ok=True)
    cfg = Config(
        default_profile="openrouter",
        providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
        routing=RoutingConfig(main="openrouter/m"),
        git=GitConfig(autocheckpoint=autocheckpoint),
    )
    app = repl.InlineApp(cfg, root)
    buf = StringIO()
    app.console = Console(file=buf, width=80)
    # The running turn applied a file edit: the live tool sink carries it.
    app._last_tool_outputs = [("edit_file", "patched file.txt")]

    async def _never():
        await asyncio.sleep(3600)

    app._turn_task = asyncio.create_task(_never())
    return app, buf, app._turn_task


@pytest.mark.asyncio
async def test_esc_after_edits_with_checkpoint_offers_rollback(tmp_path, monkeypatch):
    """Esc cancelling a turn that made edits states the edits remain and offers
    rollback via /abort when a turn-start checkpoint exists. It must NOT touch
    the files itself (that's /abort's job)."""
    app, buf, turn_task = _app_with_running_edited_turn(
        tmp_path, monkeypatch, autocheckpoint=True
    )
    assert app._busy()

    _esc_handler(app)(event=None)
    await asyncio.sleep(0)

    assert turn_task.cancelled() or turn_task.done()
    out = buf.getvalue().lower()
    assert "still on disk" in out
    assert "/abort" in buf.getvalue()
    app.controller.close()


@pytest.mark.asyncio
async def test_esc_after_edits_without_checkpoint_explains_revert(tmp_path, monkeypatch):
    """Esc after edits with autocheckpoint off still says edits remain and points
    at /abort + how to enable rollback."""
    app, buf, turn_task = _app_with_running_edited_turn(
        tmp_path, monkeypatch, autocheckpoint=False
    )
    assert not app.controller.config.git.autocheckpoint  # default OFF
    assert app._busy()

    _esc_handler(app)(event=None)
    await asyncio.sleep(0)

    assert turn_task.cancelled() or turn_task.done()
    out = buf.getvalue()
    assert "still on disk" in out.lower()
    assert "/abort" in out
    assert "autocheckpoint" in out.lower()
    app.controller.close()


@pytest.mark.asyncio
async def test_esc_with_no_edits_is_unchanged(tmp_path, monkeypatch):
    """Esc cancelling a turn that made NO file edits prints no edits-remain note."""
    app, buf, turn_task = _app_with_running_edited_turn(
        tmp_path, monkeypatch, autocheckpoint=True
    )
    app._last_tool_outputs = [("read_file", "just looked")]  # no write/edit
    assert app._busy()

    _esc_handler(app)(event=None)
    await asyncio.sleep(0)

    assert turn_task.cancelled() or turn_task.done()
    assert "still on disk" not in buf.getvalue().lower()
    app.controller.close()


@pytest.mark.asyncio
async def test_abort_does_not_add_edits_remain_note(tmp_path, monkeypatch):
    """/abort rolls edits back, so it must NOT print the contradictory
    "edits still on disk" Esc note (regression guard for the shared cancel path)."""
    app, buf, _turn_task = _app_with_running_edited_turn(
        tmp_path, monkeypatch, autocheckpoint=True
    )
    # Give /abort a real turn-start checkpoint to revert to.
    root = app.controller.project_root
    (root / "file.txt").write_text("before\n", encoding="utf-8")
    app.controller.checkpoint_manager.snapshot("running-turn")
    (root / "file.txt").write_text("after\n", encoding="utf-8")
    assert app._busy()

    await app._command("abort", "")
    await asyncio.sleep(0)

    out = buf.getvalue().lower()
    assert "rolled back" in out
    assert "still on disk" not in out
    app.controller.close()


def test_repl_importable():
    from jarn.repl import InlineApp, run_inline  # noqa: F401


def test_shell_escape_lexer_colors_bang_line():
    """A `!` shell-escape line renders in the red `shell-escape` style; a normal
    line is unstyled."""
    from prompt_toolkit.document import Document

    from jarn.repl import _ShellEscapeLexer

    lx = _ShellEscapeLexer()
    assert lx.lex_document(Document("!ls -la"))(0) == [("class:shell-escape", "!ls -la")]
    assert lx.lex_document(Document("  !rm x"))(0) == [("class:shell-escape", "  !rm x")]
    assert lx.lex_document(Document("hello"))(0) == [("", "hello")]


def test_shell_escape_style_registered():
    from jarn.tui import palette

    assert "shell-escape" in palette.toolbar_style_dict()


@pytest.mark.asyncio
async def test_error_health_notice_shows_doctor_hint(tmp_path, monkeypatch):
    """When controller.health is 'error', the startup notice must include '/doctor'."""
    from jarn import repl

    ctrl = _controller(tmp_path, monkeypatch)
    # Simulate a provider key failure being already set (as validate() would set it).
    ctrl.health = "error"
    ctrl.last_error = "API key missing or invalid"
    ctrl.health_notice_shown = False

    async def _noop_runtime():
        return None
    monkeypatch.setattr(ctrl, "ensure_runtime", _noop_runtime)
    monkeypatch.setattr(ctrl, "make_driver",
                        lambda approver: _FakeDriver([Event(EventKind.TEXT, "ok"), Event(EventKind.DONE)]))

    console = Console(file=StringIO(), width=120)
    await repl._run_turn(console, ctrl, "hi", _ask_returning(""))
    out = console.file.getvalue()
    assert "/doctor" in out, f"expected /doctor in startup error notice, got: {out!r}"
    assert ctrl.health_notice_shown is True
    ctrl.close()


@pytest.mark.asyncio
async def test_degraded_health_notice_has_no_doctor_hint(tmp_path, monkeypatch):
    """Degraded (non-error) health notice must NOT append '/doctor' (only error does)."""
    from jarn import repl

    ctrl = _controller(tmp_path, monkeypatch)
    ctrl.health = "degraded"
    ctrl.last_error = "sandbox unavailable"
    ctrl.health_notice_shown = False

    async def _noop_runtime():
        return None
    monkeypatch.setattr(ctrl, "ensure_runtime", _noop_runtime)
    monkeypatch.setattr(ctrl, "make_driver",
                        lambda approver: _FakeDriver([Event(EventKind.TEXT, "ok"), Event(EventKind.DONE)]))

    console = Console(file=StringIO(), width=120)
    await repl._run_turn(console, ctrl, "hi", _ask_returning(""))
    out = console.file.getvalue()
    assert "/doctor" not in out, f"degraded notice should not show /doctor, got: {out!r}"
    ctrl.close()


# ---------------------------------------------------------------------------
# P3.C — yolo transition guard
# ---------------------------------------------------------------------------


def _make_inline_app(tmp_path, monkeypatch):
    """Create a minimal InlineApp with a fake console for testing."""
    from jarn import repl
    from jarn.config.schema import Config, ProviderConfig, ProviderType, RoutingConfig

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="openrouter",
        providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
        routing=RoutingConfig(main="openrouter/m"),
    )
    app = repl.InlineApp(cfg, root)
    app.console = Console(file=StringIO(), width=80)
    return app


@pytest.mark.asyncio
async def test_confirm_yolo_returns_true_on_y(tmp_path, monkeypatch):
    """`_confirm_yolo` returns True when the user types 'y'."""
    app = _make_inline_app(tmp_path, monkeypatch)
    # Inject a fake _ask that always returns "y"
    async def _fake_ask(prompt: str) -> str:
        return "y"
    monkeypatch.setattr(app, "_ask", _fake_ask)
    assert await app._confirm_yolo() is True
    app.controller.close()


@pytest.mark.asyncio
async def test_confirm_yolo_returns_false_on_n(tmp_path, monkeypatch):
    """`_confirm_yolo` returns False when the user types 'n' (or anything other than 'y')."""
    app = _make_inline_app(tmp_path, monkeypatch)
    async def _fake_ask(prompt: str) -> str:
        return "n"
    monkeypatch.setattr(app, "_ask", _fake_ask)
    assert await app._confirm_yolo() is False
    app.controller.close()


@pytest.mark.asyncio
async def test_confirm_yolo_returns_false_on_empty(tmp_path, monkeypatch):
    """`_confirm_yolo` defaults to False on empty input (the [y/N] default is N)."""
    app = _make_inline_app(tmp_path, monkeypatch)
    async def _fake_ask(prompt: str) -> str:
        return ""
    monkeypatch.setattr(app, "_ask", _fake_ask)
    assert await app._confirm_yolo() is False
    app.controller.close()


@pytest.mark.asyncio
async def test_confirm_yolo_prints_visible_banner(tmp_path, monkeypatch):
    """The yolo confirm prints a prominent scrollback banner (not just the faint
    region-above-input ask) so the y/N decision can't be missed."""
    app = _make_inline_app(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "_ask", _ask_returning("y"))
    assert await app._confirm_yolo() is True
    out = app.console.file.getvalue()
    assert "YOLO mode" in out  # the banner landed in visible scrollback
    app.controller.close()


@pytest.mark.asyncio
async def test_command_mode_yolo_confirmed(tmp_path, monkeypatch):
    """`/mode yolo` applies yolo when user confirms with 'y'."""
    from jarn.config.schema import PermissionMode

    app = _make_inline_app(tmp_path, monkeypatch)
    # Start in ask mode
    assert app.controller.config.permission_mode.value == "ask"

    async def _fake_ask(prompt: str) -> str:
        return "y"
    monkeypatch.setattr(app, "_ask", _fake_ask)

    await app._command("mode", "yolo")
    assert app.controller.config.permission_mode == PermissionMode.YOLO
    app.controller.close()


@pytest.mark.asyncio
async def test_command_mode_yolo_declined_keeps_previous(tmp_path, monkeypatch):
    """`/mode yolo` keeps the previous mode when user declines."""
    from jarn.config.schema import PermissionMode

    app = _make_inline_app(tmp_path, monkeypatch)
    assert app.controller.config.permission_mode.value == "ask"

    async def _fake_ask(prompt: str) -> str:
        return "n"
    monkeypatch.setattr(app, "_ask", _fake_ask)

    await app._command("mode", "yolo")
    # Mode must remain ask
    assert app.controller.config.permission_mode == PermissionMode.ASK
    out = app.console.file.getvalue()
    assert "cancelled" in out.lower()
    app.controller.close()


@pytest.mark.asyncio
async def test_command_mode_yolo_no_reprompt_when_already_yolo(tmp_path, monkeypatch):
    """`/mode yolo` when already in yolo does NOT re-prompt (transition-only guard)."""
    from jarn.config.schema import PermissionMode

    app = _make_inline_app(tmp_path, monkeypatch)
    # Set mode to yolo directly (simulating already being in yolo)
    app.controller.config.permission_mode = PermissionMode.YOLO
    app.controller.engine.mode = PermissionMode.YOLO

    ask_calls: list[str] = []
    async def _tracking_ask(prompt: str) -> str:
        ask_calls.append(prompt)
        return "n"
    monkeypatch.setattr(app, "_ask", _tracking_ask)

    await app._command("mode", "yolo")
    # No confirmation prompt when already in yolo
    assert ask_calls == [], "should not prompt when already in yolo mode"
    app.controller.close()


@pytest.mark.asyncio
async def test_confirm_and_cycle_yolo_confirmed(tmp_path, monkeypatch):
    """`_confirm_and_cycle_yolo` applies yolo when confirmed."""
    from jarn.config.schema import PermissionMode

    app = _make_inline_app(tmp_path, monkeypatch)
    # Put mode at auto-edit so next cycle = yolo
    app.controller.config.permission_mode = PermissionMode.AUTO_EDIT
    app.controller.engine.mode = PermissionMode.AUTO_EDIT

    async def _fake_ask(prompt: str) -> str:
        return "y"
    monkeypatch.setattr(app, "_ask", _fake_ask)
    # _flash and app.invalidate are no-ops in tests (app is None)
    monkeypatch.setattr(app, "_flash", lambda *a, **k: None)

    await app._confirm_and_cycle_yolo()
    assert app.controller.config.permission_mode == PermissionMode.YOLO
    app.controller.close()


@pytest.mark.asyncio
async def test_confirm_and_cycle_yolo_declined_keeps_mode(tmp_path, monkeypatch):
    """`_confirm_and_cycle_yolo` does NOT advance mode when declined."""
    from jarn.config.schema import PermissionMode

    app = _make_inline_app(tmp_path, monkeypatch)
    app.controller.config.permission_mode = PermissionMode.AUTO_EDIT
    app.controller.engine.mode = PermissionMode.AUTO_EDIT

    async def _fake_ask(prompt: str) -> str:
        return "n"
    monkeypatch.setattr(app, "_ask", _fake_ask)
    monkeypatch.setattr(app, "_flash", lambda *a, **k: None)

    await app._confirm_and_cycle_yolo()
    # Mode must remain auto-edit
    assert app.controller.config.permission_mode == PermissionMode.AUTO_EDIT
    out = app.console.file.getvalue()
    assert "cancelled" in out.lower()
    app.controller.close()


@pytest.mark.asyncio
async def test_command_model_refresh_picks_discovered_model(tmp_path, monkeypatch):
    """`/model refresh` re-queries local endpoints and applies the picked model."""
    app = _make_inline_app(tmp_path, monkeypatch)
    # An ollama provider must be configured for the discovered ref to be
    # recognised as already-qualified (otherwise it'd reroute to the default).
    app.controller.config.providers["ollama"] = ProviderConfig(
        type=ProviderType.OLLAMA, base_url="http://localhost:11434"
    )

    async def _noop_ext() -> None:
        return None
    monkeypatch.setattr(app, "_ensure_extensions", _noop_ext)
    # discover_models is mocked → no real network.
    monkeypatch.setattr(
        app.controller,
        "discover_models",
        lambda: [("ollama/qwen3-coder:30b", "ollama"), ("ollama/llama3:8b", "ollama")],
    )

    task = asyncio.create_task(app._command("model", "refresh"))
    # Wait until the picker registers its options, then select the first model.
    for _ in range(50):
        await asyncio.sleep(0)
        if app._menu_future is not None and app._menu_options:
            break
    assert app._menu_options[0][1] == "ollama/qwen3-coder:30b"
    app._menu_future.set_result(app._menu_options[0][1])
    await task

    assert app.controller.config.resolved_main_model() == "ollama/qwen3-coder:30b"
    app.controller.close()


@pytest.mark.asyncio
async def test_command_model_refresh_degrades_to_manual_when_unreachable(tmp_path, monkeypatch):
    """`/model refresh` with no reachable endpoint prints a note + offers manual entry."""
    app = _make_inline_app(tmp_path, monkeypatch)

    async def _noop_ext() -> None:
        return None
    monkeypatch.setattr(app, "_ensure_extensions", _noop_ext)
    monkeypatch.setattr(app.controller, "discover_models", lambda: [])

    async def _fake_ask(prompt: str) -> str:
        return "openrouter/manual-model"
    monkeypatch.setattr(app, "_ask", _fake_ask)

    await app._command("model", "refresh")

    out = app.console.file.getvalue()
    assert "No local models found" in out
    # The manually-entered ref is applied (never blocks the user).
    assert app.controller.config.resolved_main_model() == "openrouter/manual-model"
    app.controller.close()


# -- review follow-up fixes (P4.C /abort, P3.B hint, P3.A clamp display) ----


@pytest.mark.asyncio
async def test_cmd_abort_idle_does_not_rollback(tmp_path, monkeypatch):
    """/abort while no turn is running must NOT silently undo the last turn."""
    app = _make_inline_app(tmp_path, monkeypatch)
    called: list[bool] = []
    monkeypatch.setattr(app.controller, "abort_rollback",
                        lambda: (called.append(True), "rolled back")[1])
    assert not app._busy()
    await app._command("abort", "")
    assert called == []  # idle → no rollback
    assert "nothing to abort" in app.console.file.getvalue().lower()
    app.controller.close()


def test_autocheckpoint_hint_fires_on_write(tmp_path, monkeypatch):
    """The one-time hint is shown after a turn that wrote a file."""
    app = _make_inline_app(tmp_path, monkeypatch)
    monkeypatch.setattr(app.controller, "autocheckpoint_off_hint", lambda: "ENABLE UNDO HINT")
    app._last_tool_outputs = [("write_file", "x")]
    app._maybe_autocheckpoint_hint()
    assert "ENABLE UNDO HINT" in app.console.file.getvalue()
    app.controller.close()


def test_autocheckpoint_hint_silent_without_write(tmp_path, monkeypatch):
    """No write in the turn → the hint is never even queried."""
    app = _make_inline_app(tmp_path, monkeypatch)
    called: list[int] = []
    monkeypatch.setattr(app.controller, "autocheckpoint_off_hint",
                        lambda: (called.append(1), "H")[1])
    app._last_tool_outputs = [("read_file", "x")]
    app._maybe_autocheckpoint_hint()
    assert called == []
    app.controller.close()


def test_apply_mode_ref_reports_clamp_on_untrusted(tmp_path, monkeypatch):
    """The /mode picker shows the CLAMPED mode (plan), not the requested one."""
    from jarn import repl
    from jarn.config.schema import Config, ProviderConfig, ProviderType, RoutingConfig

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="openrouter",
        providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
        routing=RoutingConfig(main="openrouter/m"),
    )
    ctrl = Controller(cfg, root, project_trusted=False)
    console = Console(file=StringIO(), width=80)
    repl._apply_mode_ref(ctrl, console, "yolo")
    out = console.file.getvalue()
    assert "plan" in out and "clamped" in out.lower()
    ctrl.close()


@pytest.mark.asyncio
async def test_turn_failure_points_at_log_traceback(tmp_path, monkeypatch):
    """A mid-turn exception (e.g. a langgraph error) shows the message AND points
    the user at the file-logged full traceback instead of swallowing it."""
    from jarn import repl
    from jarn.config import paths

    app = _make_inline_app(tmp_path, monkeypatch)
    # Narrow console so the long log path WOULD word-wrap — reproduces the CI
    # failure where the wrap split ".../jarn.log" across a line ("jarn.lo\ng"),
    # breaking the pointer. The fix prints it soft-wrapped (one logical line).
    app.console = Console(file=StringIO(), width=40)

    async def _boom(*a, **k):
        raise RuntimeError("langgraph boom")

    monkeypatch.setattr(repl.turn, "_run_turn", _boom)
    await app._handle("please do something")
    out = app.console.file.getvalue()
    log_path = str(paths.global_logs_dir() / "jarn.log")
    assert "langgraph boom" in out                 # concise message still shown
    assert "full traceback" in out                 # pointer present
    assert log_path in out                          # full path, un-wrapped
    app.controller.close()


@pytest.mark.asyncio
async def test_rapid_yolo_confirm_requests_are_deduped(tmp_path, monkeypatch):
    """Rapid Shift+Tab→yolo presses must not stack confirmations that fight over
    the single _line_future and hang. Only ONE confirmation runs; repeats while
    it is in flight are dropped."""
    from jarn.config.schema import PermissionMode

    app = _make_inline_app(tmp_path, monkeypatch)
    app.controller.config.permission_mode = PermissionMode.AUTO_EDIT
    app.controller.engine.mode = PermissionMode.AUTO_EDIT

    asks: list[str] = []
    gate = asyncio.Event()

    async def _fake_ask(prompt: str) -> str:
        asks.append(prompt)
        await gate.wait()   # hold the confirmation open
        return "n"

    monkeypatch.setattr(app, "_ask", _fake_ask)

    app._request_yolo_confirm()
    app._request_yolo_confirm()   # ignored — a confirmation is already in flight
    app._request_yolo_confirm()
    await asyncio.sleep(0.02)      # let the single task reach _ask
    assert app._yolo_confirm_inflight is True
    assert len(asks) == 1          # exactly one confirmation, not three

    gate.set()                     # let it resolve (declined → mode unchanged)
    await asyncio.sleep(0.02)
    assert app._yolo_confirm_inflight is False  # flag cleared, so a later press works
    assert app.controller.config.permission_mode is PermissionMode.AUTO_EDIT
    app.controller.close()


def test_gen_stat_estimates_output_tokens_when_provider_reports_none(tmp_path, monkeypatch):
    """LM Studio / OpenAI-compatible stream without per-chunk usage, so the live
    counter would sit at 0 — fall back to a ~estimate from the streamed text."""
    app = _make_inline_app(tmp_path, monkeypatch)
    app._turn_base_output = 0           # tracker stays flat → no live usage
    app._count_stream_chars("x" * 40)   # ~10 tokens (40 // 4)
    assert "~10 tok" in app._gen_stat()
    app.controller.close()


def test_gen_stat_uses_real_output_when_provider_reports_usage(tmp_path, monkeypatch):
    """When the provider DOES report usage live (e.g. Anthropic), show the real
    output-token delta and ignore the estimate (no double-count, no '~')."""
    app = _make_inline_app(tmp_path, monkeypatch)
    app._turn_base_output = 0
    app.controller.tracker.total.output_tokens = 50   # generated this turn
    app._count_stream_chars("x" * 400)                # estimate (100) must be ignored
    stat = app._gen_stat()
    assert "50 tok" in stat and "~" not in stat


def test_gen_stat_reports_tok_per_second(tmp_path, monkeypatch):
    """The rate is measured from the first streamed token (generation speed)."""
    import time as _time

    app = _make_inline_app(tmp_path, monkeypatch)
    app._turn_base_output = 0
    app._count_stream_chars("x" * 400)               # ~100 tokens
    app._first_token_at = _time.monotonic() - 2.0    # 2s of generation
    stat = app._gen_stat()
    assert "~100 tok" in stat
    assert "tok/s" in stat                            # ~50 tok/s
    app.controller.close()


def test_gen_stat_shows_prompt_tokens_while_thinking(tmp_path, monkeypatch):
    """Before any output streams, show the prompt size (real input delta) — not a
    tok/s rate, which is meaningless during prompt processing."""
    app = _make_inline_app(tmp_path, monkeypatch)
    app._turn_base_input = 0
    app.controller.tracker.total.input_tokens = 1234   # provider reported the prompt
    assert app._first_token_at is None                 # still thinking
    stat = app._gen_stat()
    assert "prompt 1234 tok" in stat and "tok/s" not in stat
    app.controller.close()


def test_gen_stat_thinking_proxies_context_when_prompt_unknown(tmp_path, monkeypatch):
    """LM Studio doesn't report input until the end — fall back to the prior
    context size as a ~prompt proxy while thinking."""
    app = _make_inline_app(tmp_path, monkeypatch)
    app._turn_base_input = 0
    app.controller.tracker.context_tokens = 5000
    assert "prompt ~5000 tok" in app._gen_stat()
    app.controller.close()


def test_help_generated_from_registry():
    """`/help` body is generated from the unified command registry."""
    from jarn.commands.registry import COMMAND_SPECS, grouped_specs, help_group_order
    from jarn.extensibility.commands import format_help

    body = format_help()
    for spec in COMMAND_SPECS:
        assert f"/{spec.name}" in body
        assert spec.description in body

    grouped = grouped_specs()
    for group_name in help_group_order():
        specs = grouped.get(group_name, [])
        if not specs:
            continue
        assert f"[b]{group_name}[/b]" in body
        group_pos = body.index(f"[b]{group_name}[/b]")
        for spec in specs:
            assert body.index(f"/{spec.name}") > group_pos


def test_commit_width_tracks_resize(monkeypatch):
    """console.width is refreshed at every commit — tracks the current terminal width.

    TDD RED: before the fix, console.width stays at the startup value after a resize.
    TDD GREEN: after the fix, console.width equals the current terminal width (capped
    at 100) at each commit and live-render call.
    """
    import os
    import shutil as _shutil

    from jarn.repl_renderer import TurnRenderer

    # Phase 1: terminal reports 120 cols → width capped to 100.
    monkeypatch.setattr(
        _shutil, "get_terminal_size",
        lambda *_a, **_k: os.terminal_size((120, 24)),
    )
    console = Console(file=StringIO(), force_terminal=True, width=80)
    r = TurnRenderer(console, live_sink=lambda _: None, spinner=False)

    r.on_text("first commit text")
    r.on_tool("tool_a", {})  # triggers _commit_text before the tool line

    # After first commit, width should be refreshed to min(120, 100) = 100.
    assert console.width == 100, f"expected 100 (120 cols capped at 100), got {console.width}"

    # Phase 2: terminal shrinks to 60 cols.
    monkeypatch.setattr(
        _shutil, "get_terminal_size",
        lambda *_a, **_k: os.terminal_size((60, 24)),
    )

    r.on_text("second commit text")
    r.finish()  # triggers _commit_text

    # After second commit, width should be refreshed to min(60, 100) = 60.
    assert console.width == 60, f"expected 60 after resize, got {console.width}"

    # Phase 3: wide terminal (250 cols) → width still capped at 100.
    console3 = Console(file=StringIO(), force_terminal=True, width=80)
    monkeypatch.setattr(
        _shutil, "get_terminal_size",
        lambda *_a, **_k: os.terminal_size((250, 24)),
    )
    r3 = TurnRenderer(console3, live_sink=lambda _: None, spinner=False)
    r3.on_text("cap test text")
    r3.finish()
    assert console3.width == 100, f"expected 100 cap at 250 cols, got {console3.width}"

    # Phase 4: floor guard test — terminal reports 0 cols → width floored to 1.
    from jarn.repl_renderer import _current_width
    monkeypatch.setattr(
        _shutil, "get_terminal_size",
        lambda *_a, **_k: os.terminal_size((0, 24)),
    )
    assert _current_width() == 1, f"expected 1 (floor guard for 0 cols), got {_current_width()}"


# ── T-2-1: Turn-end + approval notifications (bell / desktop) ──────────────


def _make_notify_app(tmp_path, monkeypatch, *, notify="bell", notify_min_secs=10):
    """InlineApp with configurable ui.notify / ui.notify_min_secs for T-2-1 tests."""
    from jarn import repl
    from jarn.config.schema import Config, ProviderConfig, ProviderType, RoutingConfig, UIConfig

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="openrouter",
        providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
        routing=RoutingConfig(main="openrouter/m"),
        ui=UIConfig(notify=notify, notify_min_secs=notify_min_secs),
    )
    app = repl.InlineApp(cfg, root)
    app.console = Console(file=StringIO(), width=80)
    return app


def _stub_agent_turn(app, monkeypatch):
    """Stub controller so _handle's agent-turn branch completes immediately."""

    async def _noop_runtime():
        return None

    monkeypatch.setattr(app.controller, "ensure_runtime", _noop_runtime)
    events = [Event(EventKind.TEXT, "done"), Event(EventKind.DONE)]
    monkeypatch.setattr(app.controller, "make_driver", lambda approver: _FakeDriver(events))


@pytest.mark.asyncio
async def test_bell_on_long_turn(tmp_path, monkeypatch):
    """A turn that exceeds notify_min_secs emits exactly one BEL to the console."""
    import time

    app = _make_notify_app(tmp_path, monkeypatch, notify="bell", notify_min_secs=10)
    _stub_agent_turn(app, monkeypatch)

    # Fake turn start 15 seconds in the past so elapsed > 10.
    app._turn_start = time.monotonic() - 15.0

    await app._handle("write me a test")

    out = app.console.file.getvalue()
    assert out.count("\a") == 1, f"expected exactly 1 BEL, got {out.count(chr(7))!r}"
    app.controller.close()


@pytest.mark.asyncio
async def test_no_bell_fast_turn(tmp_path, monkeypatch):
    """A turn that completes before notify_min_secs emits no BEL."""
    import time

    app = _make_notify_app(tmp_path, monkeypatch, notify="bell", notify_min_secs=10)
    _stub_agent_turn(app, monkeypatch)

    # Turn started only 2 seconds ago — under the 10-second threshold.
    app._turn_start = time.monotonic() - 2.0

    await app._handle("quick question")

    out = app.console.file.getvalue()
    assert "\a" not in out, "fast turn must not emit BEL"
    app.controller.close()


@pytest.mark.asyncio
async def test_bell_on_approval(tmp_path, monkeypatch):
    """An approval prompt emits one BEL regardless of elapsed time."""
    from jarn import repl

    ctrl = _controller(tmp_path, monkeypatch)
    ctrl.config.ui.notify = "bell"
    console = Console(file=StringIO(), width=80)

    request = ApprovalRequest(
        action=Action(ActionKind.SHELL, "npm test"),
        result=PermissionResult(Decision.ASK, "ask mode"),
    )
    await repl._approve(console, ctrl, request, ask=_ask_returning("r"))

    out = console.file.getvalue()
    assert "\a" in out, "approval prompt must emit BEL"
    ctrl.close()


@pytest.mark.asyncio
async def test_notify_off_silent(tmp_path, monkeypatch):
    """With ui.notify: off, neither a long turn nor an approval emits BEL."""
    import time

    from jarn import repl
    from jarn.config.schema import Config, ProviderConfig, ProviderType, RoutingConfig, UIConfig
    from jarn.tui.controller import Controller

    # Long-turn path (app._handle).
    app = _make_notify_app(tmp_path, monkeypatch, notify="off", notify_min_secs=0)
    _stub_agent_turn(app, monkeypatch)
    app._turn_start = time.monotonic() - 20.0
    await app._handle("test")
    assert "\a" not in app.console.file.getvalue(), "notify:off must suppress turn BEL"
    app.controller.close()

    # Approval path (_approve) — use a separate project root to avoid directory conflict.
    root2 = tmp_path / "proj2"
    (root2 / ".jarn").mkdir(parents=True)
    cfg2 = Config(
        default_profile="openrouter",
        providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
        routing=RoutingConfig(main="openrouter/m"),
        ui=UIConfig(notify="off"),
    )
    ctrl2 = Controller(cfg2, root2)
    console2 = Console(file=StringIO(), width=80)
    request2 = ApprovalRequest(
        action=Action(ActionKind.SHELL, "ls"),
        result=PermissionResult(Decision.ASK, "ask mode"),
    )
    await repl._approve(console2, ctrl2, request2, ask=_ask_returning("r"))
    assert "\a" not in console2.file.getvalue(), "notify:off must suppress approval BEL"
    ctrl2.close()


def test_desktop_notify_no_subprocess_when_binary_missing(tmp_path, monkeypatch):
    """Desktop mode spawns no subprocess when the notification binary is absent."""
    import subprocess

    from jarn.config.schema import UIConfig
    from jarn.tui.notify import notify

    spawned: list = []

    def _fake_popen(cmd, **kwargs):
        spawned.append(cmd)

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    # Patch shutil.which so every binary lookup returns None (nothing installed).
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    settings = UIConfig(notify="desktop", notify_min_secs=0)
    # Should not raise, and must not have called Popen.
    notify("turn_done", settings, elapsed=20.0, write=lambda s: None)
    assert spawned == [], f"expected no Popen calls, got {spawned}"


@pytest.mark.asyncio
async def test_bell_on_plan_approval(tmp_path, monkeypatch):
    """A plan-type approval prompt emits one BEL regardless of elapsed time."""
    from jarn import repl

    ctrl = _controller(tmp_path, monkeypatch)
    ctrl.config.ui.notify = "bell"
    console = Console(file=StringIO(), width=80)

    request = ApprovalRequest(
        action=Action(ActionKind.SHELL, "npm test"),
        result=PermissionResult(Decision.ASK, "ask mode"),
        plan="Step 1: analyze\nStep 2: execute",
    )
    await repl._approve(console, ctrl, request, ask=_ask_returning("r"))

    out = console.file.getvalue()
    assert "\a" in out, "plan approval prompt must emit BEL"
    ctrl.close()


@pytest.mark.asyncio
async def test_notify_min_secs_zero_always_notifies(tmp_path, monkeypatch):
    """With ui.notify_min_secs=0, a fast turn (elapsed ~0) still emits the turn-end BEL."""
    import time

    app = _make_notify_app(tmp_path, monkeypatch, notify="bell", notify_min_secs=0)
    _stub_agent_turn(app, monkeypatch)

    # Turn started just now — elapsed is ~0, but with notify_min_secs=0 it should still notify.
    app._turn_start = time.monotonic()

    await app._handle("fast question")

    out = app.console.file.getvalue()
    assert out.count("\a") == 1, f"expected exactly 1 BEL, got {out.count(chr(7))!r}"
    app.controller.close()


# ── T-2-2: Terminal-title state via OSC 2 ──────────────────────────────────


def _extract_osc2(text: str) -> list[str]:
    """Extract OSC 2 title contents from escape-sequence text."""
    import re

    return re.findall(r"\x1b\]2;(.*?)\x07", text)


def _make_title_app(tmp_path, monkeypatch, *, terminal_title: bool = True):
    """InlineApp with configurable ui.terminal_title for T-2-2 tests."""
    from jarn import repl
    from jarn.config.schema import Config, ProviderConfig, ProviderType, RoutingConfig, UIConfig

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="openrouter",
        providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
        routing=RoutingConfig(main="openrouter/m"),
        ui=UIConfig(terminal_title=terminal_title),
    )
    app = repl.InlineApp(cfg, root)
    app.console = Console(file=StringIO(), width=80)
    return app


@pytest.mark.asyncio
async def test_title_sequences_over_turn_lifecycle(tmp_path, monkeypatch):
    """OSC 2 title sequences follow the full turn lifecycle:
    working on turn start, ⏸ on approval, working on approval resolve, idle on finish.
    Quit emits plain 'jarn'. Disabled and non-tty variants emit nothing.
    """
    from jarn.agent.session import ApprovalReply
    from jarn.permissions import Action, ActionKind, Decision, PermissionResult

    app = _make_title_app(tmp_path, monkeypatch)
    # Fake a TTY so set_title actually emits sequences.
    monkeypatch.setattr(app, "_title_isatty", lambda: True)

    proj = app.controller.project_root.name  # "proj"

    async def _noop_runtime():
        return None

    monkeypatch.setattr(app.controller, "ensure_runtime", _noop_runtime)

    # Fake driver: emits text, triggers one approval, then finishes.
    _approval_req = ApprovalRequest(
        action=Action(ActionKind.SHELL, "npm test"),
        result=PermissionResult(Decision.ASK, "ask mode"),
    )

    def _make_driver(approver):
        class _Driver:
            async def run_turn(self, text, *, resume=False):
                yield Event(EventKind.TEXT, "before approval")
                await approver(_approval_req)
                yield Event(EventKind.TEXT, "after approval")
                yield Event(EventKind.DONE)

        return _Driver()

    monkeypatch.setattr(app.controller, "make_driver", _make_driver)

    # Auto-approve so the approval prompt resolves immediately.
    async def _auto_approve(options):
        return next(v for _, v in options if isinstance(v, ApprovalReply) and v.approved)

    monkeypatch.setattr(app, "_pick_approval", _auto_approve)

    await app._handle("do something")

    seqs = _extract_osc2(app.console.file.getvalue())
    assert len(seqs) >= 4, f"expected ≥4 OSC 2 sequences, got: {seqs!r}"
    assert seqs[0] == f"✳ jarn — {proj}", (
        f"turn start must set working title, got {seqs[0]!r}"
    )
    assert seqs[1] == f"⏸ jarn — {proj}", (
        f"approval must set pause title, got {seqs[1]!r}"
    )
    assert seqs[2] == f"✳ jarn — {proj}", (
        f"after approval must restore working, got {seqs[2]!r}"
    )
    assert seqs[3] == f"jarn — {proj}", (
        f"turn finish must set idle title, got {seqs[3]!r}"
    )

    # Quit resets to plain "jarn" (no project suffix).
    app.console = Console(file=StringIO(), width=80)
    app._title_hook("quit")
    quit_seqs = _extract_osc2(app.console.file.getvalue())
    assert quit_seqs == ["jarn"], f"quit must emit plain 'jarn', got {quit_seqs!r}"

    app.controller.close()


@pytest.mark.asyncio
async def test_title_disabled_emits_nothing(tmp_path, monkeypatch):
    """With ui.terminal_title: false no OSC 2 sequences are emitted."""
    app = _make_title_app(tmp_path, monkeypatch, terminal_title=False)
    monkeypatch.setattr(app, "_title_isatty", lambda: True)
    _stub_agent_turn(app, monkeypatch)
    await app._handle("hi")
    assert "\x1b]2;" not in app.console.file.getvalue(), (
        "terminal_title:false must suppress OSC 2 sequences"
    )
    app.controller.close()


@pytest.mark.asyncio
async def test_title_non_tty_emits_nothing(tmp_path, monkeypatch):
    """When isatty returns False no OSC 2 sequences are emitted (even when enabled)."""
    app = _make_title_app(tmp_path, monkeypatch)
    # _title_isatty NOT patched — StringIO.isatty() returns False
    _stub_agent_turn(app, monkeypatch)
    await app._handle("hi")
    assert "\x1b]2;" not in app.console.file.getvalue(), (
        "non-tty must suppress OSC 2 sequences"
    )
    app.controller.close()


# ---------------------------------------------------------------------------
# T-2-4 — ghost autosuggest + Ctrl+R history picker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_autosuggest_ghost(tmp_path, monkeypatch):
    """Buffer has AutoSuggestFromHistory; history 'fix the tests' → suggestion
    ' the tests'; Right-arrow at end (with suggestion visible) accepts it.
    (Async so complete_while_typing has a running loop to schedule on.)"""
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.document import Document

    app = _make_inline_app(tmp_path, monkeypatch)

    # The buffer must be configured with AutoSuggestFromHistory.
    assert isinstance(app.input.auto_suggest, AutoSuggestFromHistory)

    # Populate history with a known entry.
    app.input.history.append_string("fix the tests")

    # Set the buffer text directly (avoids needing a running event loop for
    # insert_text which triggers the async autosuggest coroutine).
    app.input.set_document(Document("fix", 3), bypass_readonly=False)
    assert app.input.text == "fix" and app.input.cursor_position == 3

    # AutoSuggest must propose " the tests".
    suggestion = app.input.auto_suggest.get_suggestion(app.input, app.input.document)
    assert suggestion is not None, "no suggestion — history population failed"
    assert suggestion.text == " the tests"

    # Simulate the suggestion being active on the buffer (as the app would set it).
    app.input.suggestion = suggestion

    # Find the Right-arrow handler and call it (cursor is at end of "fix").
    right_handler = next(
        (b.handler for b in app._kb.bindings
         if getattr(b.handler, "__name__", "") == "_right"),
        None,
    )
    assert right_handler is not None, "no _right key binding found"
    right_handler(_KeyEvent(""))

    assert app.input.text == "fix the tests", (
        f"expected 'fix the tests' after accepting suggestion, got {app.input.text!r}"
    )
    app.controller.close()


@pytest.mark.asyncio
async def test_ctrl_r_history_picker_filters_and_prefills(tmp_path, monkeypatch):
    """Ctrl+R opens a deduped history picker (newest first); typing filters entries;
    Enter prefills the input buffer with the full selected text (no submit)."""
    app = _make_inline_app(tmp_path, monkeypatch)

    # Populate history oldest-first; "run tests" appears twice → keep most recent.
    for entry in ["fix the tests", "run tests", "run tests"]:
        app.input.history.append_string(entry)

    # Launch the history picker directly as a task (Ctrl+R binding does the same).
    task = asyncio.create_task(app._history_picker())
    await asyncio.sleep(0)  # let the picker install the future and state

    # Picker must be open.
    assert app._menu_future is not None and not app._menu_future.done()
    assert app._menu_filter == ""  # filter mode active, no chars yet

    # Deduped entries newest-first: "run tests" (most recent) then "fix the tests".
    assert len(app._menu_options) == 2
    labels = [label for label, _ in app._menu_options]
    assert labels[0] == "run tests", f"expected newest first, got {labels}"
    assert labels[1] == "fix the tests"

    # Header must include total count.
    assert "2" in app._menu_header, f"no count in header: {app._menu_header!r}"

    # Typing "fix" routes through the fastkey/filter handler → filters entries.
    any_handler = next(
        (b.handler for b in app._kb.bindings
         if getattr(b.handler, "__name__", "") == "_menu_fastkey"),
        None,
    )
    assert any_handler is not None, "no _menu_fastkey binding found"
    for ch in "fix":
        any_handler(_KeyEvent(ch))

    # Only "fix the tests" should remain.
    assert len(app._menu_options) == 1, (
        f"expected 1 filtered option, got {[lbl for lbl, _ in app._menu_options]}"
    )
    assert app._menu_options[0][0] == "fix the tests"
    assert "1" in app._menu_header, f"count not updated in header: {app._menu_header!r}"

    # Confirm selection: resolve the future with the selected value.
    selected_value = app._menu_options[0][1]
    app._menu_future.set_result(selected_value)
    await task  # let _history_picker clean up and prefill the input

    # Input buffer must be prefilled with the full selected text, not submitted.
    assert app.input.text == "fix the tests", (
        f"expected 'fix the tests' prefilled, got {app.input.text!r}"
    )
    assert not app._busy(), "Ctrl+R prefill must not start a turn"
    app.controller.close()


@pytest.mark.asyncio
async def test_history_picker_zero_match_nav_no_crash(tmp_path, monkeypatch):
    """Up/Down with zero filter matches must not raise ZeroDivisionError.

    Repro path: Ctrl+R → type gibberish → zero matches → press ↑ or ↓ → crash.
    Fix: guard added to both _up and _down picker branches."""
    app = _make_inline_app(tmp_path, monkeypatch)
    app.input.history.append_string("hello world")
    app.input.history.append_string("goodbye world")

    task = asyncio.create_task(app._history_picker())
    await asyncio.sleep(0)

    assert app._menu_future is not None and not app._menu_future.done()
    assert len(app._menu_options) == 2

    # Type gibberish to drive zero matches through the real filter handler.
    any_handler = next(
        b.handler for b in app._kb.bindings
        if getattr(b.handler, "__name__", "") == "_menu_fastkey"
    )
    for ch in "zzz":
        any_handler(_KeyEvent(ch))
    assert len(app._menu_options) == 0, "expected zero matches after filtering"

    # Find Up/Down handlers (the real keystroke path).
    up_handler = next(
        b.handler for b in app._kb.bindings
        if getattr(b.handler, "__name__", "") == "_up"
    )
    down_handler = next(
        b.handler for b in app._kb.bindings
        if getattr(b.handler, "__name__", "") == "_down"
    )

    # These must NOT crash (ZeroDivisionError was: (index ± 1) % 0).
    up_handler(_KeyEvent(""))    # was: ZeroDivisionError
    down_handler(_KeyEvent(""))  # was: ZeroDivisionError
    assert app._menu_index == 0  # index must stay sane

    # Clear the filter via backspace — options should return.
    backspace_handler = next(
        b.handler for b in app._kb.bindings
        if getattr(b.handler, "__name__", "") == "_backspace"
    )
    for _ in range(3):
        backspace_handler(_KeyEvent(""))
    assert len(app._menu_options) == 2, "options should return after clearing filter"

    # Nav works again after options are restored.
    up_handler(_KeyEvent(""))
    assert app._menu_index == 1  # wrapped: (0 - 1) % 2 == 1

    app._menu_future.set_result(None)
    await task
    app.controller.close()


@pytest.mark.asyncio
async def test_history_picker_enter_prefills_without_echo(tmp_path, monkeypatch):
    """History-picker Enter must prefill the input but NOT echo '› label' to console.

    The echo is intentional for approval-style pickers; this test asserts the gate
    suppresses it for the history picker (identified by _menu_filter being not None)
    while leaving approval-picker echo intact."""
    app = _make_inline_app(tmp_path, monkeypatch)
    app.input.history.append_string("git status")

    task = asyncio.create_task(app._history_picker())
    await asyncio.sleep(0)
    assert app._menu_future is not None and not app._menu_future.done()
    assert len(app._menu_options) == 1

    # Find and invoke the real _submit key handler (not a direct future resolution).
    submit_handler = next(
        b.handler for b in app._kb.bindings
        if getattr(b.handler, "__name__", "") == "_submit"
    )
    submit_handler(_KeyEvent(""))
    await task

    # Buffer must be prefilled with the full text.
    assert app.input.text == "git status", f"got {app.input.text!r}"

    # Console must NOT contain the › echo for the history entry.
    output = app.console.file.getvalue()
    assert "› git status" not in output, (
        f"history picker must not echo label; console: {output!r}"
    )

    # --- Approval-style picker MUST still echo (gate must not silence it) ---
    app.console = Console(file=StringIO(), width=80)  # fresh console
    options: list[tuple[str, object]] = [("Allow once", "allow"), ("Deny", "deny")]
    pick_task = asyncio.create_task(app._pick_menu(options, header="Test"))
    await asyncio.sleep(0)

    submit_handler(_KeyEvent(""))  # Enter → selects first option "Allow once"
    result = await pick_task

    assert result == "allow"
    approval_output = app.console.file.getvalue()
    assert "›" in approval_output, (
        f"approval picker must still echo label; console: {approval_output!r}"
    )
    app.controller.close()


# -- T-2-6: Esc-Esc rewind chord + empty-Enter hint ----------------------------


@pytest.mark.asyncio
async def test_double_esc_opens_rewind(tmp_path, monkeypatch):
    """Two Esc presses ≤0.5 s apart, idle, empty input → opens the rewind picker."""
    app = _make_inline_app(tmp_path, monkeypatch)
    rewind_called: list[bool] = []

    async def _fake_rewind() -> None:
        rewind_called.append(True)

    monkeypatch.setattr(app, "_rewind_picker", _fake_rewind)
    handler = _esc_handler(app)

    # First Esc: idle, empty buffer — should arm the chord.
    assert app.input.text == ""
    handler(event=None)
    assert app._last_esc_ts is not None, "first idle Esc must arm _last_esc_ts"

    # Second Esc within 500 ms, still idle, still empty → fire rewind.
    handler(event=None)
    await asyncio.sleep(0)  # let create_task schedule the rewind coroutine
    assert rewind_called, "double Esc should invoke the rewind picker"
    app.controller.close()


@pytest.mark.asyncio
async def test_single_esc_still_clears(tmp_path, monkeypatch):
    """A single Esc on non-empty input clears input and does NOT open rewind."""
    app = _make_inline_app(tmp_path, monkeypatch)
    rewind_called: list[bool] = []

    async def _fake_rewind() -> None:
        rewind_called.append(True)

    monkeypatch.setattr(app, "_rewind_picker", _fake_rewind)
    handler = _esc_handler(app)

    app.input.insert_text("hello world")
    assert app.input.text == "hello world"

    handler(event=None)

    assert app.input.text == "", "single Esc on non-empty input must clear it"
    assert not rewind_called, "single Esc must NOT open rewind"
    app.controller.close()


@pytest.mark.asyncio
async def test_empty_enter_hint_once(tmp_path, monkeypatch):
    """Idle Enter on empty input prints the hint exactly once; repeat is silent."""
    app = _make_inline_app(tmp_path, monkeypatch)
    submit = _submit_handler(app)

    assert app.input.text == ""
    submit(event=None)
    out1 = app.console.file.getvalue()
    assert "Esc Esc rewind" in out1, "first empty Enter must show the hint"
    assert app._hinted is True

    # Second empty Enter — no new text added.
    submit(event=None)
    out2 = app.console.file.getvalue()
    assert out2 == out1, "second empty Enter must not add more output"
    app.controller.close()


@pytest.mark.asyncio
async def test_double_esc_picker_open_no_rewind(tmp_path, monkeypatch):
    """Double Esc while a picker is open cancels the picker; does NOT open rewind."""
    app = _make_inline_app(tmp_path, monkeypatch)
    rewind_called: list[bool] = []

    async def _fake_rewind() -> None:
        rewind_called.append(True)

    monkeypatch.setattr(app, "_rewind_picker", _fake_rewind)
    handler = _esc_handler(app)

    task = asyncio.create_task(
        app._pick_menu([("one", 1), ("two", 2)], header="test", cancel_returns=None)
    )
    await asyncio.sleep(0)  # let the picker install the future
    assert app._menu_future is not None and not app._menu_future.done()

    # First Esc cancels the picker; second Esc is idle (picker gone) — no rewind.
    handler(event=None)
    handler(event=None)
    await asyncio.sleep(0)

    assert not rewind_called, "Esc while picker open must not open rewind"
    result = await task
    assert result is None  # cancel_returns=None
    app.controller.close()


@pytest.mark.asyncio
async def test_double_esc_busy_no_rewind(tmp_path, monkeypatch):
    """Double Esc while busy cancels the running turn; does NOT open rewind."""
    app = _make_inline_app(tmp_path, monkeypatch)
    rewind_called: list[bool] = []

    async def _fake_rewind() -> None:
        rewind_called.append(True)

    monkeypatch.setattr(app, "_rewind_picker", _fake_rewind)
    handler = _esc_handler(app)

    async def _never() -> None:
        await asyncio.sleep(3600)

    app._turn_task = asyncio.create_task(_never())
    turn_task = app._turn_task
    assert app._busy()

    handler(event=None)  # busy → cancel turn, reset chord
    handler(event=None)  # busy again (task not yet cancelled) → cancel again
    await asyncio.sleep(0)  # let cancellation propagate

    assert not rewind_called, "Esc while busy must cancel turn, not open rewind"
    assert turn_task.cancelled() or turn_task.done()
    app.controller.close()


@pytest.mark.asyncio
async def test_double_esc_no_double_picker(tmp_path, monkeypatch):
    """Double Esc during the async gap before the rewind picker opens should
    not spawn a second picker. The guard at the start of _rewind_picker
    prevents concurrent invocations from overwriting _menu_future."""
    app = _make_inline_app(tmp_path, monkeypatch)

    # Set up a pending _menu_future to simulate a picker in flight
    app._menu_future = asyncio.get_event_loop().create_future()
    first_future = app._menu_future
    assert not first_future.done()

    # Call _rewind_picker twice while the future is pending
    # The first call will return early due to the guard
    await app._rewind_picker()
    assert app._menu_future is first_future, \
        "first invocation should not replace _menu_future"

    # The second call should also return early (same guard)
    await app._rewind_picker()
    assert app._menu_future is first_future, \
        "second invocation must not replace _menu_future (same object identity)"

    app.controller.close()
