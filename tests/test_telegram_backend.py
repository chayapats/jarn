"""SessionRouterBackend adapter coverage (production Telegram→daemon path)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jarn.config.schema import GatewayRepo
from jarn.gateway.approvals import PendingApproval, PendingApprovalMap
from jarn.gateway.daemon import DaemonSupervisor
from jarn.gateway.protocol import EventFrame, MediaRef, TurnFrame
from jarn.gateway.sessions import SessionRouter, UnknownRepoError
from jarn.telegram.backend import SessionRouterBackend
from jarn.telegram.outbox import Outbox


class _FakeSender:
    def __init__(self) -> None:
        self.drafts: list[str] = []
        self.messages: list[dict] = []

    async def send_message_draft(self, chat_id, draft_id, text=None, **kwargs):
        self.drafts.append(text or "")
        return True

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append({"chat_id": chat_id, "text": text, **kwargs})
        return {"message_id": len(self.messages)}


@pytest.mark.asyncio
async def test_submit_verdict_routes_to_active_root(tmp_path: Path, monkeypatch):
    """Callback verdicts must hit supervisor.send_approval_verdict on active root."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("JARN_HOME", str(home))

    personal = tmp_path / "personal"
    personal.mkdir()
    (personal / ".jarn").mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".jarn").mkdir()

    monkeypatch.setattr("jarn.config.paths.ensure_personal_root", lambda: personal)

    supervisor = MagicMock(spec=DaemonSupervisor)
    supervisor.on_outbound = None
    supervisor.on_worker_death = None
    router = SessionRouter(
        supervisor,
        repos=[GatewayRepo(path=str(repo), name="app")],
        personal_root=personal,
    )
    backend = SessionRouterBackend(router=router, supervisor=supervisor)

    await backend.set_repo(chat_id=42, user_id=1, name_or_path="app")
    router.approval_store.put(
        PendingApproval(
            token="tok-1",
            root=str(repo.resolve()),
            thread_id=router.thread_id_for(42),
            interrupt_id="interrupt",
            chat_id=42,
        )
    )
    await backend.submit_verdict(
        chat_id=42,
        user_id=1,
        token="tok-1",
        approved=True,
        scope="session",
        plan_mode_target="auto-edit",
        message="go",
        kind="tool",
    )

    supervisor.send_approval_verdict.assert_called_once_with(
        repo.resolve(),
        token="tok-1",
        approved=True,
        scope="session",
        message="go",
        plan_mode_target="auto-edit",
    )


