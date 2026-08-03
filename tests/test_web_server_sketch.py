"""Regression tests for the future web worker sketch."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from jarn.agent.session import (
    ApprovalRequest,
    Event,
    EventKind,
    SuggestedMemory,
)
from jarn.permissions import Action, ActionKind, Decision, PermissionResult


def _load_sketch():
    path = Path(__file__).parents[1] / "web" / "server_sketch.py"
    spec = importlib.util.spec_from_file_location("_jarn_web_server_sketch", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_web_approver_round_trips_every_contract_field():
    run_turn_over_websocket = _load_sketch().run_turn_over_websocket

    request = ApprovalRequest(
        action=Action(ActionKind.WRITE, "notes.txt"),
        result=PermissionResult(Decision.ASK, "write needs approval", dangerous=True),
        description="Update the notes",
        args={"file_path": "notes.txt", "content": "draft"},
        plan="1. Update notes",
        suggested_memory=SuggestedMemory(
            name="preference",
            description="Writing preference",
            body="Keep notes short",
            type="project",
            scope="global",
        ),
    )
    captured = {}

    class _Driver:
        async def run_turn(self, text):
            captured["reply"] = await captured["approver"](request)
            yield Event(EventKind.DONE)

    class _Controller:
        async def ensure_runtime(self):
            return None

        def make_driver(self, approver):
            captured["approver"] = approver
            return _Driver()

        @property
        def status_line(self):
            return "ready"

    class _WebSocket:
        def __init__(self):
            self.sent = []

        async def send_json(self, payload):
            self.sent.append(payload)

        async def receive_json(self):
            return {
                "approved": True,
                "scope": "session",
                "message": "approved after editing",
                "edited_args": {"file_path": "notes.txt", "content": "final"},
                "plan_mode_target": "auto-edit",
            }

    ws = _WebSocket()
    await run_turn_over_websocket(ws, _Controller(), "update notes")

    assert ws.sent[0] == {
        "type": "approval",
        "action": "write",
        "target": "notes.txt",
        "dangerous": True,
        "reason": "write needs approval",
        "description": "Update the notes",
        "args": {"file_path": "notes.txt", "content": "draft"},
        "plan": "1. Update notes",
        "suggested_memory": {
            "name": "preference",
            "description": "Writing preference",
            "body": "Keep notes short",
            "type": "project",
            "scope": "global",
        },
    }
    reply = captured["reply"]
    assert reply.approved is True
    assert reply.scope.value == "session"
    assert reply.message == "approved after editing"
    assert reply.edited_args == {"file_path": "notes.txt", "content": "final"}
    assert reply.plan_mode_target == "auto-edit"


@pytest.mark.asyncio
async def test_websocket_endpoint_awaits_async_controller_cleanup(monkeypatch):
    sketch = _load_sketch()

    endpoints = {}

    class _FastAPI:
        def websocket(self, path):
            def _register(endpoint):
                endpoints[path] = endpoint
                return endpoint

            return _register

    fake_fastapi = ModuleType("fastapi")
    fake_fastapi.FastAPI = _FastAPI
    fake_fastapi.WebSocket = object
    monkeypatch.setitem(sys.modules, "fastapi", fake_fastapi)
    monkeypatch.setattr(sketch, "load_config", lambda: object())

    instances = []

    class _Controller:
        def __init__(self, config, project_root):
            self.sync_closed = False
            self.async_closed = False
            instances.append(self)

        def close(self):
            self.sync_closed = True

        async def aclose(self):
            self.async_closed = True

    monkeypatch.setattr(sketch, "Controller", _Controller)

    class _Disconnected(Exception):
        pass

    class _WebSocket:
        async def accept(self):
            return None

        async def receive_json(self):
            raise _Disconnected

    sketch.build_app()
    with pytest.raises(_Disconnected):
        await endpoints["/ws"](_WebSocket())

    assert len(instances) == 1
    assert instances[0].async_closed is True
    assert instances[0].sync_closed is False
