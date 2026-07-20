"""Project capability detection (verify.py)."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from types import SimpleNamespace

import pytest

from jarn.agent.verify import ProjectCapabilities, detect_capabilities, summarize_output


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
    # Suggest surfaces the full detected set (test + build), not just test[0].
    assert ev.data.get("verify", {}).get("cmd") == "go test ./... && go build ./..."


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
    # The gate runs the full detected set (test + build), in order, not just test[0].
    assert ran == ["go test ./...", "go build ./..."]
    assert ev is not None
    assert ev.kind is EventKind.NOTICE
    assert ev.data.get("verify", {}).get("ok") is True


@pytest.mark.asyncio
async def test_gate_runs_build_and_lint_not_just_test(tmp_path):
    """A change that breaks the build but keeps tests green must fail the gate.

    Regression guard: the gate previously ran only ``caps.test[0]``, so a broken
    build or failing lint slipped through. It must now run the fuller detected set.
    """
    from jarn.agent.session import SessionDriver
    from jarn.agent.verify import verify_after_edit
    from jarn.config.schema import PermissionMode
    from jarn.cost import CostTracker
    from jarn.permissions import PermissionEngine

    # go.mod detects both `go test ./...` and `go build ./...`.
    (tmp_path / "go.mod").write_text("module example.com/x\n", encoding="utf-8")
    ran: list[str] = []

    def _executor(cmd: str):
        ran.append(cmd)
        # tests pass, build fails
        ok = "test" in cmd
        return SimpleNamespace(exit_code=0 if ok else 1, output="" if ok else "build error")

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
    assert ran == ["go test ./...", "go build ./..."], f"build must run too; got {ran}"
    assert ev.data["verify"]["ok"] is False, "build failure must fail the gate"
    assert "go build ./..." in ev.data["verify"]["cmd"]


@pytest.mark.asyncio
async def test_gate_runs_all_test_commands(tmp_path):
    """Every detected test command runs, not just the first (test[0])."""
    from jarn.agent.session import SessionDriver
    from jarn.agent.verify import verify_after_edit
    from jarn.config.schema import PermissionMode
    from jarn.cost import CostTracker
    from jarn.permissions import PermissionEngine

    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "vitest run", "test:unit": "vitest unit"}}',
        encoding="utf-8",
    )
    ran: list[str] = []

    def _executor(cmd: str):
        ran.append(cmd)
        return SimpleNamespace(exit_code=0, output="ok")

    driver = SessionDriver(
        agent=None,
        engine=PermissionEngine(mode=PermissionMode.YOLO),
        tracker=CostTracker(),
        thread_id="t",
        verify_gate="auto",
        project_root=tmp_path,
        verify_executor=_executor,
    )
    ev = await verify_after_edit(driver, "write_file")
    assert ran == ["npm run test", "npm run test:unit"], f"all test cmds must run; got {ran}"
    assert ev.data["verify"]["ok"] is True


@pytest.mark.asyncio
async def test_gate_lint_failure_blocks(tmp_path):
    """A lint failure (tests green) must fail the gate."""
    from jarn.agent.session import SessionDriver
    from jarn.agent.verify import verify_after_edit
    from jarn.config.schema import PermissionMode
    from jarn.cost import CostTracker
    from jarn.permissions import PermissionEngine

    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n[tool.ruff]\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    ran: list[str] = []

    def _executor(cmd: str):
        ran.append(cmd)
        ok = "pytest" in cmd  # tests pass, lint fails
        return SimpleNamespace(exit_code=0 if ok else 1, output="" if ok else "E501 line too long")

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
    assert ran == ["pytest -q", "ruff check ."], f"lint must run too; got {ran}"
    vd = ev.data["verify"]
    assert vd["ok"] is False, "lint failure must fail the gate"
    assert "ruff check ." in vd["cmd"]
    assert "E501" in vd.get("full_output", ""), "failing lint output must be fed back"


@pytest.mark.asyncio
async def test_build_failure_blocks_done_end_to_end(tmp_path):
    """Tests-pass-but-build-fails is terminal: it must block DONE, not slip through."""
    from jarn.agent.events import EventKind
    from jarn.agent.session import SessionDriver
    from jarn.config.schema import PermissionMode
    from jarn.cost import CostTracker
    from jarn.permissions import PermissionEngine

    (tmp_path / "go.mod").write_text("module example.com/x\n", encoding="utf-8")
    agent = _RepairingAgent()

    def _executor(cmd: str):
        ok = "test" in cmd  # tests always pass; build always fails (repair can't fix)
        return SimpleNamespace(exit_code=0 if ok else 1, output="" if ok else "build error")

    driver = SessionDriver(
        agent=agent,
        engine=PermissionEngine(mode=PermissionMode.YOLO),
        tracker=CostTracker(),
        thread_id="build-blocks-e2e",
        verify_gate="auto",
        verify_max_repair_rounds=1,
        project_root=tmp_path,
        verify_executor=_executor,
    )
    events = [event async for event in driver.run_turn("implement it")]

    # One repair round is attempted, then the still-failing build is terminal.
    assert events[-1].kind is EventKind.ERROR
    assert events[-1].data["verification"]["ok"] is False
    assert "go build ./..." in events[-1].data["verification"]["cmd"]
    assert not any(e.kind is EventKind.DONE for e in events)


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
    # The full detected set runs exactly once per turn (debounced), not once per edit.
    assert ran == ["go test ./...", "go build ./..."], (
        f"command set must run exactly once per turn; got calls={ran}"
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

    assert vd["cmd"] == "go test ./... && go build ./...", f"Expected joined cmd, got: {vd}"
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


class _RepairingAgent:
    """End-to-end fake graph: first edit fails verification, repair edit passes."""

    def __init__(self) -> None:
        self.stream_calls = 0
        self.injected: list[str] = []

    async def astream(self, payload, config, **kw):
        self.stream_calls += 1
        if self.stream_calls == 1:
            msg = _FakeAIMsg()
            msg.content = "Initial implementation is complete."
            yield ("messages", (msg,))
            yield ("messages", (_FakeToolMsg("write_file"),))
            return
        msg = _FakeAIMsg()
        msg.content = "Repaired implementation passes verification."
        yield ("messages", (msg,))
        yield ("messages", (_FakeToolMsg("edit_file"),))

    async def aupdate_state(self, config, values):
        messages = values.get("messages", [])
        self.injected.extend(str(getattr(m, "content", "")) for m in messages)


@pytest.mark.asyncio
async def test_verification_failure_repairs_and_reverifies_end_to_end(tmp_path):
    """A failing acceptance command is fed back, repaired, and must pass before DONE."""
    from jarn.agent.events import EventKind
    from jarn.agent.session import SessionDriver
    from jarn.config.schema import PermissionMode
    from jarn.cost import CostTracker
    from jarn.permissions import PermissionEngine

    # Single-command project (pytest only): keeps this repair-loop test focused on
    # the loop mechanics; multi-command aggregation is covered by dedicated tests.
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    agent = _RepairingAgent()
    calls = 0

    class _Resp:
        def __init__(self, ok: bool) -> None:
            self.exit_code = 0 if ok else 1
            self.output = "1 passed" if ok else "1 failed\nassert 1 == 2"

    def _executor(_cmd: str) -> _Resp:
        nonlocal calls
        calls += 1
        return _Resp(ok=calls == 2)

    driver = SessionDriver(
        agent=agent,
        engine=PermissionEngine(mode=PermissionMode.YOLO),
        tracker=CostTracker(),
        thread_id="verify-e2e",
        verify_gate="auto",
        verify_max_repair_rounds=1,
        project_root=tmp_path,
        verify_executor=_executor,
    )

    events = [event async for event in driver.run_turn("implement it")]
    verify = [e.data["verify"] for e in events if e.data.get("verify")]

    assert [item["ok"] for item in verify] == [False, True]
    assert calls == 2
    assert agent.stream_calls == 2
    assert len(agent.injected) == 1
    assert "assert 1 == 2" in agent.injected[0]
    assert any(e.data.get("verification_repair") for e in events)
    assert not any(e.kind is EventKind.ERROR for e in events)
    assert events[-1].kind is EventKind.DONE


@pytest.mark.asyncio
async def test_verification_persistent_failure_blocks_done_end_to_end(tmp_path):
    """After the bounded repair, a still-failing acceptance command is terminal."""
    from jarn.agent.events import EventKind
    from jarn.agent.session import SessionDriver
    from jarn.config.schema import PermissionMode
    from jarn.cost import CostTracker
    from jarn.permissions import PermissionEngine

    (tmp_path / "go.mod").write_text("module example.com/x\n", encoding="utf-8")
    agent = _RepairingAgent()

    class _Resp:
        exit_code = 1
        output = "2 failed"

    driver = SessionDriver(
        agent=agent,
        engine=PermissionEngine(mode=PermissionMode.YOLO),
        tracker=CostTracker(),
        thread_id="verify-fail-e2e",
        verify_gate="auto",
        verify_max_repair_rounds=1,
        project_root=tmp_path,
        verify_executor=lambda _cmd: _Resp(),
    )

    events = [event async for event in driver.run_turn("implement it")]

    assert sum(bool(e.data.get("verify")) for e in events) == 2
    assert events[-1].kind is EventKind.ERROR
    assert events[-1].data["verification"]["ok"] is False
    assert not any(e.kind is EventKind.DONE for e in events)


@pytest.mark.asyncio
async def test_real_pytest_failure_repair_and_pass_acceptance_end_to_end(tmp_path):
    """Real acceptance E2E: broken files -> pytest fails -> repair -> pytest passes."""
    from jarn.agent.events import EventKind
    from jarn.agent.session import SessionDriver
    from jarn.config.schema import PermissionMode
    from jarn.cost import CostTracker
    from jarn.permissions import PermissionEngine

    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(
        "from calc import double\n\n"
        "def test_double():\n"
        "    assert double(3) == 6\n",
        encoding="utf-8",
    )

    class _FilesystemRepairAgent:
        def __init__(self) -> None:
            self.calls = 0
            self.injected = ""

        async def astream(self, payload, config, **kw):
            self.calls += 1
            if self.calls == 1:
                (tmp_path / "calc.py").write_text(
                    "def double(value):\n    return 4\n", encoding="utf-8"
                )
                yield ("messages", (_FakeToolMsg("write_file"),))
                return
            assert "1 failed" in self.injected
            (tmp_path / "calc.py").write_text(
                "def double(value):\n    return value * 2\n", encoding="utf-8"
            )
            yield ("messages", (_FakeToolMsg("edit_file"),))
            msg = _FakeAIMsg()
            msg.content = "Fixed double and verified the real test suite."
            yield ("messages", (msg,))

        async def aupdate_state(self, config, values):
            self.injected = str(values["messages"][0].content)

    def _execute(_cmd: str):
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return SimpleNamespace(
            exit_code=proc.returncode,
            output=(proc.stdout or "") + (proc.stderr or ""),
        )

    agent = _FilesystemRepairAgent()
    driver = SessionDriver(
        agent=agent,
        engine=PermissionEngine(mode=PermissionMode.YOLO),
        tracker=CostTracker(),
        thread_id="real-acceptance-e2e",
        verify_gate="auto",
        verify_max_repair_rounds=1,
        project_root=tmp_path,
        verify_executor=_execute,
    )

    events = [event async for event in driver.run_turn("implement double")]
    outcomes = [e.data["verify"] for e in events if e.data.get("verify")]

    assert [outcome["ok"] for outcome in outcomes] == [False, True]
    assert "1 passed" in outcomes[-1]["summary"]
    assert events[-1].kind is EventKind.DONE
    assert (tmp_path / "calc.py").read_text(encoding="utf-8").endswith(
        "return value * 2\n"
    )


# ---------------------------------------------------------------------------
# Adversarial unit tests for summarize_output (T-3-2)
# ---------------------------------------------------------------------------


def test_summarize_pytest_pass_and_fail_counts():
    out = summarize_output("pytest", "2 failed, 5 passed in 3.2s", exit_code=1)
    assert "2 failed" in out and "5 passed" in out


def test_summarize_ansi_still_matches():
    # ANSI codes don't prevent pattern matching; output includes both content and codes.
    out = summarize_output("pytest", "\x1b[31m2 failed\x1b[0m in 3.2s", exit_code=1)
    assert out  # never empty, never raises
    assert "failed" in out  # Pattern matches despite ANSI codes


def test_summarize_empty_output_fallback():
    assert summarize_output("pytest", "", exit_code=0) == "exit 0"


def test_summarize_never_raises_on_junk():
    result = summarize_output(None, None, exit_code=None)  # type: ignore[arg-type]
    assert result is not None
    assert isinstance(result, str)
