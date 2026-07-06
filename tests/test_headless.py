"""Tests for the headless one-shot runner (``jarn -p "prompt"``)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from jarn.agent.session import ApprovalRequest, EventKind
from jarn.config.schema import PermissionMode
from jarn.headless import (
    EXIT_ERROR,
    EXIT_REFUSED,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
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


def _stub_controller(
    monkeypatch,
    text: str = "Hello from model",
    *,
    tool_events: int = 0,
    run_turn_side_effect=None,
):
    """Patch Controller so ensure_runtime and make_driver are no-ops.

    ``make_driver`` returns a stub whose ``run_turn`` yields a single TEXT event
    followed by DONE. When ``tool_events`` > 0, TOOL_START events are yielded first.
    ``run_turn_side_effect`` may be an async generator function used instead.
    """
    from jarn.agent.session import Event

    async def _fake_run_turn(prompt, *, resume: bool = False):
        if run_turn_side_effect is not None:
            async for item in run_turn_side_effect(prompt, resume=resume):
                yield item
            return
        for _ in range(tool_events):
            yield Event(kind=EventKind.TOOL_START, text="execute")
        yield Event(kind=EventKind.TEXT, text=text)
        yield Event(kind=EventKind.DONE)

    fake_driver = MagicMock()
    fake_driver.run_turn = _fake_run_turn

    import jarn.headless as headless_mod

    async def _fake_ensure_runtime(self):
        return SimpleNamespace(agent=object(), main_model_ref="m")

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


@pytest.mark.asyncio
async def test_run_headless_threads_extra_roots(tmp_path, monkeypatch, base_config):
    """_run_headless builds the Controller with add_dirs in scope (item F): the
    --add-dir grant must reach the engine/backend, not stop at the CLI."""
    import jarn.headless as headless_mod

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    captured: dict = {}
    orig_init = headless_mod.Controller.__init__

    def _spy_init(self, *a, **k):
        captured["extra_roots"] = k.get("extra_roots")
        orig_init(self, *a, **k)

    monkeypatch.setattr(headless_mod.Controller, "__init__", _spy_init)
    _stub_controller(monkeypatch)

    extra = tmp_path / "extra"
    extra.mkdir()
    await _run_headless("hi", base_config, tmp_path, add_dirs=[extra])

    assert captured["extra_roots"] == [extra], (
        "add_dirs must be passed to Controller(extra_roots=…) in headless"
    )


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
async def test_fail_closed_approver_raises_in_auto_edit_mode():
    """In auto-edit mode, danger-guard items still raise HeadlessRefusal."""
    approver = _make_fail_closed_approver(PermissionMode.AUTO_EDIT)
    req = ApprovalRequest(
        action=Action(kind=ActionKind.SHELL, target="rm -rf /", tool="execute"),
        result=PermissionResult(
            decision=Decision.ASK, reason="danger-guard: unconditionally blocked",
            dangerous=True, block_remember_always=True,
        ),
    )
    with pytest.raises(HeadlessRefusal) as exc_info:
        await approver(req)
    assert "execute" in str(exc_info.value)
    assert "danger-guard" in exc_info.value.reason


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

    assert code == EXIT_REFUSED
    err = capsys.readouterr().err
    assert "headless" in err.lower() or "gated" in err.lower() or "refused" in err.lower()


# ---------------------------------------------------------------------------
# Multi-turn cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_turn_cap(tmp_path, monkeypatch, base_config):
    """--max-turns bounds consecutive run_turn calls; stops when no tools fire."""
    from jarn.agent.session import Event

    calls: list[tuple[str, bool]] = []

    async def _multi_run_turn(prompt, *, resume: bool = False):
        calls.append((prompt, resume))
        if len(calls) == 1:
            yield Event(kind=EventKind.TOOL_START, text="read_file")
            yield Event(kind=EventKind.TEXT, text="step one")
            yield Event(kind=EventKind.DONE)
            return
        yield Event(kind=EventKind.TEXT, text="final answer")
        yield Event(kind=EventKind.DONE)

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _stub_controller(monkeypatch, run_turn_side_effect=_multi_run_turn)

    result = await _run_headless(
        "do the thing", base_config, tmp_path, max_turns=5,
    )

    assert len(calls) == 2
    assert calls[0] == ("do the thing", False)
    assert calls[1] == ("", True)
    assert result.turns == 2
    assert result.result == "step onefinal answer"
    assert result.tool_calls == 1


@pytest.mark.asyncio
async def test_multi_turn_cap_respects_limit(tmp_path, monkeypatch, base_config):
    """When max_turns=1, only one run_turn runs even if tools were used."""
    from jarn.agent.session import Event

    calls: list[int] = []

    async def _always_tools(prompt, *, resume: bool = False):
        calls.append(1)
        yield Event(kind=EventKind.TOOL_START, text="execute")
        yield Event(kind=EventKind.TEXT, text="partial")
        yield Event(kind=EventKind.DONE)

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _stub_controller(monkeypatch, run_turn_side_effect=_always_tools)

    result = await _run_headless(
        "keep going", base_config, tmp_path, max_turns=1,
    )

    assert len(calls) == 1
    assert result.turns == 1
    assert result.tool_calls == 1


# ---------------------------------------------------------------------------
# JSON tool_calls + structured errors
# ---------------------------------------------------------------------------


def test_json_includes_tool_calls(tmp_path, monkeypatch, base_config, capsys):
    """--json success payload includes tool_calls count."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _stub_controller(monkeypatch, text="done", tool_events=2)

    code = run_headless("do something", base_config, tmp_path, as_json=True)

    assert code == EXIT_SUCCESS
    data = json.loads(capsys.readouterr().out)
    assert data["tool_calls"] == 2
    assert data["turns"] == 1


