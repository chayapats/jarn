"""T-TG-2: long-poll app — backlog, auth wire-up, callbacks, 409 stand-down."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from jarn.telegram.backend import InMemoryGatewayBackend
from jarn.telegram.bot import (
    EXIT_CONFLICT,
    EXIT_UNAUTHORIZED,
    TelegramBotApp,
    describe_update_verbatim,
    drain_backlog,
)
from jarn.telegram.outbox import Outbox, encode_callback
from jarn.telegram.poller_lock import PollerLock, PollerLockHeldError
from jarn.tui.i18n import t


@dataclass
class FakeBot:
    """Minimal aiogram Bot stand-in for backlog + conflict tests."""

    updates_pages: list[list[Any]] = field(default_factory=list)
    conflict_on_poll: bool = False
    unauthorized_on_poll: bool = False
    sent: list[tuple[Any, ...]] = field(default_factory=list)
    answered: list[Any] = field(default_factory=list)
    edits: list[dict] = field(default_factory=list)
    deletes: list[tuple[int, int]] = field(default_factory=list)
    commands_set: list[Any] = field(default_factory=list)
    webhook_url: str = ""
    pending_update_count: int = 0
    _page: int = 0

    async def get_webhook_info(self):
        return SimpleNamespace(url=self.webhook_url, pending_update_count=self.pending_update_count)

    async def get_updates(self, offset=None, timeout=0, limit=100, allowed_updates=None):
        if self.unauthorized_on_poll and timeout and timeout > 0:
            from aiogram.exceptions import TelegramUnauthorizedError
            from aiogram.methods import GetUpdates

            raise TelegramUnauthorizedError(
                method=GetUpdates(),
                message="Unauthorized",
            )
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

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kwargs):
        self.edits.append(
            {
                "text": text,
                "chat_id": chat_id,
                "message_id": message_id,
                **kwargs,
            }
        )
        return True

    async def delete_message(self, chat_id, message_id, **kwargs):
        self.deletes.append((chat_id, message_id))
        return True

    async def set_my_commands(self, commands, **kwargs):
        self.commands_set = list(commands)

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


def _update_callback(
    *,
    uid: int,
    user_id: int,
    chat_id: int,
    data: str,
    message_id: int | None = None,
    html_text: str = "",
    text: str = "",
):
    return SimpleNamespace(
        update_id=uid,
        message=None,
        edited_message=None,
        callback_query=SimpleNamespace(
            id=f"cb-{uid}",
            data=data,
            from_user=SimpleNamespace(id=user_id),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=chat_id, type="private"),
                message_id=message_id,
                html_text=html_text or None,
                text=text or None,
            ),
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


def test_bot_busy_ack_detail_defaults_off():
    backend = InMemoryGatewayBackend()
    app = TelegramBotApp(token="fake", allowed_user_ids=[42], backend=backend)
    assert app.busy_ack_detail is False
    out = Outbox(sender=FakeBot(), busy_ack_detail=app.busy_ack_detail)
    assert out.busy_ack_detail is False


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
    upd = _update_message(uid=1, user_id=42, chat_id=-100, text="hi", chat_type="supergroup")
    await app.handle_update(upd)
    assert backend.turns == []


@pytest.mark.asyncio
async def test_commands_stop_new_repo():
    backend = InMemoryGatewayBackend()
    app = TelegramBotApp(token="fake", allowed_user_ids=[1], backend=backend)
    app._outbox = Outbox(sender=FakeBot())
    await app.handle_update(_update_message(uid=1, user_id=1, chat_id=1, text="/stop"))
    await app.handle_update(_update_message(uid=2, user_id=1, chat_id=1, text="/new"))
    await app.handle_update(_update_message(uid=3, user_id=1, chat_id=1, text="/repo myapp"))
    assert backend.stops == [(1, 1)]
    assert backend.threads == [(1, 1)]
    assert backend.repos == [(1, 1, "myapp")]


@pytest.mark.asyncio
async def test_help_uses_command_catalog_html_and_does_not_submit_a_turn():
    backend = InMemoryGatewayBackend()
    fake = FakeBot()
    app = TelegramBotApp(token="fake", allowed_user_ids=[1], backend=backend)
    app._bot = None
    app._outbox = Outbox(sender=fake)
    await app.handle_update(_update_message(uid=1, user_id=1, chat_id=1, text="/help"))
    await app.handle_update(_update_message(uid=2, user_id=1, chat_id=1, text="/help compact"))
    assert backend.turns == []
    bodies = [sent[1] for sent in fake.sent]
    joined = "\n".join(bodies)
    assert "<b>Work</b>" in joined
    assert "/compact" in joined
    assert "Gateway" in joined
    assert "/reset" in joined
    assert "/rollback" in joined
    assert "<span" not in joined
    assert any(sent[2].get("parse_mode") == "HTML" for sent in fake.sent)


@pytest.mark.parametrize("locale", ["en", "th"])
@pytest.mark.asyncio
async def test_help_html_follows_locale_and_stays_local(locale):
    backend = InMemoryGatewayBackend()
    fake = FakeBot()
    app = TelegramBotApp(token="fake", allowed_user_ids=[1], backend=backend, locale=locale)
    app._bot = None
    app._outbox = Outbox(sender=fake, locale=locale)
    await app.handle_update(_update_message(uid=1, user_id=1, chat_id=1, text="/help"))
    await app.handle_update(_update_message(uid=2, user_id=1, chat_id=1, text="/help mode"))
    assert backend.turns == []
    joined = "\n".join(sent[1] for sent in fake.sent)
    assert f"<b>{t('help.group.Work', locale)}</b>" in joined
    assert f"<b>{t('telegram.help.group', locale)}</b>" in joined
    assert t("telegram.help.stop", locale) in joined
    assert "/model" in joined
    assert "ask" in joined and "yolo" in joined
    assert t("help.mode.ask", locale) in joined
    assert "<span" not in joined
    other = "en" if locale == "th" else "th"
    assert t("help.group.Work", other) not in joined
    assert t("telegram.help.group", other) not in joined


@pytest.mark.parametrize("locale", ["en", "th"])
@pytest.mark.asyncio
async def test_mutating_slash_localizes_hint_without_submit_turn(locale):
    backend = InMemoryGatewayBackend()
    fake = FakeBot()
    app = TelegramBotApp(token="fake", allowed_user_ids=[1], backend=backend, locale=locale)
    app._bot = None
    app._outbox = Outbox(sender=fake, locale=locale)
    await app.handle_update(
        _update_message(uid=1, user_id=1, chat_id=1, text="/config set ui.theme light")
    )
    assert backend.turns == []
    joined = "\n".join(sent[1] for sent in fake.sent)
    assert t("telegram.mutating.named", locale, name="config") in joined
    assert "/config" in joined


@pytest.mark.asyncio
async def test_callback_verdicts_tool_memory_plan_yolo():
    backend = InMemoryGatewayBackend()
    app = TelegramBotApp(token="fake", allowed_user_ids=[5], backend=backend)
    fake = FakeBot()
    app._bot = fake
    app._outbox = Outbox(sender=fake)

    await app.handle_update(
        _update_callback(uid=1, user_id=5, chat_id=5, data=encode_callback("t", "tokA", "session"))
    )
    await app.handle_update(
        _update_callback(uid=2, user_id=5, chat_id=5, data=encode_callback("m", "tokB", "save"))
    )
    await app.handle_update(
        _update_callback(
            uid=3, user_id=5, chat_id=5, data=encode_callback("p", "tokC", "auto-edit")
        )
    )
    await app.handle_update(
        _update_callback(uid=4, user_id=5, chat_id=5, data=encode_callback("y", "yolo", "ok"))
    )
    # Unauthorized callback must fail closed.
    await app.handle_update(
        _update_callback(uid=5, user_id=999, chat_id=5, data=encode_callback("t", "tokD", "once"))
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
async def test_callback_deletes_card():
    backend = InMemoryGatewayBackend()
    app = TelegramBotApp(token="fake", allowed_user_ids=[5], backend=backend)
    fake = FakeBot()
    app._bot = fake
    app._outbox = Outbox(sender=fake)
    html = "<b>Allow shell ls?</b>"
    await app.handle_update(
        _update_callback(
            uid=1,
            user_id=5,
            chat_id=5,
            data=encode_callback("t", "tokA", "once"),
            message_id=99,
            html_text=html,
        )
    )
    assert len(backend.verdicts) == 1
    assert fake.answered == ["cb-1"]
    assert fake.deletes == [(5, 99)]
    assert fake.edits == []


@pytest.mark.asyncio
async def test_stale_callback_gets_visible_notice_instead_of_poll_error():
    class StaleBackend(InMemoryGatewayBackend):
        async def submit_verdict(self, **kwargs):
            raise KeyError("expired")

    app = TelegramBotApp(
        token="fake",
        allowed_user_ids=[5],
        backend=StaleBackend(),
    )
    fake = FakeBot()
    app._bot = fake
    app._outbox = Outbox(sender=fake)
    await app.handle_update(
        _update_callback(
            uid=1,
            user_id=5,
            chat_id=5,
            data=encode_callback("t", "stale", "once"),
        )
    )
    assert fake.answered == ["cb-1"]
    assert fake.sent
    assert "could not be resumed" in fake.sent[-1][1]
    assert "stale" in fake.sent[-1][1]


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


@pytest.mark.asyncio
async def test_poll_loop_stands_down_on_unauthorized_never_retries():
    pytest.importorskip("aiogram")
    app = TelegramBotApp(
        token="fake",
        allowed_user_ids=[7],
        backend=InMemoryGatewayBackend(),
        poll_timeout=25,
    )
    fake = FakeBot(unauthorized_on_poll=True)
    app._bot = fake
    app._outbox = Outbox(sender=fake)
    code = await app._poll_loop()
    assert code == EXIT_UNAUTHORIZED
    assert fake._page == 0


@pytest.mark.asyncio
async def test_drain_backlog_does_not_swallow_unauthorized():
    pytest.importorskip("aiogram")
    from aiogram.exceptions import TelegramUnauthorizedError
    from aiogram.methods import GetWebhookInfo

    class UnauthorizedBot(FakeBot):
        async def get_webhook_info(self):
            raise TelegramUnauthorizedError(
                method=GetWebhookInfo(),
                message="Unauthorized",
            )

    with pytest.raises(TelegramUnauthorizedError):
        await drain_backlog(UnauthorizedBot())


@pytest.mark.asyncio
async def test_start_binds_restores_and_unbinds_production_backend_lifecycle(
    isolated_home, monkeypatch: pytest.MonkeyPatch
):
    pytest.importorskip("aiogram")
    import aiogram

    calls: list[object] = []

    class LifecycleBackend(InMemoryGatewayBackend):
        def bind_outbox(self, outbox, *, loop=None):
            calls.append(("bind", outbox, loop))

        async def restore_pending_approvals(self, *, allowed_chat_ids):
            calls.append(("restore", tuple(allowed_chat_ids)))
            return 0

        def unbind_outbox(self):
            calls.append("unbind")

    holder: dict[str, TelegramBotApp] = {}

    class StartupBot(FakeBot):
        async def get_updates(
            self,
            offset=None,
            timeout=0,
            limit=100,
            allowed_updates=None,
        ):
            if timeout and timeout > 0:
                holder["app"].stop()
            return []

    fake = StartupBot()
    monkeypatch.setattr(aiogram, "Bot", lambda *args, **kwargs: fake)
    app = TelegramBotApp(
        token="123:ABC",
        allowed_user_ids=[42],
        backend=LifecycleBackend(),
        poll_timeout=1,
    )
    holder["app"] = app

    assert await app.start() == 0
    assert calls[0][0] == "bind"
    assert calls[1] == ("restore", (42,))
    assert calls[-1] == "unbind"
    menu_names = {getattr(cmd, "command", None) or cmd.get("command") for cmd in fake.commands_set}
    assert "status" in menu_names
    assert "stop" in menu_names
    assert "reset" in menu_names
    assert "config" not in menu_names


@pytest.mark.asyncio
async def test_start_invalid_token_fails_fast_without_network(isolated_home):
    pytest.importorskip("aiogram")
    app = TelegramBotApp(
        token="not-a-valid-token",
        allowed_user_ids=[42],
        backend=InMemoryGatewayBackend(),
    )
    assert await app.start() == EXIT_UNAUTHORIZED


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


@pytest.mark.asyncio
async def test_reset_aliases_new_and_does_not_submit_a_turn():
    backend = InMemoryGatewayBackend()
    fake = FakeBot()
    app = TelegramBotApp(token="fake", allowed_user_ids=[1], backend=backend)
    app._bot = None
    app._outbox = Outbox(sender=fake)
    await app.handle_update(_update_message(uid=1, user_id=1, chat_id=1, text="/reset"))
    assert backend.threads == [(1, 1)]
    assert backend.turns == []
    assert any("New thread:" in sent[1] for sent in fake.sent)


@pytest.mark.asyncio
async def test_rollback_is_help_alias_not_a_turn_or_mutate():
    backend = InMemoryGatewayBackend()
    fake = FakeBot()
    app = TelegramBotApp(token="fake", allowed_user_ids=[1], backend=backend)
    app._bot = None
    app._outbox = Outbox(sender=fake)
    await app.handle_update(_update_message(uid=1, user_id=1, chat_id=1, text="/rollback"))
    assert backend.turns == []
    assert backend.threads == []
    joined = "\n".join(sent[1] for sent in fake.sent)
    assert "/checkpoints" in joined
    assert "/undo" in joined


@pytest.mark.asyncio
async def test_mutating_config_set_is_refused_without_submit_turn():
    backend = InMemoryGatewayBackend()
    fake = FakeBot()
    app = TelegramBotApp(token="fake", allowed_user_ids=[1], backend=backend)
    app._bot = None
    app._outbox = Outbox(sender=fake)
    await app.handle_update(
        _update_message(uid=1, user_id=1, chat_id=1, text="/config set ui.theme light")
    )
    await app.handle_update(_update_message(uid=2, user_id=1, chat_id=1, text="/clear"))
    assert backend.turns == []
    joined = "\n".join(sent[1] for sent in fake.sent)
    assert "terminal" in joined.lower() or "jarn CLI" in joined
    assert "/config" in joined


@pytest.mark.asyncio
async def test_help_chunks_oversized_html(monkeypatch):
    backend = InMemoryGatewayBackend()
    fake = FakeBot()
    app = TelegramBotApp(token="fake", allowed_user_ids=[1], backend=backend)
    app._bot = None
    app._outbox = Outbox(sender=fake)
    monkeypatch.setattr(
        "jarn.telegram.outbox.chunk_html",
        lambda html, **kwargs: ["HELP-PART-1", "HELP-PART-2"],
    )
    await app.handle_update(_update_message(uid=1, user_id=1, chat_id=1, text="/help"))
    assert backend.turns == []
    bodies = [sent[1] for sent in fake.sent]
    assert bodies == ["HELP-PART-1", "HELP-PART-2"]


def test_botfather_menu_names_are_local_or_gateway_only():
    from jarn.commands.registry import (
        GATEWAY_LOCAL_COMMANDS,
        GATEWAY_ONLY_COMMANDS,
        gateway_botfather_commands,
        is_gateway_mutating_command,
    )

    menu = gateway_botfather_commands()
    names = {name for name, _ in menu}
    assert names <= (GATEWAY_LOCAL_COMMANDS | GATEWAY_ONLY_COMMANDS)
    assert {"stop", "new", "repo", "help", "reset"} <= names
    assert "config" not in names
    assert "clear" not in names
    assert "memory" not in names
    for name in names:
        if name not in GATEWAY_ONLY_COMMANDS:
            assert not is_gateway_mutating_command(name)
