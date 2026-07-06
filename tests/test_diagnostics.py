"""Diagnostics feedback loop tests (T-3-3)."""
from __future__ import annotations

import shutil

import pytest

# ── Shared fake agent infrastructure (mirrors test_verify.py patterns) ────────


class _FakeToolMsg:
    """Minimal ToolMessage stub: produces a TOOL_END event."""

    type = "tool"
    content = ""
    tool_call_id = None
    usage_metadata = None
    response_metadata: dict = {}

    def __init__(self, name: str = "write_file"):
        self.name = name


class _FakeAIToolCallMsg:
    """AI message carrying a write_file tool call (updates mode).

    Mirrors the real stream: the model's tool_call arrives as an ``updates``
    chunk (which is where the session driver reads ``file_path`` into
    ``_last_edit_target``), then the tool result arrives as a ``messages``
    TOOL_END chunk.
    """

    type = "ai"
    content = ""
    usage_metadata = None
    response_metadata: dict = {}

    def __init__(self, path: str):
        self.tool_calls = [
            {"name": "write_file", "args": {"file_path": path}, "id": "c1"}
        ]


class _WriteFileAgent:
    """Fake agent: write_file tool-call (updates) + TOOL_END (messages)."""

    def __init__(self, path: str = "f.py"):
        self.path = path

    async def astream(self, payload, config, **kw):
        yield ("updates", {"model": {"messages": [_FakeAIToolCallMsg(self.path)]}})
        yield ("messages", (_FakeToolMsg("write_file"),))


# ── test_ruff_parse ────────────────────────────────────────────────────────────


def test_ruff_parse(tmp_path):
    """A file with an unused import yields a Diag from ruff (real subprocess)."""
    from jarn.agent.diagnostics import collect_diagnostics

    bad = tmp_path / "bad.py"
    bad.write_text("import os\n\ndef foo(): pass\n", encoding="utf-8")
    diags = collect_diagnostics([bad], tmp_path)
    ruff_diags = [d for d in diags if d.tool == "ruff"]
    assert ruff_diags, f"Expected ruff diagnostics, got {diags}"
    assert any("F401" in d.code for d in ruff_diags), (
        f"Expected F401, got {[d.code for d in ruff_diags]}"
    )
    assert ruff_diags[0].severity in ("error", "warning")
    assert ruff_diags[0].line >= 1


# ── test_pyright_parse ─────────────────────────────────────────────────────────


@pytest.mark.skipif(not shutil.which("pyright"), reason="pyright not installed")
def test_pyright_parse(tmp_path):
    """pyright --outputjson parsing works (skipped when binary absent)."""
    from jarn.agent.diagnostics import collect_diagnostics

    bad = tmp_path / "bad.py"
    bad.write_text(
        "def foo(x: int) -> str:\n    return x  # type error\n",
        encoding="utf-8",
    )
    diags = collect_diagnostics([bad], tmp_path)
    py_diags = [d for d in diags if d.tool == "pyright"]
    assert py_diags, f"Expected pyright diagnostics, got {diags}"
    assert py_diags[0].severity in ("error", "warning", "information")


# ── test_auto_queues_once ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_queues_once(tmp_path, monkeypatch):
    """auto mode with fresh errors queues exactly ONE internal follow-up turn."""
    from jarn.agent.diagnostics import Diag
    from jarn.agent.session import SessionDriver
    from jarn.cost import CostTracker
    from jarn.permissions import PermissionEngine

    monkeypatch.setattr(
        "jarn.agent.session.collect_diagnostics",
        lambda paths, project_root, **kw: [
            Diag(
                file="f.py",
                line=1,
                severity="error",
                code="F401",
                message="unused import os",
                tool="ruff",
            ),
        ],
    )

    driver = SessionDriver(
        agent=_WriteFileAgent(),
        engine=PermissionEngine(),
        tracker=CostTracker(),
        thread_id="t1",
        diagnostics_mode="auto",
        diagnostics_max_rounds=1,
        _diag_round=0,
        project_root=tmp_path,
    )
    events = [ev async for ev in driver.run_turn("fix it")]
    queue_evs = [e for e in events if e.data.get("diagnostics_auto_queue")]
    assert len(queue_evs) == 1, f"Expected 1 queue event, got {len(queue_evs)}"
    payload = queue_evs[0].data["diagnostics_auto_queue"]
    assert payload.startswith("Diagnostics after your edits:"), repr(payload)


# ── test_round_cap ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_round_cap(tmp_path, monkeypatch):
    """Second round with remaining errors does NOT queue again (max_rounds=1)."""
    from jarn.agent.diagnostics import Diag
    from jarn.agent.session import SessionDriver
    from jarn.cost import CostTracker
    from jarn.permissions import PermissionEngine

    monkeypatch.setattr(
        "jarn.agent.session.collect_diagnostics",
        lambda paths, project_root, **kw: [
            Diag(
                file="f.py",
                line=1,
                severity="error",
                code="F401",
                message="unused import os",
                tool="ruff",
            ),
        ],
    )

    driver = SessionDriver(
        agent=_WriteFileAgent(),
        engine=PermissionEngine(),
        tracker=CostTracker(),
        thread_id="t1",
        diagnostics_mode="auto",
        diagnostics_max_rounds=1,
        _diag_round=1,  # already at cap
        project_root=tmp_path,
    )
    events = [ev async for ev in driver.run_turn("fix it again")]
    queue_evs = [e for e in events if e.data.get("diagnostics_auto_queue")]
    assert len(queue_evs) == 0, "Should not queue when _diag_round >= diagnostics_max_rounds"