def test_json_structured_error_on_refusal(tmp_path, monkeypatch, base_config, capsys):
    """--json emits {error: {kind, message}} on refusal."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))

    async def _raising_run_turn(prompt, *, resume: bool = False):
        raise HeadlessRefusal("execute", "ask mode requires confirmation")
        yield  # pragma: no cover - makes this an async generator

    fake_driver = MagicMock()
    fake_driver.run_turn = _raising_run_turn

    import jarn.headless as headless_mod

    async def _fake_ensure_runtime(self):
        pass

    async def _fake_aclose(self):
        pass

    monkeypatch.setattr(headless_mod.Controller, "ensure_runtime", _fake_ensure_runtime)
    monkeypatch.setattr(headless_mod.Controller, "make_driver", lambda self, a: fake_driver)
    monkeypatch.setattr(headless_mod.Controller, "validate", lambda self: (True, "ready"))
    monkeypatch.setattr(headless_mod.Controller, "enrich_turn_input", lambda self, t: t)
    monkeypatch.setattr(headless_mod.Controller, "aclose", _fake_aclose)

    code = run_headless("risky", base_config, tmp_path, as_json=True)

    assert code == EXIT_REFUSED
    data = json.loads(capsys.readouterr().out)
    assert data["error"]["kind"] == "refusal"
    assert "confirmation" in data["error"]["message"]


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------


def test_exit_codes(tmp_path, monkeypatch, base_config, capsys):
    """Distinct exit codes for success, generic error, refusal, budget, timeout."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    import jarn.headless as headless_mod
    from jarn.agent.session import Event
    from jarn.cost import BudgetExceeded

    async def _fake_ensure_runtime(self):
        pass

    async def _fake_aclose(self):
        pass

    monkeypatch.setattr(headless_mod.Controller, "ensure_runtime", _fake_ensure_runtime)
    monkeypatch.setattr(headless_mod.Controller, "aclose", _fake_aclose)
    monkeypatch.setattr(headless_mod.Controller, "enrich_turn_input", lambda self, t: t)

    # Success
    _stub_controller(monkeypatch, text="ok")
    assert run_headless("ok", base_config, tmp_path) == EXIT_SUCCESS

    # Generic error
    monkeypatch.setattr(
        headless_mod.Controller,
        "validate",
        lambda self: (False, "no key"),
    )
    assert run_headless("x", base_config, tmp_path) == EXIT_ERROR
    assert "no key" in capsys.readouterr().err

    # Refusal
    monkeypatch.setattr(headless_mod.Controller, "validate", lambda self: (True, "ready"))

    async def _refuse(prompt, *, resume: bool = False):
        raise HeadlessRefusal("write_file", "plan mode is read-only")
        yield  # pragma: no cover - makes this an async generator

    fake_driver = MagicMock()
    fake_driver.run_turn = _refuse
    monkeypatch.setattr(headless_mod.Controller, "make_driver", lambda self, a: fake_driver)

    assert run_headless("x", base_config, tmp_path) == EXIT_REFUSED

    # Budget hard-stop via ERROR event
    async def _budget_stop(prompt, *, resume: bool = False):
        yield Event(
            kind=EventKind.ERROR,
            text=str(BudgetExceeded(spent=1.5, limit=1.0)),
            data={"budget": True},
        )

    fake_driver.run_turn = _budget_stop
    assert run_headless("x", base_config, tmp_path) == EXIT_REFUSED

    # Timeout
    async def _timeout(prompt, *, resume: bool = False):
        yield Event(
            kind=EventKind.ERROR,
            text="Error: request timed out after 30 seconds",
            data={},
        )

    fake_driver.run_turn = _timeout
    assert run_headless("x", base_config, tmp_path) == EXIT_TIMEOUT


