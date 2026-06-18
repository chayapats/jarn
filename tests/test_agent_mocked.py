"""Agent-layer tests with a mocked agent graph.

These exercise the SessionDriver's stream translation and the interrupt →
permission-engine → approval flow deterministically, without any LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest

from jarn.agent.permissions_bridge import interrupt_map, tool_to_action
from jarn.agent.session import (
    ApprovalReply,
    ApprovalRequest,
    EventKind,
    SessionDriver,
)
from jarn.config.schema import PermissionMode
from jarn.cost import CostTracker
from jarn.permissions import ActionKind, PermissionEngine, RememberScope

# -- permission bridge ------------------------------------------------------

def test_tool_to_action_mapping():
    assert tool_to_action("execute", {"command": "ls"}).kind is ActionKind.SHELL
    assert tool_to_action("write_file", {"file_path": "a.py"}).kind is ActionKind.WRITE
    assert tool_to_action("read_file", {"file_path": "a.py"}).kind is ActionKind.READ
    # Unknown / network / MCP tools map to NETWORK so they still get evaluated.
    assert tool_to_action("web_fetch", {"url": "http://x"}).kind is ActionKind.NETWORK
    assert tool_to_action("mcp__srv__act", {}).kind is ActionKind.NETWORK


def test_network_targets_include_url_and_args():
    fetch = tool_to_action("web_fetch", {"url": "https://example.com/doc"})
    assert "https://example.com/doc" in fetch.target
    search = tool_to_action("web_search", {"query": "pytest docs"})
    assert "pytest docs" in search.target
    mcp = tool_to_action("mcp__srv__act", {"path": "/tmp"})
    assert mcp.target.startswith("mcp/srv/act")
    assert "/tmp" in mcp.target


def test_edit_file_hits_danger_guard_even_in_yolo():
    """edit_file to a sensitive path is forced to ASK in YOLO (no bypass)."""
    from pathlib import Path

    engine = PermissionEngine(mode=PermissionMode.YOLO, project_root=Path("/repo"))
    action = tool_to_action("edit_file", {"file_path": "/repo/.git/config"})
    result = engine.evaluate(action)
    assert result.decision.value == "ask" and result.dangerous is True


def test_network_tool_asks_in_ask_mode_and_denied_in_plan():
    engine = PermissionEngine(mode=PermissionMode.ASK)
    assert engine.evaluate(tool_to_action("web_fetch", {"url": "http://x"})).decision.value == "ask"
    engine.mode = PermissionMode.PLAN
    assert engine.evaluate(tool_to_action("web_fetch", {"url": "http://x"})).decision.value == "deny"


def test_readonly_network_auto_allowed_in_auto_edit():
    engine = PermissionEngine(mode=PermissionMode.AUTO_EDIT)
    assert engine.evaluate(tool_to_action("web_fetch", {"url": "http://x"})).decision.value == "allow"
    assert engine.evaluate(tool_to_action("web_search", {"query": "gold"})).decision.value == "allow"
    assert engine.evaluate(tool_to_action("mcp__srv__act", {})).decision.value == "ask"
    assert engine.evaluate(tool_to_action("check_async_task", {})).decision.value == "allow"
    assert engine.evaluate(tool_to_action("start_async_task", {})).decision.value == "ask"


def test_interrupt_map_gates_all_mutating_tools():
    # Every mutating tool is gated in every mode (the engine decides the verdict),
    # so edit_file can never skip the danger-guard.
    assert set(interrupt_map()) == {"write_file", "edit_file", "execute"}


def test_interrupt_map_gates_extra_network_and_mcp_tools():
    m = interrupt_map(["web_search", "web_fetch", "mcp__server__do_thing"])
    assert "edit_file" in m  # mutating tools always present
    assert "web_fetch" in m and "web_search" in m
    assert "mcp__server__do_thing" in m


def test_interrupt_map_excludes_async_tools_by_default():
    # No async subagents configured → the 5 fixed-name async tools (which only
    # exist when AsyncSubAgentMiddleware is installed) are NOT gated.
    from jarn.agent.permissions_bridge import ASYNC_SUBAGENT_TOOLS

    m = interrupt_map()
    assert not any(t in m for t in ASYNC_SUBAGENT_TOOLS)


def test_interrupt_map_gates_async_tools_when_requested():
    from jarn.agent.permissions_bridge import ASYNC_SUBAGENT_TOOLS

    m = interrupt_map(include_async=True)
    assert all(t in m for t in ASYNC_SUBAGENT_TOOLS)
    assert "edit_file" in m  # mutating tools still present
    # Remote HTTP-calling async tools route through the engine as NETWORK.
    assert tool_to_action("start_async_task", {}).kind is ActionKind.NETWORK


# -- fake agent for the driver ---------------------------------------------

@dataclass
class _Interrupt:
    value: Any


class _FakeAIChunk:
    type = "ai"

    def __init__(self, content="", usage=None, model=None):
        self.content = content
        self.usage_metadata = usage
        # Providers stamp the model that produced the message here; the driver
        # attributes usage from it. ``None`` mimics an early chunk with no model.
        self.response_metadata = {"model_name": model} if model else {}


class FakeAgent:
    """Scripts two astream passes: first interrupts, second completes."""

    def __init__(self, command="npm test"):
        self.calls = 0
        self.command = command
        self.resumed_with = None

    async def astream(self, payload, config, stream_mode=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield ("messages", (_FakeAIChunk("Running tests… ", {"input_tokens": 10, "output_tokens": 5}),))
            yield ("updates", {"__interrupt__": (
                _Interrupt({"action_requests": [
                    {"action": "execute", "args": {"command": self.command}}
                ]}),
            )})
        else:
            from langgraph.types import Command
            self.resumed_with = payload if isinstance(payload, Command) else None
            yield ("messages", (_FakeAIChunk("done.", {"input_tokens": 3, "output_tokens": 2}),))


class NamespacedAgent:
    """A single-pass agent that streams chunks under explicit subgraph
    namespaces, mimicking ``astream(subgraphs=True)`` where a delegated subagent
    runs nested under the parent's ``task`` tool. Each scripted item is a
    ``(namespace_tuple, mode, chunk)`` triple."""

    def __init__(self, items):
        self.items = items
        self.calls = 0

    async def astream(self, payload, config, stream_mode=None, **kwargs):
        self.calls += 1
        for item in self.items:
            yield item


def _driver(agent, mode=PermissionMode.ASK, approver=None, **extra):
    engine = PermissionEngine(mode=mode)
    tracker = extra.pop("tracker", None) or CostTracker()
    kwargs = dict(extra)
    if approver is not None:
        kwargs["approver"] = approver
    return SessionDriver(agent=agent, engine=engine, tracker=tracker,
                         thread_id="t", main_model_ref="claude-opus-4-8", **kwargs)


async def _collect(driver, text):
    return [ev async for ev in driver.run_turn(text)]


@pytest.mark.asyncio
async def test_text_streaming_and_usage():
    agent = FakeAgent()
    approved = ApprovalReply(approved=True, scope=RememberScope.ONCE)
    driver = _driver(agent, approver=_async(approved))
    events = await _collect(driver, "test it")
    texts = [e.text for e in events if e.kind is EventKind.TEXT]
    assert "Running tests… " in texts and "done." in texts
    assert driver.tracker.total.input_tokens == 13  # 10 + 3
    assert any(e.kind is EventKind.DONE for e in events)


@pytest.mark.asyncio
async def test_usage_attributed_to_main_by_default():
    """Regression guard: a chunk carrying no model on its response_metadata is
    attributed to the main model exactly as before — no phantom buckets."""
    agent = NamespacedAgent([
        (("tools:abc",), "messages",
         (_FakeAIChunk("hi", {"input_tokens": 7, "output_tokens": 3}),)),
    ])
    driver = _driver(agent)
    await _collect(driver, "go")
    assert driver.tracker.total.input_tokens == 7
    assert set(driver.tracker.per_model) == {"claude-opus-4-8"}


@pytest.mark.asyncio
async def test_usage_attributed_per_model_from_response_metadata():
    """A chunk whose response_metadata reports a different model is billed to that
    model; the main loop's chunk (no model reported) stays on the main model."""
    agent = NamespacedAgent([
        # Main-loop chunk: no model on the message → falls back to main.
        ((), "messages",
         (_FakeAIChunk("delegating", {"input_tokens": 10, "output_tokens": 2}),)),
        # Subagent chunk: the provider reports the haiku model on the message.
        (("tools:xyz",), "messages",
         (_FakeAIChunk("subagent work", {"input_tokens": 100, "output_tokens": 40},
                       model="claude-haiku-4-5"),)),
    ])
    driver = _driver(agent, known_model_refs=("claude-opus-4-8", "claude-haiku-4-5"))
    await _collect(driver, "go")
    per = driver.tracker.per_model
    assert per["claude-opus-4-8"].input_tokens == 10
    assert per["claude-haiku-4-5"].input_tokens == 100
    assert per["claude-haiku-4-5"].output_tokens == 40


