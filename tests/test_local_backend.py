"""CancellableLocalShellBackend — spawned commands are killable on cancel."""

from __future__ import annotations

import sys
import threading
import time

import pytest

from jarn.agent.events import ToolProgress
from jarn.agent.local_backend import CancellableLocalShellBackend, _TailTracker


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


# --- Live tool-output streaming (TOOL_PROGRESS: tail + heartbeat) -----------


def test_tail_tracker_rolls_tail_and_reports_elapsed():
    """on_output bounds the tail to N lines and reports elapsed from the clock."""
    now = [0.0]
    tt = _TailTracker(clock=lambda: now[0], start=0.0, tail_lines=3, command="build")
    p = None
    for i in range(5):
        now[0] = float(i)
        p = tt.on_output(f"line{i}\n")
    assert p is not None and not p.heartbeat
    assert p.elapsed == 4.0           # deterministic via the injected clock
    assert p.chunk == "line4\n"
    # Only the last 3 lines are retained in the rolling tail.
    assert "line0" not in p.tail and "line1" not in p.tail
    assert "line2" in p.tail and "line4" in p.tail


def test_tail_tracker_heartbeat_fires_only_when_quiet():
    """A heartbeat fires once the quiet window elapses, never twice in a row."""
    now = [0.0]
    tt = _TailTracker(clock=lambda: now[0], start=0.0, heartbeat_secs=5.0)
    tt.on_output("compiling…\n")      # resets the quiet-window anchor at t=0
    assert tt.maybe_heartbeat() is None     # no time has passed
    now[0] = 4.9
    assert tt.maybe_heartbeat() is None     # still inside the window
    now[0] = 5.0
    hb = tt.maybe_heartbeat()
    assert hb is not None and hb.heartbeat and hb.elapsed == 5.0
    assert "compiling…" in hb.tail          # tail preserved on a heartbeat
    assert hb.chunk == ""                    # heartbeat carries no new bytes
    assert tt.maybe_heartbeat() is None      # anchor advanced → no immediate repeat


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell timing")
def test_execute_streams_progress_before_completion(tmp_path):
    """With a progress_sink, a command that emits output over time surfaces at least
    one TOOL_PROGRESS before it finishes — and the final output still matches."""
    seen: list[ToolProgress] = []
    backend = CancellableLocalShellBackend(
        root_dir=str(tmp_path),
        virtual_mode=True,
        progress_sink=seen.append,
        progress_heartbeat_secs=0.05,
        progress_poll_secs=0.02,
    )
    res = backend.execute("for i in 1 2 3; do echo line$i; sleep 0.05; done")
    assert res.exit_code == 0
    assert "line1" in res.output and "line3" in res.output
    assert seen, "no progress events emitted"
    assert any(p.chunk and not p.heartbeat for p in seen), "no incremental output progress"
    assert any("line" in p.tail for p in seen)


def test_execute_without_sink_is_unchanged(tmp_path):
    """No progress_sink → the original blocking path; output/exit are byte-identical."""
    backend = CancellableLocalShellBackend(root_dir=str(tmp_path), virtual_mode=True)
    res = backend.execute("echo hello")
    assert res.output == "hello\n"
    assert res.exit_code == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
def test_execute_streaming_preserves_stderr_prefix(tmp_path):
    """The streaming path keeps the base backend's [stderr] prefixing + exit code."""
    seen: list[ToolProgress] = []
    backend = CancellableLocalShellBackend(
        root_dir=str(tmp_path), virtual_mode=True, progress_sink=seen.append,
    )
    res = backend.execute("echo out; echo err 1>&2; exit 2")
    assert "out" in res.output
    assert "[stderr] err" in res.output
    assert res.exit_code == 2
    assert "Exit code: 2" in res.output


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell / process groups")
def test_execute_streaming_enforces_timeout_after_pipe_eof(tmp_path):
    """Regression (#5): the streaming path stopped supervising as soon as BOTH
    stdout and stderr hit EOF, then waited on the process OUTSIDE the deadline. A
    process that closes both pipes but keeps running therefore bypassed its timeout
    (returned its real exit code long after the deadline). It must instead time out
    on the single monotonic deadline — exit 124 with the same message/shape as the
    blocking path — exactly as the no-sink path does."""
    seen: list[ToolProgress] = []
    backend = CancellableLocalShellBackend(
        root_dir=str(tmp_path),
        virtual_mode=True,
        progress_sink=seen.append,
        progress_poll_secs=0.02,
    )
    # Close both pipes immediately (exec 1>&- 2>&-), then keep running for 5s — far
    # past the 0.3s deadline. Before the fix this returned exit=7 at ~5s.
    start = time.monotonic()
    res = backend.execute("exec 1>&- 2>&-; sleep 5; exit 7", timeout=0.3)  # type: ignore[arg-type]
    elapsed = time.monotonic() - start

    assert res.exit_code == 124
    assert "timed out after 0.3 seconds" in res.output
    # Returned on the deadline, NOT after the 5s sleep, and never reported success.
    assert elapsed < 3.0, f"streaming ignored the deadline (elapsed={elapsed:.3f}s)"
    assert "Exit code: 7" not in res.output


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
def test_execute_streaming_pumps_bytes_partial_and_bad_utf8(tmp_path):
    """Regression (#7): the streaming reader was line-based + strict-text.

    (a) Output without a trailing newline was buffered until EOF instead of being
        streamed live (broke progress bars / partial prompts): here ``partial`` must
        surface as a chunk BEFORE ``done`` is ever written.
    (b) Invalid UTF-8 killed the daemon reader thread uncaught, so streaming silently
        returned exit=0 / '<no output>' (data loss + false success). It must instead
        be surfaced exactly like the blocking path — which raises UnicodeDecodeError —
        never a silent success."""
    # (a) live partial (no-newline) output.
    seen: list[ToolProgress] = []
    backend = CancellableLocalShellBackend(
        root_dir=str(tmp_path),
        virtual_mode=True,
        progress_sink=seen.append,
        progress_poll_secs=0.02,
    )
    res = backend.execute("printf partial; sleep 0.4; printf done")
    assert res.exit_code == 0
    assert res.output == "partialdone"  # final output byte-identical to blocking
    chunks = [p.chunk for p in seen if p.chunk]
    assert chunks, "no output chunks were streamed"
    # A chunk arrived that does NOT contain 'done' → the no-newline prefix was
    # emitted live (before the 0.4s sleep), not buffered to EOF. Before the fix the
    # only chunk was the EOF-time 'partialdone'.
    assert any("done" not in c for c in chunks), (
        f"no-newline output was not streamed live (only emitted at EOF): {chunks}"
    )

    # (b) invalid UTF-8 must be surfaced, matching the blocking path's raise.
    blocking = CancellableLocalShellBackend(root_dir=str(tmp_path), virtual_mode=True)
    with pytest.raises(UnicodeDecodeError):
        blocking.execute(r"printf '\377'")

    sink: list[ToolProgress] = []
    streaming = CancellableLocalShellBackend(
        root_dir=str(tmp_path), virtual_mode=True, progress_sink=sink.append,
    )
    with pytest.raises(UnicodeDecodeError):
        streaming.execute(r"printf '\377'")
