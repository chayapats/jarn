"""Agent-suggested skills with user approval (suggest_skill).

The agent calls ``suggest_skill``; the driver routes it to the approver (not the
engine), which surfaces a "Save this skill?" prompt. On approval the skill is
written under ``<root>/.jarn/skills/<name>/SKILL.md``; on decline nothing is
written. The tool itself never writes.
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
    SuggestedSkill,
)
from jarn.config.schema import PermissionMode
from jarn.cost import CostTracker
from jarn.extensibility.skills import load_skills
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
    """Pass 1 raises a suggest_skill interrupt; pass 2 (resume) completes."""

    def __init__(self, args=None):
        self.calls = 0
        self.args = args or {
            "name": "run-migrations",
            "description": "Apply DB migrations safely",
            "body": "1. Check status\n2. Apply\n3. Verify",
            "trigger": "manual",
        }

    async def astream(self, payload, config, stream_mode=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield ("updates", {"__interrupt__": (
                _Interrupt({"action_requests": [
                    {"action": "suggest_skill", "args": self.args}
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
async def test_suggest_skill_passes_suggestion_to_approver():
    agent = _SuggestAgent()
    approver = _capturing_approver(ApprovalReply(True))
    driver = _driver(agent, approver)
    events = [e async for e in driver.run_turn("save this skill")]

    sug = approver.captured["req"].suggested_skill
    assert isinstance(sug, SuggestedSkill)
    assert sug.name == "run-migrations"
    assert sug.trigger == "manual"
    assert agent.calls == 2  # approved → tool resumed → second pass ran
    assert any(
        e.kind is EventKind.APPROVAL and "skill saved" in e.text for e in events
    )


@pytest.mark.asyncio
async def test_suggest_skill_decline_keeps_going_without_saving():
    agent = _SuggestAgent()
    approver = _capturing_approver(ApprovalReply(False, message="no thanks"))
    driver = _driver(agent, approver)
    events = [e async for e in driver.run_turn("save this skill")]

    assert agent.calls == 2  # resumed with a reject decision
    assert any(
        e.kind is EventKind.APPROVAL and "skill not saved" in e.text for e in events
    )


@pytest.mark.asyncio
async def test_suggest_skill_reaches_approver_in_plan_mode():
    """The engine denies writes in plan mode; suggest_skill must reach the
    approver regardless (the write happens there, gated by trust)."""
    agent = _SuggestAgent()
    approver = _capturing_approver(ApprovalReply(True))
    driver = _driver(agent, approver, mode=PermissionMode.PLAN)
    [e async for e in driver.run_turn("x")]
    assert approver.captured.get("req") is not None


# -- builder wiring ----------------------------------------------------------

def test_suggest_skill_tool_registered_and_gated(base_config, tmp_path):
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
    assert "suggest_skill" in tool_names
    assert "suggest_skill" in (captured.get("interrupt_on") or {})


# -- controller write path ---------------------------------------------------

def _controller(tmp_path, monkeypatch, base_config, *, trusted: bool = True):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    from jarn.tui.controller import Controller
    return Controller(base_config, root, project_trusted=trusted)


def test_save_suggested_skill_writes_nested_layout(tmp_path, monkeypatch, base_config):
    ctrl = _controller(tmp_path, monkeypatch, base_config, trusted=True)
    suggestion = SuggestedSkill(
        name="run-migrations",
        description="Apply DB migrations safely",
        body="1. Check status\n2. Apply\n3. Verify",
        trigger="manual",
    )
    saved, message = ctrl.save_suggested_skill(suggestion)
    assert saved
    assert "run-migrations" in message
    path = ctrl.project_root / ".jarn" / "skills" / "run-migrations" / "SKILL.md"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "name: run-migrations" in text
    assert "trigger: manual" in text
    assert "Apply DB migrations safely" in text
    skills = load_skills(ctrl.project_root, project_trusted=True)
    assert "run-migrations" in skills
    assert skills["run-migrations"].path == path
    assert skills["run-migrations"].trigger == "manual"


def test_save_suggested_skill_refused_when_untrusted(tmp_path, monkeypatch, base_config):
    ctrl = _controller(tmp_path, monkeypatch, base_config, trusted=False)
    suggestion = SuggestedSkill(
        name="hostile", description="nope", body="inject", trigger="auto",
    )
    saved, message = ctrl.save_suggested_skill(suggestion)
    assert not saved
    assert "trust" in message.lower()
    skills_dir = ctrl.project_root / ".jarn" / "skills"
    assert not skills_dir.exists() or not any(skills_dir.rglob("SKILL.md"))


def test_save_suggested_skill_rejects_path_separators(
    tmp_path, monkeypatch, base_config
):
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    suggestion = SuggestedSkill(
        name="../escape", description="x", body="y", trigger="auto",
    )
    saved, message = ctrl.save_suggested_skill(suggestion)
    assert not saved
    assert "path" in message.lower()


def test_save_suggested_skill_requires_project_root(
    tmp_path, monkeypatch, base_config
):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    from jarn.tui.controller import Controller
    ctrl = Controller(base_config, None, project_trusted=True)
    suggestion = SuggestedSkill(
        name="orphan", description="x", body="y", trigger="auto",
    )
    saved, message = ctrl.save_suggested_skill(suggestion)
    assert not saved
    assert "project root" in message.lower()


# -- REPL approval prompt ----------------------------------------------------

def _ask_returning(answer: str):
    async def _ask(_prompt: str) -> str:
        return answer
    return _ask


def _pick_returning(value):
    async def _pick(_options):
        return value
    return _pick


def _request(trigger="auto"):
    from jarn.agent.session import ApprovalRequest
    from jarn.permissions import Action, ActionKind, Decision, PermissionResult

    return ApprovalRequest(
        action=Action(ActionKind.READ, target="skill", tool="suggest_skill"),
        result=PermissionResult(Decision.ASK, "skill suggested"),
        suggested_skill=SuggestedSkill(
            name="run-migrations",
            description="Apply DB migrations safely",
            body="1. Check\n2. Apply",
            trigger=trigger,
        ),
    )


@pytest.mark.asyncio
async def test_repl_approve_saves_nested_skill(tmp_path, monkeypatch, base_config):
    from jarn import repl

    ctrl = _controller(tmp_path, monkeypatch, base_config)
    console = Console(file=StringIO(), force_terminal=True)
    reply = await repl._approve(
        console, ctrl, _request(), ask=_ask_returning("y")
    )
    assert reply.approved
    path = ctrl.project_root / ".jarn" / "skills" / "run-migrations" / "SKILL.md"
    assert path.is_file()


@pytest.mark.asyncio
async def test_repl_decline_writes_nothing(tmp_path, monkeypatch, base_config):
    from jarn import repl

    ctrl = _controller(tmp_path, monkeypatch, base_config)
    console = Console(file=StringIO(), force_terminal=True)
    reply = await repl._approve(
        console, ctrl, _request(), ask=_ask_returning("n")
    )
    assert not reply.approved
    skills_dir = ctrl.project_root / ".jarn" / "skills"
    assert not skills_dir.exists() or not any(skills_dir.rglob("SKILL.md"))


@pytest.mark.asyncio
async def test_repl_approve_via_pick_menu_saves(tmp_path, monkeypatch, base_config):
    from jarn import repl

    ctrl = _controller(tmp_path, monkeypatch, base_config)
    console = Console(file=StringIO(), force_terminal=True)
    reply = await repl._approve(
        console, ctrl, _request(), pick=_pick_returning(True)
    )
    assert reply.approved
    assert (ctrl.project_root / ".jarn" / "skills" / "run-migrations" / "SKILL.md").is_file()


@pytest.mark.asyncio
async def test_repl_edit_then_save_uses_edited_body(tmp_path, monkeypatch, base_config):
    from jarn import repl

    ctrl = _controller(tmp_path, monkeypatch, base_config)
    console = Console(file=StringIO(), force_terminal=True)

    async def _edit(_req):
        return None

    with patch.object(repl.turn, "_edit_text_in_editor", return_value="edited body"):
        reply = await repl._approve(
            console, ctrl, _request(),
            pick=_pick_returning(repl._EDIT_SKILL), edit=_edit,
        )
    assert reply.approved
    text = (
        ctrl.project_root / ".jarn" / "skills" / "run-migrations" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "edited body" in text


@pytest.mark.asyncio
async def test_repl_edit_aborted_writes_nothing(tmp_path, monkeypatch, base_config):
    from jarn import repl

    ctrl = _controller(tmp_path, monkeypatch, base_config)
    console = Console(file=StringIO(), force_terminal=True)

    async def _edit(_req):
        return None

    with patch.object(repl.turn, "_edit_text_in_editor", return_value=None):
        reply = await repl._approve(
            console, ctrl, _request(),
            pick=_pick_returning(repl._EDIT_SKILL), edit=_edit,
        )
    assert not reply.approved
    skills_dir = ctrl.project_root / ".jarn" / "skills"
    assert not skills_dir.exists() or not any(skills_dir.rglob("SKILL.md"))
