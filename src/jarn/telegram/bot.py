"""aiogram 3 long-poll Telegram transport (T-TG-2).

Binding
-------
* DM-only; deny-by-default allowlist on ``from.id`` (messages **and** callbacks).
* Restart backlog: fetch once, report verbatim, **do not execute** (#53), then
  re-display durable parked approval cards.
* Second-poller: host :class:`~jarn.telegram.poller_lock.PollerLock`; on first
  Telegram 409 (``TelegramConflictError``) send one chat notice and exit with
  :data:`EXIT_CONFLICT` (75) — **never retry 409**, **never call ``logOut``**.
  Same-host flock contention exits :data:`EXIT_LOCK_HELD` (76). Wire both into
  systemd ``RestartPreventExitStatus`` (see ``docs/TELEGRAM_GATEWAY.md``).
* ``getWebhookInfo`` at startup: report, do not repair.
* Invalid/unauthorized tokens fail fast with :data:`EXIT_UNAUTHORIZED` (77).
* Depends on :class:`~jarn.telegram.backend.GatewayBackend` only (not daemon.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jarn.telegram.auth import authorize_update
from jarn.telegram.backend import GatewayBackend
from jarn.telegram.inbound_media import (
    AiogramDownloader,
    download_and_prepare,
)
from jarn.telegram.outbox import Outbox, parse_callback
from jarn.telegram.poller_lock import PollerLock, PollerLockHeldError

_log = logging.getLogger("jarn.telegram.bot")

__all__ = [
    "EXIT_CONFLICT",
    "EXIT_LOCK_HELD",
    "EXIT_UNAUTHORIZED",
    "TelegramBotApp",
    "BacklogReport",
    "describe_update_verbatim",
    "drain_backlog",
    "run_gateway_bot",
]

#: Distinct process exit when another getUpdates client won the token (#53).
#: Bind systemd ``RestartPreventExitStatus=`` to this value.
EXIT_CONFLICT = 75

#: Distinct exit when the host flock is already held (same-box second poller).
EXIT_LOCK_HELD = 76

#: Permanent authentication/configuration failure; never retry in the poll loop.
EXIT_UNAUTHORIZED = 77

_CONFLICT_NOTICE = (
    "jarn gateway: another getUpdates client took this bot token "
    "(Telegram 409). Standing down — not retrying, not calling logOut."
)

_GATEWAY_HELP_ROWS: tuple[tuple[str, str], ...] = (
    ("/stop", "Cancel the in-flight turn"),
    ("/new", "Start a fresh thread"),
    ("/reset", "Alias of /new (fresh gateway thread, not /clear)"),
    ("/repo <name>", "Switch the active repo"),
    ("/help [name]", "This catalog (same commands as the REPL)"),
    ("/rollback", "Alias: use /checkpoints and /undo — not a mutate command"),
)

_ROLLBACK_NOTICE = (
    "There is no /rollback mutate command. Use /checkpoints to list restores "
    "and /undo to revert the last turn."
)


def _split_slash(text: str) -> tuple[str, str] | None:
    """``(/name, rest)`` for a Telegram slash line; strips ``@bot`` suffix."""
    from jarn.commands.registry import parse_slash_line

    return parse_slash_line(text)


def _telegram_help_html(topic: str = "") -> str:
    """``/help`` body in Telegram HTML — same catalog as the REPL."""
    from jarn.commands.help import format_help, format_help_detail
    from jarn.tui import layout

    if topic:
        return format_help_detail(topic, dialect="html")
    lines = [format_help(dialect="html").rstrip(), ""]
    lines.append(layout.section("Gateway", dialect="html"))
    for name, desc in _GATEWAY_HELP_ROWS:
        lines.append(layout.row(name, desc, dialect="html"))
    return "\n".join(lines)


def _is_telegram_unauthorized(exc: BaseException) -> bool:
    """Classify Telegram's permanent 401 without coupling module import-time."""
    try:
        from aiogram.exceptions import TelegramUnauthorizedError
    except ImportError:  # pragma: no cover - telegram extra validated at startup
        return False
    return isinstance(exc, TelegramUnauthorizedError)


@dataclass(slots=True)
class BacklogReport:
    """Result of the startup backlog drain (#53) — nothing was executed."""

    count: int
    offset: int | None
    lines: list[str] = field(default_factory=list)
    webhook_url: str = ""
    pending_update_count: int | None = None


