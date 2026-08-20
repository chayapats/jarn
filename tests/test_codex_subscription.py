from __future__ import annotations

import asyncio
import copy
import os
import shlex
import sys
import time
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver

from jarn.agent.events import EventKind
from jarn.agent.runtime import build_runtime
from jarn.agent.session import SessionDriver
from jarn.config.schema import Config, ProviderConfig, ProviderType, RoutingConfig
from jarn.cost import CostTracker
from jarn.cost.pricing import cost_of
from jarn.permissions import PermissionEngine
from jarn.providers import ModelFactory
from jarn.providers.codex_subscription import (
    _BRIDGE_BASE_INSTRUCTIONS,
    CodexAppServer,
    CodexProtocolError,
    CodexSubscriptionAuthError,
    CodexSubscriptionChatModel,
    CodexTurnError,
    _looks_like_codex_sandbox_write_refusal,
    codex_subscription_account,
    normalize_codex_command,
)

FAKE_SERVER = Path(__file__).with_name("codex_fake_app_server.py")
FAKE_COMMAND = (sys.executable, str(FAKE_SERVER))
READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read one file",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}
WRITE_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write one file",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
}


def model(tmp_path: Path) -> CodexSubscriptionChatModel:
    return CodexSubscriptionChatModel(
        model_name="gpt-5.6-terra",
        codex_command=FAKE_COMMAND,
        working_directory=str(tmp_path),
        timeout_seconds=5,
    )


def test_command_normalization_is_argv_not_shell_split():
    assert normalize_codex_command("/Applications/Codex CLI/codex") == (
        "/Applications/Codex CLI/codex",
    )
    assert normalize_codex_command(["python", "fake.py"]) == ("python", "fake.py")


def test_account_read_reports_managed_chatgpt(tmp_path):
    account = codex_subscription_account(command=FAKE_COMMAND, cwd=tmp_path, timeout_seconds=5)
    assert account == {"type": "chatgpt", "planType": "plus"}


@pytest.mark.skipif(os.name == "nt", reason="POSIX loader-path contract")
def test_app_server_does_not_inherit_frozen_bundle_libraries(monkeypatch, tmp_path):
    """Codex/Node must not load shared libraries from J.A.R.N.'s _MEI dir."""

    from jarn.util import process_env

    wrapper = tmp_path / "codex-env-wrapper"
    wrapper.write_text(
        "#!/bin/sh\n"
        'test "${PYINSTALLER_RESET_ENVIRONMENT:-}" = 1 || exit 91\n'
        'test -z "${LD_LIBRARY_PATH:-}" || exit 92\n'
        'test -z "${LD_LIBRARY_PATH_ORIG+x}" || exit 93\n'
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE_SERVER))} \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEI-jarn")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "")
    monkeypatch.setattr(process_env.sys, "frozen", True, raising=False)
    monkeypatch.setattr(process_env.sys, "platform", "linux")

    account = codex_subscription_account(command=(str(wrapper),), cwd=tmp_path)

    assert account == {"type": "chatgpt", "planType": "plus"}
    assert os.environ["LD_LIBRARY_PATH"] == "/tmp/_MEI-jarn"
    assert os.environ["LD_LIBRARY_PATH_ORIG"] == ""


def test_wedged_app_server_is_reaped_within_cancellation_budget(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "hang_close")
    server = CodexAppServer(command=FAKE_COMMAND, cwd=tmp_path, timeout_seconds=5)
    server.__enter__()
    assert server.account() == {"type": "chatgpt", "planType": "plus"}
    proc = server._proc
    assert proc is not None and proc.poll() is None

    started = time.monotonic()
    server.close()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert proc.poll() is not None


@pytest.mark.asyncio
async def test_cancelled_model_turn_reaps_its_wedged_app_server_within_one_second(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "hang_turn")
    entered: list[object] = []
    original_enter = CodexAppServer.__enter__

    def capture(server):
        result = original_enter(server)
        entered.append(server)
        return result

    monkeypatch.setattr(CodexAppServer, "__enter__", capture)
    task = asyncio.create_task(model(tmp_path).ainvoke([HumanMessage(content="wait")]))
    for _ in range(100):
        if entered and entered[0]._proc is not None:  # type: ignore[attr-defined]
            break
        await asyncio.sleep(0.01)
    assert entered
    proc = entered[0]._proc  # type: ignore[attr-defined]
    assert proc is not None and proc.poll() is None

    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert proc.poll() is not None


