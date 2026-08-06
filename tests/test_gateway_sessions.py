"""Session routing: (chat_id, root)→thread_id, /repo, queue+stop, /new."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from jarn.config.schema import GatewayRepo
from jarn.gateway.daemon import DaemonSupervisor
from jarn.gateway.lease import RootLease
from jarn.gateway.protocol import EventFrame
from jarn.gateway.sessions import (
    QUEUED_NOTICE,
    ForbiddenRootError,
    RootBusyLeaseError,
    SessionRouter,
    UnknownRepoError,
    validate_gateway_root,
)

FAKE_WORKER = Path(__file__).resolve().parent / "gateway_fake_worker.py"


def _worker_cmd() -> list[str]:
    return [sys.executable, str(FAKE_WORKER)]


@pytest.fixture
def personal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Personal root under a temp JARN_HOME (avoid ``git init`` in the sandbox)."""
    jarn_home = tmp_path / "home"
    root = jarn_home / "personal"
    root.mkdir(parents=True)
    (root / ".git").mkdir()
    monkeypatch.setenv("JARN_HOME", str(jarn_home))
    return root.resolve()


@pytest.fixture
def allowlisted_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "allowlisted"
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo.resolve()


@pytest.fixture
def router(personal: Path, allowlisted_repo: Path):
    notices: list[tuple[int, str]] = []
    events: list = []
    deaths: list = []
    done = threading.Event()

    def on_notice(chat_id: int, text: str) -> None:
        notices.append((chat_id, text))

    def on_event(chat_id: int, root: Path, frame) -> None:
        events.append((chat_id, root, frame))
        if isinstance(frame, EventFrame) and frame.kind == "done":
            done.set()

    def on_death(chat_id: int, root: Path, err) -> None:
        deaths.append((chat_id, root, err))

    sup = DaemonSupervisor(
        worker_command=_worker_cmd(),
        handshake_timeout_secs=5.0,
        env={"FAKE_WORKER_TURN_HOLD_SECS": "0.15"},
    )
    r = SessionRouter(
        sup,
        repos=[GatewayRepo(path=str(allowlisted_repo), name="app")],
        personal_root=personal,
        on_notice=on_notice,
        on_event=on_event,
        on_worker_death=on_death,
    )
    r.notices = notices  # type: ignore[attr-defined]
    r.events = events  # type: ignore[attr-defined]
    r.deaths = deaths  # type: ignore[attr-defined]
    r.done = done  # type: ignore[attr-defined]
    r.supervisor = sup  # type: ignore[attr-defined]
    yield r
    sup.shutdown()


def test_default_root_is_personal(router: SessionRouter, personal: Path):
    assert router.active_root(42) == personal
    tid = router.thread_id_for(42)
    assert isinstance(tid, str) and len(tid) == 32
    assert router.thread_id_for(42) == tid


def test_repo_switch_and_personal_return(
    router: SessionRouter, personal: Path, allowlisted_repo: Path
):
    root = router.cmd_repo(7, "app")
    assert root == allowlisted_repo
    assert router.active_root(7) == allowlisted_repo
    tid_repo = router.thread_id_for(7)
    back = router.cmd_repo(7, "personal")
    assert back == personal
    tid_personal = router.thread_id_for(7)
    assert tid_personal != tid_repo
    # Switching back to repo restores the prior thread id for that root.
    router.cmd_repo(7, str(allowlisted_repo))
    assert router.thread_id_for(7) == tid_repo


def test_repo_unknown_refused(router: SessionRouter, tmp_path: Path):
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(UnknownRepoError):
        router.cmd_repo(1, str(other))


def test_repo_home_hard_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "fake-home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    with pytest.raises(ForbiddenRootError, match=r"\$HOME"):
        validate_gateway_root(home)


def test_repo_global_home_hard_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from jarn.config import paths

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    with pytest.raises(ForbiddenRootError, match="global home"):
        validate_gateway_root(paths.global_home())


def test_new_mints_thread_id(router: SessionRouter):
    a = router.thread_id_for(9)
    b = router.cmd_new(9)
    assert b != a
    assert router.thread_id_for(9) == b


def test_submit_turn_routes_to_worker(router: SessionRouter, personal: Path):
    tid = router.submit_turn(11, "ping")
    assert router.done.wait(timeout=5)  # type: ignore[attr-defined]
    assert any(
        isinstance(f, EventFrame) and f.kind == "done" and f.thread_id == tid
        for _, _, f in router.events  # type: ignore[attr-defined]
    )
    handle = router.supervisor.get_worker(personal)  # type: ignore[attr-defined]
    assert handle is not None


