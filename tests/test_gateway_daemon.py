"""Daemon supervisor: spawn/reap, handshake, lease, eviction, fail-loud death."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from jarn.gateway.daemon import (
    DaemonSupervisor,
    WorkerProtocolError,
)
from jarn.gateway.lease import RootLease, RootLeaseHeldError
from jarn.gateway.protocol import (
    SCHEMA_VERSION,
    EventFrame,
    StatusFrame,
    TurnFrame,
)

FAKE_WORKER = Path(__file__).resolve().parent / "gateway_fake_worker.py"


def _worker_cmd() -> list[str]:
    return [sys.executable, str(FAKE_WORKER)]


def _root(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    return root.resolve()


@pytest.fixture
def supervisor(tmp_path: Path):
    deaths: list = []
    frames: list = []

    def on_death(handle, code, thread_id):
        deaths.append((handle.root, code, thread_id))

    def on_outbound(handle, frame):
        frames.append((handle.root, frame))

    sup = DaemonSupervisor(
        worker_command=_worker_cmd(),
        idle_timeout_ms=1_000,
        on_worker_death=on_death,
        on_outbound=on_outbound,
        handshake_timeout_secs=5.0,
    )
    sup.deaths = deaths  # type: ignore[attr-defined]
    sup.frames = frames  # type: ignore[attr-defined]
    yield sup
    sup.shutdown()


def test_spawn_handshake_and_status(supervisor: DaemonSupervisor, tmp_path: Path):
    root = _root(tmp_path)
    handle = supervisor.ensure_worker(root)
    assert handle.alive
    assert handle.lease.held
    assert handle.status is not None
    assert handle.status.turn_in_flight is False
    # Same root returns the same live worker.
    assert supervisor.ensure_worker(root) is handle


def test_refuses_when_root_lease_held(supervisor: DaemonSupervisor, tmp_path: Path):
    root = _root(tmp_path)
    foreign = RootLease(root)
    foreign.acquire()
    try:
        with pytest.raises(RootLeaseHeldError):
            supervisor.ensure_worker(root)
    finally:
        foreign.release()


def test_handshake_schema_mismatch(tmp_path: Path):
    root = _root(tmp_path)
    deaths: list = []
    sup = DaemonSupervisor(
        worker_command=_worker_cmd(),
        env={"FAKE_WORKER_SCHEMA": str(SCHEMA_VERSION + 99)},
        on_worker_death=lambda h, c, t: deaths.append(c),
        handshake_timeout_secs=5.0,
    )
    try:
        with pytest.raises(WorkerProtocolError, match="schema_version"):
            sup.ensure_worker(root)
        assert root not in sup.workers()
    finally:
        sup.shutdown()


def test_send_turn_receives_done(supervisor: DaemonSupervisor, tmp_path: Path):
    root = _root(tmp_path)
    supervisor.ensure_worker(root)
    supervisor.send(root, TurnFrame(thread_id="thr-1", text="hello"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        kinds = [
            f.kind
            for r, f in supervisor.frames  # type: ignore[attr-defined]
            if r == root and isinstance(f, EventFrame)
        ]
        if "done" in kinds:
            break
        time.sleep(0.05)
    else:
        pytest.fail("never saw done event")
    handle = supervisor.get_worker(root)
    assert handle is not None
    assert handle.in_flight_thread_id is None


def test_worker_death_mid_turn_fail_loud_no_replay(tmp_path: Path):
    root = _root(tmp_path)
    deaths: list = []
    death_event = threading.Event()
    turns_seen = {"n": 0}

    def on_death(handle, code, thread_id):
        deaths.append((handle.root, code, thread_id))
        death_event.set()

    def on_outbound(handle, frame):
        if isinstance(frame, StatusFrame) and frame.turn_in_flight:
            turns_seen["n"] += 1

    sup = DaemonSupervisor(
        worker_command=_worker_cmd(),
        env={"FAKE_WORKER_DIE_ON_TURN": "1"},
        on_worker_death=on_death,
        on_outbound=on_outbound,
        handshake_timeout_secs=5.0,
    )
    try:
        handle = sup.ensure_worker(root)
        sup.send(root, TurnFrame(thread_id="thr-die", text="boom"))
        assert death_event.wait(timeout=5), "death hook not called"
        assert len(deaths) == 1
        assert deaths[0][0] == root
        assert deaths[0][1] == 1
        assert deaths[0][2] == "thr-die"
        # Fail-loud: worker dead, and supervisor did not auto-replay a second turn.
        assert handle.dead or not handle.alive
        assert turns_seen["n"] == 1
        # Recovery is explicit restart — not silent respawn+replay.
        fresh = sup.restart_worker(root)
        assert fresh.alive
        assert fresh is not handle
    finally:
        sup.shutdown()


def test_evict_idle_without_bg_or_turn(tmp_path: Path):
    root = _root(tmp_path)
    sup = DaemonSupervisor(
        worker_command=_worker_cmd(),
        idle_timeout_ms=50,
        env={"FAKE_WORKER_IDLE_MS": "100", "FAKE_WORKER_BG_JOBS": "0"},
        handshake_timeout_secs=5.0,
    )
    try:
        handle = sup.ensure_worker(root)
        assert handle.status is not None
        assert handle.is_evictable(idle_timeout_ms=50)
        reaped = sup.evict_idle()
        assert root in reaped
        assert sup.get_worker(root) is None
        # Lease released — a new holder can acquire.
        with RootLease(root) as lease:
            assert lease.held
    finally:
        sup.shutdown()


def test_parked_approvals_do_not_pin_worker(tmp_path: Path):
    root = _root(tmp_path)
    sup = DaemonSupervisor(
        worker_command=_worker_cmd(),
        idle_timeout_ms=10,
        env={
            "FAKE_WORKER_IDLE_MS": "999",
            "FAKE_WORKER_BG_JOBS": "0",
            "FAKE_WORKER_PARKED": "3",
        },
        handshake_timeout_secs=5.0,
    )
    try:
        handle = sup.ensure_worker(root)
        assert handle.status is not None
        assert handle.status.parked_approvals == 3
        assert handle.is_evictable(idle_timeout_ms=10)
        assert root in sup.evict_idle()
    finally:
        sup.shutdown()


def test_live_bg_jobs_block_eviction(tmp_path: Path):
    root = _root(tmp_path)
    sup = DaemonSupervisor(
        worker_command=_worker_cmd(),
        idle_timeout_ms=10,
        env={
            "FAKE_WORKER_IDLE_MS": "999",
            "FAKE_WORKER_BG_JOBS": "2",
        },
        handshake_timeout_secs=5.0,
    )
    try:
        handle = sup.ensure_worker(root)
        assert not handle.is_evictable(idle_timeout_ms=10)
        assert sup.evict_idle() == []
        assert handle.alive
    finally:
        sup.shutdown()


def test_turn_in_flight_blocks_eviction(tmp_path: Path):
    root = _root(tmp_path)
    frames: list = []
    sup = DaemonSupervisor(
        worker_command=_worker_cmd(),
        idle_timeout_ms=10,
        env={
            "FAKE_WORKER_IDLE_MS": "999",
            "FAKE_WORKER_TURN_HOLD_SECS": "0.4",
        },
        on_outbound=lambda h, f: frames.append(f),
        handshake_timeout_secs=5.0,
    )
    try:
        handle = sup.ensure_worker(root)
        sup.send(root, TurnFrame(thread_id="t", text="slow"))
        # Wait until status shows in flight (or local marker).
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not handle.turn_in_flight:
            time.sleep(0.02)
        assert handle.turn_in_flight
        assert not handle.is_evictable(idle_timeout_ms=10)
        assert sup.evict_idle() == []
        # Let turn finish.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and handle.turn_in_flight:
            time.sleep(0.05)
        assert not handle.turn_in_flight
    finally:
        sup.shutdown()


def test_reap_and_restart_api(supervisor: DaemonSupervisor, tmp_path: Path):
    root = _root(tmp_path)
    first = supervisor.ensure_worker(root)
    pid1 = first.pid
    supervisor.reap_worker(root)
    assert supervisor.get_worker(root) is None
    second = supervisor.restart_worker(root)
    assert second.alive
    assert second.pid != pid1


def test_different_roots_get_different_workers(
    supervisor: DaemonSupervisor, tmp_path: Path
):
    a = _root(tmp_path, "a")
    b = _root(tmp_path, "b")
    ha = supervisor.ensure_worker(a)
    hb = supervisor.ensure_worker(b)
    assert ha is not hb
    assert ha.pid != hb.pid
    assert ha.lease.held and hb.lease.held


def test_shutdown_reaps_all(supervisor: DaemonSupervisor, tmp_path: Path):
    a = _root(tmp_path, "a")
    b = _root(tmp_path, "b")
    supervisor.ensure_worker(a)
    supervisor.ensure_worker(b)
    supervisor.shutdown()
    assert supervisor.workers() == {}
    with RootLease(a):
        pass
    with RootLease(b):
        pass