# ── test_suggest_notice_only ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_suggest_notice_only(tmp_path, monkeypatch):
    """suggest mode yields a NOTICE listing top diagnostics but queues nothing."""
    from jarn.agent.diagnostics import Diag
    from jarn.agent.session import SessionDriver
    from jarn.cost import CostTracker
    from jarn.permissions import PermissionEngine

    monkeypatch.setattr(
        "jarn.agent.session.collect_diagnostics",
        lambda paths, project_root, **kw: [
            Diag(
                file="f.py",
                line=1,
                severity="error",
                code="F401",
                message="unused import os",
                tool="ruff",
            ),
        ],
    )

    driver = SessionDriver(
        agent=_WriteFileAgent(),
        engine=PermissionEngine(),
        tracker=CostTracker(),
        thread_id="t1",
        diagnostics_mode="suggest",
        project_root=tmp_path,
    )
    events = [ev async for ev in driver.run_turn("fix it")]
    diag_notices = [e for e in events if e.data.get("diagnostics")]
    queue_evs = [e for e in events if e.data.get("diagnostics_auto_queue")]
    assert len(diag_notices) == 1, f"Expected 1 diag notice, got {diag_notices}"
    assert len(queue_evs) == 0, "suggest mode must not queue auto-fix"
    notice_data = diag_notices[0].data["diagnostics"]
    assert "F401" in notice_data.get("text", ""), repr(notice_data)


# ── test_clean_silent ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clean_silent(tmp_path, monkeypatch):
    """Clean run → no diagnostics notice, no queue."""
    from jarn.agent.session import SessionDriver
    from jarn.cost import CostTracker
    from jarn.permissions import PermissionEngine

    monkeypatch.setattr(
        "jarn.agent.session.collect_diagnostics",
        lambda paths, project_root, **kw: [],  # clean
    )

    driver = SessionDriver(
        agent=_WriteFileAgent(),
        engine=PermissionEngine(),
        tracker=CostTracker(),
        thread_id="t1",
        diagnostics_mode="auto",
        project_root=tmp_path,
    )
    events = [ev async for ev in driver.run_turn("fix it")]
    assert not any(e.data.get("diagnostics") for e in events), (
        "Clean run should emit no diag notice"
    )
    assert not any(e.data.get("diagnostics_auto_queue") for e in events), (
        "Clean run should not queue"
    )


# ── supporting behaviours ──────────────────────────────────────────────────────


def test_format_diagnostics_caps_at_limit():
    """format_diagnostics shows at most ``limit`` items + an over-cap footer."""
    from jarn.agent.diagnostics import Diag, format_diagnostics

    diags = [
        Diag(f"f{i}.py", i + 1, "error", "E1", f"msg {i}", "ruff")
        for i in range(40)
    ]
    out = format_diagnostics(diags, limit=30)
    lines = out.splitlines()
    assert len(lines) == 31  # 30 items + the "+N more" footer
    assert "(+10 more)" in lines[-1]
    assert "f0.py:1" in lines[0]


def test_tsc_parse_scopes_to_edited_files(tmp_path):
    """tsc output parsing keeps only edited-file findings (tsc is project-wide)."""
    from pathlib import Path

    from jarn.agent.diagnostics import _parse_tsc_output

    output = (
        "src/edited.ts(12,5): error TS2322: Type 'string' is not assignable to type 'number'.\n"
        "src/untouched.ts(3,1): error TS2304: Cannot find name 'x'.\n"
        "not a tsc line\n"
    )
    diags = _parse_tsc_output(output, [Path("src/edited.ts")], tmp_path)
    assert len(diags) == 1, f"pre-existing errors in untouched files must be filtered: {diags}"
    d = diags[0]
    assert d.tool == "tsc" and d.line == 12 and d.code == "TS2322"
    assert d.severity == "error"
    assert d.file.endswith("edited.ts")


def test_tsc_gated_off_by_default(tmp_path, monkeypatch):
    """collect_diagnostics never invokes tsc unless ts=True is passed."""
    from jarn.agent import diagnostics as diag_mod

    called: list[str] = []
    monkeypatch.setattr(
        diag_mod, "_run_tsc", lambda paths, root: called.append("tsc") or []
    )
    monkeypatch.setattr(diag_mod, "_run_ruff", lambda paths: [])
    monkeypatch.setattr(diag_mod, "_run_pyright", lambda paths, root: [])

    diag_mod.collect_diagnostics([tmp_path / "a.py"], tmp_path)
    assert called == [], "tsc must not run by default"
    diag_mod.collect_diagnostics([tmp_path / "a.py"], tmp_path, ts=True)
    assert called == ["tsc"], "ts=True must enable the tsc pass"