def test_busy_queues_with_notice_and_stop(personal: Path, allowlisted_repo: Path):
    notices: list[tuple[int, str]] = []

    def on_notice(chat_id: int, text: str) -> None:
        notices.append((chat_id, text))

    sup = DaemonSupervisor(
        worker_command=_worker_cmd(),
        handshake_timeout_secs=5.0,
        env={"FAKE_WORKER_TURN_HOLD_SECS": "0.35"},
    )
    r = SessionRouter(
        sup,
        repos=[GatewayRepo(path=str(allowlisted_repo), name="app")],
        personal_root=personal,
        on_notice=on_notice,
    )
    try:
        tid1 = r.submit_turn(3, "first")
        tid2 = r.submit_turn(3, "second")
        assert tid1 == tid2
        assert any(QUEUED_NOTICE in text for _, text in notices)
        assert r.queue_depth(personal) == 1

        # /stop cancels in-flight and drops this chat's queue.
        assert r.cmd_stop(3) is True
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and r.queue_depth(personal) > 0:
            time.sleep(0.05)
        assert r.queue_depth(personal) == 0
    finally:
        sup.shutdown()


def test_queue_drains_after_turn_completes(personal: Path, allowlisted_repo: Path):
    done_texts: list[str] = []
    all_done = threading.Event()

    def on_event(chat_id: int, root: Path, frame) -> None:
        if isinstance(frame, EventFrame) and frame.kind == "done":
            done_texts.append(frame.text)
            if len(done_texts) >= 2:
                all_done.set()

    sup = DaemonSupervisor(
        worker_command=_worker_cmd(),
        handshake_timeout_secs=5.0,
        env={"FAKE_WORKER_TURN_HOLD_SECS": "0.1"},
    )
    r = SessionRouter(
        sup,
        repos=[GatewayRepo(path=str(allowlisted_repo), name="app")],
        personal_root=personal,
        on_event=on_event,
        on_notice=lambda *_: None,
    )
    try:
        r.submit_turn(5, "one")
        r.submit_turn(5, "two")
        assert all_done.wait(timeout=5)
        assert done_texts == ["echo:one", "echo:two"]
    finally:
        sup.shutdown()


def test_lease_held_refuses_not_queues(router: SessionRouter, personal: Path):
    foreign = RootLease(personal)
    foreign.acquire()
    try:
        with pytest.raises(RootBusyLeaseError):
            router.submit_turn(99, "hello")
        # Refusal notice, not a queue entry.
        assert router.queue_depth(personal) == 0
        assert any("in use" in text for _, text in router.notices)  # type: ignore[attr-defined]
    finally:
        foreign.release()


def test_worker_death_notifies_no_auto_replay(personal: Path, allowlisted_repo: Path):
    notices: list[str] = []
    deaths: list = []
    death_event = threading.Event()
    done_texts: list[str] = []

    def on_death(chat_id, root, err):
        deaths.append((chat_id, root, err))
        death_event.set()

    def on_event(chat_id, root, frame):
        if isinstance(frame, EventFrame) and frame.kind == "done":
            done_texts.append(frame.text)

    sup = DaemonSupervisor(
        worker_command=_worker_cmd(),
        handshake_timeout_secs=5.0,
        env={"FAKE_WORKER_DIE_ON_TURN": "1"},
    )
    r = SessionRouter(
        sup,
        repos=[GatewayRepo(path=str(allowlisted_repo), name="app")],
        personal_root=personal,
        on_notice=lambda _c, t: notices.append(t),
        on_event=on_event,
        on_worker_death=on_death,
    )
    try:
        r.submit_turn(8, "boom")
        assert death_event.wait(timeout=5), "death hook not called"
        assert deaths and deaths[0][0] == 8
        assert any("not replayed" in n for n in notices)
        # The crashed turn must not reappear as a done event (no auto-replay).
        time.sleep(0.3)
        assert done_texts == []
    finally:
        sup.shutdown()


def test_per_chat_roots_independent(
    router: SessionRouter, personal: Path, allowlisted_repo: Path
):
    router.cmd_repo(1, "app")
    assert router.active_root(1) == allowlisted_repo
    assert router.active_root(2) == personal
    t1 = router.thread_id_for(1)
    t2 = router.thread_id_for(2)
    assert t1 != t2
