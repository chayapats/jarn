"""DM-only auth: principal = ``from.id`` on every update (#34 / T-TG-2).

Deny-by-default. An empty ``allowed_user_ids`` list admits nobody. Fail closed
when ``from`` is missing (including callback queries without a user).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "AuthDecision",
    "AuthRejectReason",
    "authorize_update",
    "is_private_chat",
    "principal_from_update",
]


AuthRejectReason = str  # "not_private" | "no_principal" | "not_allowed"


@dataclass(slots=True, frozen=True)
class AuthDecision:
    """Result of :func:`authorize_update`."""

    ok: bool
    user_id: int | None = None
    chat_id: int | None = None
    reason: AuthRejectReason | None = None


def is_private_chat(chat: Any) -> bool:
    """True when *chat* is a Telegram private DM (``type == "private"``)."""
    if chat is None:
        return False
    chat_type = getattr(chat, "type", None)
    if chat_type is None and isinstance(chat, dict):
        chat_type = chat.get("type")
    value = getattr(chat_type, "value", chat_type)
    return str(value) == "private"


def principal_from_update(update: Any) -> int | None:
    """Extract ``from.id`` from a message, edited message, or callback query.

    Fail closed: missing ``from`` → ``None`` (never fall back to ``chat.id``).
    """
    if update is None:
        return None
    for attr in ("callback_query", "message", "edited_message"):
        obj = getattr(update, attr, None)
        if obj is None and isinstance(update, dict):
            obj = update.get(attr)
        if obj is None:
            continue
        user = getattr(obj, "from_user", None)
        if user is None:
            user = getattr(obj, "from", None)
        if user is None and isinstance(obj, dict):
            user = obj.get("from_user") or obj.get("from")
        if user is None:
            return None  # present envelope, missing principal → fail closed
        uid = getattr(user, "id", None)
        if uid is None and isinstance(user, dict):
            uid = user.get("id")
        if isinstance(uid, int) and not isinstance(uid, bool):
            return uid
        return None
    return None


def _chat_from_update(update: Any) -> Any:
    if update is None:
        return None
    cb = getattr(update, "callback_query", None)
    if cb is None and isinstance(update, dict):
        cb = update.get("callback_query")
    if cb is not None:
        msg = getattr(cb, "message", None)
        if msg is None and isinstance(cb, dict):
            msg = cb.get("message")
        if msg is not None:
            chat = getattr(msg, "chat", None)
            if chat is None and isinstance(msg, dict):
                chat = msg.get("chat")
            return chat
    for attr in ("message", "edited_message"):
        msg = getattr(update, attr, None)
        if msg is None and isinstance(update, dict):
            msg = update.get(attr)
        if msg is None:
            continue
        chat = getattr(msg, "chat", None)
        if chat is None and isinstance(msg, dict):
            chat = msg.get("chat")
        return chat
    return None


def authorize_update(
    update: Any,
    allowed_user_ids: Sequence[int],
) -> AuthDecision:
    """DM-only + allowlist check. Empty allowlist denies everyone."""
    chat = _chat_from_update(update)
    chat_id = getattr(chat, "id", None) if chat is not None else None
    if chat_id is None and isinstance(chat, dict):
        chat_id = chat.get("id")
    if not is_private_chat(chat):
        return AuthDecision(ok=False, chat_id=chat_id, reason="not_private")
    user_id = principal_from_update(update)
    if user_id is None:
        return AuthDecision(ok=False, chat_id=chat_id, reason="no_principal")
    allow = {int(x) for x in allowed_user_ids}
    if user_id not in allow:
        return AuthDecision(
            ok=False, user_id=user_id, chat_id=chat_id, reason="not_allowed"
        )
    return AuthDecision(ok=True, user_id=user_id, chat_id=chat_id)
