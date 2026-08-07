"""Per-root gateway worker loop (T-WKR-1 / T-WKR-2)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from jarn.agent.events import ApprovalRequest, Event, EventKind
from jarn.gateway.approvals import (
    PendingApproval,
    PendingApprovalMap,
)
from jarn.gateway.protocol import (
    SCHEMA_VERSION,
    ApprovalAskFrame,
    ApprovalVerdictFrame,
    CancelFrame,
    ErrorFrame,
    EventFrame,
    HandshakeFrame,
    MediaRef,
    ShutdownFrame,
    StatusFrame,
    SteerFrame,
    TurnFrame,
    decode_outbound_line,
    encode_line,
)
from jarn.gateway.worker import (
    GatewayWorker,
    WorkerOrderingError,
    event_to_frame,
    redact_outbound_frame,
    redact_outbound_value,
)
from jarn.permissions import (
    Action,
    ActionKind,
    Decision,
    PermissionResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Stdin:
    def __init__(self) -> None:
        self._q: list[str] = []
        self._closed = False

    def push(self, line: str) -> None:
        if not line.endswith("\n"):
            line += "\n"
        self._q.append(line)

    def close(self) -> None:
        self._closed = True

    def readline(self) -> str:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self._q:
                return self._q.pop(0)
            if self._closed:
                return ""
            time.sleep(0.005)
        return ""


class _Stdout:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self._buf = ""

    def write(self, data: str) -> int:
        self._buf += data
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.lines.append(line + "\n")
        return len(data)

    def flush(self) -> None:
        pass

    def frames(self) -> list[Any]:
        return [decode_outbound_line(line) for line in self.lines]


def _ask(tool: str = "execute", target: str = "rm -rf /tmp/x") -> ApprovalRequest:
    return ApprovalRequest(
        action=Action(ActionKind.SHELL, target=target, tool=tool),
        result=PermissionResult(Decision.ASK, "needs confirmation", dangerous=True),
        description="run command",
        args={"command": target, "token": "sk-proj-ABCDEFGH1234567890WXYZ"},
    )


def _stub_controller(
    *,
    run_turn=None,
    resume_pending=None,
    root: Path | None = None,
):
    """Minimal Controller double for GatewayWorker tests."""

    class _Driver:
        def __init__(self, approver):
            self.approver = approver
            self.thread_id = "thr"
            self.transcript = None

        async def run_turn(self, text, **kwargs):
            if run_turn is not None:
                async for ev in run_turn(self, text, **kwargs):
                    yield ev
                return
            yield Event(EventKind.TEXT, text=f"echo:{text}")
            yield Event(EventKind.DONE)

        async def resume_pending_approval(self, *args, **kwargs):
            if resume_pending is not None:
                async for ev in resume_pending(self, *args, **kwargs):
                    yield ev
                return
            reply = await self.approver(_ask())
            yield Event(
                EventKind.APPROVAL,
                text="approved" if reply.approved else "rejected",
            )
            yield Event(EventKind.DONE)

    ctrl = MagicMock()
    ctrl.project_root = root
    ctrl.thread_id = "initial"
    ctrl._steer_slot = None
    ctrl._turn_task = None
    ctrl.inline_images_disabled = False
    ctrl.config = SimpleNamespace(
        execution=SimpleNamespace(inline_images="off"),
    )

    async def ensure_runtime():
        ctrl.runtime = SimpleNamespace(agent=object(), main_model_ref="m")

    async def aclose():
        pass

    async def abort():
        task = ctrl._turn_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return SimpleNamespace(message="aborted")

    def resume_thread(tid: str) -> None:
        ctrl.thread_id = tid

    def bind_turn_task(task) -> None:
        ctrl._turn_task = task

    def make_driver(approver):
        d = _Driver(approver)
        d.thread_id = ctrl.thread_id
        ctrl._active_driver = d
        return d

    ctrl.ensure_runtime = ensure_runtime
    ctrl.aclose = aclose
    ctrl.abort = abort
    ctrl.resume_thread = resume_thread
    ctrl.bind_turn_task = bind_turn_task
    ctrl.make_driver = make_driver
    ctrl.enrich_turn_input = lambda text: text
    ctrl.reset_model_rotation = lambda: None
    ctrl.rotate_to_fallback = lambda: None
    ctrl.rotate_to_keyed_fallback = lambda: None
    return ctrl


def _frames_of(stdout: _Stdout, cls: type) -> list[Any]:
    return [f for f in stdout.frames() if isinstance(f, cls)]


# ---------------------------------------------------------------------------
# Redaction (T-WKR-2)
# ---------------------------------------------------------------------------


def test_redact_outbound_value_scrubs_secrets():
    out = redact_outbound_value(
        {"cmd": "export OPENAI_API_KEY=sk-proj-ABCDEFGH1234567890WXYZ"}
    )
    assert "sk-proj-ABCDEFGH1234567890WXYZ" not in str(out)
    assert "OPENAI_API_KEY" in str(out) or "REDACTED" in str(out).upper() or "sk-" in str(out)


def test_redact_event_frame_args_via_sanitize():
    frame = EventFrame(
        thread_id="t",
        kind="tool_start",
        text="execute",
        data={"args": {"command": "echo secret", "body": "PASSWORD=hunter2"}},
    )
    safe = redact_outbound_frame(frame)
    assert "hunter2" not in str(safe.data)
    assert "PASSWORD" in str(safe.data) or safe.data["args"].get("body") != "PASSWORD=hunter2"


def test_redact_approval_ask_args():
    ask = ApprovalAskFrame(
        token="tok",
        thread_id="t",
        action="execute",
        target="x",
        args={"command": "OPENAI_API_KEY=sk-proj-ABCDEFGH1234567890WXYZ"},
    )
    safe = redact_outbound_frame(ask)
    assert "sk-proj-ABCDEFGH1234567890WXYZ" not in str(safe.args)


def test_event_to_frame_maps_kind():
    ev = Event(EventKind.TEXT, text="hi", data={"a": 1})
    frame = event_to_frame(ev, thread_id="thr")
    assert frame.kind == "text"
    assert frame.thread_id == "thr"
    assert frame.text == "hi"


# ---------------------------------------------------------------------------
# Handshake / status / loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handshake_emits_status_with_eviction_fields(
    isolated_home: Path, tmp_path: Path
):
    root = tmp_path / "proj"
    root.mkdir()
    stdin, stdout = _Stdin(), _Stdout()
    ctrl = _stub_controller(root=root)
    worker = GatewayWorker(
        root=root,
        controller=ctrl,
        stdin=stdin,
        stdout=stdout,
        heartbeat_interval=60.0,
        approval_store=PendingApprovalMap(isolated_home / "pending.json"),
        clock=lambda: 100.0,
    )
    task = asyncio.create_task(worker.run())
    stdin.push(encode_line(HandshakeFrame(schema_version=SCHEMA_VERSION)))
    await asyncio.sleep(0.15)
    statuses = _frames_of(stdout, StatusFrame)
    assert statuses, "expected status after handshake"
    st = statuses[0]
    assert st.turn_in_flight is False
    assert st.live_bg_jobs == 0
    assert st.idle_ms >= 0
    assert st.parked_approvals == 0
    stdin.push(encode_line(ShutdownFrame()))
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_schema_mismatch_emits_error_and_exits(
    isolated_home: Path, tmp_path: Path
):
    root = tmp_path / "proj"
    root.mkdir()
    stdin, stdout = _Stdin(), _Stdout()
    worker = GatewayWorker(
        root=root,
        controller=_stub_controller(root=root),
        stdin=stdin,
        stdout=stdout,
        heartbeat_interval=60.0,
        approval_store=PendingApprovalMap(isolated_home / "pending.json"),
    )
    task = asyncio.create_task(worker.run())
    stdin.push(encode_line(HandshakeFrame(schema_version=SCHEMA_VERSION + 9)))
    code = await asyncio.wait_for(task, timeout=2)
    assert code == 2
    errs = _frames_of(stdout, ErrorFrame)
    assert errs
    assert errs[0].code == "unsupported_schema_version"


@pytest.mark.asyncio
async def test_turn_before_handshake_errors(isolated_home: Path, tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    stdin, stdout = _Stdin(), _Stdout()
    worker = GatewayWorker(
        root=root,
        controller=_stub_controller(root=root),
        stdin=stdin,
        stdout=stdout,
        heartbeat_interval=60.0,
        approval_store=PendingApprovalMap(isolated_home / "pending.json"),
    )
    task = asyncio.create_task(worker.run())
    stdin.push(encode_line(TurnFrame(thread_id="t", text="hi")))
    code = await asyncio.wait_for(task, timeout=2)
    assert code == 2
    errs = _frames_of(stdout, ErrorFrame)
    assert any(e.code == "handshake_required" for e in errs)


@pytest.mark.asyncio
async def test_turn_emits_events(isolated_home: Path, tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    stdin, stdout = _Stdin(), _Stdout()
    worker = GatewayWorker(
        root=root,
        controller=_stub_controller(root=root),
        stdin=stdin,
        stdout=stdout,
        heartbeat_interval=60.0,
        approval_store=PendingApprovalMap(isolated_home / "pending.json"),
    )
    task = asyncio.create_task(worker.run())
    stdin.push(encode_line(HandshakeFrame(schema_version=SCHEMA_VERSION)))
    await asyncio.sleep(0.05)
    stdin.push(
        encode_line(
            TurnFrame(
                thread_id="thr-1",
                text="hello",
                media=[MediaRef(path="/tmp/a.png", mime="image/png", modality="image")],
            )
        )
    )
    # Wait for done
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if any(
            isinstance(f, EventFrame) and f.kind == "done" for f in stdout.frames()
        ):
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail(f"no done event; got {stdout.frames()!r}")
    events = _frames_of(stdout, EventFrame)
    assert any(e.kind == "text" and "hello" in e.text for e in events)
    assert any(e.kind == "done" and e.thread_id == "thr-1" for e in events)
    stdin.push(encode_line(ShutdownFrame()))
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_park_emits_approval_ask(isolated_home: Path, tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    store = PendingApprovalMap(isolated_home / "pending.json")

    async def run_turn_park(driver, text, **kwargs):
        await driver.approver(_ask())
        yield Event(EventKind.DONE)  # pragma: no cover

    stdin, stdout = _Stdin(), _Stdout()
    worker = GatewayWorker(
        root=root,
        controller=_stub_controller(root=root, run_turn=run_turn_park),
        stdin=stdin,
        stdout=stdout,
        heartbeat_interval=60.0,
        approval_store=store,
        chat_id=42,
    )
    task = asyncio.create_task(worker.run())
    stdin.push(encode_line(HandshakeFrame(schema_version=SCHEMA_VERSION)))
    await asyncio.sleep(0.05)
    stdin.push(encode_line(TurnFrame(thread_id="thr-park", text="do it")))
    deadline = time.monotonic() + 2
    asks: list[ApprovalAskFrame] = []
    while time.monotonic() < deadline:
        asks = _frames_of(stdout, ApprovalAskFrame)
        if asks:
            break
        await asyncio.sleep(0.02)
    assert asks, f"expected approval_ask; got {stdout.frames()!r}"
    ask = asks[0]
    assert ask.thread_id == "thr-park"
    assert ask.action == "execute"
    assert ask.dangerous is True
    assert "sk-proj-ABCDEFGH1234567890WXYZ" not in str(ask.args)
    assert store.get(ask.token) is not None
    # Parked must NOT keep turn_in_flight true after release.
    await asyncio.sleep(0.1)
    assert worker._turn_in_flight() is False
    stdin.push(encode_line(ShutdownFrame()))
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_approval_verdict_resumes(isolated_home: Path, tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    store = PendingApprovalMap(isolated_home / "pending.json")
    store.put(
        PendingApproval(
            token="tok-resume",
            root=str(root.resolve()),
            thread_id="thr-r",
            interrupt_id="intr",
            chat_id=1,
        )
    )
    stdin, stdout = _Stdin(), _Stdout()
    worker = GatewayWorker(
        root=root,
        controller=_stub_controller(root=root),
        stdin=stdin,
        stdout=stdout,
        heartbeat_interval=60.0,
        approval_store=store,
    )
    task = asyncio.create_task(worker.run())
    stdin.push(encode_line(HandshakeFrame(schema_version=SCHEMA_VERSION)))
    await asyncio.sleep(0.05)
    stdin.push(
        encode_line(
            ApprovalVerdictFrame(token="tok-resume", approved=True, scope="once")
        )
    )
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if any(
            isinstance(f, EventFrame) and f.kind == "done" for f in stdout.frames()
        ):
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail(f"resume did not complete; got {stdout.frames()!r}")
    assert store.get("tok-resume") is None
    stdin.push(encode_line(ShutdownFrame()))
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_steer_sets_controller_slot(isolated_home: Path, tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    gate = asyncio.Event()

    async def run_turn_slow(driver, text, **kwargs):
        yield Event(EventKind.TEXT, text="working")
        await gate.wait()
        yield Event(EventKind.DONE)

    stdin, stdout = _Stdin(), _Stdout()
    ctrl = _stub_controller(root=root, run_turn=run_turn_slow)
    worker = GatewayWorker(
        root=root,
        controller=ctrl,
        stdin=stdin,
        stdout=stdout,
        heartbeat_interval=60.0,
        approval_store=PendingApprovalMap(isolated_home / "pending.json"),
    )
    task = asyncio.create_task(worker.run())
    stdin.push(encode_line(HandshakeFrame(schema_version=SCHEMA_VERSION)))
    await asyncio.sleep(0.05)
    stdin.push(encode_line(TurnFrame(thread_id="thr-s", text="go")))
    # Wait until turn is in flight
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not worker._turn_in_flight():
        await asyncio.sleep(0.02)
    stdin.push(encode_line(SteerFrame(thread_id="thr-s", text="prefer pytest")))
    await asyncio.sleep(0.1)
    assert ctrl._steer_slot == "prefer pytest"
    gate.set()
    await asyncio.sleep(0.1)
    stdin.push(encode_line(ShutdownFrame()))
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_cancel_aborts_in_flight_turn(isolated_home: Path, tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    started = asyncio.Event()

    async def run_turn_block(driver, text, **kwargs):
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise
        yield Event(EventKind.DONE)  # pragma: no cover

    stdin, stdout = _Stdin(), _Stdout()
    worker = GatewayWorker(
        root=root,
        controller=_stub_controller(root=root, run_turn=run_turn_block),
        stdin=stdin,
        stdout=stdout,
        heartbeat_interval=60.0,
        approval_store=PendingApprovalMap(isolated_home / "pending.json"),
    )
    task = asyncio.create_task(worker.run())
    stdin.push(encode_line(HandshakeFrame(schema_version=SCHEMA_VERSION)))
    await asyncio.sleep(0.05)
    stdin.push(encode_line(TurnFrame(thread_id="thr-c", text="long")))
    await asyncio.wait_for(started.wait(), timeout=2)
    stdin.push(encode_line(CancelFrame(thread_id="thr-c")))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if any(
            isinstance(f, ErrorFrame) and f.code == "cancelled" for f in stdout.frames()
        ):
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail(f"expected cancelled error; got {stdout.frames()!r}")
    stdin.push(encode_line(ShutdownFrame()))
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_status_parked_does_not_imply_busy(
    isolated_home: Path, tmp_path: Path
):
    """Eviction fields: parked_approvals > 0 with turn_in_flight False (#37)."""
    root = tmp_path / "proj"
    root.mkdir()
    store = PendingApprovalMap(isolated_home / "pending.json")
    store.put(
        PendingApproval(
            token="p1",
            root=str(root.resolve()),
            thread_id="t",
            interrupt_id="i",
            chat_id=1,
        )
    )
    now = {"t": 50.0}
    worker = GatewayWorker(
        root=root,
        controller=_stub_controller(root=root),
        stdin=_Stdin(),
        stdout=_Stdout(),
        heartbeat_interval=60.0,
        approval_store=store,
        clock=lambda: now["t"],
    )
    worker._handshaken = True
    worker._last_activity = 40.0
    st = worker.status_frame()
    assert st.turn_in_flight is False
    assert st.parked_approvals == 1
    assert st.idle_ms == 10_000


@pytest.mark.asyncio
async def test_busy_second_turn_rejected(isolated_home: Path, tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    gate = asyncio.Event()

    async def run_turn_slow(driver, text, **kwargs):
        await gate.wait()
        yield Event(EventKind.DONE)

    stdin, stdout = _Stdin(), _Stdout()
    worker = GatewayWorker(
        root=root,
        controller=_stub_controller(root=root, run_turn=run_turn_slow),
        stdin=stdin,
        stdout=stdout,
        heartbeat_interval=60.0,
        approval_store=PendingApprovalMap(isolated_home / "pending.json"),
    )
    task = asyncio.create_task(worker.run())
    stdin.push(encode_line(HandshakeFrame(schema_version=SCHEMA_VERSION)))
    await asyncio.sleep(0.05)
    stdin.push(encode_line(TurnFrame(thread_id="t1", text="a")))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not worker._turn_in_flight():
        await asyncio.sleep(0.02)
    stdin.push(encode_line(TurnFrame(thread_id="t2", text="b")))
    await asyncio.sleep(0.1)
    errs = _frames_of(stdout, ErrorFrame)
    assert any(e.code == "busy" for e in errs)
    gate.set()
    stdin.push(encode_line(ShutdownFrame()))
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_emit_redacts_before_write(isolated_home: Path, tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    stdout = _Stdout()
    worker = GatewayWorker(
        root=root,
        controller=_stub_controller(root=root),
        stdin=_Stdin(),
        stdout=stdout,
        heartbeat_interval=60.0,
        approval_store=PendingApprovalMap(isolated_home / "pending.json"),
    )
    worker.emit(
        EventFrame(
            thread_id="t",
            kind="text",
            text="key sk-proj-ABCDEFGH1234567890WXYZ here",
            data={},
        )
    )
    assert "sk-proj-ABCDEFGH1234567890WXYZ" not in stdout.lines[0]


def test_worker_ordering_error_is_protocol_error():
    assert issubclass(WorkerOrderingError, Exception)