# ---------------------------------------------------------------------------
# Session resume (--resume-session)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_last(tmp_path, monkeypatch, base_config):
    """--resume-session last picks the most recent session and resumes its thread."""
    from jarn.agent.session import Event
    from jarn.memory.sessions import SessionInfo

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))

    resumed: list[str] = []
    run_calls: list[tuple[str, bool]] = []

    async def _resume_run_turn(prompt, *, resume: bool = False):
        run_calls.append((prompt, resume))
        yield Event(kind=EventKind.TEXT, text="continued")
        yield Event(kind=EventKind.DONE)

    fake_driver = MagicMock()
    fake_driver.run_turn = _resume_run_turn

    import jarn.headless as headless_mod

    async def _fake_ensure_runtime(self):
        self.sessions.list = lambda limit=30: [
            SessionInfo("thread-abc", "prior work", 1.0),
        ]

    async def _fake_aclose(self):
        pass

    def _resume_thread(self, thread_id: str) -> None:
        resumed.append(thread_id)

    monkeypatch.setattr(headless_mod.Controller, "ensure_runtime", _fake_ensure_runtime)
    monkeypatch.setattr(headless_mod.Controller, "resume_thread", _resume_thread)
    monkeypatch.setattr(headless_mod.Controller, "make_driver", lambda self, a: fake_driver)
    monkeypatch.setattr(headless_mod.Controller, "validate", lambda self: (True, "ready"))
    monkeypatch.setattr(headless_mod.Controller, "enrich_turn_input", lambda self, t: t)
    monkeypatch.setattr(headless_mod.Controller, "aclose", _fake_aclose)

    result = await _run_headless(
        "",
        base_config,
        tmp_path,
        resume_session="last",
    )

    assert resumed == ["thread-abc"]
    assert run_calls == [("", True)]
    assert result.result == "continued"


