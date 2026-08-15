"""Telegram outbox: draft→finalize HTML + approval/media cards (#40/#37/#39).

Binding (#40)
-------------
* Stream assistant prose via ``sendMessageDraft``; finalize with ``sendMessage``.
* HTML parse mode (never MarkdownV2).
* Tool progress OFF — ignore ``tool_start`` / ``tool_end`` / ``tool_progress``.
* Drop subagent inner stream (events stamped with ``data['agent']``).
* Any real message (approval card, notice, media refusal) destroys the live
  draft — restart with a fresh ``draft_id`` for subsequent prose.

Cards (#37/#39)
---------------
* Tool: name / description / redacted args; Once / Session / Deny (no Always).
* Memory / skill: Save / Decline.
* Plan-mode: auto-edit / ask / keep planning (three-way).
* Yolo escalate confirm: Confirm / Cancel.
* Media refusal: plain notice card (no buttons).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from jarn.agent.tool_arg_redact import sanitize_tool_args
from jarn.telegram.htmlutil import (
    TELEGRAM_MESSAGE_MAX,
    chunk_html,
    escape_html,
    format_code,
    format_pre,
)
from jarn.tui import grammar

_log = logging.getLogger("jarn.telegram.outbox")

__all__ = [
    "CallbackKind",
    "Outbox",
    "TelegramSender",
    "build_approval_card",
    "build_media_refusal_card",
    "build_yolo_confirm_card",
    "encode_callback",
    "parse_callback",
    "should_drop_event",
]

#: Callback payload budget (Telegram hard limit is 64 bytes).
_CB_MAX = 64


class TelegramSender(Protocol):
    """Minimal Bot surface the outbox needs (aiogram ``Bot`` satisfies this)."""

    async def send_message_draft(
        self,
        chat_id: int,
        draft_id: int,
        text: str | None = None,
        parse_mode: str | None = None,
        **kwargs: Any,
    ) -> Any: ...

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_markup: Any = None,
        **kwargs: Any,
    ) -> Any: ...


@dataclass(slots=True, frozen=True)
class CallbackKind:
    """Decoded callback button payload."""

    kind: str  # tool | memory | skill | plan | yolo
    token: str
    action: str  # once | session | deny | save | decline | auto-edit | ask | keep | ok | no


def encode_callback(kind: str, token: str, action: str) -> str:
    """Compact ``kind:token:action`` under Telegram's 64-byte callback limit."""
    payload = f"{kind[0]}:{token}:{action}"
    if len(payload.encode("utf-8")) > _CB_MAX:
        raise ValueError(f"callback_data too long ({len(payload)}): {payload!r}")
    return payload


def parse_callback(data: str | None) -> CallbackKind | None:
    """Parse a button payload; return ``None`` when malformed."""
    if not data or ":" not in data:
        return None
    parts = data.split(":", 2)
    if len(parts) != 3:
        return None
    prefix, token, action = parts
    kind_map = {
        "t": "tool",
        "m": "memory",
        "s": "skill",
        "p": "plan",
        "y": "yolo",
    }
    kind = kind_map.get(prefix)
    if kind is None or not token:
        return None
    return CallbackKind(kind=kind, token=token, action=action)


def should_drop_event(kind: str, data: dict[str, Any] | None = None) -> bool:
    """True when the outbox must ignore this worker event (#40)."""
    data = data or {}
    if data.get("agent"):
        return True  # subagent inner stream
    return kind in {"tool_start", "tool_end", "tool_progress", "tool_call"}


def _inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    """Build a plain dict keyboard (tests + aiogram ``InlineKeyboardMarkup``)."""
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": cb} for label, cb in row]
            for row in rows
        ]
    }


