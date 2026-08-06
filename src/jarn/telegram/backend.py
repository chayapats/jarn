"""Narrow daemon/session facade the Telegram bot calls (T-TG-2).

``gateway/daemon.py`` / ``sessions.py`` may not be present yet; the long-poll
app depends only on this Protocol. Tests ship an in-memory backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from jarn.gateway.protocol import MediaRef

__all__ = [
    "GatewayBackend",
    "InMemoryGatewayBackend",
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
    kind: str = "tool"  # tool | memory | skill | plan | yolo


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
        self.turns.append(
            TurnSubmission(chat_id=chat_id, user_id=user_id, text=text, media=refs)
        )

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
