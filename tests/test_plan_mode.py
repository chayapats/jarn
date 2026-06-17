"""F4: plan-mode handoff — exit_plan_mode tool, driver special-case, config.

The driver must route exit_plan_mode to the approver (not the engine, which
would deny it in plan mode), pass the plan text, and resume approve/reject.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

from jarn.agent.session import ApprovalReply, EventKind, SessionDriver
from jarn.config.loader import ConfigError, load_config
from jarn.config.schema import PermissionMode, PlanConfig
from jarn.cost import CostTracker
from jarn.permissions import PermissionEngine


@dataclass
class _Interrupt:
    value: Any


class _Chunk:
    type = "ai"

    def __init__(self, text):
        self.content = text
        self.usage_metadata = {"input_tokens": 1, "output_tokens": 1}
        self.response_metadata = {}


class _PlanAgent:
    """Pass 1 raises an exit_plan_mode interrupt; pass 2 (resume) completes."""

    def __init__(self, plan="1. refactor\n2. test"):
        self.calls = 0
        self.plan = plan
        self.resumed_with = None

    async def astream(self, payload, config, stream_mode=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield ("updates", {"__interrupt__": (
                _Interrupt({"action_requests": [
                    {"action": "exit_plan_mode", "args": {"plan": self.plan}}
                ]}),
            )})
        else:
            from langgraph.types import Command

            self.resumed_with = payload if isinstance(payload, Command) else None
            yield ("messages", (_Chunk("done."),))


def _capturing_approver(reply):
    captured: dict[str, Any] = {}

    async def _inner(req):
        captured["req"] = req
        return reply

    _inner.captured = captured  # type: ignore[attr-defined]
    return _inner


def _driver(agent, approver, mode=PermissionMode.PLAN):
    return SessionDriver(
        agent=agent, engine=PermissionEngine(mode=mode), tracker=CostTracker(),
        thread_id="t", main_model_ref="m", approver=approver,
    )


# -- driver special-case -----------------------------------------------------

@pytest.mark.asyncio
async def test_exit_plan_mode_passes_plan_and_resumes_on_approve():
    agent = _PlanAgent(plan="1. refactor\n2. test")
    approver = _capturing_approver(ApprovalReply(True, plan_mode_target="auto-edit"))
    driver = _driver(agent, approver)
    events = [e async for e in driver.run_turn("do it")]

    assert approver.captured["req"].plan == "1. refactor\n2. test"
    assert agent.calls == 2  # approved → tool resumed → second pass ran
    assert any(
        e.kind is EventKind.APPROVAL and "approved" in e.text for e in events
    )


@pytest.mark.asyncio
async def test_exit_plan_mode_reject_keeps_planning():
    agent = _PlanAgent()
    approver = _capturing_approver(ApprovalReply(False, message="not yet"))
    driver = _driver(agent, approver)
    events = [e async for e in driver.run_turn("do it")]

    assert agent.calls == 2  # resumed with a reject decision
    assert any(
        e.kind is EventKind.APPROVAL and "still planning" in e.text for e in events
    )


@pytest.mark.asyncio
async def test_exit_plan_mode_reaches_approver_even_in_plan_mode():
    """The engine denies writes in plan mode; exit_plan_mode must NOT be denied —
    it has to reach the approver so the user can act on the plan."""
    agent = _PlanAgent()
    approver = _capturing_approver(ApprovalReply(True, plan_mode_target="auto-edit"))
    driver = _driver(agent, approver, mode=PermissionMode.PLAN)
    [e async for e in driver.run_turn("x")]
    assert approver.captured.get("req") is not None


# -- builder wiring ----------------------------------------------------------

def test_exit_plan_mode_tool_registered_and_gated(base_config, tmp_path):
    captured: dict = {}

    def fake_cda(**kwargs):
        captured.update(kwargs)
        return object()

    fake = GenericFakeChatModel(messages=iter([]))
    from jarn.agent import builder

    with patch("jarn.providers.models.ModelFactory.build", return_value=fake), patch(
        "deepagents.create_deep_agent", side_effect=fake_cda
    ):
        builder.build_runtime(base_config, project_root=tmp_path)

    tool_names = {getattr(t, "name", "") for t in (captured.get("tools") or [])}
    assert "exit_plan_mode" in tool_names
    assert "exit_plan_mode" in (captured.get("interrupt_on") or {})


# -- config ------------------------------------------------------------------

def test_plan_config_default():
    assert PlanConfig().exit_mode == "auto-edit"


def test_loader_plan_exit_mode(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("plan:\n  exit_mode: ask\n")
    cfg = load_config(global_path=p, project_path=None)
    assert cfg.plan.exit_mode == "ask"


def test_loader_plan_exit_mode_invalid(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("plan:\n  exit_mode: yolo\n")
    with pytest.raises(ConfigError):
        load_config(global_path=p, project_path=None)
