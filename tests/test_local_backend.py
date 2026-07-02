"""CancellableLocalShellBackend — spawned commands are killable on cancel."""

from __future__ import annotations

import sys
import threading
import time

import pytest

from jarn.agent.local_backend import CancellableLocalShellBackend


def test_execute_runs_and_reports_output(tmp_path):
    backend = CancellableLocalShellBackend(root_dir=str(tmp_path), virtual_mode=True)
    res = backend.execute("echo hello")
    assert "hello" in res.output
    assert res.exit_code == 0


def test_execute_reports_nonzero_exit(tmp_path):
    backend = CancellableLocalShellBackend(root_dir=str(tmp_path), virtual_mode=True)
    if sys.platform == "win32":
        res = backend.execute(f'"{sys.executable}" -c "import sys; sys.exit(3)"')
    else:
        res = backend.execute("sh -c 'exit 3'")
    assert res.exit_code == 3


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_terminate_all_kills_spawned_process_tree(tmp_path):
    """A long command, once terminated, must not finish its side effect.

    Regression for: Esc/Ctrl+C only cancelled the asyncio task, leaving the
    spawned shell (and the file it would create) running on the host."""
    backend = CancellableLocalShellBackend(root_dir=str(tmp_path), virtual_mode=True)
    marker = tmp_path / "survived.txt"
    cmd = f"sleep 5; echo survived > {marker}"

    result: dict = {}

    def run():
        result["res"] = backend.execute(cmd, timeout=30)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    # Wait until the process is registered as live, then kill it mid-sleep.
    for _ in range(100):
        if backend._live:
            break
        time.sleep(0.01)
    killed = backend.terminate_all()
    t.join(timeout=5)

    assert killed == 1
    assert not t.is_alive()           # execute returned promptly after the kill
    time.sleep(0.2)
    assert not marker.exists()         # the `echo > file` side effect never ran