@pytest.mark.asyncio
async def test_model_name_canonicalized_to_known_ref():
    """A provider-raw model name is canonicalized to our fuller config ref, so the
    bucket is one pricing-stable key (not split 'haiku' vs 'openrouter/.../haiku')."""
    agent = NamespacedAgent([
        ((), "messages",
         (_FakeAIChunk("x", {"input_tokens": 5, "output_tokens": 1},
                       model="claude-haiku-4-5"),)),
    ])
    driver = _driver(agent, known_model_refs=("openrouter/anthropic/claude-haiku-4-5",))
    await _collect(driver, "go")
    assert set(driver.tracker.per_model) == {"openrouter/anthropic/claude-haiku-4-5"}


@pytest.mark.asyncio
async def test_mid_turn_budget_aborts_cleanly():
    """When a streamed chunk pushes spend over a hard-stop budget, the turn ends
    with a clean (non-retryable) ERROR rather than continuing to stream."""
    from jarn.config.schema import BudgetConfig

    # $0.01 hard-stop; the first chunk's 1M input tokens far exceeds it.
    tracker = CostTracker(budget=BudgetConfig(per_session_usd=0.01, hard_stop=True))
    second_seen = {"n": 0}

    def _mk(content, usage):
        return (("",), "messages", (_FakeAIChunk(content, usage),))

    class _Agent:
        async def astream(self, payload, config, stream_mode=None, **kwargs):
            yield _mk("over budget now", {"input_tokens": 1_000_000, "output_tokens": 0})
            second_seen["n"] += 1
            yield _mk("should not stream", {"input_tokens": 1, "output_tokens": 1})

    driver = _driver(_Agent(), tracker=tracker)
    events = await _collect(driver, "go")
    errs = [e for e in events if e.kind is EventKind.ERROR]
    assert errs and errs[0].data.get("budget") is True
    assert errs[0].data.get("retryable") is False
    # The turn aborted before recording the second chunk's usage.
    assert second_seen["n"] == 0
    assert tracker.per_model["claude-opus-4-8"].calls == 1
    assert not any(e.kind is EventKind.DONE for e in events)


