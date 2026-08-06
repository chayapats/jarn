"""T-TG-2: DM-only auth + principal on callbacks (#34)."""

from __future__ import annotations

from types import SimpleNamespace

from jarn.telegram.auth import (
    authorize_update,
    is_private_chat,
    principal_from_update,
)


def _msg(*, chat_type: str, chat_id: int, user_id: int | None, text: str = "hi"):
    chat = SimpleNamespace(id=chat_id, type=chat_type)
    user = None if user_id is None else SimpleNamespace(id=user_id)
    return SimpleNamespace(
        message=SimpleNamespace(chat=chat, from_user=user, text=text),
        callback_query=None,
        edited_message=None,
    )


def _cb(*, chat_type: str, chat_id: int, user_id: int | None, data: str = "t:tok:once"):
    chat = SimpleNamespace(id=chat_id, type=chat_type)
    user = None if user_id is None else SimpleNamespace(id=user_id)
    msg = SimpleNamespace(chat=chat)
    return SimpleNamespace(
        message=None,
        edited_message=None,
        callback_query=SimpleNamespace(
            id="1", data=data, from_user=user, message=msg
        ),
    )


def test_is_private_chat():
    assert is_private_chat(SimpleNamespace(type="private"))
    assert not is_private_chat(SimpleNamespace(type="group"))
    assert not is_private_chat(None)


def test_principal_from_message_and_callback():
    upd = _msg(chat_type="private", chat_id=1, user_id=42)
    assert principal_from_update(upd) == 42
    cb = _cb(chat_type="private", chat_id=1, user_id=99)
    assert principal_from_update(cb) == 99


def test_principal_fail_closed_when_from_missing():
    upd = _msg(chat_type="private", chat_id=1, user_id=None)
    assert principal_from_update(upd) is None
    decision = authorize_update(upd, [1])
    assert decision.ok is False
    assert decision.reason == "no_principal"


def test_deny_by_default_empty_allowlist():
    upd = _msg(chat_type="private", chat_id=1, user_id=42)
    decision = authorize_update(upd, [])
    assert decision.ok is False
    assert decision.reason == "not_allowed"


def test_reject_group_chat():
    upd = _msg(chat_type="group", chat_id=-100, user_id=42)
    decision = authorize_update(upd, [42])
    assert decision.ok is False
    assert decision.reason == "not_private"


def test_allowlisted_dm_ok():
    upd = _msg(chat_type="private", chat_id=1, user_id=42)
    decision = authorize_update(upd, [42, 7])
    assert decision.ok is True
    assert decision.user_id == 42
    assert decision.chat_id == 1


def test_callback_auth_uses_from_id_not_chat():
    """Fail closed: callback principal is from.id even when chat differs."""
    cb = _cb(chat_type="private", chat_id=1, user_id=42)
    assert authorize_update(cb, [42]).ok is True
    assert authorize_update(cb, [999]).ok is False
