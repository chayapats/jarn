"""Project capability detection (verify.py)."""

from __future__ import annotations

from jarn.agent.verify import ProjectCapabilities, detect_capabilities


def test_detect_python_project(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest]\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    caps = detect_capabilities(tmp_path)
    assert "pytest -q" in caps.test
    assert "ruff check ." in caps.lint


def test_detect_node_project(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "vitest", "build": "vite build", "lint": "eslint ."}}',
        encoding="utf-8",
    )
    caps = detect_capabilities(tmp_path)
    assert "npm run test" in caps.test
    assert "npm run build" in caps.build
    assert "npm run lint" in caps.lint


def test_prompt_block_empty_when_no_capabilities():
    assert ProjectCapabilities().as_prompt_block() == ""


def test_prompt_block_lists_detected_commands(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/x\n", encoding="utf-8")
    block = detect_capabilities(tmp_path).as_prompt_block()
    assert "go test ./..." in block
    assert "Verification commands" in block