def test_budget_stop_exit_code(tmp_path, monkeypatch, base_config, capsys):
    """Budget hard-stop surfaces exit code 2 (alias for the budget branch in test_exit_codes)."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    import jarn.headless as headless_mod
    from jarn.agent.session import Event
    from jarn.cost import BudgetExceeded

    async def _fake_ensure_runtime(self):
        pass

    async def _fake_aclose(self):
        pass

    monkeypatch.setattr(headless_mod.Controller, "ensure_runtime", _fake_ensure_runtime)
    monkeypatch.setattr(headless_mod.Controller, "aclose", _fake_aclose)
    monkeypatch.setattr(headless_mod.Controller, "validate", lambda self: (True, "ready"))
    monkeypatch.setattr(headless_mod.Controller, "enrich_turn_input", lambda self, t: t)

    async def _budget_stop(prompt, *, resume: bool = False):
        yield Event(
            kind=EventKind.ERROR,
            text=str(BudgetExceeded(spent=1.5, limit=1.0)),
            data={"budget": True},
        )

    fake_driver = MagicMock()
    fake_driver.run_turn = _budget_stop
    monkeypatch.setattr(headless_mod.Controller, "make_driver", lambda self, a: fake_driver)

    assert run_headless("x", base_config, tmp_path) == EXIT_REFUSED

    code = run_headless("x", base_config, tmp_path, as_json=True)
    assert code == EXIT_REFUSED
    data = json.loads(capsys.readouterr().out)
    assert data["error"]["kind"] == "budget"


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

    def _fake_launch(
        *, resume: bool = False, profile_override: str | None = None,
        add_dirs: list | None = None,
    ) -> int:
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


# ---------------------------------------------------------------------------
# T-3-6: --output-schema structured output
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_output_schema_roundtrip(tmp_path, monkeypatch, base_config):
    """_run_headless with response_format passes it to Controller and returns structured result.

    The spy records the response_format stored on the Controller (which is then
    forwarded to build_runtime → create_deep_agent). The mocked agent's aget_state
    returns a state with structured_response so result.result is the parsed object.
    """
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))

    import jarn.headless as headless_mod
    from jarn.agent.session import Event

    schema_dict = {"type": "object", "properties": {"answer": {"type": "string"}}}
    response_format = {"type": "json_schema", "schema": schema_dict}
    structured_data = {"answer": "42"}

    captured_rf: list = []

    async def _fake_run_turn(prompt, *, resume=False):
        yield Event(kind=EventKind.TEXT, text="The answer is 42.")
        yield Event(kind=EventKind.DONE)

    fake_driver = MagicMock()
    fake_driver.run_turn = _fake_run_turn

    fake_state = SimpleNamespace(values={"structured_response": structured_data})
    fake_agent = MagicMock()
    fake_agent.aget_state = AsyncMock(return_value=fake_state)
    fake_runtime = SimpleNamespace(agent=fake_agent, main_model_ref="m")

    async def _fake_ensure_runtime(self):
        # Spy: record the response_format stored on the controller (proves the
        # plumbing from _run_headless → Controller → build_runtime is wired).
        captured_rf.append(self.response_format)
        self.runtime = fake_runtime
        return fake_runtime

    async def _fake_aclose(self):
        pass

    monkeypatch.setattr(headless_mod.Controller, "ensure_runtime", _fake_ensure_runtime)
    monkeypatch.setattr(headless_mod.Controller, "make_driver", lambda self, a: fake_driver)
    monkeypatch.setattr(headless_mod.Controller, "validate", lambda self: (True, "ready"))
    monkeypatch.setattr(headless_mod.Controller, "enrich_turn_input", lambda self, t: t)
    monkeypatch.setattr(headless_mod.Controller, "aclose", _fake_aclose)

    result = await _run_headless(
        "what is the answer?", base_config, tmp_path,
        response_format=response_format,
    )

    assert captured_rf == [response_format], (
        "response_format must flow from _run_headless → Controller (spy on self.response_format)"
    )
    assert result.result == structured_data, (
        "HeadlessResult.result must be the parsed structured object from state.structured_response"
    )


def test_schema_validation_failure_exit(tmp_path, monkeypatch, base_config, capsys):
    """When the agent doesn't produce a structured response, run_headless exits 1 with kind: 'schema'."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))

    import jarn.headless as headless_mod
    from jarn.agent.session import Event

    async def _fake_run_turn(prompt, *, resume=False):
        yield Event(kind=EventKind.TEXT, text="I couldn't produce structured output.")
        yield Event(kind=EventKind.DONE)

    fake_driver = MagicMock()
    fake_driver.run_turn = _fake_run_turn

    # structured_response is None — agent didn't satisfy the schema
    fake_state = SimpleNamespace(values={"structured_response": None})
    fake_agent = MagicMock()
    fake_agent.aget_state = AsyncMock(return_value=fake_state)
    fake_runtime = SimpleNamespace(agent=fake_agent, main_model_ref="m")

    async def _fake_ensure_runtime(self):
        self.runtime = fake_runtime
        return fake_runtime

    async def _fake_aclose(self):
        pass

    monkeypatch.setattr(headless_mod.Controller, "ensure_runtime", _fake_ensure_runtime)
    monkeypatch.setattr(headless_mod.Controller, "make_driver", lambda self, a: fake_driver)
    monkeypatch.setattr(headless_mod.Controller, "validate", lambda self: (True, "ready"))
    monkeypatch.setattr(headless_mod.Controller, "enrich_turn_input", lambda self, t: t)
    monkeypatch.setattr(headless_mod.Controller, "aclose", _fake_aclose)

    response_format = {"type": "json_schema", "schema": {"type": "object"}}

    code = run_headless(
        "what is the answer?", base_config, tmp_path,
        as_json=True,
        response_format=response_format,
    )

    assert code == EXIT_ERROR
    data = json.loads(capsys.readouterr().out)
    assert data["error"]["kind"] == "schema"
