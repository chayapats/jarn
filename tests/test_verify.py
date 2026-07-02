"""Project capability detection (verify.py)."""

from __future__ import annotations

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
    assert "go test ./..." in ev.text


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
    assert "passed" in ev.text
