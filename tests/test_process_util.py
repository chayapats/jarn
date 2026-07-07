"""Unit tests for process-group termination helpers.

The Windows path is exercised here with mocks so it runs (and regresses) on any
OS — the original bug was a Windows-only hang in ``_wait_dead`` caused by reusing
the POSIX ``os.kill(pid, 0)`` liveness probe, which is destructive on Windows.
"""

from __future__ import annotations

import os
import signal
import subprocess

import pytest

from jarn.agent import process_util


def _boom(*args, **kwargs):
    raise AssertionError("POSIX liveness/spin path must not run on Windows")


def test_windows_taskkills_the_tree_and_never_spins(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    calls: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        calls["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(process_util.subprocess, "run", fake_run)
    # Guards: neither the busy-wait nor a per-pid os.kill probe may run here.
    monkeypatch.setattr(process_util, "_wait_dead", _boom)
    monkeypatch.setattr(process_util.os, "kill", _boom)

    process_util.terminate_process_group(4242, grace_secs=3)

    assert calls["cmd"] == ["taskkill", "/F", "/T", "/PID", "4242"]
    assert calls["timeout"] is not None  # bounded — cannot hang the caller


def test_windows_falls_back_to_single_kill_when_taskkill_missing(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")

    def missing(cmd, **kwargs):
        raise FileNotFoundError("taskkill")

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(process_util.subprocess, "run", missing)
    monkeypatch.setattr(process_util.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    process_util.terminate_process_group(4242, grace_secs=3)

    assert killed == [(4242, signal.SIGTERM)]


def test_windows_swallows_dead_pid(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")

    def missing(cmd, **kwargs):
        raise FileNotFoundError("taskkill")

    def dead(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(process_util.subprocess, "run", missing)
    monkeypatch.setattr(process_util.os, "kill", dead)

    # Must not propagate — best-effort terminate.
    process_util.terminate_process_group(4242, grace_secs=3)


@pytest.mark.skipif(
    os.name != "posix", reason="os.getpgid/os.killpg/signal.SIGKILL are POSIX-only"
)
def test_posix_immediate_sigkill_without_grace(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(process_util.os, "getpgid", lambda pid: pid)
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(process_util.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))

    process_util.terminate_process_group(4242)

    assert sent == [(4242, signal.SIGKILL)]