def describe_update_verbatim(update: Any) -> str:
    """One human-readable line for the backlog report (no execution)."""
    uid = getattr(update, "update_id", None)
    if uid is None and isinstance(update, dict):
        uid = update.get("update_id")
    msg = getattr(update, "message", None) or getattr(update, "edited_message", None)
    if msg is None and isinstance(update, dict):
        msg = update.get("message") or update.get("edited_message")
    cb = getattr(update, "callback_query", None)
    if cb is None and isinstance(update, dict):
        cb = update.get("callback_query")
    if cb is not None:
        data = getattr(cb, "data", None)
        if data is None and isinstance(cb, dict):
            data = cb.get("data")
        user = getattr(cb, "from_user", None) or getattr(cb, "from", None)
        if user is None and isinstance(cb, dict):
            user = cb.get("from_user") or cb.get("from")
        from_id = getattr(user, "id", None) if user is not None else None
        if from_id is None and isinstance(user, dict):
            from_id = user.get("id")
        return f"update_id={uid} callback from={from_id} data={data!r}"
    if msg is not None:
        text = getattr(msg, "text", None) or getattr(msg, "caption", None)
        if text is None and isinstance(msg, dict):
            text = msg.get("text") or msg.get("caption")
        user = getattr(msg, "from_user", None) or getattr(msg, "from", None)
        if user is None and isinstance(msg, dict):
            user = msg.get("from_user") or msg.get("from")
        from_id = getattr(user, "id", None) if user is not None else None
        if from_id is None and isinstance(user, dict):
            from_id = user.get("id")
        chat = getattr(msg, "chat", None)
        if chat is None and isinstance(msg, dict):
            chat = msg.get("chat")
        chat_id = getattr(chat, "id", None) if chat is not None else None
        if chat_id is None and isinstance(chat, dict):
            chat_id = chat.get("id")
        preview = (text or "").replace("\n", "\\n")
        if len(preview) > 200:
            preview = preview[:200] + "…"
        return f"update_id={uid} message chat={chat_id} from={from_id} text={preview!r}"
    return f"update_id={uid} (unrecognized update kind)"


async def drain_backlog(bot: Any) -> BacklogReport:
    """Fetch pending updates once, confirm offset, execute nothing (#53).

    Also reads ``getWebhookInfo`` (report-only; never ``deleteWebhook`` /
    ``logOut``).
    """
    webhook_url = ""
    pending_update_count: int | None = None
    try:
        info = await bot.get_webhook_info()
        webhook_url = getattr(info, "url", "") or ""
        pending_update_count = getattr(info, "pending_update_count", None)
        if webhook_url:
            _log.warning(
                "getWebhookInfo reports url=%r pending=%s — not repairing "
                "(deleteWebhook would stomp another deployment)",
                webhook_url,
                pending_update_count,
            )
        else:
            _log.info(
                "getWebhookInfo: no webhook; pending_update_count=%s",
                pending_update_count,
            )
    except Exception as exc:  # noqa: BLE001
        if _is_telegram_unauthorized(exc):
            raise
        _log.warning("getWebhookInfo failed (continuing): %s", exc)

    lines: list[str] = []
    offset: int | None = None
    total = 0
    # Drain in pages until empty. timeout=0 = short poll; do not execute.
    while True:
        try:
            updates = await bot.get_updates(offset=offset, timeout=0, limit=100)
        except Exception as exc:  # noqa: BLE001
            if _is_telegram_unauthorized(exc):
                raise
            _log.error("backlog getUpdates failed: %s", exc)
            break
        if not updates:
            break
        for upd in updates:
            total += 1
            lines.append(describe_update_verbatim(upd))
            uid = getattr(upd, "update_id", None)
            if uid is None and isinstance(upd, dict):
                uid = upd.get("update_id")
            if isinstance(uid, int):
                offset = uid + 1
        if len(updates) < 100:
            break

    return BacklogReport(
        count=total,
        offset=offset,
        lines=lines,
        webhook_url=webhook_url,
        pending_update_count=pending_update_count,
    )


