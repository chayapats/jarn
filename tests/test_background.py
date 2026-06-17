"""F2: background processes — ProcessManager, tools, permission mapping, wiring."""

from __future__ import annotations

import time
from unittest.mock import patch

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

from jarn.agent.background import ProcessManager, build_background_tools
from jarn.agent.permissions_bridge import tool_to_action
from jarn.permissions import ActionKind


def _wait(mgr, pid, *, timeout=4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = mgr.status(pid)
        if st and not st["running"]:
            return st
        time.sleep(0.05)
    return mgr.status(pid)


# -- ProcessManager ----------------------------------------------------------

def test_start_runs_and_captures_output(tmp_path):
    mgr = ProcessManager()
    proc = mgr.start("echo hello-bg", cwd=str(tmp_path))
    st = _wait(mgr, proc.id)
    assert st is not None
    assert st["running"] is False
    assert st["exit_code"] == 0
    assert "hello-bg" in st["tail"]


def test_start_is_non_blocking(tmp_path):
    """start() returns immediately even for a long-running command."""
    mgr = ProcessManager()
    t0 = time.monotonic()
    proc = mgr.start("sleep 30", cwd=str(tmp_path))
    assert time.monotonic() - t0 < 1.0
    assert mgr.status(proc.id)["running"] is True
    assert mgr.kill(proc.id) is True


def test_kill_stops_process(tmp_path):
    mgr = ProcessManager()
    proc = mgr.start("sleep 30", cwd=str(tmp_path))
    assert mgr.kill(proc.id) is True
    st = _wait(mgr, proc.id)
    assert st["running"] is False


def test_list_and_unknown(tmp_path):
    mgr = ProcessManager()
    assert mgr.list() == []
    assert mgr.status("nope") is None
    assert mgr.kill("nope") is False
    proc = mgr.start("echo hi", cwd=str(tmp_path))
    ids = {p["id"] for p in mgr.list()}
    assert proc.id in ids


def test_shutdown_terminates_running(tmp_path):
    mgr = ProcessManager()
    proc = mgr.start("sleep 30", cwd=str(tmp_path))
    mgr.shutdown()
    st = _wait(mgr, proc.id)
    assert st["running"] is False


# -- tools -------------------------------------------------------------------

def test_tools_run_and_check(tmp_path):
    tools = {t.name: t for t in build_background_tools(tmp_path)}
    assert set(tools) == {
        "run_in_background", "check_background", "kill_background", "list_background"
    }
    out = tools["run_in_background"].invoke({"command": "echo tool-bg"})
    assert "started bg" in out
    pid = out.split()[1].rstrip(":")
    # poll check_background until it reports exited
    deadline = time.monotonic() + 4.0
    seen = ""
    while time.monotonic() < deadline:
        seen = tools["check_background"].invoke({"id": pid})
        if "exited" in seen:
            break
        time.sleep(0.05)
    assert "tool-bg" in seen


def test_check_unknown_tool(tmp_path):
    tools = {t.name: t for t in build_background_tools(tmp_path)}
    assert "no background process" in tools["check_background"].invoke({"id": "bgX"})


# -- permission mapping ------------------------------------------------------

def test_run_in_background_maps_to_shell():
    action = tool_to_action("run_in_background", {"command": "rm -rf /tmp/x"})
    assert action.kind is ActionKind.SHELL
    assert action.target == "rm -rf /tmp/x"


def test_control_tools_map_to_read():
    for name in ("check_background", "kill_background", "list_background"):
        assert tool_to_action(name, {"id": "bg1"}).kind is ActionKind.READ


# -- builder wiring ----------------------------------------------------------

def _capture_tools(cfg, tmp_path, *, patch_backend=False):
    captured: dict = {}

    def fake_cda(**kwargs):
        captured.update(kwargs)
        return object()

    fake = GenericFakeChatModel(messages=iter([]))
    from jarn.agent import builder

    patches = [
        patch("jarn.providers.models.ModelFactory.build", return_value=fake),
        patch("deepagents.create_deep_agent", side_effect=fake_cda),
    ]
    if patch_backend:
        patches.append(patch("jarn.agent.builder._make_backend", return_value=object()))
    import contextlib

    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        builder.build_runtime(cfg, project_root=tmp_path)
    return {getattr(t, "name", "") for t in (captured.get("tools") or [])}


def test_background_tools_registered_on_local(base_config, tmp_path):
    names = _capture_tools(base_config, tmp_path)
    assert "run_in_background" in names


def test_background_tools_absent_when_disabled(base_config, tmp_path):
    base_config.execution.background = False
    names = _capture_tools(base_config, tmp_path)
    assert "run_in_background" not in names


def test_background_tools_absent_on_docker(base_config, tmp_path):
    base_config.execution.backend = "docker"
    names = _capture_tools(base_config, tmp_path, patch_backend=True)
    assert "run_in_background" not in names
