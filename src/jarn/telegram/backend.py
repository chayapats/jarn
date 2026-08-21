"""Narrow daemon/session facade the Telegram bot calls (T-TG-2).

The long-poll app depends only on this Protocol. Production wiring uses
:class:`SessionRouterBackend` (daemon + sessions); tests use
:class:`InMemoryGatewayBackend`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from jarn.gateway.approvals import PendingApprovalMap
from jarn.gateway.protocol import (
    ApprovalAskFrame,
    ErrorFrame,
    EventFrame,
    MediaRef,
    OutboundFrame,
    StatusFrame,
)

if TYPE_CHECKING:
    from jarn.gateway.daemon import DaemonSupervisor
    from jarn.gateway.sessions import SessionRouter
    from jarn.telegram.outbox import Outbox

_log = logging.getLogger("jarn.telegram.backend")

__all__ = [
    "GatewayBackend",
    "InMemoryGatewayBackend",
    "SessionRouterBackend",
    "TurnSubmission",
    "VerdictSubmission",
]


@dataclass(slots=True, frozen=True)
class TurnSubmission:
    """One inbound user turn recorded by :class:`InMemoryGatewayBackend`."""

    chat_id: int
    user_id: int
    text: str
    media: tuple[MediaRef, ...] = ()


@dataclass(slots=True, frozen=True)
class VerdictSubmission:
    """One approval / plan / memory / skill verdict from a callback button."""

    chat_id: int
    user_id: int
    token: str
    approved: bool
    scope: str = "once"
    plan_mode_target: str | None = None
    message: str = ""
    kind: str = "tool"  # tool | memory | skill | plan | yolo | undo


@runtime_checkable
class GatewayBackend(Protocol):
    """Session/daemon surface the Telegram transport talks to.

    Implementations live in ``jarn.gateway`` (daemon + sessions). The bot must
    not import those modules directly so Wave-3 packages stay parallel-safe.
    """

    async def submit_turn(
        self,
        *,
        chat_id: int,
        user_id: int,
        text: str,
        media: list[MediaRef] | None = None,
    ) -> None:
        """Queue one user turn (text + optional staged media refs)."""

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
        """Deliver an approval-card (or yolo-confirm) verdict."""

    async def stop(self, *, chat_id: int, user_id: int) -> None:
        """``/stop`` — cancel in-flight turn / drain busy queue for this chat."""

    async def new_thread(self, *, chat_id: int, user_id: int) -> str:
        """``/new`` — mint a fresh ``thread_id`` for ``(chat_id, root)``."""

    async def set_repo(self, *, chat_id: int, user_id: int, name_or_path: str) -> str:
        """``/repo`` — switch active root (allowlist-enforced by the daemon)."""


@dataclass
class InMemoryGatewayBackend:
    """Fake backend for unit tests — records calls, no workers."""

    turns: list[TurnSubmission] = field(default_factory=list)
    verdicts: list[VerdictSubmission] = field(default_factory=list)
    stops: list[tuple[int, int]] = field(default_factory=list)
    threads: list[tuple[int, int]] = field(default_factory=list)
    repos: list[tuple[int, int, str]] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    _thread_seq: int = 0
    _active_repo: str = "personal"

    async def submit_turn(
        self,
        *,
        chat_id: int,
        user_id: int,
        text: str,
        media: list[MediaRef] | None = None,
    ) -> None:
        refs = tuple(media or ())
        self.turns.append(TurnSubmission(chat_id=chat_id, user_id=user_id, text=text, media=refs))

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
        self.verdicts.append(
            VerdictSubmission(
                chat_id=chat_id,
                user_id=user_id,
                token=token,
                approved=approved,
                scope=scope,
                plan_mode_target=plan_mode_target,
                message=message,
                kind=kind,
            )
        )

    async def stop(self, *, chat_id: int, user_id: int) -> None:
        self.stops.append((chat_id, user_id))

    async def new_thread(self, *, chat_id: int, user_id: int) -> str:
        self._thread_seq += 1
        self.threads.append((chat_id, user_id))
        return f"thread-{self._thread_seq}"

    async def set_repo(self, *, chat_id: int, user_id: int, name_or_path: str) -> str:
        self.repos.append((chat_id, user_id, name_or_path))
        self._active_repo = name_or_path
        return name_or_path

    def record_notice(self, text: str) -> None:
        """Test helper — daemon would push notices through the outbox."""
        self.notices.append(text)

    def last_turn(self) -> TurnSubmission | None:
        return self.turns[-1] if self.turns else None

    def as_mapping(self) -> dict[str, Any]:
        """Debug snapshot for assertions."""
        return {
            "turns": len(self.turns),
            "verdicts": len(self.verdicts),
            "stops": len(self.stops),
            "threads": len(self.threads),
            "repos": list(self.repos),
        }


@dataclass
class SessionRouterBackend:
    """Async :class:`GatewayBackend` over :class:`~jarn.gateway.sessions.SessionRouter`.

    The router/supervisor APIs are synchronous (pipe I/O + locks); this adapter
    exposes the async surface the Telegram transport awaits.
    """

    router: SessionRouter
    supervisor: DaemonSupervisor
    _outbox: Outbox | None = field(default=None, init=False, repr=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)
    _bound: bool = field(default=False, init=False, repr=False)

    @property
    def approval_store(self) -> PendingApprovalMap:
        return self.router.approval_store

    def bind_outbox(
        self,
        outbox: Outbox,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Bridge synchronous worker hooks onto the Telegram asyncio loop."""
        self._outbox = outbox
        self._loop = loop or asyncio.get_running_loop()
        if self._bound:
            return

        previous_event = self.router.on_event
        previous_notice = self.router.on_notice

        def on_event(chat_id: int, root: Path, frame: OutboundFrame) -> None:
            if previous_event is not None:
                previous_event(chat_id, root, frame)
            if self._outbox is not None:
                self._schedule(self._deliver_frame(chat_id, frame))

        def on_notice(chat_id: int, text: str) -> None:
            if previous_notice is not None:
                previous_notice(chat_id, text)
            if self._outbox is not None:
                self._schedule(self._deliver_notice(chat_id, text))

        self.router.on_event = on_event
        self.router.on_notice = on_notice
        self._bound = True

    def unbind_outbox(self) -> None:
        """Stop scheduling transport work while workers are being shut down."""
        self._outbox = None
        self._loop = None

    def _schedule(self, coro: Coroutine[Any, Any, Any]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            coro.close()
            _log.error("dropping Telegram delivery: event loop is unavailable")
            return
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            coro.close()
            _log.exception("failed to schedule Telegram delivery")
            return

        def report_failure(done: Any) -> None:
            try:
                done.result()
            except Exception:  # noqa: BLE001
                _log.exception("Telegram delivery failed")

        future.add_done_callback(report_failure)

    async def _deliver_notice(self, chat_id: int, text: str) -> None:
        if self._outbox is not None:
            await self._outbox.send_notice(chat_id, text)

    async def _deliver_frame(self, chat_id: int, frame: OutboundFrame) -> None:
        outbox = self._outbox
        if outbox is None:
            return
        if isinstance(frame, StatusFrame):
            await outbox.maybe_long_running(chat_id, turn_in_flight=frame.turn_in_flight)
            if frame.turn_in_flight:
                await outbox.keepalive_draft(chat_id)
            return
        if isinstance(frame, EventFrame):
            if frame.kind == "thread_switch":
                new_id = frame.data.get("thread_id") if isinstance(frame.data, dict) else None
                if isinstance(new_id, str) and new_id:
                    binder = getattr(self.router, "bind_thread", None)
                    if callable(binder):
                        binder(chat_id, new_id)
                return
            await outbox.on_event(
                chat_id,
                kind=frame.kind,
                text=frame.text,
                data=dict(frame.data),
                progress=frame.progress,
            )
            return
        if isinstance(frame, ErrorFrame):
            await outbox.on_event(
                chat_id,
                kind="error",
                text=frame.message,
                data={"code": frame.code},
            )
            return
        if isinstance(frame, ApprovalAskFrame):
            message = await outbox.send_approval_card(
                chat_id,
                token=frame.token,
                **_approval_card_kwargs(frame),
            )
            message_id = _message_id(message)
            if isinstance(message_id, int):
                self.approval_store.set_message_id(frame.token, message_id)

    async def restore_pending_approvals(
        self,
        *,
        allowed_chat_ids: Iterable[int],
    ) -> int:
        """Re-display durable approval cards after a gateway restart."""
        outbox = self._outbox
        if outbox is None:
            raise RuntimeError("Telegram outbox is not bound")
        allowed = {int(chat_id) for chat_id in allowed_chat_ids}
        restored = 0
        for record in self.approval_store.list():
            if record.chat_id not in allowed:
                _log.warning(
                    "not re-carding token=%s: chat_id=%s is not allowlisted",
                    record.token,
                    record.chat_id,
                )
                continue
            try:
                self.router.resolve_repo(record.root)
            except ValueError as exc:
                _log.warning(
                    "not re-carding token=%s: stored root is no longer allowlisted (%s)",
                    record.token,
                    exc,
                )
                continue
            if record.card is None:
                _log.warning(
                    "not re-carding legacy token=%s: card metadata is unavailable",
                    record.token,
                )
                continue
            try:
                message = await outbox.send_approval_card(
                    record.chat_id,
                    token=record.token,
                    **dict(record.card),
                )
            except Exception:  # noqa: BLE001 -- one blocked chat must not stop others
                _log.exception(
                    "failed to re-card parked approval token=%s chat=%s",
                    record.token,
                    record.chat_id,
                )
                continue
            message_id = _message_id(message)
            if message_id is not None:
                self.approval_store.set_message_id(record.token, message_id)
            restored += 1
        return restored

    async def submit_turn(
        self,
        *,
        chat_id: int,
        user_id: int,
        text: str,
        media: list[MediaRef] | None = None,
    ) -> None:
        del user_id  # auth already enforced by the transport allowlist
        self.router.submit_turn(chat_id, text, media=media)

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
        del user_id
        confirm_kinds = {"yolo", "undo"}
        record = None if kind in confirm_kinds else self.approval_store.get(token)
        if record is None and kind not in confirm_kinds:
            raise KeyError(f"unknown or expired approval token: {token}")
        if record is None:
            # Yolo / undo confirmations are controller-owned and have no parked row.
            root = self.router.active_root(chat_id)
        else:
            if record.chat_id not in (0, chat_id):
                raise PermissionError("approval token belongs to another chat")
            root = self.router.claim_approval_resume(
                chat_id,
                root=record.root,
                thread_id=record.thread_id,
            )
        try:
            self.supervisor.send_approval_verdict(
                root,
                token=token,
                approved=approved,
                scope=scope,
                message=message,
                plan_mode_target=plan_mode_target,
            )
        except Exception:
            if record is not None:
                self.router.release_approval_claim(root)
            raise

    async def stop(self, *, chat_id: int, user_id: int) -> None:
        del user_id
        self.router.cmd_stop(chat_id)

    async def new_thread(self, *, chat_id: int, user_id: int) -> str:
        del user_id
        return self.router.cmd_new(chat_id)

    async def set_repo(self, *, chat_id: int, user_id: int, name_or_path: str) -> str:
        del user_id
        return str(self.router.cmd_repo(chat_id, name_or_path))


def _approval_card_kwargs(frame: ApprovalAskFrame) -> dict[str, Any]:
    """Translate a private worker frame to the public Telegram card surface."""
    return {
        "action": frame.action,
        "target": frame.target,
        "description": frame.description,
        "args": dict(frame.args),
        "plan": frame.plan,
        "suggested_memory": frame.suggested_memory,
        "suggested_skill": frame.suggested_skill,
        "dangerous": frame.dangerous,
    }


def _message_id(message: Any) -> int | None:
    value = (
        message.get("message_id")
        if isinstance(message, Mapping)
        else getattr(message, "message_id", None)
    )
    return value if isinstance(value, int) and not isinstance(value, bool) else None