@dataclass
class TelegramBotApp:
    """Long-poll application wired to a :class:`GatewayBackend` + :class:`Outbox`."""

    token: str
    allowed_user_ids: Sequence[int]
    backend: GatewayBackend
    project_root: Path | None = None
    notify_chat_id: int | None = None
    poll_timeout: int = 25
    tool_progress: str = "off"
    tool_progress_cleanup: str = "delete"
    long_running_notifications: bool = True
    busy_ack_detail: bool = False
    _bot: Any = field(default=None, repr=False)
    _outbox: Outbox | None = field(default=None, repr=False)
    _offset: int | None = field(default=None, repr=False)
    _running: bool = field(default=False, repr=False)

    @property
    def outbox(self) -> Outbox:
        if self._outbox is None:
            raise RuntimeError("outbox not started")
        return self._outbox

    async def start(self) -> int:
        """Acquire host flock, drain backlog, long-poll until stop/409.

        Returns a process exit code (0 = clean stop, :data:`EXIT_CONFLICT`,
        :data:`EXIT_LOCK_HELD`, or :data:`EXIT_UNAUTHORIZED` on stand-down).
        """
        from jarn.telegram import require_aiogram

        require_aiogram()
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode
        from aiogram.exceptions import TelegramUnauthorizedError
        from aiogram.utils.token import TokenValidationError

        try:
            lock = PollerLock()
            lock.acquire()
        except PollerLockHeldError as exc:
            _log.error("%s", exc)
            return EXIT_LOCK_HELD

        try:
            try:
                self._bot = Bot(
                    token=self.token,
                    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
                )
            except TokenValidationError as exc:
                _log.error("invalid Telegram bot token: %s", exc)
                return EXIT_UNAUTHORIZED
            self._outbox = Outbox(
                sender=self._bot,
                progress=self.tool_progress,
                tool_progress_cleanup=self.tool_progress_cleanup,
                long_running_notifications=self.long_running_notifications,
                busy_ack_detail=self.busy_ack_detail,
            )
            binder = getattr(self.backend, "bind_outbox", None)
            if callable(binder):
                binder(self._outbox, loop=asyncio.get_running_loop())
            try:
                report = await drain_backlog(self._bot)
            except TelegramUnauthorizedError as exc:
                _log.error("Telegram bot token unauthorized — standing down: %s", exc)
                return EXIT_UNAUTHORIZED
            self._offset = report.offset
            await self._report_backlog(report)
            restorer = getattr(self.backend, "restore_pending_approvals", None)
            if callable(restorer):
                restored = await restorer(
                    allowed_chat_ids=self.allowed_user_ids,
                )
                if restored:
                    _log.info("re-displayed %s parked approval card(s)", restored)
            await self._register_bot_commands()
            return await self._poll_loop()
        finally:
            unbinder = getattr(self.backend, "unbind_outbox", None)
            if callable(unbinder):
                unbinder()
            lock.release()
            if self._bot is not None:
                await self._bot.session.close()

    async def _report_backlog(self, report: BacklogReport) -> None:
        summary_lines = [
            f"jarn gateway restart backlog: {report.count} update(s) "
            f"fetched and discarded (not executed).",
        ]
        if report.webhook_url:
            summary_lines.append(f"webhook still set: {report.webhook_url}")
        if report.pending_update_count is not None:
            summary_lines.append(
                f"getWebhookInfo.pending_update_count={report.pending_update_count}"
            )
        for line in report.lines[:50]:
            summary_lines.append(f"  · {line}")
        if report.count > 50:
            summary_lines.append(f"  · … +{report.count - 50} more")
        summary = "\n".join(summary_lines)
        _log.warning("%s", summary)
        chat_id = self.notify_chat_id
        if chat_id is None and self.allowed_user_ids:
            # Single-operator appliance: DM the first allowlisted id.
            chat_id = int(self.allowed_user_ids[0])
        if chat_id is not None and self._outbox is not None:
            try:
                await self._outbox.send_notice(chat_id, summary)
            except Exception as exc:  # noqa: BLE001
                _log.warning("failed to DM backlog report: %s", exc)

    async def _poll_loop(self) -> int:
        assert self._bot is not None
        self._running = True
        from aiogram.exceptions import (
            TelegramConflictError,
            TelegramUnauthorizedError,
        )

        while self._running:
            try:
                updates = await self._bot.get_updates(
                    offset=self._offset,
                    timeout=self.poll_timeout,
                    allowed_updates=[
                        "message",
                        "edited_message",
                        "callback_query",
                    ],
                )
            except TelegramConflictError as exc:
                _log.error("Telegram 409 conflict — standing down: %s", exc)
                await self._stand_down_conflict()
                return EXIT_CONFLICT
            except TelegramUnauthorizedError as exc:
                _log.error("Telegram bot token unauthorized — standing down: %s", exc)
                return EXIT_UNAUTHORIZED
            except Exception as exc:  # noqa: BLE001
                # Transient network errors: brief pause then continue.
                # Never treat non-409 as conflict, never call logOut.
                _log.warning("getUpdates error (will retry): %s", exc)
                await asyncio.sleep(1)
                continue
            for upd in updates:
                uid = getattr(upd, "update_id", None)
                if isinstance(uid, int):
                    self._offset = uid + 1
                try:
                    await self.handle_update(upd)
                except Exception as exc:  # noqa: BLE001
                    _log.exception("handle_update failed: %s", exc)
        return 0

    async def _stand_down_conflict(self) -> None:
        """One chat notice, then exit. Never retry 409. Never logOut."""
        chat_id = self.notify_chat_id
        if chat_id is None and self.allowed_user_ids:
            chat_id = int(self.allowed_user_ids[0])
        if chat_id is not None and self._bot is not None:
            try:
                await self._bot.send_message(chat_id, _CONFLICT_NOTICE)
            except Exception as exc:  # noqa: BLE001
                _log.warning("conflict notice send failed: %s", exc)

    async def _register_bot_commands(self) -> None:
        """Publish the BotFather menu from local + gateway-only names."""
        if self._bot is None:
            return
        setter = getattr(self._bot, "set_my_commands", None)
        if not callable(setter):
            return
        from jarn.commands.registry import gateway_botfather_commands

        rows = gateway_botfather_commands()
        try:
            from aiogram.types import BotCommand

            commands = [
                BotCommand(command=name, description=description) for name, description in rows
            ]
        except Exception:  # noqa: BLE001 — tests/fakes accept tuples
            commands = [{"command": name, "description": description} for name, description in rows]
        try:
            await setter(commands)
        except Exception:  # noqa: BLE001 — menu is best-effort
            _log.warning("setMyCommands failed", exc_info=True)

    def stop(self) -> None:
        self._running = False

    async def handle_update(self, update: Any) -> None:
        """Authorize + dispatch one update (used by tests with fakes)."""
        decision = authorize_update(update, self.allowed_user_ids)
        if not decision.ok:
            _log.info(
                "reject update reason=%s user=%s chat=%s",
                decision.reason,
                decision.user_id,
                decision.chat_id,
            )
            return
        assert decision.user_id is not None and decision.chat_id is not None
        user_id = decision.user_id
        chat_id = decision.chat_id

        cb = getattr(update, "callback_query", None)
        if cb is None and isinstance(update, dict):
            cb = update.get("callback_query")
        if cb is not None:
            try:
                await self._handle_callback(
                    cb,
                    chat_id=chat_id,
                    user_id=user_id,
                )
            except (LookupError, PermissionError, ValueError) as exc:
                _log.info(
                    "approval callback rejected chat=%s user=%s: %s",
                    chat_id,
                    user_id,
                    exc,
                )
                if self._outbox is not None:
                    await self._outbox.send_notice(
                        chat_id,
                        f"Approval could not be resumed: {exc}. If the card is "
                        "stale, re-run the request to create a new one.",
                    )
            return

        msg = getattr(update, "message", None) or getattr(update, "edited_message", None)
        if msg is None and isinstance(update, dict):
            msg = update.get("message") or update.get("edited_message")
        if msg is None:
            return
        await self._handle_message(msg, chat_id=chat_id, user_id=user_id)

    async def _handle_callback(self, cb: Any, *, chat_id: int, user_id: int) -> None:
        data = getattr(cb, "data", None)
        if data is None and isinstance(cb, dict):
            data = cb.get("data")
        parsed = parse_callback(data if isinstance(data, str) else None)
        # Always answer the callback to clear the spinner (best-effort).
        if self._bot is not None:
            cb_id = getattr(cb, "id", None)
            if cb_id is None and isinstance(cb, dict):
                cb_id = cb.get("id")
            if cb_id is not None:
                with contextlib.suppress(Exception):
                    await self._bot.answer_callback_query(cb_id)
        if parsed is None:
            return

        if parsed.kind in {"yolo", "undo"}:
            await self.backend.submit_verdict(
                chat_id=chat_id,
                user_id=user_id,
                token=parsed.token,
                approved=parsed.action == "ok",
                kind=parsed.kind,
            )
            return

        if parsed.kind == "tool":
            approved = parsed.action in {"once", "session"}
            scope = "session" if parsed.action == "session" else "once"
            await self.backend.submit_verdict(
                chat_id=chat_id,
                user_id=user_id,
                token=parsed.token,
                approved=approved,
                scope=scope if approved else "once",
                kind="tool",
                message="" if approved else "denied by operator",
            )
            return

        if parsed.kind in {"memory", "skill"}:
            await self.backend.submit_verdict(
                chat_id=chat_id,
                user_id=user_id,
                token=parsed.token,
                approved=parsed.action == "save",
                kind=parsed.kind,
                message="" if parsed.action == "save" else "declined",
            )
            return

        if parsed.kind == "plan":
            if parsed.action == "keep":
                await self.backend.submit_verdict(
                    chat_id=chat_id,
                    user_id=user_id,
                    token=parsed.token,
                    approved=False,
                    kind="plan",
                    message="keep planning",
                )
                return
            target = "auto-edit" if parsed.action == "auto-edit" else "ask"
            await self.backend.submit_verdict(
                chat_id=chat_id,
                user_id=user_id,
                token=parsed.token,
                approved=True,
                kind="plan",
                plan_mode_target=target,
            )

    async def _handle_message(self, msg: Any, *, chat_id: int, user_id: int) -> None:
        text = getattr(msg, "text", None) or getattr(msg, "caption", None) or ""
        if text is None:
            text = ""
        text = str(text)

        parsed = _split_slash(text)
        if parsed is not None:
            name, rest = parsed
            if name == "stop":
                await self.backend.stop(chat_id=chat_id, user_id=user_id)
                if self._outbox:
                    await self._outbox.send_notice(chat_id, "Stopped.")
                return
            if name in {"new", "reset"}:
                thread_id = await self.backend.new_thread(chat_id=chat_id, user_id=user_id)
                if self._outbox:
                    await self._outbox.send_notice(chat_id, f"New thread: {thread_id}")
                return
            if name == "repo":
                from jarn.tui import layout

                if not rest:
                    if self._outbox:
                        await self._outbox.send_html(
                            chat_id,
                            layout.err("Usage: /repo <name-or-path>", dialect="html"),
                        )
                    return
                try:
                    root = await self.backend.set_repo(
                        chat_id=chat_id, user_id=user_id, name_or_path=rest
                    )
                except Exception as exc:  # noqa: BLE001
                    if self._outbox:
                        await self._outbox.send_notice(chat_id, f"/repo failed: {exc}")
                    return
                if self._outbox:
                    await self._outbox.send_notice(chat_id, f"Active repo: {root}")
                return
            if name == "help":
                if self._outbox:
                    await self._outbox.send_html(chat_id, _telegram_help_html(rest))
                return
            if name == "rollback":
                if self._outbox:
                    await self._outbox.send_notice(chat_id, _ROLLBACK_NOTICE)
                return
            from jarn.commands.registry import (
                GATEWAY_ONLY_COMMANDS,
                gateway_mutating_notice,
                is_gateway_mutating_command,
            )

            if is_gateway_mutating_command(name) and name not in GATEWAY_ONLY_COMMANDS:
                if self._outbox:
                    await self._outbox.send_notice(chat_id, gateway_mutating_notice(name))
                return

        # Media path (T-TG-4).
        media_refs = []
        turn_text = text
        if self._bot is not None:
            downloader = AiogramDownloader(self._bot)
            result = await download_and_prepare(
                msg,
                downloader,
                caption=text,
                project_root=self.project_root,
            )
            turn_text = result.text
            media_refs = list(result.media_refs)
            if result.refusals and self._outbox is not None:
                for refusal in result.refusals:
                    await self._outbox.send_media_refusal(
                        chat_id,
                        message=refusal.message,
                        reason=refusal.reason,
                        modality=refusal.modality,
                        filename=refusal.filename,
                    )
            if not result.has_work:
                return
        elif not turn_text.strip():
            return

        await self.backend.submit_turn(
            chat_id=chat_id,
            user_id=user_id,
            text=turn_text,
            media=media_refs or None,
        )


async def run_gateway_bot(
    *,
    token: str,
    allowed_user_ids: Sequence[int],
    backend: GatewayBackend,
    project_root: Path | None = None,
    notify_chat_id: int | None = None,
    tool_progress: str = "off",
    tool_progress_cleanup: str = "delete",
    long_running_notifications: bool = True,
    busy_ack_detail: bool = False,
) -> int:
    """Convenience entry used by ``python -m jarn.telegram``."""
    app = TelegramBotApp(
        token=token,
        allowed_user_ids=allowed_user_ids,
        backend=backend,
        project_root=project_root,
        notify_chat_id=notify_chat_id,
        tool_progress=tool_progress,
        tool_progress_cleanup=tool_progress_cleanup,
        long_running_notifications=long_running_notifications,
        busy_ack_detail=busy_ack_detail,
    )
    return await app.start()
