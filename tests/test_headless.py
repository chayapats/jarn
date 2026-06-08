"""Tests for the headless one-shot runner (``jarn -p "prompt"``)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jarn.agent.session import ApprovalRequest, EventKind
from jarn.config.schema import PermissionMode
from jarn.headless import (
    HeadlessRefusal,
    HeadlessResult,
    _make_fail_closed_approver,
    _run_headless,
    run_headless,
)
from jarn.permissions import Action, ActionKind, Decision, PermissionResult

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _stub_controller(monkeypatch, text: str = "Hello from model"):
    """Patch Controller so ensure_runtime and make_driver are no-ops.

    ``make_driver`` returns a stub whose ``run_turn`` yields a single TEXT event
    followed by DONE.
    """
    from jarn.agent.session import Event

    async def _fake_run_turn(prompt, *, resume: bool = False):
        yield Event(kind=EventKind.TEXT, text=text)
        yield Event(kind=EventKind.DONE)

    fake_driver = MagicMock()
    fake_driver.run_turn = _fake_run_turn

    import jarn.headless as headless_mod

    async def _fake_ensure_runtime(self):
        return SimpleNamespace(agent=object(), main_model_ref="m", warnings=())

    async def _fake_aclose(self):
        pass

    monkeypatch.setattr(
        headless_mod.Controller,
        "ensure_runtime",
        _fake_ensure_runtime,
    )
    monkeypatch.setattr(
        headless_mod.Controller,
        "make_driver",
        lambda self, approver: fake_driver,
    )
    monkeypatch.setattr(
        headless_mod.Controller,
        "validate",
        lambda self: (True, "ready"),
    )
    monkeypatch.setattr(
        headless_mod.Controller,
        "enrich_turn_input",
        lambda self, text: text,
    )
    monkeypatch.setattr(
        headless_mod.Controller,
        "aclose",
        _fake_aclose,
    )
    return fake_driver


# ---------------------------------------------------------------------------
# Headless core: one-shot returns model text, exits 0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_headless_returns_model_text(tmp_path, monkeypatch, base_config):
    """_run_headless returns the model's text in HeadlessResult.result."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _stub_controller(monkeypatch, text="The answer is 42.")

    result = await _run_headless("what is the answer?", base_config, tmp_path)

    assert isinstance(result, HeadlessResult)
    assert result.result == "The answer is 42."
    assert result.turns == 1


def test_run_headless_exits_0_via_run_headless(tmp_path, monkeypatch, base_config, capsys):
    """run_headless prints the text and returns 0."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _stub_controller(monkeypatch, text="Done!")

    code = run_headless("do something", base_config, tmp_path)

    assert code == 0
    out = capsys.readouterr().out
    assert "Done!" in out


# ---------------------------------------------------------------------------
# --json flag
# ---------------------------------------------------------------------------


def test_run_headless_json_output_has_result_key(tmp_path, monkeypatch, base_config, capsys):
    """--json emits a JSON object containing a 'result' key."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _stub_controller(monkeypatch, text="JSON answer.")

    code = run_headless("do something", base_config, tmp_path, as_json=True)

    assert code == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert "result" in data
    assert data["result"] == "JSON answer."
    assert "tokens" in data
    assert "cost" in data
    assert "turns" in data


# ---------------------------------------------------------------------------
# Stdin prompt via '-'
# ---------------------------------------------------------------------------