@pytest.mark.asyncio
async def test_ask_triggers_approver_and_resumes_approve():
    agent = FakeAgent(command="npm test")
    seen = {}

    async def approver(req: ApprovalRequest) -> ApprovalReply:
        seen["action"] = req.action
        return ApprovalReply(approved=True, scope=RememberScope.SESSION)

    driver = _driver(agent, approver=approver)
    await _collect(driver, "run")
    assert seen["action"].target == "npm test"
    assert agent.resumed_with is not None
    assert agent.resumed_with.resume["decisions"] == [{"type": "approve"}]


@pytest.mark.asyncio
async def test_reject_resumes_with_reject():
    agent = FakeAgent(command="npm test")

    async def approver(req):
        return ApprovalReply(approved=False, message="no thanks")

    driver = _driver(agent, approver=approver)
    await _collect(driver, "run")
    assert agent.resumed_with.resume["decisions"][0]["type"] == "reject"


class WriteAgent:
    """Scripts a single ``write_file`` interrupt, then completes on resume."""

    def __init__(self, args):
        self.calls = 0
        self.args = args
        self.resumed_with = None

    async def astream(self, payload, config, stream_mode=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield ("updates", {"__interrupt__": (
                _Interrupt({"action_requests": [
                    {"action": "write_file", "args": self.args}
                ]}),
            )})
        else:
            from langgraph.types import Command
            self.resumed_with = payload if isinstance(payload, Command) else None
            yield ("messages", (_FakeAIChunk("done.", {"input_tokens": 1, "output_tokens": 1}),))


@pytest.mark.asyncio
async def test_edit_before_apply_resumes_with_edit_decision():
    """An approve carrying ``edited_args`` resumes with a LangGraph ``edit``
    decision whose ``edited_action`` carries the user's edited content — so the
    edited content (not the agent's original) is what the tool runs with."""
    agent = WriteAgent({"file_path": "a.txt", "content": "agent original\n"})

    async def approver(req: ApprovalRequest) -> ApprovalReply:
        return ApprovalReply(
            approved=True, scope=RememberScope.ONCE,
            edited_args={"file_path": "a.txt", "content": "user edited\n"},
        )

    driver = _driver(agent, approver=approver)
    await _collect(driver, "write it")
    decision = agent.resumed_with.resume["decisions"][0]
    assert decision["type"] == "edit"
    assert decision["edited_action"]["name"] == "write_file"
    assert decision["edited_action"]["args"]["content"] == "user edited\n"


@pytest.mark.asyncio
async def test_approve_without_edit_resumes_with_plain_approve():
    """An ordinary approve (no ``edited_args``) still resumes with a plain
    ``approve`` decision — the edit path is opt-in."""
    agent = WriteAgent({"file_path": "a.txt", "content": "x\n"})

    async def approver(req: ApprovalRequest) -> ApprovalReply:
        return ApprovalReply(approved=True, scope=RememberScope.ONCE)

    driver = _driver(agent, approver=approver)
    await _collect(driver, "write it")
    assert agent.resumed_with.resume["decisions"] == [{"type": "approve"}]


class AsyncTaskAgent:
    """Scripts an interrupt for a ``start_async_task`` tool call (the remote
    async-subagent launcher), then completes on resume."""

    def __init__(self):
        self.calls = 0
        self.resumed_with = None

    async def astream(self, payload, config, stream_mode=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield ("updates", {"__interrupt__": (
                _Interrupt({"action_requests": [
                    {"action": "start_async_task",
                     "args": {"subagent_type": "researcher", "description": "go"}}
                ]}),
            )})
        else:
            from langgraph.types import Command
            self.resumed_with = payload if isinstance(payload, Command) else None
            yield ("messages", (_FakeAIChunk("done.", {"input_tokens": 1, "output_tokens": 1}),))


@pytest.mark.asyncio
async def test_async_subagent_tool_intercepted_as_network():
    """A start_async_task call is gated: the engine sees it as NETWORK and the
    approver is consulted (default policy → ASK in ASK mode)."""
    agent = AsyncTaskAgent()
    seen = {}

    async def approver(req: ApprovalRequest) -> ApprovalReply:
        seen["kind"] = req.action.kind
        seen["tool"] = req.action.tool
        return ApprovalReply(approved=True)

    driver = _driver(agent, approver=approver)
    await _collect(driver, "launch")
    assert seen["kind"] is ActionKind.NETWORK
    assert seen["tool"] == "start_async_task"
    assert agent.resumed_with.resume["decisions"][0]["type"] == "approve"


@pytest.mark.asyncio
async def test_dangerous_command_forces_ask_even_in_yolo():
    agent = FakeAgent(command="git push --force")
    asked = {"n": 0}

    async def approver(req):
        asked["n"] += 1
        assert req.result.dangerous is True
        return ApprovalReply(approved=False)

    driver = _driver(agent, mode=PermissionMode.YOLO, approver=approver)
    await _collect(driver, "deploy")
    assert asked["n"] == 1  # YOLO still asked because of danger-guard


@pytest.mark.asyncio
async def test_safe_command_auto_allowed_in_yolo():
    agent = FakeAgent(command="ls -la")
    called = {"n": 0}

    async def approver(req):
        called["n"] += 1
        return ApprovalReply(approved=True)

    driver = _driver(agent, mode=PermissionMode.YOLO, approver=approver)
    await _collect(driver, "list")
    assert called["n"] == 0  # auto-allowed, approver never invoked
    assert agent.resumed_with.resume["decisions"][0]["type"] == "approve"


def test_tool_results_summarized_not_dumped():
    """A fetched web page (ToolMessage) is summarized, never dumped as text."""
    from jarn.agent.session import EventKind

    class _Msg:
        def __init__(self, type_, content, name=""):
            self.type = type_
            self.content = content
            self.name = name
            self.usage_metadata = None

    driver = _driver(FakeAgent())
    huge = "<huge web page text>\n" * 500
    ev = driver._handle_message_chunk((_Msg("tool", huge, name="web_fetch"),))
    assert ev is not None and ev.kind is EventKind.TOOL_END
    assert ev.text == "web_fetch"
    # The full payload is never surfaced — only a compact line-count summary.
    assert ev.data["summary"] == "500 lines"
    assert huge not in ev.data["summary"]
    assert driver._handle_message_chunk((_Msg("human", "echo"),)) is None
    ev = driver._handle_message_chunk((_Msg("ai", "Here is the answer."),))
    assert ev is not None and ev.text == "Here is the answer."


def test_reasoning_surfaced_as_dim_event():
    """Extended-reasoning text is surfaced as a REASONING event (not TEXT)."""
    from jarn.agent.session import EventKind

    class _Msg:
        def __init__(self, type_, content, additional_kwargs=None):
            self.type = type_
            self.content = content
            self.additional_kwargs = additional_kwargs or {}
            self.usage_metadata = None

    driver = _driver(FakeAgent())
    # DeepSeek-style: reasoning in additional_kwargs, no visible text yet.
    ev = driver._handle_message_chunk((_Msg("ai", "", {"reasoning_content": "let me think"}),))
    assert ev is not None and ev.kind is EventKind.REASONING and ev.text == "let me think"
    # Anthropic-style: a thinking content block.
    ev2 = driver._handle_message_chunk((_Msg("ai", [{"type": "thinking", "thinking": "hmm"}]),))
    assert ev2.kind is EventKind.REASONING and ev2.text == "hmm"
    # Visible answer text always wins over reasoning.
    ev3 = driver._handle_message_chunk((_Msg("ai", "answer", {"reasoning_content": "x"}),))
    assert ev3.kind is EventKind.TEXT and ev3.text == "answer"


# -- build_runtime assembly -------------------------------------------------

def test_build_runtime_assembles(base_config, tmp_path):
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from jarn.agent.builder import build_runtime

    fake = GenericFakeChatModel(messages=iter([]))
    with patch("jarn.providers.models.ModelFactory.build", return_value=fake):
        rt = build_runtime(base_config, project_root=tmp_path)
    assert type(rt.agent).__name__ == "CompiledStateGraph"
    assert "reliable" in rt.system_prompt.lower()
    assert rt.main_model_ref == "openrouter/anthropic/claude-opus-4-8"


def _async(value):
    async def _inner(_req):
        return value
    return _inner


# -- lifecycle hooks --------------------------------------------------------

@pytest.mark.asyncio
async def test_blocking_pre_tool_hook_rejects_call(tmp_path):
    """A failing blocking pre_tool hook rejects the tool before it runs, and the
    approver is never consulted — even in YOLO with an otherwise-safe command."""
    from jarn.config.schema import HookSpec
    from jarn.extensibility.hooks import HookRunner

    agent = FakeAgent(command="ls -la")  # safe → would auto-allow in YOLO
    runner = HookRunner(
        hooks=[HookSpec(event="pre_tool", command="echo nope >&2; exit 3", blocking=True)],
        cwd=tmp_path,
    )
    called = {"n": 0}

    async def approver(req):
        called["n"] += 1
        return ApprovalReply(approved=True)

    driver = SessionDriver(
        agent=agent, engine=PermissionEngine(mode=PermissionMode.YOLO),
        tracker=CostTracker(), thread_id="t", approver=approver, hooks=runner,
    )
    events = [ev async for ev in driver.run_turn("go")]

    assert agent.resumed_with.resume["decisions"][0]["type"] == "reject"
    assert called["n"] == 0  # hook aborted before any approval
    assert any(
        e.kind is EventKind.APPROVAL and "blocked by hook" in e.text for e in events
    )


@pytest.mark.asyncio
async def test_non_blocking_pre_tool_hook_does_not_reject(tmp_path):
    from jarn.config.schema import HookSpec
    from jarn.extensibility.hooks import HookRunner

    agent = FakeAgent(command="ls -la")
    runner = HookRunner(
        hooks=[HookSpec(event="pre_tool", command="exit 1", blocking=False)],
        cwd=tmp_path,
    )
    driver = SessionDriver(
        agent=agent, engine=PermissionEngine(mode=PermissionMode.YOLO),
        tracker=CostTracker(), thread_id="t", hooks=runner,
    )
    await _collect(driver, "go")
    # Non-blocking hook failure must not abort: the safe command auto-allows.
    assert agent.resumed_with.resume["decisions"][0]["type"] == "approve"


@pytest.mark.asyncio
async def test_run_turn_tags_retryable_error_from_astream():
    """The real run_turn except-block emits an ERROR event tagged retryable."""
    class _BoomAgent:
        async def astream(self, payload, config, **kw):
            raise RuntimeError("429 rate limit exceeded")
            yield  # unreachable; makes this an async generator

    driver = SessionDriver(agent=_BoomAgent(), engine=PermissionEngine(),
                           tracker=CostTracker(), thread_id="t")
    events = [e async for e in driver.run_turn("hi")]
    errs = [e for e in events if e.kind is EventKind.ERROR]
    assert errs and errs[0].data.get("retryable") is True


@pytest.mark.asyncio
async def test_run_turn_tags_non_retryable_error_from_astream():
    class _BoomAgent:
        async def astream(self, payload, config, **kw):
            raise ValueError("unknown model ref")
            yield

    driver = SessionDriver(agent=_BoomAgent(), engine=PermissionEngine(),
                           tracker=CostTracker(), thread_id="t")
    events = [e async for e in driver.run_turn("hi")]
    errs = [e for e in events if e.kind is EventKind.ERROR]
    assert errs and errs[0].data.get("retryable") is False


def test_is_retryable_error_classifies():
    from jarn.agent.session import _is_retryable_error

    assert _is_retryable_error(Exception("Rate limit exceeded (429)"))
    assert _is_retryable_error(TimeoutError("request timed out"))
    assert _is_retryable_error(Exception("upstream connection reset"))
    assert not _is_retryable_error(ValueError("unknown model ref"))

    class _Http(Exception):
        status_code = 503

    class _Bad(Exception):
        status_code = 400

    assert _is_retryable_error(_Http("server error"))
    assert not _is_retryable_error(_Bad("bad request"))


def test_is_auth_error_classifies():
    from jarn.agent.session import _is_auth_error, _is_retryable_error

    # Raw SDK string from a rejected Anthropic key (the reported symptom).
    raw = ("Error code: 401 - {'type': 'error', 'error': {'type': "
           "'authentication_error', 'message': 'invalid x-api-key'}}")
    assert _is_auth_error(Exception(raw))
    # An auth rejection must NOT be classified as retryable (no model rotation).
    assert not _is_retryable_error(Exception(raw))

    assert _is_auth_error(Exception("401 Unauthorized"))
    assert _is_auth_error(Exception("Incorrect API key provided"))

    class _AuthErr(Exception):
        status_code = 401

    assert _is_auth_error(_AuthErr("nope"))
    # Unrelated/transient errors are not auth errors.
    assert not _is_auth_error(Exception("Rate limit exceeded (429)"))
    assert not _is_auth_error(ValueError("unknown model ref"))


@pytest.mark.asyncio
async def test_run_turn_tags_auth_error_with_provider():
    """A 401/auth failure on the first astream call is tagged auth=True (and NOT
    retryable) with the provider profile derived from the main model ref."""
    class _BoomAgent:
        async def astream(self, payload, config, **kw):
            raise RuntimeError(
                "Error code: 401 - {'error': {'message': 'invalid x-api-key'}}"
            )
            yield  # unreachable; makes this an async generator

    driver = SessionDriver(agent=_BoomAgent(), engine=PermissionEngine(),
                           tracker=CostTracker(), thread_id="t",
                           main_model_ref="anthropic/claude-opus-4-8")
    events = [e async for e in driver.run_turn("hi")]
    errs = [e for e in events if e.kind is EventKind.ERROR]
    assert errs and errs[0].data.get("auth") is True
    assert errs[0].data.get("retryable") is False
    assert errs[0].data.get("provider") == "anthropic"


@pytest.mark.asyncio
async def test_post_edit_hook_emits_notice_on_failure(tmp_path):
    """post_edit hook failure surfaces as a NOTICE (report-only, tool already ran)."""
    from jarn.config.schema import HookSpec
    from jarn.extensibility.hooks import HookRunner

    runner = HookRunner(
        hooks=[HookSpec(event="post_edit", command="echo lint failed >&2; exit 1")],
        cwd=tmp_path,
    )
    driver = SessionDriver(
        agent=FakeAgent(), engine=PermissionEngine(), tracker=CostTracker(),
        thread_id="t", hooks=runner,
    )
    notices = [n async for n in driver._run_post_hooks("edit_file")]
    assert notices and notices[0].kind is EventKind.NOTICE
    assert "post_edit hook failed" in notices[0].text


@pytest.mark.asyncio
async def test_post_edit_hook_matcher_scopes_by_file_path(tmp_path):
    """A post_edit matcher glob scopes by the edited file path, not the tool name."""
    from jarn.config.schema import HookSpec
    from jarn.extensibility.hooks import HookRunner

    runner = HookRunner(
        hooks=[HookSpec(event="post_edit", command="exit 1", matcher="*.py")],
        cwd=tmp_path,
    )
    driver = SessionDriver(
        agent=FakeAgent(), engine=PermissionEngine(), tracker=CostTracker(),
        thread_id="t", hooks=runner,
    )
    driver._last_edit_target = "src/app.py"
    assert [n async for n in driver._run_post_hooks("edit_file")]  # *.py matches → fires
    driver._last_edit_target = "README.md"
    assert not [n async for n in driver._run_post_hooks("edit_file")]  # no match → silent


def test_looks_like_git_commit():
    from jarn.agent.session import _looks_like_git_commit

    assert _looks_like_git_commit("git commit -m x")
    assert _looks_like_git_commit("git -C /repo commit")
    assert _looks_like_git_commit("git -c user.name=x commit")
    assert not _looks_like_git_commit('echo "git commit"')   # quoted string
    assert not _looks_like_git_commit("git log --grep='git commit'")  # other subcommand
    assert not _looks_like_git_commit("cat git-commit.txt")
