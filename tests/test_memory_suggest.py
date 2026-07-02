"""Agent-suggested memory with user approval (suggest_memory).

The agent calls ``suggest_memory``; the driver routes it to the approver (not the
engine), which surfaces a "Save this memory?" prompt. On approval the memory is
written through the existing store (respecting global/project tier + trust); on
decline nothing is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from rich.console import Console

from jarn.agent import builder
from jarn.agent.session import (
    ApprovalReply,
    EventKind,
    SessionDriver,
    SuggestedMemory,
)
from jarn.config.schema import PermissionMode
from jarn.cost import CostTracker
from jarn.memory.store import MemoryStore
from jarn.permissions import PermissionEngine

# -- fixtures / fakes --------------------------------------------------------

@dataclass
class _Interrupt:
    value: Any


class _Chunk:
    type = "ai"

    def __init__(self, text):
        self.content = text
        self.usage_metadata = {"input_tokens": 1, "output_tokens": 1}
        self.response_metadata = {}


class _SuggestAgent:
    """Pass 1 raises a suggest_memory interrupt; pass 2 (resume) completes."""

    def __init__(self, args=None):
        self.calls = 0
        self.args = args or {
            "name": "Likes pytest",
            "description": "prefers pytest",
            "body": "The user prefers parametrized pytest tests.",
            "type": "user",
            "scope": "global",
        }

    async def astream(self, payload, config, stream_mode=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield ("updates", {"__interrupt__": (
                _Interrupt({"action_requests": [
                    {"action": "suggest_memory", "args": self.args}
                ]}),
            )})
        else:
            yield ("messages", (_Chunk("done."),))


def _capturing_approver(reply):
    captured: dict[str, Any] = {}

    async def _inner(req):
        captured["req"] = req
        return reply

    _inner.captured = captured  # type: ignore[attr-defined]
    return _inner


def _driver(agent, approver, mode=PermissionMode.AUTO_EDIT):
    return SessionDriver(
        agent=agent, engine=PermissionEngine(mode=mode), tracker=CostTracker(),
        thread_id="t", main_model_ref="m", approver=approver,
    )


# -- driver special-case -----------------------------------------------------

@pytest.mark.asyncio
async def test_suggest_memory_passes_suggestion_to_approver():
    agent = _SuggestAgent()
    approver = _capturing_approver(ApprovalReply(True))
    driver = _driver(agent, approver)
    events = [e async for e in driver.run_turn("remember this")]

    sug = approver.captured["req"].suggested_memory
    assert isinstance(sug, SuggestedMemory)
    assert sug.name == "Likes pytest"
    assert sug.type == "user"
    assert sug.scope == "global"
    assert agent.calls == 2  # approved → tool resumed → second pass ran
    assert any(
        e.kind is EventKind.APPROVAL and "memory saved" in e.text for e in events
    )


@pytest.mark.asyncio
async def test_suggest_memory_decline_keeps_going_without_saving():
    agent = _SuggestAgent()
    approver = _capturing_approver(ApprovalReply(False, message="no thanks"))
    driver = _driver(agent, approver)
    events = [e async for e in driver.run_turn("remember this")]

    assert agent.calls == 2  # resumed with a reject decision
    assert any(
        e.kind is EventKind.APPROVAL and "memory not saved" in e.text for e in events
    )


@pytest.mark.asyncio
async def test_suggest_memory_reaches_approver_in_plan_mode():
    """The engine denies writes in plan mode; suggest_memory must reach the
    approver regardless (the write happens there, gated by trust)."""
    agent = _SuggestAgent()
    approver = _capturing_approver(ApprovalReply(True))
    driver = _driver(agent, approver, mode=PermissionMode.PLAN)
    [e async for e in driver.run_turn("x")]
    assert approver.captured.get("req") is not None


# -- builder wiring ----------------------------------------------------------

def test_suggest_memory_tool_registered_and_gated(base_config, tmp_path):
    captured: dict = {}

    def fake_cda(**kwargs):
        captured.update(kwargs)
        return object()

    fake = GenericFakeChatModel(messages=iter([]))

    with patch("jarn.providers.models.ModelFactory.build", return_value=fake), patch(
        "deepagents.create_deep_agent", side_effect=fake_cda
    ):
        builder.build_runtime(base_config, project_root=tmp_path)

    tool_names = {getattr(t, "name", "") for t in (captured.get("tools") or [])}
    assert "suggest_memory" in tool_names
    assert "suggest_memory" in (captured.get("interrupt_on") or {})


# -- controller store path ---------------------------------------------------

def _controller(tmp_path, monkeypatch, base_config, *, trusted: bool = True):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    from jarn.tui.controller import Controller
    return Controller(base_config, root, project_trusted=trusted)


def test_save_suggested_memory_writes_to_global_store(tmp_path, monkeypatch, base_config):
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    suggestion = SuggestedMemory(
        name="Likes pytest", description="prefers pytest",
        body="parametrized tests", type="user", scope="global",
    )
    saved, message = ctrl.save_suggested_memory(suggestion)
    assert saved
    assert "global" in message
    names = [m.name for m in MemoryStore.global_store().load_all()]
    assert "Likes pytest" in names


def test_save_suggested_memory_writes_to_project_store(tmp_path, monkeypatch, base_config):
    ctrl = _controller(tmp_path, monkeypatch, base_config, trusted=True)
    suggestion = SuggestedMemory(
        name="Test style", description="use pytest",
        body="prefer parametrized", type="project", scope="project",
    )
    saved, _message = ctrl.save_suggested_memory(suggestion)
    assert saved
    store = MemoryStore.project_store(ctrl.project_root)
    assert store is not None
    assert "Test style" in [m.name for m in store.load_all()]


def test_save_suggested_memory_project_refused_when_untrusted(tmp_path, monkeypatch, base_config):
    ctrl = _controller(tmp_path, monkeypatch, base_config, trusted=False)
    suggestion = SuggestedMemory(
        name="Test style", description="use pytest", body="b",
        type="project", scope="project",
    )
    saved, message = ctrl.save_suggested_memory(suggestion)
    assert not saved
    assert "trust" in message.lower()
    store = MemoryStore.project_store(ctrl.project_root)
    assert store is None or store.load_all() == []


def test_save_suggested_memory_rejects_bad_type(tmp_path, monkeypatch, base_config):
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    suggestion = SuggestedMemory(
        name="x", description="y", body="z", type="bogus", scope="global",
    )
    saved, message = ctrl.save_suggested_memory(suggestion)
    assert not saved
    assert "bogus" in message
    assert MemoryStore.global_store().load_all() == []


# -- REPL approval prompt (suggest → approve → store, and decline) -----------

def _approve_controller(tmp_path, monkeypatch, base_config, *, trusted: bool = True):
    return _controller(tmp_path, monkeypatch, base_config, trusted=trusted)


def _ask_returning(answer: str):
    async def _ask(_prompt: str) -> str:
        return answer
    return _ask


def _pick_returning(value):
    async def _pick(_options):
        return value
    return _pick


def _request(scope="global", type="user"):
    from jarn.agent.session import ApprovalRequest
    from jarn.permissions import Action, ActionKind, Decision, PermissionResult

    return ApprovalRequest(
        action=Action(ActionKind.READ, target="memory", tool="suggest_memory"),
        result=PermissionResult(Decision.ASK, "memory suggested"),
        suggested_memory=SuggestedMemory(
            name="Likes pytest", description="prefers pytest",
            body="parametrized tests", type=type, scope=scope,
        ),
    )


@pytest.mark.asyncio
async def test_repl_approve_saves_via_store(tmp_path, monkeypatch, base_config):
    from jarn import repl

    ctrl = _approve_controller(tmp_path, monkeypatch, base_config)
    console = Console(file=StringIO(), force_terminal=True)
    reply = await repl._approve(
        console, ctrl, _request(), ask=_ask_returning("y")
    )
    assert reply.approved
    assert "Likes pytest" in [m.name for m in MemoryStore.global_store().load_all()]


@pytest.mark.asyncio
async def test_repl_decline_writes_nothing(tmp_path, monkeypatch, base_config):
    from jarn import repl

    ctrl = _approve_controller(tmp_path, monkeypatch, base_config)
    console = Console(file=StringIO(), force_terminal=True)
    reply = await repl._approve(
        console, ctrl, _request(), ask=_ask_returning("n")
    )
    assert not reply.approved
    assert MemoryStore.global_store().load_all() == []


@pytest.mark.asyncio
async def test_repl_approve_via_pick_menu_saves(tmp_path, monkeypatch, base_config):
    from jarn import repl

    ctrl = _approve_controller(tmp_path, monkeypatch, base_config)
    console = Console(file=StringIO(), force_terminal=True)
    reply = await repl._approve(
        console, ctrl, _request(), pick=_pick_returning(True)
    )
    assert reply.approved
    assert "Likes pytest" in [m.name for m in MemoryStore.global_store().load_all()]


@pytest.mark.asyncio
async def test_repl_edit_then_save_uses_edited_body(tmp_path, monkeypatch, base_config):
    from jarn import repl

    ctrl = _approve_controller(tmp_path, monkeypatch, base_config)
    console = Console(file=StringIO(), force_terminal=True)

    async def _edit(_req):  # presence of an editor enables the edit option
        return None

    with patch.object(repl.turn, "_edit_text_in_editor", return_value="edited body"):
        reply = await repl._approve(
            console, ctrl, _request(),
            pick=_pick_returning(repl._EDIT_MEMORY), edit=_edit,
        )
    assert reply.approved
    saved = MemoryStore.global_store().get("Likes pytest")
    assert saved is not None and saved.body == "edited body"


@pytest.mark.asyncio
async def test_repl_edit_aborted_writes_nothing(tmp_path, monkeypatch, base_config):
    from jarn import repl

    ctrl = _approve_controller(tmp_path, monkeypatch, base_config)
    console = Console(file=StringIO(), force_terminal=True)

    async def _edit(_req):
        return None

    with patch.object(repl.turn, "_edit_text_in_editor", return_value=None):
        reply = await repl._approve(
            console, ctrl, _request(),
            pick=_pick_returning(repl._EDIT_MEMORY), edit=_edit,
        )
    assert not reply.approved
    assert MemoryStore.global_store().load_all() == []