def test_cmd_headless_stdin_prompt(tmp_path, monkeypatch, base_config, capsys):
    """``jarn -p -`` routes to _cmd_headless with prompt_arg='-' and reads stdin."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))

    import jarn.cli as cli_mod

    calls: list[dict] = []

    def _recording_cmd_headless(**kw):
        calls.append(kw)
        print("stdin answer")
        return 0

    monkeypatch.setattr(cli_mod, "_cmd_headless", _recording_cmd_headless)

    from jarn.cli import main

    code = main(["-p", "-"])
    assert code == 0
    assert calls[0]["prompt_arg"] == "-"
    out = capsys.readouterr().out
    assert "stdin answer" in out


# ---------------------------------------------------------------------------
# Fail-closed safety
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fail_closed_approver_raises_in_ask_mode():
    """In ask mode, _make_fail_closed_approver raises HeadlessRefusal."""
    approver = _make_fail_closed_approver(PermissionMode.ASK)
    req = ApprovalRequest(
        action=Action(kind=ActionKind.SHELL, target="rm -rf /tmp/x", tool="execute"),
        result=PermissionResult(
            decision=Decision.ASK, reason="ask mode requires confirmation"
        ),
        description="shell execution",
    )
    with pytest.raises(HeadlessRefusal) as exc_info:
        await approver(req)
    assert "execute" in str(exc_info.value) or "tool" in str(exc_info.value)
    assert "confirmation" in exc_info.value.reason


@pytest.mark.asyncio
async def test_fail_closed_approver_raises_in_plan_mode():
    """In plan mode, _make_fail_closed_approver raises HeadlessRefusal."""
    approver = _make_fail_closed_approver(PermissionMode.PLAN)
    req = ApprovalRequest(
        action=Action(kind=ActionKind.WRITE, target="/tmp/foo.py", tool="write_file"),
        result=PermissionResult(
            decision=Decision.ASK, reason="plan mode is read-only"
        ),
    )
    with pytest.raises(HeadlessRefusal):
        await approver(req)


@pytest.mark.asyncio
async def test_fail_closed_approver_denies_in_auto_edit_mode():
    """In auto-edit mode, danger-guard items still get denied (not raised)."""
    approver = _make_fail_closed_approver(PermissionMode.AUTO_EDIT)
    req = ApprovalRequest(
        action=Action(kind=ActionKind.SHELL, target="rm -rf /", tool="execute"),
        result=PermissionResult(
            decision=Decision.ASK, reason="danger-guard: unconditionally blocked",
            dangerous=True, block_remember_always=True,
        ),
    )
    reply = await approver(req)
    assert reply.approved is False
    assert "auto-denied" in reply.message


def test_headless_gated_tool_exits_nonzero_and_prints_message(
    tmp_path, monkeypatch, base_config, capsys
):
    """run_headless exits non-zero + prints a clear message when a gated tool is refused."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))

    from jarn.agent.session import Event

    async def _raising_run_turn(prompt, *, resume: bool = False):
        # The approver raises HeadlessRefusal when it encounters an ASK.
        yield Event(kind=EventKind.TEXT, text="thinking…")
        raise HeadlessRefusal("execute", "ask mode requires confirmation")

    fake_driver = MagicMock()
    fake_driver.run_turn = _raising_run_turn

    import jarn.headless as headless_mod

    async def _fake_ensure_runtime_g(self):
        pass

    async def _fake_aclose_g(self):
        pass

    monkeypatch.setattr(headless_mod.Controller, "ensure_runtime", _fake_ensure_runtime_g)
    monkeypatch.setattr(headless_mod.Controller, "make_driver", lambda self, approver: fake_driver)
    monkeypatch.setattr(headless_mod.Controller, "validate", lambda self: (True, "ready"))
    monkeypatch.setattr(headless_mod.Controller, "enrich_turn_input", lambda self, t: t)
    monkeypatch.setattr(headless_mod.Controller, "aclose", _fake_aclose_g)

    code = run_headless("run something risky", base_config, tmp_path)

    assert code == 1
    err = capsys.readouterr().err
    assert "headless" in err.lower() or "gated" in err.lower() or "refused" in err.lower()


# ---------------------------------------------------------------------------
# Existing subcommands still dispatch correctly (smoke test)
# ---------------------------------------------------------------------------


def test_doctor_subcommand_still_works(tmp_path, monkeypatch, capsys):
    """jarn doctor still dispatches correctly after adding headless flags."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    import jarn.cli as cli_mod

    called: list[str] = []

    def _fake_doctor(*, as_json: bool = False) -> int:
        called.append("doctor")
        return 0

    monkeypatch.setattr(cli_mod, "_cmd_doctor", _fake_doctor)

    from jarn.cli import main

    code = main(["doctor"])
    assert code == 0
    assert called == ["doctor"]


def test_no_args_calls_launch_not_headless(tmp_path, monkeypatch):
    """jarn (no args) still routes to _cmd_launch, not headless."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    import jarn.cli as cli_mod

    called: list[str] = []

    def _fake_launch(*, resume: bool = False) -> int:
        called.append("launch")
        return 0

    def _fake_keyfix() -> None:
        pass

    monkeypatch.setattr(cli_mod, "_cmd_launch", _fake_launch)
    import jarn.tui.keyfix as kf_mod

    monkeypatch.setattr(kf_mod, "apply_kitty_keyfix", _fake_keyfix)

    from jarn.cli import main

    code = main([])
    assert code == 0
    assert called == ["launch"]
