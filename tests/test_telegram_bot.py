"""T-TG-2: long-poll app — backlog, auth wire-up, callbacks, 409 stand-down."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from jarn.telegram.backend import InMemoryGatewayBackend
from jarn.telegram.bot import (
    EXIT_CONFLICT,
    TelegramBotApp,
    describe_update_verbatim,
    drain_backlog,
)
from jarn.telegram.outbox import Outbox, encode_callback
from jarn.telegram.poller_lock import PollerLock, PollerLockHeldError


@dataclass
class FakeBot:
    """Minimal aiogram Bot stand-in for backlog + conflict tests."""

    updates_pages: list[list[Any]] = field(default_factory=list)
    conflict_on_poll: bool = False
    sent: list[tuple[Any, ...]] = field(default_factory=list)
    answered: list[Any] = field(default_factory=list)
    webhook_url: str = ""
    pending_update_count: int = 0
    _page: int = 0

    async def get_webhook_info(self):
        return SimpleNamespace(
            url=self.webhook_url, pending_update_count=self.pending_update_count
        )

    async def get_updates(self, offset=None, timeout=0, limit=100, allowed_updates=None):
        if self.conflict_on_poll and timeout and timeout > 0:
            from aiogram.exceptions import TelegramConflictError
            from aiogram.methods import GetUpdates

            raise TelegramConflictError(
                method=GetUpdates(),
                message="Conflict: terminated by other getUpdates request; "
                "make sure that only one bot instance is running",
            )
        if self._page >= len(self.updates_pages):
            return []
        page = self.updates_pages[self._page]
        self._page += 1
        return page

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=len(self.sent))

    async def send_message_draft(self, chat_id, draft_id, text=None, **kwargs):
        return True

    async def answer_callback_query(self, callback_query_id, **kwargs):
        self.answered.append(callback_query_id)

    @property
    def session(self):
        return SimpleNamespace(close=self._close)

    async def _close(self):
        return None


def _update_message(*, uid: int, user_id: int, chat_id: int, text: str, chat_type="private"):
    return SimpleNamespace(
        update_id=uid,
        message=SimpleNamespace(
            chat=SimpleNamespace(id=chat_id, type=chat_type),
            from_user=SimpleNamespace(id=user_id),
            text=text,
            caption=None,
            photo=None,
            document=None,
            voice=None,
            audio=None,
            video=None,
            video_note=None,
            animation=None,
            sticker=None,
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


def test_describe_update_verbatim():
    upd = _update_message(uid=9, user_id=1, chat_id=1, text="hello\nworld")
    line = describe_update_verbatim(upd)
    assert "update_id=9" in line
    assert "hello" in line and "world" in line
    assert "from=1" in line


@pytest.mark.asyncio
async def test_drain_backlog_fetches_reports_does_not_execute():
    pages = [
        [
            _update_message(uid=1, user_id=7, chat_id=7, text="/repo secrets"),
            _update_message(uid=2, user_id=7, chat_id=7, text="do dangerous thing"),
        ]
    ]
    bot = FakeBot(updates_pages=pages, webhook_url="", pending_update_count=2)
    report = await drain_backlog(bot)
    assert report.count == 2
    assert report.offset == 3
    assert any("do dangerous thing" in line for line in report.lines)
    assert any("/repo secrets" in line for line in report.lines)


@pytest.mark.asyncio
async def test_drain_backlog_reports_webhook_without_repair():
    bot = FakeBot(updates_pages=[], webhook_url="https://example/hook", pending_update_count=4)
    report = await drain_backlog(bot)
    assert report.webhook_url == "https://example/hook"
    assert report.pending_update_count == 4
    assert report.count == 0


@pytest.mark.asyncio
async def test_handle_message_submits_turn_when_allowed():
    backend = InMemoryGatewayBackend()
    app = TelegramBotApp(
        token="fake",
        allowed_user_ids=[42],
        backend=backend,
    )
    app._bot = None  # skip media download path
    app._outbox = Outbox(sender=FakeBot())
    upd = _update_message(uid=1, user_id=42, chat_id=42, text="hello agent")
    await app.handle_update(upd)
    assert backend.last_turn() is not None
    assert backend.last_turn().text == "hello agent"


@pytest.mark.asyncio
async def test_handle_message_rejects_unauthorized():
    backend = InMemoryGatewayBackend()
    app = TelegramBotApp(token="fake", allowed_user_ids=[42], backend=backend)
    app._outbox = Outbox(sender=FakeBot())
    upd = _update_message(uid=1, user_id=99, chat_id=99, text="pwn")
    await app.handle_update(upd)
    assert backend.turns == []


@pytest.mark.asyncio
async def test_handle_group_rejected():
    backend = InMemoryGatewayBackend()
    app = TelegramBotApp(token="fake", allowed_user_ids=[42], backend=backend)
    app._outbox = Outbox(sender=FakeBot())
    upd = _update_message(
        uid=1, user_id=42, chat_id=-100, text="hi", chat_type="supergroup"
    )
    await app.handle_update(upd)
    assert backend.turns == []


@pytest.mark.asyncio
async def test_commands_stop_new_repo():
    backend = InMemoryGatewayBackend()
    app = TelegramBotApp(token="fake", allowed_user_ids=[1], backend=backend)
    app._outbox = Outbox(sender=FakeBot())
    await app.handle_update(_update_message(uid=1, user_id=1, chat_id=1, text="/stop"))
    await app.handle_update(_update_message(uid=2, user_id=1, chat_id=1, text="/new"))
    await app.handle_update(
        _update_message(uid=3, user_id=1, chat_id=1, text="/repo myapp")
    )
    assert backend.stops == [(1, 1)]
    assert backend.threads == [(1, 1)]
    assert backend.repos == [(1, 1, "myapp")]


@pytest.mark.asyncio
async def test_callback_verdicts_tool_memory_plan_yolo():
    backend = InMemoryGatewayBackend()
    app = TelegramBotApp(token="fake", allowed_user_ids=[5], backend=backend)
    fake = FakeBot()
    app._bot = fake
    app._outbox = Outbox(sender=fake)

    await app.handle_update(
        _update_callback(
            uid=1, user_id=5, chat_id=5, data=encode_callback("t", "tokA", "session")
        )
    )
    await app.handle_update(
        _update_callback(
            uid=2, user_id=5, chat_id=5, data=encode_callback("m", "tokB", "save")
        )
    )
    await app.handle_update(
        _update_callback(
            uid=3, user_id=5, chat_id=5, data=encode_callback("p", "tokC", "auto-edit")
        )
    )
    await app.handle_update(
        _update_callback(
            uid=4, user_id=5, chat_id=5, data=encode_callback("y", "yolo", "ok")
        )
    )
    # Unauthorized callback must fail closed.
    await app.handle_update(
        _update_callback(
            uid=5, user_id=999, chat_id=5, data=encode_callback("t", "tokD", "once")
        )
    )

    assert len(backend.verdicts) == 4
    tool = backend.verdicts[0]
    assert tool.approved and tool.scope == "session" and tool.kind == "tool"
    mem = backend.verdicts[1]
    assert mem.approved and mem.kind == "memory"
    plan = backend.verdicts[2]
    assert plan.approved and plan.plan_mode_target == "auto-edit"
    yolo = backend.verdicts[3]
    assert yolo.approved and yolo.kind == "yolo"
    assert fake.answered  # spinner cleared


@pytest.mark.asyncio
async def test_poll_loop_stands_down_on_409_never_retries():
    pytest.importorskip("aiogram")
    backend = InMemoryGatewayBackend()
    app = TelegramBotApp(
        token="fake",
        allowed_user_ids=[7],
        backend=backend,
        notify_chat_id=7,
        poll_timeout=25,
    )
    fake = FakeBot(conflict_on_poll=True)
    app._bot = fake
    app._outbox = Outbox(sender=fake)
    app._running = True
    code = await app._poll_loop()
    assert code == EXIT_CONFLICT
    assert fake.sent
    assert "409" in fake.sent[0][1] or "Standing down" in fake.sent[0][1]
    # Only one conflict path — loop exited (no retry storm).
    assert app._running is True  # flag uncleared; exit via return


def test_poller_lock_second_holder_fails(tmp_path):
    path = tmp_path / "telegram.poll.lock"
    first = PollerLock(path)
    first.acquire()
    try:
        with pytest.raises(PollerLockHeldError):
            PollerLock(path).acquire()
    finally:
        first.release()
    with PollerLock(path) as lock:
        assert lock.held