def test_safe_model_mode_disables_inner_execution_features(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "require_safe_flags")
    with CodexAppServer(
        command=FAKE_COMMAND,
        cwd=tmp_path,
        timeout_seconds=5,
        safe_model_mode=True,
    ) as server:
        assert server.account() == {"type": "chatgpt", "planType": "plus"}


def test_bridge_instructions_allow_jarn_mutating_tools_despite_readonly_sandbox():
    text = _BRIDGE_BASE_INSTRUCTIONS
    lowered = text.casefold()
    compact = lowered.replace('"', "").replace("\\", "")
    assert "write_file" in lowered
    assert "edit_file" in lowered
    assert "execute" in lowered
    assert "tool_calls" in lowered
    assert "read-only" in lowered
    assert "never use kind=final" in compact
    assert "file operations" not in lowered


def test_sandbox_write_refusal_detector_matches_uat_prose_only():
    assert _looks_like_codex_sandbox_write_refusal(
        "Cannot call write_file because this session’s filesystem permission is read-only"
    )
    assert _looks_like_codex_sandbox_write_refusal(
        "Cannot call write_file because this session's filesystem permission is read-only"
    )
    assert not _looks_like_codex_sandbox_write_refusal(
        "README.md contains the install steps for a read-only review."
    )
    assert not _looks_like_codex_sandbox_write_refusal("")


def test_thread_and_turn_start_keep_readonly_sandbox(monkeypatch, tmp_path):
    captured: list[tuple[str, dict]] = []
    original = CodexAppServer.request

    def wrapped(self, method, params, **kwargs):
        if method in {"thread/start", "turn/start"}:
            captured.append((method, copy.deepcopy(params)))
        return original(self, method, params, **kwargs)

    monkeypatch.setattr(CodexAppServer, "request", wrapped)
    bound = model(tmp_path).bind_tools([WRITE_FILE_TOOL])
    bound.invoke([HumanMessage(content="write notes.txt")])

    thread = next(params for method, params in captured if method == "thread/start")
    turn = next(params for method, params in captured if method == "turn/start")
    assert thread["sandbox"] == "read-only"
    assert thread["approvalPolicy"] == "never"
    assert "write_file" in thread["baseInstructions"]
    assert "file operations" not in thread["baseInstructions"]
    assert "kind=tool_calls is allowed" in thread["developerInstructions"]
    assert turn["sandboxPolicy"] == {"type": "readOnly", "networkAccess": False}
    assert turn["approvalPolicy"] == "never"


def test_sandbox_readonly_prose_retries_once_as_jarn_write_tool(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "sandbox_refuse")
    bound = model(tmp_path).bind_tools([WRITE_FILE_TOOL])

    message = bound.invoke([HumanMessage(content="write notes.txt")])

    assert message.content == ""
    assert len(message.tool_calls) == 1
    assert message.tool_calls[0]["name"] == "write_file"
    assert message.tool_calls[0]["args"] == {"path": "notes.txt", "content": "ok"}


def test_sandbox_readonly_prose_is_surfaced_after_one_retry(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "sandbox_refuse_persist")
    captured: list[str] = []
    original = CodexAppServer.request

    def wrapped(self, method, params, **kwargs):
        if method == "turn/start":
            captured.append(method)
        return original(self, method, params, **kwargs)

    monkeypatch.setattr(CodexAppServer, "request", wrapped)
    bound = model(tmp_path).bind_tools([WRITE_FILE_TOOL])

    message = bound.invoke([HumanMessage(content="write notes.txt")])

    assert len(captured) == 2
    assert message.tool_calls == []
    assert "read-only" in str(message.content)
    assert "write_file" in str(message.content)


def test_sandbox_readonly_prose_is_not_retried_without_mutating_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "sandbox_refuse")
    captured: list[str] = []
    original = CodexAppServer.request

    def wrapped(self, method, params, **kwargs):
        if method == "turn/start":
            captured.append(method)
        return original(self, method, params, **kwargs)

    monkeypatch.setattr(CodexAppServer, "request", wrapped)
    bound = model(tmp_path).bind_tools([READ_FILE_TOOL])

    message = bound.invoke([HumanMessage(content="write notes.txt")])

    assert captured == ["turn/start"]
    assert message.tool_calls == []
    assert "write_file" in str(message.content)


def test_model_returns_final_text_and_subscription_usage(tmp_path):
    message = model(tmp_path).invoke([HumanMessage(content="hello")])

    assert message.content == "subscription ready"
    assert message.tool_calls == []
    assert message.usage_metadata == {
        "input_tokens": 123,
        "output_tokens": 7,
        "total_tokens": 130,
    }
    assert message.response_metadata["auth_mode"] == "chatgpt"
    assert message.response_metadata["plan_type"] == "plus"


