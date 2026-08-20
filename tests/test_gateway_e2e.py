"""T-QA-2: end-to-end scripted gateway path without a live Telegram network.

Composes existing pieces:

* ``telegram.auth`` / ``TelegramBotApp`` (synthetic updates + FakeBot)
* ``telegram.outbox`` (approval + media refusal cards)
* ``gateway.approvals`` (park Approver → pending map → resume)
* ``gateway.sessions`` + fake worker (DM turn, busy queue, ``/new``)
* ``InMemoryGatewayBackend`` (transport recording)

Happy path: unauthorized reject → allowed DM → turn → park → card →
callback verdict → ``resume_parked_approval``.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jarn.agent.events import ApprovalRequest, Event, EventKind
from jarn.config.schema import GatewayRepo
from jarn.gateway.approvals import (
    ApprovalParked,
    PendingApproval,
    PendingApprovalMap,
    make_park_approver,
    resume_parked_approval,
)
from jarn.gateway.daemon import DaemonSupervisor
from jarn.gateway.protocol import EventFrame
from jarn.gateway.sessions import QUEUED_NOTICE, SessionRouter
from jarn.permissions import (
    Action,
    ActionKind,
    Decision,
    PermissionResult,
    RememberScope,
)
from jarn.telegram.auth import authorize_update
from jarn.telegram.backend import InMemoryGatewayBackend, SessionRouterBackend
from jarn.telegram.bot import TelegramBotApp
from jarn.telegram.outbox import Outbox, encode_callback, parse_callback
from jarn.tui.i18n import t

FAKE_WORKER = Path(__file__).resolve().parent / "gateway_fake_worker.py"


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeBot:
    """Minimal aiogram Bot stand-in (no network)."""

    sent: list[dict[str, Any]] = field(default_factory=list)
    answered: list[Any] = field(default_factory=list)
    drafts: list[tuple[Any, ...]] = field(default_factory=list)

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(message_id=len(self.sent))

    async def send_message_draft(self, chat_id, draft_id, text=None, **kwargs):
        self.drafts.append((chat_id, draft_id, text))
        return True

    async def answer_callback_query(self, callback_query_id, **kwargs):
        self.answered.append(callback_query_id)


def _msg_fields(**overrides: Any) -> dict[str, Any]:
    base = {
        "photo": None,
        "document": None,
        "voice": None,
        "audio": None,
        "video": None,
        "video_note": None,
        "animation": None,
        "sticker": None,
        "caption": None,
        "text": None,
    }
    base.update(overrides)
    return base


def _update_message(
    *,
    uid: int,
    user_id: int,
    chat_id: int,
    text: str | None = None,
    chat_type: str = "private",
    **media: Any,
):
    fields = _msg_fields(text=text, **media)
    return SimpleNamespace(
        update_id=uid,
        message=SimpleNamespace(
            chat=SimpleNamespace(id=chat_id, type=chat_type),
            from_user=SimpleNamespace(id=user_id),
            **fields,
        ),
        edited_message=None,
        callback_query=None,
    )


def _update_callback(*, uid: int, user_id: int, chat_id: int, data: str):
    return SimpleNamespace(
        update_id=uid,
        message=None,
        edited_message=None,
        callback_query=SimpleNamespace(
            id=f"cb-{uid}",
            data=data,
            from_user=SimpleNamespace(id=user_id),
            message=SimpleNamespace(chat=SimpleNamespace(id=chat_id, type="private")),
        ),
    )


def _ask(tool: str = "execute", target: str = "rm -rf /tmp/e2e") -> ApprovalRequest:
    return ApprovalRequest(
        action=Action(ActionKind.SHELL, target=target, tool=tool),
        result=PermissionResult(Decision.ASK, "needs confirmation", dangerous=True),
        description="run command",
        args={"command": target},
    )


@dataclass
class ParkResumeBackend:
    """GatewayBackend that parks on turn and resumes on verdict (no workers).

    Mirrors the production path: park Approver → pending map + outbox card →
    callback → ``resume_parked_approval``.
    """

    root: Path
    outbox: Outbox
    store: PendingApprovalMap
    thread_id: str = "e2e-thread"
    turns: list[tuple[int, int, str]] = field(default_factory=list)
    resume_events: list[Event] = field(default_factory=list)
    parked_token: str | None = None
    _thread_seq: int = 0

    async def submit_turn(
        self,
        *,
        chat_id: int,
        user_id: int,
        text: str,
        media: list[Any] | None = None,
    ) -> None:
        del media
        self.turns.append((chat_id, user_id, text))
        seen: list[PendingApproval] = []

        async def on_park(record: PendingApproval, request: ApprovalRequest) -> None:
            seen.append(record)
            await self.outbox.send_approval_card(
                chat_id,
                token=record.token,
                action=request.action.tool or "",
                target=request.action.target or "",
                description=request.description or text,
                args=request.args if isinstance(request.args, dict) else None,
                dangerous=bool(getattr(request.result, "dangerous", False)),
            )

        park = make_park_approver(
            root=self.root,
            thread_id=self.thread_id,
            interrupt_id="parked",
            chat_id=chat_id,
            store=self.store,
            token_factory=lambda: "e2e-park-token",
            on_park=on_park,
        )
        with pytest.raises(ApprovalParked) as excinfo:
            await park(_ask())
        self.parked_token = excinfo.value.token
        assert seen and seen[0].token == self.parked_token
        assert self.store.get(self.parked_token) is not None

    async def submit_verdict(
        self,
        *,
        chat_id: int,
        user_id: int,
        token: str,
        approved: bool,
        scope: str = "once",
        plan_mode_target: str | None = None,
        message: str = "",
        kind: str = "tool",
    ) -> None:
        del user_id, kind

        class _Driver:
            def __init__(self) -> None:
                self.thread_id = "other"
                self.approver = None

            async def resume_pending_approval(self, *args: Any, **kwargs: Any):
                del args, kwargs
                reply = await self.approver(_ask())  # type: ignore[misc]
                yield Event(
                    EventKind.APPROVAL,
                    text="approved" if reply.approved else "rejected",
                    data={"scope": str(reply.scope)},
                )
                yield Event(EventKind.TEXT, text="resumed-after-verdict")
                yield Event(EventKind.DONE)

        driver = _Driver()
        async for ev in resume_parked_approval(
            driver,
            token=token,
            approved=approved,
            scope=scope,
            message=message,
            plan_mode_target=plan_mode_target,
            store=self.store,
        ):
            self.resume_events.append(ev)
            if ev.kind == EventKind.TEXT and ev.text:
                await self.outbox.on_event(chat_id, kind="text", text=ev.text)
            elif ev.kind == EventKind.DONE:
                await self.outbox.on_event(chat_id, kind="done")
        assert driver.thread_id == self.thread_id

    async def stop(self, *, chat_id: int, user_id: int) -> None:
        del chat_id, user_id

    async def new_thread(self, *, chat_id: int, user_id: int) -> str:
        del user_id
        self._thread_seq += 1
        tid = f"e2e-thread-{self._thread_seq}"
        self.thread_id = tid
        return tid

    async def set_repo(self, *, chat_id: int, user_id: int, name_or_path: str) -> str:
        del chat_id, user_id
        return name_or_path


def _worker_cmd() -> list[str]:
    return [sys.executable, str(FAKE_WORKER)]


def _personal_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    jarn_home = tmp_path / "home"
    root = jarn_home / "personal"
    root.mkdir(parents=True)
    (root / ".git").mkdir()
    monkeypatch.setenv("JARN_HOME", str(jarn_home))
    return root.resolve()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_e2e_auth_unauthorized_rejected_allowed_accepted():
    allowed = [42]
    bad = _update_message(uid=1, user_id=99, chat_id=99, text="pwn")
    good = _update_message(uid=2, user_id=42, chat_id=42, text="hello")

    deny = authorize_update(bad, allowed)
    assert not deny.ok
    assert deny.reason == "not_allowed"

    allow = authorize_update(good, allowed)
    assert allow.ok
    assert allow.user_id == 42
    assert allow.chat_id == 42


@pytest.mark.asyncio
async def test_e2e_transport_rejects_unauthorized_dm():
    backend = InMemoryGatewayBackend()
    app = TelegramBotApp(token="fake", allowed_user_ids=[42], backend=backend)
    app._outbox = Outbox(sender=FakeBot())
    await app.handle_update(_update_message(uid=1, user_id=99, chat_id=99, text="nope"))
    assert backend.turns == []


# ---------------------------------------------------------------------------
# Core happy path: DM → park → card → verdict → resume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_dm_turn_park_verdict_resume(isolated_home: Path, tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    bot = FakeBot()
    outbox = Outbox(sender=bot)
    store = PendingApprovalMap(isolated_home / "gateway" / "pending_approvals.json")
    backend = ParkResumeBackend(root=root, outbox=outbox, store=store)
    app = TelegramBotApp(
        token="fake",
        allowed_user_ids=[7],
        backend=backend,
        project_root=root,
    )
    # FakeBot enables callback spinner clear; plain-text DMs have no attachments.
    app._bot = bot
    app._outbox = outbox

    # 1) Unauthorized update never reaches the backend.
    await app.handle_update(_update_message(uid=1, user_id=999, chat_id=999, text="intruder"))
    assert backend.turns == []
    assert store.list() == []

    # 2) Allowed DM → turn → park Approver records pending + ApprovalParked.
    await app.handle_update(
        _update_message(uid=2, user_id=7, chat_id=7, text="please delete /tmp/e2e")
    )
    assert backend.turns == [(7, 7, "please delete /tmp/e2e")]
    assert backend.parked_token == "e2e-park-token"
    pending = store.get("e2e-park-token")
    assert pending is not None
    assert pending.thread_id == "e2e-thread"
    assert pending.chat_id == 7
    assert pending.root == str(root.resolve())

    # Outbox emitted an approval card with Once / Session / Deny callbacks.
    assert bot.sent, "expected approval card send_message"
    card = bot.sent[-1]
    assert card["chat_id"] == 7
    assert t("approval.header.shell", "en", object="rm -rf /tmp/e2e") in card["text"]
    markup = card.get("reply_markup")
    assert markup is not None
    callbacks: list[str] = []
    if isinstance(markup, dict):
        for row in markup.get("inline_keyboard", []):
            for btn in row:
                callbacks.append(btn["callback_data"])
    else:
        for row in getattr(markup, "inline_keyboard", []) or []:
            for btn in row:
                callbacks.append(btn.callback_data)
    assert any(parse_callback(c) and parse_callback(c).action == "once" for c in callbacks)
    once_payload = next(c for c in callbacks if parse_callback(c).action == "once")
    assert parse_callback(once_payload).token == "e2e-park-token"

    # 3) Verdict callback → resume_parked_approval deletes map row + streams events.
    await app.handle_update(_update_callback(uid=3, user_id=7, chat_id=7, data=once_payload))
    assert store.get("e2e-park-token") is None
    kinds = [e.kind for e in backend.resume_events]
    assert EventKind.APPROVAL in kinds
    assert EventKind.DONE in kinds
    assert any(
        e.kind == EventKind.TEXT and "resumed-after-verdict" in e.text
        for e in backend.resume_events
    )
    approval = next(e for e in backend.resume_events if e.kind == EventKind.APPROVAL)
    assert approval.data.get("scope") == str(RememberScope.ONCE)
    assert bot.answered  # spinner cleared


# ---------------------------------------------------------------------------
# Optional paths: /new, media refusal, busy queue via SessionRouter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_new_mints_thread_via_transport():
    backend = InMemoryGatewayBackend()
    bot = FakeBot()
    app = TelegramBotApp(token="fake", allowed_user_ids=[1], backend=backend)
    app._outbox = Outbox(sender=bot)

    await app.handle_update(_update_message(uid=1, user_id=1, chat_id=1, text="/new"))
    assert backend.threads == [(1, 1)]
    assert any("New thread:" in m["text"] for m in bot.sent)


@pytest.mark.asyncio
async def test_e2e_media_refusal_card_voice_dm(tmp_path: Path):
    backend = InMemoryGatewayBackend()
    bot = FakeBot()
    app = TelegramBotApp(
        token="fake",
        allowed_user_ids=[3],
        backend=backend,
        project_root=tmp_path,
    )
    app._bot = bot  # enable media path (voice refused without download)
    app._outbox = Outbox(sender=bot)

    await app.handle_update(
        _update_message(
            uid=1,
            user_id=3,
            chat_id=3,
            text=None,
            caption="transcribe this",
            voice=SimpleNamespace(file_id="v-e2e", mime_type="audio/ogg"),
        )
    )
    # Caption still proceeds as a turn; voice is refused with a card.
    assert backend.last_turn() is not None
    assert backend.last_turn().text == "transcribe this"
    assert backend.last_turn().media == ()
    assert any("Media not accepted" in m["text"] for m in bot.sent)


def test_e2e_session_router_dm_turn_and_busy_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    personal = _personal_root(tmp_path, monkeypatch)
    allowlisted = tmp_path / "app"
    allowlisted.mkdir()
    (allowlisted / ".git").mkdir()

    notices: list[tuple[int, str]] = []
    events: list[Any] = []
    done = threading.Event()

    def on_notice(chat_id: int, text: str) -> None:
        notices.append((chat_id, text))

    def on_event(chat_id: int, root: Path, frame: Any) -> None:
        events.append((chat_id, root, frame))
        if isinstance(frame, EventFrame) and frame.kind == "done":
            done.set()

    sup = DaemonSupervisor(
        worker_command=_worker_cmd(),
        handshake_timeout_secs=5.0,
        env={"FAKE_WORKER_TURN_HOLD_SECS": "0.35"},
    )
    router = SessionRouter(
        sup,
        repos=[GatewayRepo(path=str(allowlisted.resolve()), name="app")],
        personal_root=personal,
        on_notice=on_notice,
        on_event=on_event,
    )
    try:
        # Synthetic DM → fake worker; second submit while held → steer, not queue.
        tid1 = router.submit_turn(11, "first")
        tid2 = router.submit_turn(11, "second")
        assert tid1 == tid2
        assert router.queue_depth(personal) == 0
        assert not any(QUEUED_NOTICE in text for _, text in notices)

        assert done.wait(timeout=5)
        assert any(
            isinstance(f, EventFrame) and f.kind == "done" and f.thread_id == tid1
            for _, _, f in events
        )

        # /new mints a fresh thread for this chat/root.
        before = router.thread_id_for(11)
        minted = router.cmd_new(11)
        assert minted != before
        assert router.thread_id_for(11) == minted
    finally:
        sup.shutdown()


@pytest.mark.asyncio
async def test_e2e_session_router_backend_accepts_dm_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """TelegramBotApp → SessionRouterBackend → fake worker (no live token)."""
    personal = _personal_root(tmp_path, monkeypatch)
    done = threading.Event()
    events: list[Any] = []

    def on_event(chat_id: int, root: Path, frame: Any) -> None:
        events.append(frame)
        if isinstance(frame, EventFrame) and frame.kind == "done":
            done.set()

    sup = DaemonSupervisor(
        worker_command=_worker_cmd(),
        handshake_timeout_secs=5.0,
        env={
            "FAKE_WORKER_TURN_HOLD_SECS": "0.05",
            "FAKE_WORKER_EMIT_TEXT": "1",
        },
    )
    router = SessionRouter(
        sup,
        personal_root=personal,
        on_event=on_event,
        on_notice=lambda *_: None,
    )
    backend = SessionRouterBackend(router=router, supervisor=sup)
    app = TelegramBotApp(token="fake", allowed_user_ids=[5], backend=backend)
    bot = FakeBot()
    app._bot = None
    app._outbox = Outbox(sender=bot)
    backend.bind_outbox(app._outbox)
    try:
        await app.handle_update(_update_message(uid=1, user_id=5, chat_id=5, text="hello worker"))
        assert done.wait(timeout=5)
        assert any(isinstance(f, EventFrame) and f.kind == "done" for f in events)
        for _ in range(50):
            if bot.sent:
                break
            await asyncio.sleep(0.01)
        assert bot.sent
        assert any("echo:hello worker" in row["text"] for row in bot.sent)
    finally:
        backend.unbind_outbox()
        sup.shutdown()


def test_e2e_callback_encode_matches_park_token():
    """Sanity: transport callback payload carries the park map token."""
    payload = encode_callback("t", "e2e-park-token", "session")
    parsed = parse_callback(payload)
    assert parsed is not None
    assert parsed.kind == "tool"
    assert parsed.token == "e2e-park-token"
    assert parsed.action == "session"
