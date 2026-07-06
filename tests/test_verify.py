"""Project capability detection (verify.py)."""

from __future__ import annotations

import asyncio

import pytest

from jarn.agent.verify import ProjectCapabilities, detect_capabilities


def test_detect_python_project(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n[tool.ruff]\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    caps = detect_capabilities(tmp_path)
    assert "pytest -q" in caps.test
    assert "ruff check ." in caps.lint


def test_pyproject_without_pytest_skips_pytest(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    caps = detect_capabilities(tmp_path)
    assert "pytest -q" not in caps.test


def test_pyproject_without_ruff_skips_ruff(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    caps = detect_capabilities(tmp_path)
    assert "ruff check ." not in caps.lint


def test_detect_node_project(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "vitest", "build": "vite build", "lint": "eslint ."}}',
        encoding="utf-8",
    )
    caps = detect_capabilities(tmp_path)
    assert "npm run test" in caps.test
    assert "npm run build" in caps.build
    assert "npm run lint" in caps.lint


def test_detect_node_test_unit_and_typecheck(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test:unit": "vitest run", "typecheck": "tsc --noEmit", "check": "biome check"}}',
        encoding="utf-8",
    )
    caps = detect_capabilities(tmp_path)
    assert "npm run test:unit" in caps.test
    assert "npm run typecheck" in caps.lint
    assert "npm run check" in caps.lint


def test_makefile_target_detection(tmp_path):
    (tmp_path / "Makefile").write_text(
        ".PHONY: test\n\ntest:\n\tpytest -q\n\nbuild:\n\techo build\n",
        encoding="utf-8",
    )
    caps = detect_capabilities(tmp_path)
    assert "make test" in caps.test
    assert "make build" in caps.build


def test_prompt_block_empty_when_no_capabilities():
    assert ProjectCapabilities().as_prompt_block() == ""


def test_prompt_block_lists_detected_commands(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/x\n", encoding="utf-8")
    block = detect_capabilities(tmp_path).as_prompt_block()
    assert "go test ./..." in block
    assert "Verification commands" in block


@pytest.mark.asyncio
async def test_gate_suggest(tmp_path):
    from jarn.agent.events import EventKind
    from jarn.agent.session import SessionDriver
    from jarn.agent.verify import verify_after_edit
    from jarn.cost import CostTracker
    from jarn.permissions import PermissionEngine

    (tmp_path / "go.mod").write_text("module example.com/x\n", encoding="utf-8")
    driver = SessionDriver(
        agent=None,
        engine=PermissionEngine(),
        tracker=CostTracker(),
        thread_id="t",
        verify_gate="suggest",
        project_root=tmp_path,
    )
    ev = await verify_after_edit(driver, "write_file")
    assert ev is not None
    assert ev.kind is EventKind.NOTICE
    assert ev.data.get("verify", {}).get("cmd") == "go test ./..."


@pytest.mark.asyncio
async def test_gate_auto_runs_detected_command(tmp_path):
    from jarn.agent.events import EventKind
    from jarn.agent.session import SessionDriver
    from jarn.agent.verify import verify_after_edit
    from jarn.config.schema import PermissionMode
    from jarn.cost import CostTracker
    from jarn.permissions import PermissionEngine

    (tmp_path / "go.mod").write_text("module example.com/x\n", encoding="utf-8")
    ran: list[str] = []

    class _Resp:
        exit_code = 0
        output = "ok"

    def _executor(cmd: str) -> _Resp:
        ran.append(cmd)
        return _Resp()

    driver = SessionDriver(
        agent=None,
        engine=PermissionEngine(mode=PermissionMode.YOLO),
        tracker=CostTracker(),
        thread_id="t",
        verify_gate="auto",
        project_root=tmp_path,
        verify_executor=_executor,
    )
    ev = await verify_after_edit(driver, "edit_file")
    assert ran == ["go test ./..."]
    assert ev is not None
    assert ev.kind is EventKind.NOTICE
    assert ev.data.get("verify", {}).get("ok") is True


# ---------------------------------------------------------------------------
# Once-per-turn debounce tests (T-1-3)
# ---------------------------------------------------------------------------

class _FakeToolMsg:
    """Minimal ToolMessage stub: produces a TOOL_END event in the session driver."""

    type = "tool"
    content = ""
    tool_call_id = None
    usage_metadata = None
    response_metadata: dict = {}

    def __init__(self, name: str = "write_file"):
        self.name = name


class _MultiEditAgent:
    """Streams N write_file TOOL_END messages then completes without interrupts."""

    def __init__(self, n: int = 10, tool_name: str = "write_file"):
        self.n = n
        self.tool_name = tool_name

    async def astream(self, payload, config, **kw):
        for _ in range(self.n):
            yield ("messages", (_FakeToolMsg(self.tool_name),))


class _CancelMidStreamAgent:
    """Yields one write_file TOOL_END then raises CancelledError."""

    async def astream(self, payload, config, **kw):
        yield ("messages", (_FakeToolMsg("write_file"),))
        raise asyncio.CancelledError


@pytest.mark.asyncio
async def test_verify_runs_once_per_turn(tmp_path):
    """10 write_file TOOL_ENDs in one turn → verify executor invoked exactly once."""
    from jarn.agent.session import SessionDriver
    from jarn.config.schema import PermissionMode
    from jarn.cost import CostTracker
    from jarn.permissions import PermissionEngine

    (tmp_path / "go.mod").write_text("module example.com/x\n", encoding="utf-8")
    ran: list[str] = []

    class _Resp:
        exit_code = 0
        output = "ok"

    def _executor(cmd: str) -> _Resp:
        ran.append(cmd)
        return _Resp()

    driver = SessionDriver(
        agent=_MultiEditAgent(n=10),
        engine=PermissionEngine(mode=PermissionMode.YOLO),
        tracker=CostTracker(),
        thread_id="t",
        verify_gate="auto",
        project_root=tmp_path,
        verify_executor=_executor,
    )
    _events = [ev async for ev in driver.run_turn("fix it")]
    assert ran == ["go test ./..."], (
        f"executor must be called exactly once per turn; got calls={ran}"
    )


class _FakeAIMsg:
    """Minimal AI message stub: produces a TEXT event in the session driver."""

    type = "ai"
    content = "Fixed!"
    tool_calls: list = []
    usage_metadata = None
    response_metadata: dict = {}


class _TextThenEditAgent:
    """Streams AI text + write_file TOOL_END."""

    async def astream(self, payload, config, **kw):
        yield ("messages", (_FakeAIMsg(),))
        yield ("messages", (_FakeToolMsg("write_file"),))


class _ReadOnlyAgent:
    """Streams only AI text, no file writes."""

    async def astream(self, payload, config, **kw):
        yield ("messages", (_FakeAIMsg(),))


@pytest.mark.asyncio
async def test_verify_badge_event_emitted_after_final_text(tmp_path):
    """NOTICE with data['verify'] must be emitted, after all TEXT events, for edit turns."""
    from jarn.agent.events import EventKind
    from jarn.agent.session import SessionDriver
    from jarn.config.schema import PermissionMode
    from jarn.cost import CostTracker
    from jarn.permissions import PermissionEngine

    (tmp_path / "go.mod").write_text("module example.com/x\n", encoding="utf-8")

    class _Resp:
        exit_code = 0
        output = "ok\n1 passed, 0 failed"

    def _executor(cmd: str) -> _Resp:
        return _Resp()

    driver = SessionDriver(
        agent=_TextThenEditAgent(),
        engine=PermissionEngine(mode=PermissionMode.YOLO),
        tracker=CostTracker(),
        thread_id="t",
        verify_gate="auto",
        project_root=tmp_path,
        verify_executor=_executor,
    )
    events = [ev async for ev in driver.run_turn("fix it")]

    verify_notices = [
        ev for ev in events
        if ev.kind is EventKind.NOTICE and ev.data.get("verify")
    ]
    assert len(verify_notices) == 1, f"Expected 1 verify notice, got: {verify_notices}"

    vd = verify_notices[0].data["verify"]
    text_events = [ev for ev in events if ev.kind is EventKind.TEXT]
    assert text_events, "Expected at least one TEXT event"

    verify_idx = events.index(verify_notices[0])
    max_text_idx = max(events.index(ev) for ev in text_events)
    assert verify_idx > max_text_idx, (
        f"verify notice index {verify_idx} should be > max text index {max_text_idx}"
    )

    assert vd["cmd"] == "go test ./...", f"Expected cmd='go test ./...', got: {vd}"
    assert vd["ok"] is True, f"Expected ok=True, got: {vd}"
    assert isinstance(vd["secs"], float), f"Expected secs to be float, got: {type(vd['secs'])}"


@pytest.mark.asyncio
async def test_no_verify_badge_on_read_only_turn(tmp_path):
    """Read-only turns (no write_file/edit_file) must not emit a verify badge."""
    from jarn.agent.events import EventKind
    from jarn.agent.session import SessionDriver
    from jarn.config.schema import PermissionMode
    from jarn.cost import CostTracker
    from jarn.permissions import PermissionEngine

    (tmp_path / "go.mod").write_text("module example.com/x\n", encoding="utf-8")

    ran: list[str] = []

    class _Resp:
        exit_code = 0
        output = "ok"

    def _executor(cmd: str) -> _Resp:
        ran.append(cmd)
        return _Resp()

    driver = SessionDriver(
        agent=_ReadOnlyAgent(),
        engine=PermissionEngine(mode=PermissionMode.YOLO),
        tracker=CostTracker(),
        thread_id="t",
        verify_gate="auto",
        project_root=tmp_path,
        verify_executor=_executor,
    )
    events = [ev async for ev in driver.run_turn("read something")]

    assert ran == [], f"Executor should not be called for read-only turns; got: {ran}"

    verify_notices = [
        ev for ev in events
        if ev.kind is EventKind.NOTICE and ev.data.get("verify")
    ]
    assert verify_notices == [], f"No verify notice expected for read-only turns; got: {verify_notices}"


@pytest.mark.asyncio
async def test_no_verify_after_cancel(tmp_path):
    """A cancelled turn (CancelledError mid-stream) must NOT run verify."""
    from jarn.agent.session import SessionDriver
    from jarn.config.schema import PermissionMode
    from jarn.cost import CostTracker
    from jarn.permissions import PermissionEngine

    (tmp_path / "go.mod").write_text("module example.com/x\n", encoding="utf-8")
    ran: list[str] = []

    class _Resp:
        exit_code = 0
        output = "ok"

    def _executor(cmd: str) -> _Resp:
        ran.append(cmd)
        return _Resp()

    driver = SessionDriver(
        agent=_CancelMidStreamAgent(),
        engine=PermissionEngine(mode=PermissionMode.YOLO),
        tracker=CostTracker(),
        thread_id="t",
        verify_gate="auto",
        project_root=tmp_path,
        verify_executor=_executor,
    )
    with pytest.raises(asyncio.CancelledError):
        async for _ in driver.run_turn("go"):
            pass
    assert ran == [], f"verify must not run after cancel; got calls={ran}"