def test_model_translates_structured_request_to_langchain_tool_call(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "tool")
    bound = model(tmp_path).bind_tools([READ_FILE_TOOL])

    first = bound.invoke([HumanMessage(content="read the readme")])
    assert first.content == ""
    assert len(first.tool_calls) == 1
    assert first.tool_calls[0]["name"] == "read_file"
    assert first.tool_calls[0]["args"] == {"path": "README.md"}

    final = bound.invoke(
        [
            HumanMessage(content="read the readme"),
            AIMessage(content="", tool_calls=first.tool_calls),
            ToolMessage(
                content="Jarn readme contents",
                name="read_file",
                tool_call_id=first.tool_calls[0]["id"],
            ),
        ]
    )
    assert final.content == "subscription ready"
    assert final.tool_calls == []


def test_model_rejects_api_key_auth_to_prevent_separate_billing(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "api_key")
    with pytest.raises(CodexSubscriptionAuthError, match="separate API billing"):
        model(tmp_path).invoke([HumanMessage(content="hello")])


def test_model_rejects_non_object_tool_arguments(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "bad_arguments")
    bound = model(tmp_path).bind_tools([READ_FILE_TOOL])
    with pytest.raises(CodexProtocolError, match="must be an object"):
        bound.invoke([HumanMessage(content="read")])


def test_model_surfaces_failed_turn(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "turn_error")
    with pytest.raises(CodexTurnError, match="synthetic turn failure"):
        model(tmp_path).invoke([HumanMessage(content="hello")])


def test_factory_builds_codex_subscription_without_api_key(tmp_path):
    cfg = Config(
        default_profile="codex_subscription",
        default_model="codex_subscription/gpt-5.6-terra",
        providers={
            "codex_subscription": ProviderConfig(
                type=ProviderType.CODEX_SUBSCRIPTION,
                extra={"codex_command": list(FAKE_COMMAND)},
            )
        },
        routing=RoutingConfig(main="codex_subscription/gpt-5.6-terra"),
    )
    built = ModelFactory(cfg, working_directory=tmp_path).build_main()

    assert isinstance(built, CodexSubscriptionChatModel)
    assert built.working_directory == str(tmp_path)
    assert built.codex_command == FAKE_COMMAND


def test_codex_model_reports_stable_provider_for_harness_profiles(tmp_path):
    from deepagents._models import get_model_identifier, get_model_provider

    built = model(tmp_path)
    assert get_model_provider(built) == "codex_subscription"
    assert get_model_identifier(built) == "gpt-5.6-terra"


def test_subscription_usage_is_zero_dollar_not_unpriced():
    assert cost_of("codex_subscription/gpt-5.6-terra", 100_000, 10_000) == 0.0


@pytest.mark.asyncio
async def test_deepagent_executes_codex_requested_tool_through_jarn_gate(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "tool")
    (tmp_path / "README.md").write_text("fixture readme", encoding="utf-8")
    cfg = Config(
        default_profile="codex_subscription",
        default_model="codex_subscription/gpt-5.6-terra",
        providers={
            "codex_subscription": ProviderConfig(
                type=ProviderType.CODEX_SUBSCRIPTION,
                extra={"codex_command": list(FAKE_COMMAND), "timeout_seconds": 5},
            )
        },
        routing=RoutingConfig(main="codex_subscription/gpt-5.6-terra"),
    )
    runtime = build_runtime(cfg, project_root=tmp_path, checkpointer=MemorySaver())
    driver = SessionDriver(
        agent=runtime.agent,
        engine=PermissionEngine(project_root=tmp_path),
        tracker=CostTracker(),
        thread_id="codex-fake-e2e",
        main_model_ref="codex_subscription/gpt-5.6-terra",
        known_model_refs=("codex_subscription/gpt-5.6-terra",),
    )

    events = [event async for event in driver.run_turn("Read README.md, then answer.")]

    assert [event.kind for event in events] == [
        EventKind.TOOL_START,
        EventKind.APPROVAL,
        EventKind.TOOL_END,
        EventKind.TEXT,
        EventKind.DONE,
    ]
    assert events[0].text == "read_file"
    assert events[1].text == "auto-allowed: read_file"
    assert events[3].text == "subscription ready"
    assert driver.tracker.total.cost_usd == 0.0