@pytest.mark.asyncio
async def test_submit_turn_forwards_media_refs(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("JARN_HOME", str(home))
    personal = tmp_path / "personal"
    personal.mkdir()
    (personal / ".jarn").mkdir()

    supervisor = MagicMock(spec=DaemonSupervisor)
    supervisor.on_outbound = None
    supervisor.on_worker_death = None
    sent: list = []

    def _send(root, frame):
        sent.append((root, frame))

    supervisor.send.side_effect = _send
    supervisor.ensure_worker.return_value = SimpleNamespace(root=personal.resolve())
    supervisor.get_worker.return_value = None

    router = SessionRouter(supervisor, personal_root=personal)
    backend = SessionRouterBackend(router=router, supervisor=supervisor)
    media = [MediaRef(path="/tmp/x.png", mime="image/png", modality="image")]
    await backend.submit_turn(chat_id=7, user_id=1, text="see this", media=media)

    assert sent
    _root, frame = sent[0]
    assert _root == personal.resolve()
    assert isinstance(frame, TurnFrame)
    assert frame.text == "see this"
    assert frame.media and frame.media[0].path == "/tmp/x.png"
    assert frame.chat_id == 7


@pytest.mark.asyncio
async def test_bind_outbox_delivers_worker_events_from_reader_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("JARN_HOME", str(home))
    personal = tmp_path / "personal"
    personal.mkdir()
    (personal / ".jarn").mkdir()
    supervisor = MagicMock(spec=DaemonSupervisor)
    supervisor.on_outbound = None
    supervisor.on_worker_death = None
    router = SessionRouter(supervisor, personal_root=personal)
    backend = SessionRouterBackend(router=router, supervisor=supervisor)
    sender = _FakeSender()
    backend.bind_outbox(Outbox(sender=sender))

    await asyncio.to_thread(
        router.on_event,
        42,
        personal,
        EventFrame(thread_id="thread", kind="text", text="hello"),
    )
    await asyncio.to_thread(
        router.on_event,
        42,
        personal,
        EventFrame(thread_id="thread", kind="done"),
    )
    for _ in range(20):
        if sender.messages:
            break
        await asyncio.sleep(0.01)
    assert sender.drafts and "hello" in sender.drafts[-1]
    assert sender.messages and sender.messages[-1]["text"] == "hello"
    backend.unbind_outbox()


@pytest.mark.asyncio
async def test_restart_verdict_uses_stored_root_thread_and_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("JARN_HOME", str(home))
    personal = tmp_path / "personal"
    repo = tmp_path / "repo"
    for root in (personal, repo):
        root.mkdir()
        (root / ".jarn").mkdir()
    store = PendingApprovalMap()
    store.put(
        PendingApproval(
            token="parked",
            root=str(repo.resolve()),
            thread_id="original-thread",
            interrupt_id="interrupt",
            chat_id=42,
            card={"action": "execute", "description": "run"},
        )
    )
    supervisor = MagicMock(spec=DaemonSupervisor)
    supervisor.on_outbound = None
    supervisor.on_worker_death = None
    router = SessionRouter(
        supervisor,
        repos=[GatewayRepo(path=str(repo), name="app")],
        personal_root=personal,
        approval_store=store,
    )
    backend = SessionRouterBackend(router=router, supervisor=supervisor)

    assert router.active_root(42) == personal.resolve()
    await backend.submit_verdict(
        chat_id=42,
        user_id=42,
        token="parked",
        approved=True,
    )
    supervisor.send_approval_verdict.assert_called_once()
    assert supervisor.send_approval_verdict.call_args.args[0] == repo.resolve()
    assert router.active_root(42) == repo.resolve()
    assert router.thread_id_for(42) == "original-thread"


@pytest.mark.asyncio
async def test_restart_verdict_rejects_other_chat_and_removed_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("JARN_HOME", str(home))
    personal = tmp_path / "personal"
    removed = tmp_path / "removed"
    for root in (personal, removed):
        root.mkdir()
        (root / ".jarn").mkdir()
    store = PendingApprovalMap()
    store.put(
        PendingApproval(
            token="parked",
            root=str(removed.resolve()),
            thread_id="thread",
            interrupt_id="interrupt",
            chat_id=42,
        )
    )
    supervisor = MagicMock(spec=DaemonSupervisor)
    supervisor.on_outbound = None
    supervisor.on_worker_death = None
    router = SessionRouter(
        supervisor,
        personal_root=personal,
        approval_store=store,
    )
    backend = SessionRouterBackend(router=router, supervisor=supervisor)

    with pytest.raises(PermissionError, match="another chat"):
        await backend.submit_verdict(chat_id=99, user_id=99, token="parked", approved=True)
    with pytest.raises(UnknownRepoError):
        await backend.submit_verdict(chat_id=42, user_id=42, token="parked", approved=True)
    supervisor.send_approval_verdict.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_approval_token_fails_closed_without_spawning_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("JARN_HOME", str(home))
    personal = tmp_path / "personal"
    personal.mkdir()
    (personal / ".jarn").mkdir()
    supervisor = MagicMock(spec=DaemonSupervisor)
    supervisor.on_outbound = None
    supervisor.on_worker_death = None
    router = SessionRouter(supervisor, personal_root=personal)
    backend = SessionRouterBackend(router=router, supervisor=supervisor)

    with pytest.raises(KeyError, match="unknown or expired"):
        await backend.submit_verdict(
            chat_id=42,
            user_id=42,
            token="stale",
            approved=True,
        )
    supervisor.send_approval_verdict.assert_not_called()


@pytest.mark.asyncio
async def test_parked_approval_cannot_steal_busy_root_from_another_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jarn.gateway.sessions import ApprovalResumeBusyError

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("JARN_HOME", str(home))
    personal = tmp_path / "personal"
    personal.mkdir()
    (personal / ".jarn").mkdir()
    store = PendingApprovalMap()
    store.put(
        PendingApproval(
            token="old-card",
            root=str(personal.resolve()),
            thread_id="old-thread",
            interrupt_id="interrupt",
            chat_id=42,
        )
    )
    supervisor = MagicMock(spec=DaemonSupervisor)
    supervisor.on_outbound = None
    supervisor.on_worker_death = None
    router = SessionRouter(
        supervisor,
        personal_root=personal,
        approval_store=store,
    )
    # Simulate another allowed chat currently owning a turn on this root.
    router._busy_roots.add(personal.resolve())
    router._root_owner[personal.resolve()] = 99
    backend = SessionRouterBackend(router=router, supervisor=supervisor)

    with pytest.raises(ApprovalResumeBusyError, match="another turn"):
        await backend.submit_verdict(
            chat_id=42,
            user_id=42,
            token="old-card",
            approved=True,
        )
    assert router._root_owner[personal.resolve()] == 99
    supervisor.send_approval_verdict.assert_not_called()


@pytest.mark.asyncio
async def test_restore_pending_cards_filters_allowlist_and_updates_message_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("JARN_HOME", str(home))
    personal = tmp_path / "personal"
    personal.mkdir()
    (personal / ".jarn").mkdir()
    removed = tmp_path / "removed"
    removed.mkdir()
    (removed / ".jarn").mkdir()
    store = PendingApprovalMap()
    for token, chat_id, card in (
        ("allowed", 42, {"action": "execute", "description": "run"}),
        ("blocked", 99, {"action": "execute", "description": "no"}),
        ("legacy", 42, None),
    ):
        store.put(
            PendingApproval(
                token=token,
                root=str(personal.resolve()),
                thread_id=f"thread-{token}",
                interrupt_id="interrupt",
                chat_id=chat_id,
                card=card,
            )
        )
    store.put(
        PendingApproval(
            token="removed-root",
            root=str(removed.resolve()),
            thread_id="thread-removed",
            interrupt_id="interrupt",
            chat_id=42,
            card={"action": "execute", "description": "removed"},
        )
    )
    supervisor = MagicMock(spec=DaemonSupervisor)
    supervisor.on_outbound = None
    supervisor.on_worker_death = None
    router = SessionRouter(
        supervisor,
        personal_root=personal,
        approval_store=store,
    )
    backend = SessionRouterBackend(router=router, supervisor=supervisor)
    sender = _FakeSender()
    backend.bind_outbox(Outbox(sender=sender))

    assert await backend.restore_pending_approvals(allowed_chat_ids=[42]) == 1
    assert len(sender.messages) == 1
    assert sender.messages[0]["chat_id"] == 42
    assert store.get("allowed").message_id == 1
    assert store.get("blocked").message_id is None
    assert store.get("legacy").message_id is None
    assert store.get("removed-root").message_id is None
