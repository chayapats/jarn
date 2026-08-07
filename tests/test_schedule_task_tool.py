"""Agent self-schedule tool (T-SCHED-2 / #42)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

from jarn.agent import builder
from jarn.agent.session import ApprovalReply, EventKind, SessionDriver
from jarn.config.schema import PermissionMode
from jarn.cost import CostTracker
from jarn.gateway.scheduler import Scheduler
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


class _ScheduleAgent:
    """Pass 1 raises a schedule_task interrupt; pass 2 (resume) completes."""

    def __init__(self, args=None):
        self.calls = 0
        self.args = args or {
            "action": "propose",
            "prompt": "daily notes",
            "cron": "0 8 * * *",
            "chat_id": 77,
        }

    async def astream(self, payload, config, stream_mode=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield (
                "updates",
                {
                    "__interrupt__": (
                        _Interrupt(
                            {
                                "action_requests": [
                                    {"action": "schedule_task", "args": self.args}
                                ]
                            }
                        ),
                    )
                },
            )
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
        agent=agent,
        engine=PermissionEngine(mode=mode),
        tracker=CostTracker(),
        thread_id="t",
        main_model_ref="m",
        approver=approver,
    )


@pytest.mark.asyncio
async def test_schedule_task_propose_reaches_approver(isolated_home):
    agent = _ScheduleAgent()
    approver = _capturing_approver(ApprovalReply(True))
    driver = _driver(agent, approver)
    events = [e async for e in driver.run_turn("schedule this")]

    req = approver.captured["req"]
    assert req.action.tool == "schedule_task"
    assert req.args["prompt"] == "daily notes"
    assert agent.calls == 2
    assert any(
        e.kind is EventKind.APPROVAL and "schedule" in e.text and "approved" in e.text
        for e in events
    )


@pytest.mark.asyncio
async def test_schedule_task_decline(isolated_home):
    agent = _ScheduleAgent()
    approver = _capturing_approver(ApprovalReply(False, message="nope"))
    driver = _driver(agent, approver)
    events = [e async for e in driver.run_turn("schedule this")]
    assert agent.calls == 2
    assert any(e.kind is EventKind.APPROVAL and "declined" in e.text for e in events)


@pytest.mark.asyncio
async def test_schedule_task_list_auto_allows(isolated_home):
    agent = _ScheduleAgent(args={"action": "list"})
    approver = _capturing_approver(ApprovalReply(True))
    driver = _driver(agent, approver)
    [e async for e in driver.run_turn("list schedules")]
    assert approver.captured.get("req") is None
    assert agent.calls == 2


def test_schedule_task_tool_registered_and_gated(base_config, tmp_path, isolated_home):
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
    assert "schedule_task" in tool_names
    assert "schedule_task" in (captured.get("interrupt_on") or {})


def test_schedule_task_tool_create_persists(isolated_home, tmp_path):
    from jarn.agent.builtin_tools import _schedule_task_tool

    personal = isolated_home / "personal"
    personal.mkdir(parents=True)
    (personal / ".git").mkdir()
    tool = _schedule_task_tool(default_root=tmp_path)
    result = tool.invoke(
        {
            "action": "create",
            "prompt": "ping",
            "at": "2099-12-01T00:00:00Z",
            "chat_id": 9,
        }
    )
    assert "Scheduled job" in result
    jobs = Scheduler().list_jobs(chat_id=9)
    assert len(jobs) == 1
    assert jobs[0].prompt == "ping"

    listed = tool.invoke({"action": "list", "chat_id": 9})
    assert "ping" in listed
