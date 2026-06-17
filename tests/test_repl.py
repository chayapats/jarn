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
async def test_run_turn_auto_compacts_over_threshold(tmp_path, monkeypatch):
    """After a turn, _run_turn auto-compacts when the context gauge is over the
    configured threshold (and not otherwise)."""
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

    # Under threshold → no compaction.
    monkeypatch.setattr(ctrl, "should_auto_compact", lambda: False)
    await repl._run_turn(console, ctrl, "hi", _ask_returning(""))
    assert compacted == []

    # Over threshold → compaction fires automatically.
    monkeypatch.setattr(ctrl, "should_auto_compact", lambda: True)
    await repl._run_turn(console, ctrl, "hi", _ask_returning(""))
    assert compacted == [True]
    assert "auto-compact" in console.file.getvalue().lower()
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
    monkeypatch.setattr(repl.subprocess, "run", _fake_editor)
    out = repl._edit_text_in_editor("original\n")
    assert out == "original\n+appended\n"


def test_edit_text_in_editor_abort_returns_none(tmp_path, monkeypatch):
    """A non-zero editor exit (e.g. vim :cq) is treated as an abort → None."""
    from jarn import repl

    def _fake_editor(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1)  # aborted

    monkeypatch.setenv("EDITOR", "fake-editor")
    monkeypatch.setattr(repl.subprocess, "run", _fake_editor)
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
    monkeypatch.setattr(repl, "_edit_text_in_editor", lambda text, **k: result)


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

    app = _make_inline_app(tmp_path, monkeypatch)

    async def _boom(*a, **k):
        raise RuntimeError("langgraph boom")

    monkeypatch.setattr(repl, "_run_turn", _boom)
    await app._handle("please do something")
    out = app.console.file.getvalue()
    assert "langgraph boom" in out                 # concise message still shown
    assert "full traceback" in out and "jarn.log" in out  # pointer to the log
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


def test_thinking_line_estimates_tokens_when_provider_reports_none(tmp_path, monkeypatch):
    """LM Studio / OpenAI-compatible stream without per-chunk usage, so the live
    token counter would sit at 0 — fall back to a ~estimate from streamed text."""
    app = _make_inline_app(tmp_path, monkeypatch)
    app._turn_base_tokens = 0           # tracker stays flat → no live usage
    app._count_stream_chars("x" * 40)   # ~10 tokens (40 // 4)
    assert "~10 tok" in str(app._thinking_line().value)
    app.controller.close()


def test_thinking_line_uses_real_total_when_provider_reports_usage(tmp_path, monkeypatch):
    """When the provider DOES report usage live (e.g. Anthropic), show the real
    running total and don't add the estimate on top (no double-count)."""
    app = _make_inline_app(tmp_path, monkeypatch)
    app._turn_base_tokens = 0
    app.controller.tracker.total.input_tokens = 100
    app.controller.tracker.total.output_tokens = 50   # grew this turn → 150
    app._count_stream_chars("x" * 400)                # estimate must be ignored
    line = str(app._thinking_line().value)
    assert "150 tok" in line and "~150" not in line
    app.controller.close()