def build_approval_card(
    *,
    token: str,
    action: str = "",
    target: str = "",
    description: str = "",
    args: dict[str, Any] | None = None,
    plan: str | None = None,
    suggested_memory: dict[str, Any] | None = None,
    suggested_skill: dict[str, Any] | None = None,
    dangerous: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Return ``(html_text, reply_markup_dict)`` for an approval ask (#37/#39)."""
    if suggested_memory is not None:
        name = str(suggested_memory.get("name") or "memory")
        body = str(suggested_memory.get("body") or "")
        desc = str(suggested_memory.get("description") or description or "")
        text = (
            f"<b>Save memory?</b> {escape_html(name)}\n"
            f"{escape_html(desc)}\n"
            f"{format_pre(body[:1500])}"
        )
        markup = _inline_keyboard(
            [
                [
                    ("Save", encode_callback("m", token, "save")),
                    ("Decline", encode_callback("m", token, "decline")),
                ]
            ]
        )
        return text, markup

    if suggested_skill is not None:
        name = str(suggested_skill.get("name") or "skill")
        body = str(suggested_skill.get("body") or "")
        desc = str(suggested_skill.get("description") or description or "")
        text = (
            f"<b>Save skill?</b> {escape_html(name)}\n"
            f"{escape_html(desc)}\n"
            f"{format_pre(body[:1500])}"
        )
        markup = _inline_keyboard(
            [
                [
                    ("Save", encode_callback("s", token, "save")),
                    ("Decline", encode_callback("s", token, "decline")),
                ]
            ]
        )
        return text, markup

    if plan is not None:
        text = f"<b>Plan ready</b>\n{escape_html(plan[:3000])}"
        markup = _inline_keyboard(
            [
                [
                    ("auto-edit", encode_callback("p", token, "auto-edit")),
                    ("ask", encode_callback("p", token, "ask")),
                    ("keep planning", encode_callback("p", token, "keep")),
                ]
            ]
        )
        return text, markup

    # Tool-only card: name / description / already-redacted args (#37).
    safe_args = sanitize_tool_args(args)
    args_blob = ""
    if safe_args:
        try:
            args_blob = json.dumps(safe_args, ensure_ascii=False, indent=2)[:1200]
        except (TypeError, ValueError):
            args_blob = escape_html(str(safe_args)[:1200])
    danger = f" {grammar.GLYPH_WARN}" if dangerous else ""
    title = escape_html(action or "tool")
    lines = [f"<b>Approve{danger}</b> {format_code(title)}"]
    if target:
        lines.append(f"target: {format_code(target)}")
    if description:
        lines.append(escape_html(description))
    if args_blob:
        lines.append(format_pre(args_blob))
    text = "\n".join(lines)
    markup = _inline_keyboard(
        [
            [
                ("Once", encode_callback("t", token, "once")),
                ("Session", encode_callback("t", token, "session")),
                ("Deny", encode_callback("t", token, "deny")),
            ]
        ]
    )
    return text, markup


def build_yolo_confirm_card(*, token: str = "yolo") -> tuple[str, dict[str, Any]]:
    """Controller-owned yolo escalate confirm (#59 / #39)."""
    text = (
        "<b>Enable yolo mode?</b>\n"
        "No prompts except danger-guard. Confirm only if you trust this root."
    )
    markup = _inline_keyboard(
        [
            [
                ("Confirm", encode_callback("y", token, "ok")),
                ("Cancel", encode_callback("y", token, "no")),
            ]
        ]
    )
    return text, markup


def build_media_refusal_card(
    *,
    message: str,
    reason: str = "",
    modality: str = "",
    filename: str | None = None,
) -> str:
    """HTML notice for voice/unsupported/oversize media (#54)."""
    parts = ["<b>Media not accepted</b>"]
    if modality or reason:
        meta = " · ".join(p for p in (modality, reason) if p)
        parts.append(escape_html(meta))
    if filename:
        parts.append(format_code(filename))
    parts.append(escape_html(message))
    return "\n".join(parts)


@dataclass
class Outbox:
    """Per-chat draft state machine + card sender.

    ``sender`` is normally an aiogram ``Bot``. Tests pass a fake with the same
    async methods.
    """

    sender: TelegramSender
    parse_mode: str = "HTML"
    _draft_id: dict[int, int] = field(default_factory=dict)
    _draft_buf: dict[int, str] = field(default_factory=dict)
    _draft_alive: dict[int, bool] = field(default_factory=dict)
    _last_draft_at: dict[int, float] = field(default_factory=dict)
    _seq: int = 0

    def _next_draft_id(self, chat_id: int) -> int:
        self._seq += 1
        # Telegram requires non-zero draft_id; keep per-chat uniqueness.
        draft_id = (abs(chat_id) % 1_000_000) * 1000 + (self._seq % 1000) or 1
        self._draft_id[chat_id] = draft_id
        self._draft_buf[chat_id] = ""
        self._draft_alive[chat_id] = True
        return draft_id

    def restart_draft(self, chat_id: int) -> None:
        """Invalidate any live draft after a real message destroyed it (#40)."""
        self._draft_alive[chat_id] = False
        self._draft_buf.pop(chat_id, None)
        self._draft_id.pop(chat_id, None)

    async def on_event(
        self,
        chat_id: int,
        *,
        kind: str,
        text: str = "",
        data: dict[str, Any] | None = None,
    ) -> None:
        """Consume one worker/agent event and update Telegram accordingly."""
        data = data or {}
        if should_drop_event(kind, data):
            return
        if kind in {"text", "reasoning"}:
            # Reasoning stays out of the draft (secondary); only TEXT streams.
            if kind == "reasoning":
                return
            await self._append_draft(chat_id, text)
            return
        if kind == "done":
            await self.finalize(chat_id)
            return
        if kind == "error":
            await self.send_notice(chat_id, f"Error: {text or data.get('message', '')}")
            return
        if kind == "notice":
            await self.send_notice(chat_id, text or str(data.get("message") or ""))
            return
        if kind == "approval_ask" or data.get("approval_ask"):
            await self.send_approval_card(
                chat_id,
                token=str(data.get("token") or ""),
                action=str(data.get("action") or ""),
                target=str(data.get("target") or ""),
                description=str(data.get("description") or text or ""),
                args=data.get("args") if isinstance(data.get("args"), dict) else None,
                plan=data.get("plan") if isinstance(data.get("plan"), str) else None,
                suggested_memory=data.get("suggested_memory")
                if isinstance(data.get("suggested_memory"), dict)
                else None,
                suggested_skill=data.get("suggested_skill")
                if isinstance(data.get("suggested_skill"), dict)
                else None,
                dangerous=bool(data.get("dangerous")),
            )
            return
        if data.get("media_refusal"):
            html = build_media_refusal_card(
                message=str(data.get("message") or text or ""),
                reason=str(data.get("reason") or ""),
                modality=str(data.get("modality") or ""),
                filename=data.get("filename")
                if isinstance(data.get("filename"), str)
                else None,
            )
            await self.send_html(chat_id, html)
            return

    async def _append_draft(self, chat_id: int, chunk: str) -> None:
        if not chunk:
            return
        if not self._draft_alive.get(chat_id):
            self._next_draft_id(chat_id)
        draft_id = self._draft_id.get(chat_id) or self._next_draft_id(chat_id)
        buf = self._draft_buf.get(chat_id, "") + chunk
        # Draft preview is ephemeral; keep under Telegram draft text budget.
        if len(buf) > TELEGRAM_MESSAGE_MAX:
            buf = buf[-TELEGRAM_MESSAGE_MAX:]
        self._draft_buf[chat_id] = buf
        await self.sender.send_message_draft(
            chat_id=chat_id,
            draft_id=draft_id,
            text=escape_html(buf),
            parse_mode=self.parse_mode,
        )
        self._last_draft_at[chat_id] = time.monotonic()

    async def keepalive_draft(self, chat_id: int) -> None:
        """Re-send the same draft_id to reset the ~30s TTL (#40 / #32)."""
        if not self._draft_alive.get(chat_id):
            return
        draft_id = self._draft_id.get(chat_id)
        if draft_id is None:
            return
        buf = self._draft_buf.get(chat_id, "")
        await self.sender.send_message_draft(
            chat_id=chat_id,
            draft_id=draft_id,
            text=escape_html(buf) if buf else "",
            parse_mode=self.parse_mode,
        )
        self._last_draft_at[chat_id] = time.monotonic()

    async def finalize(self, chat_id: int) -> None:
        """Persist the draft buffer via ``sendMessage`` and clear draft state."""
        buf = self._draft_buf.get(chat_id, "")
        self.restart_draft(chat_id)
        if not buf.strip():
            return
        await self.send_plain(chat_id, buf)

    async def send_plain(self, chat_id: int, text: str) -> None:
        """Send escaped HTML chunks as real messages (destroys any live draft)."""
        self.restart_draft(chat_id)
        for piece in chunk_html(escape_html(text)):
            await self.sender.send_message(
                chat_id=chat_id, text=piece, parse_mode=self.parse_mode
            )

    async def send_html(self, chat_id: int, html: str) -> None:
        """Send already-built HTML; restarts draft (#40)."""
        self.restart_draft(chat_id)
        for piece in chunk_html(html):
            await self.sender.send_message(
                chat_id=chat_id, text=piece, parse_mode=self.parse_mode
            )

    async def send_notice(self, chat_id: int, text: str) -> None:
        await self.send_plain(chat_id, text)

    async def send_approval_card(self, chat_id: int, **kwargs: Any) -> Any:
        html, markup = build_approval_card(**kwargs)
        self.restart_draft(chat_id)
        return await self.sender.send_message(
            chat_id=chat_id,
            text=html,
            parse_mode=self.parse_mode,
            reply_markup=self._coerce_markup(markup),
        )

    async def send_yolo_confirm(self, chat_id: int, *, token: str = "yolo") -> Any:
        html, markup = build_yolo_confirm_card(token=token)
        self.restart_draft(chat_id)
        return await self.sender.send_message(
            chat_id=chat_id,
            text=html,
            parse_mode=self.parse_mode,
            reply_markup=self._coerce_markup(markup),
        )

    def _coerce_markup(self, markup: dict[str, Any]) -> Any:
        """Prefer aiogram types when available; keep dict for fakes/tests."""
        try:
            return self.to_aiogram_markup(markup)
        except Exception:  # noqa: BLE001
            return markup

    async def send_media_refusal(self, chat_id: int, **kwargs: Any) -> Any:
        html = build_media_refusal_card(**kwargs)
        return await self.send_html(chat_id, html)

    def to_aiogram_markup(self, markup: dict[str, Any]) -> Any:
        """Convert a dict keyboard to aiogram types when the extra is installed."""
        try:
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        except ImportError:  # pragma: no cover
            return markup
        rows = []
        for row in markup.get("inline_keyboard", []):
            rows.append(
                [
                    InlineKeyboardButton(text=btn["text"], callback_data=btn["callback_data"])
                    for btn in row
                ]
            )
        return InlineKeyboardMarkup(inline_keyboard=rows)
