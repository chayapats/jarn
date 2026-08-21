"""Telegram outbox: draft→finalize HTML + approval/media cards (#40/#37/#39).

Binding (#40)
-------------
* Stream assistant prose via ``sendMessageDraft``; finalize with ``sendMessage``.
* HTML parse mode (never MarkdownV2).
* Draft HTTP is coalesced: TEXT deltas accumulate in ``_draft_buf`` and the
  live draft is updated at most once per ``DRAFT_FLUSH_INTERVAL_S`` or when
  unsent chars pass ``DRAFT_CHUNK_THRESHOLD`` (whichever first). One in-flight
  ``sendMessageDraft`` at a time; a pending-flush flag runs exactly one
  trailing edit after that HTTP returns.
* Tool progress default OFF — ignore ``tool_start`` / ``tool_end`` /
  ``tool_progress`` unless session density is ``new`` / ``all`` / ``verbose``.
* Drop subagent inner stream (events stamped with ``data['agent']``) always.
* Progress bubble is a **separate** ``sendMessage`` / ``editMessageText``
  channel from the prose draft. Do not overload ``send_message_draft``.
* A real notice/media message still dismisses Telegram's ephemeral draft —
  restart with a fresh ``draft_id`` for subsequent prose. Approval / yolo /
  undo cards persist the live buffer as ``sendMessage`` first so the operator
  does not lose the prose they were reading. Progress upserts do **not**
  restart the draft.

Cards (#37/#39)
---------------
* Tool: catalog header + Once / Session / Deny nouns (no Always).
* Verbose progress uses the same ``tool_labels`` catalog as the CLI.
* Memory / skill: Save / Decline.
* Plan-mode: auto-edit / ask / keep planning (three-way).
* Yolo escalate confirm: Confirm / Cancel.
* Media refusal: plain notice card (no buttons).
* After a tap, the card is deleted from the chat. If delete fails, buttons
  are stripped so it cannot be tapped again.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from jarn.agent.tool_arg_redact import sanitize_tool_args
from jarn.telegram.htmlutil import (
    TELEGRAM_MESSAGE_MAX,
    chunk_html,
    escape_html,
)
from jarn.tui import layout
from jarn.tui.grammar import TOOL_PROGRESS_VALUES
from jarn.tui.i18n import resolve_locale, t
from jarn.tui.tool_labels import activity_open, activity_result, primary_object

_log = logging.getLogger("jarn.telegram.outbox")

__all__ = [
    "BUSY_ACK_TEXT",
    "CallbackKind",
    "DRAFT_CHUNK_THRESHOLD",
    "DRAFT_FLUSH_INTERVAL_S",
    "LONG_RUNNING_INTERVAL_S",
    "Outbox",
    "TelegramSender",
    "build_approval_card",
    "build_media_refusal_card",
    "build_undo_confirm_card",
    "build_yolo_confirm_card",
    "effective_telegram_busy_ack_detail",
    "effective_telegram_busy_input_mode",
    "effective_telegram_locale",
    "effective_telegram_tool_progress",
    "encode_callback",
    "parse_callback",
    "should_drop_event",
]

#: Callback payload budget (Telegram hard limit is 64 bytes).
_CB_MAX = 64

#: Quiet minutes (as seconds) before a user-visible ``Working — N min`` line.
#: Separate from draft TTL ``keepalive_draft`` and from ``ui.notify_min_secs``.
LONG_RUNNING_INTERVAL_S = 180.0

#: Minimum seconds between ``sendMessageDraft`` HTTP calls while TEXT streams.
#: ~0.6s keeps the draft progressive without bursting Telegram's chat limits.
DRAFT_FLUSH_INTERVAL_S = 0.6

#: Unsent chars that force a draft update before the flush interval elapses.
DRAFT_CHUNK_THRESHOLD = 280

#: One-line busy ack. No queued/steering paragraph unless ``busy_ack_detail``.
BUSY_ACK_TEXT = "Working…"

_BUSY_ACK_DETAIL = {
    "steer": "Steering into the current turn.",
    "queue": "Queued until this turn finishes.",
}

_TOOL_EVENT_KINDS = frozenset({"tool_start", "tool_end", "tool_progress", "tool_call"})
_PROGRESS_VISIBLE = frozenset({"new", "all", "verbose"})
_PROGRESS_TAILS = frozenset({"all", "verbose"})
_PROGRESS_TAIL_LINES = 8
_PROGRESS_TAIL_WIDTH = 200


class TelegramSender(Protocol):
    """Minimal Bot surface the outbox needs (aiogram ``Bot`` satisfies this).

    ``edit_message`` maps to aiogram ``edit_message_text``. Production wraps a
    raw Bot via :class:`_AiogramSenderAdapter` so the Protocol name stays
    stable. Do not overload ``send_message_draft`` for the progress bubble.
    """

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

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_markup: Any = None,
        **kwargs: Any,
    ) -> Any: ...

    async def delete_message(
        self,
        chat_id: int,
        message_id: int,
        **kwargs: Any,
    ) -> Any: ...


@dataclass(slots=True, frozen=True)
class CallbackKind:
    """Decoded callback button payload."""

    kind: str  # tool | memory | skill | plan | yolo | undo
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
        "u": "undo",
    }
    kind = kind_map.get(prefix)
    if kind is None or not token:
        return None
    return CallbackKind(kind=kind, token=token, action=action)


class _AiogramSenderAdapter:
    """Map Protocol ``edit_message`` onto aiogram ``Bot.edit_message_text``."""

    def __init__(self, bot: Any) -> None:
        self._bot = bot

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bot, name)

    async def send_message_draft(
        self,
        chat_id: int,
        draft_id: int,
        text: str | None = None,
        parse_mode: str | None = None,
        **kwargs: Any,
    ) -> Any:
        return await self._bot.send_message_draft(
            chat_id=chat_id,
            draft_id=draft_id,
            text=text,
            parse_mode=parse_mode,
            **kwargs,
        )

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_markup: Any = None,
        **kwargs: Any,
    ) -> Any:
        return await self._bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            **kwargs,
        )

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_markup: Any = None,
        **kwargs: Any,
    ) -> Any:
        return await self._bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=message_id,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            **kwargs,
        )

    async def delete_message(
        self,
        chat_id: int,
        message_id: int,
        **kwargs: Any,
    ) -> Any:
        return await self._bot.delete_message(chat_id=chat_id, message_id=message_id, **kwargs)


def _coerce_message_id(message: Any) -> int | None:
    value = (
        message.get("message_id")
        if isinstance(message, Mapping)
        else getattr(message, "message_id", None)
    )
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _adapt_sender(sender: Any) -> Any:
    if hasattr(sender, "edit_message"):
        return sender
    if hasattr(sender, "edit_message_text"):
        return _AiogramSenderAdapter(sender)
    return sender


def effective_telegram_tool_progress(config: Any | None) -> str:
    """Telegram session density. Unset/invalid overlay is ``off``, never CLI ``ui``.

    ``gateway.telegram.tool_progress`` is the chat overlay. ``ui.tool_progress``
    (CLI default ``new``) must not leak onto Telegram (#40 quiet default).
    """
    tg = getattr(getattr(config, "gateway", None), "telegram", None)
    raw = getattr(tg, "tool_progress", None)
    if isinstance(raw, str) and raw in TOOL_PROGRESS_VALUES:
        return raw
    return "off"


def effective_telegram_busy_input_mode(config: Any | None) -> str:
    """Telegram busy overlay. Default ``steer``; never inherit CLI queue."""
    from jarn.config.schema import TELEGRAM_BUSY_INPUT_DEFAULT, TELEGRAM_BUSY_INPUT_MODES

    tg = getattr(getattr(config, "gateway", None), "telegram", None)
    raw = getattr(tg, "busy_input_mode", None)
    if isinstance(raw, str) and raw in TELEGRAM_BUSY_INPUT_MODES:
        return raw
    return TELEGRAM_BUSY_INPUT_DEFAULT


def effective_telegram_busy_ack_detail(config: Any | None) -> bool:
    """Busy-ack detail. Default off. Either Telegram overlay or ``ui`` enables it."""
    tg = getattr(getattr(config, "gateway", None), "telegram", None)
    tg_raw = getattr(tg, "busy_ack_detail", None)
    ui_raw = getattr(getattr(config, "ui", None), "busy_ack_detail", None)
    return bool(tg_raw) or bool(ui_raw)


def effective_telegram_locale(
    config: Any | None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Same ``ui.locale`` as the CLI. Do not invent a Telegram overlay key."""
    return resolve_locale(config, environ)


def _chrome_locale(locale: str | None) -> str:
    return locale if locale in {"en", "th"} else "en"


def should_drop_event(
    kind: str,
    data: dict[str, Any] | None = None,
    *,
    progress: str = "off",
) -> bool:
    """True when the outbox must ignore this worker event (#40).

    Default ``progress="off"`` still drops every tool event. ``new`` / ``all`` /
    ``verbose`` keep ``tool_start`` / ``tool_end`` / ``tool_progress``. Subagent
    inner stream (``data['agent']``) is **always** dropped. ``text`` / ``done`` /
    cards are never dropped here.
    """
    data = data or {}
    if data.get("agent"):
        return True  # subagent inner stream — always, even when progress is on
    if kind not in _TOOL_EVENT_KINDS:
        return False
    if kind == "tool_call":
        return True
    return progress not in _PROGRESS_VISIBLE


#: Tool name or ActionKind value → S07 header key. Remote ALWAYS is not a noun here.
_APPROVAL_HEADER_KEYS: dict[str, str] = {
    "edit_file": "approval.header.edit",
    "write_file": "approval.header.write",
    "read_file": "approval.header.read",
    "bash": "approval.header.shell",
    "shell": "approval.header.shell",
    "execute": "approval.header.shell",
    "read": "approval.header.read",
    "write": "approval.header.write",
    "network": "approval.header.network",
}


def _approval_header_key(action: str, args: dict[str, Any] | None) -> str:
    name = (action or "").strip().lower()
    key = _APPROVAL_HEADER_KEYS.get(name)
    if key is not None:
        return key
    if args and "old_string" in args:
        return "approval.header.edit"
    return "approval.header.write"


def _approval_object(action: str, target: str, args: dict[str, Any] | None) -> str:
    if (target or "").strip():
        return target.strip()
    obj = primary_object(action, args)
    if obj:
        return obj
    return (action or "tool").strip() or "tool"


def _clip_tail(tail: str) -> str:
    lines = tail.splitlines()[-_PROGRESS_TAIL_LINES:]
    clipped: list[str] = []
    for line in lines:
        if len(line) > _PROGRESS_TAIL_WIDTH:
            line = line[: _PROGRESS_TAIL_WIDTH - 1] + "…"
        clipped.append(line)
    return "\n".join(clipped)


@dataclass
class _ToolLine:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    duration: str = ""
    tail: str = ""


def _inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    """Build a plain dict keyboard (tests + aiogram ``InlineKeyboardMarkup``)."""
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": cb} for label, cb in row] for row in rows
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
    locale: str = "en",
) -> tuple[str, dict[str, Any]]:
    """Return ``(html_text, reply_markup_dict)`` for an approval ask (#37/#39).

    Tool cards use S07 catalog nouns. Remote ALWAYS is not offered.
    """
    loc = _chrome_locale(locale)
    if suggested_memory is not None:
        name = str(suggested_memory.get("name") or "memory")
        body = str(suggested_memory.get("body") or "")
        desc = str(suggested_memory.get("description") or description or "")
        text = (
            f"{layout.strong('Save memory?', dialect='html')} "
            f"{layout.escape(name, dialect='html')}\n"
            f"{layout.escape(desc, dialect='html')}\n"
            f"{layout.pre(body[:1500], dialect='html')}"
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
            f"{layout.strong('Save skill?', dialect='html')} "
            f"{layout.escape(name, dialect='html')}\n"
            f"{layout.escape(desc, dialect='html')}\n"
            f"{layout.pre(body[:1500], dialect='html')}"
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
        text = (
            f"{layout.strong('Plan ready', dialect='html')}\n"
            f"{layout.escape(plan[:3000], dialect='html')}"
        )
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

    # Tool-only card: S07 header + Once / Session / Deny. Never Always (#39).
    safe_args = sanitize_tool_args(args)
    args_blob = ""
    if safe_args:
        try:
            args_blob = json.dumps(safe_args, ensure_ascii=False, indent=2)[:1200]
        except (TypeError, ValueError):
            args_blob = str(safe_args)[:1200]
    header = t(
        _approval_header_key(action, args),
        loc,
        object=_approval_object(action, target, args),
    )
    if dangerous:
        line = (
            f"{layout.err(t('approval.danger', loc), dialect='html')} "
            f"{layout.strong(header, dialect='html')}"
        )
    else:
        line = layout.strong(header, dialect="html")
    lines = [line]
    if description:
        lines.append(layout.escape(description, dialect="html"))
    if args_blob:
        lines.append(layout.pre(args_blob, dialect="html"))
    text = "\n".join(lines)
    markup = _inline_keyboard(
        [
            [
                (t("approval.once", loc), encode_callback("t", token, "once")),
                (t("approval.session", loc), encode_callback("t", token, "session")),
                (t("approval.deny", loc), encode_callback("t", token, "deny")),
            ]
        ]
    )
    return text, markup


def build_yolo_confirm_card(*, token: str = "yolo") -> tuple[str, dict[str, Any]]:
    """Controller-owned yolo escalate confirm (#59 / #39)."""
    text = f"{layout.strong('Enable yolo mode?', dialect='html')}\n" + layout.escape(
        "No prompts except danger-guard. Confirm only if you trust this root.",
        dialect="html",
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


def build_undo_confirm_card(*, token: str = "undo") -> tuple[str, dict[str, Any]]:
    """Confirm/Cancel card for Telegram ``/undo`` (same callback pattern as yolo)."""
    text = f"{layout.strong('Restore checkpoint?', dialect='html')}\n" + layout.escape(
        "This reverts the last turn's file changes. Cancel leaves files unchanged.",
        dialect="html",
    )
    markup = _inline_keyboard(
        [
            [
                ("Confirm", encode_callback("u", token, "ok")),
                ("Cancel", encode_callback("u", token, "no")),
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
    parts = [layout.strong("Media not accepted", dialect="html")]
    if modality or reason:
        meta = " · ".join(p for p in (modality, reason) if p)
        parts.append(layout.escape(meta, dialect="html"))
    if filename:
        parts.append(layout.code(filename, dialect="html"))
    parts.append(layout.escape(message, dialect="html"))
    return "\n".join(parts)


@dataclass
class Outbox:
    """Per-chat draft state machine + card sender + opt-in progress bubble.

    ``sender`` is normally an aiogram ``Bot`` (adapted to :class:`TelegramSender`).
    Tests pass a fake with the same async methods.

    Prose uses ``sendMessageDraft`` → ``sendMessage``. Tool progress uses one
    HTML bubble edited in place. Those channels must not share a message id.
    """

    sender: TelegramSender
    parse_mode: str = "HTML"
    #: Session density. YAML overlay seeds this; ``/verbose`` events override.
    progress: str = "off"
    tool_progress_cleanup: str = "delete"
    long_running_notifications: bool = True
    long_running_interval_s: float = LONG_RUNNING_INTERVAL_S
    #: Extra queued/steering paragraph on the Working… ack. Default off.
    busy_ack_detail: bool = False
    #: Resolved ``ui.locale`` (``en`` | ``th``). No Telegram overlay key.
    locale: str = "en"
    clock: Callable[[], float] = field(default=time.monotonic)
    #: Injected so tests can force interval/threshold without wall-clock waits.
    flush_interval_s: float = DRAFT_FLUSH_INTERVAL_S
    chunk_threshold: int = DRAFT_CHUNK_THRESHOLD
    _draft_id: dict[int, int] = field(default_factory=dict)
    _draft_buf: dict[int, str] = field(default_factory=dict)
    _draft_alive: dict[int, bool] = field(default_factory=dict)
    _last_draft_at: dict[int, float] = field(default_factory=dict)
    _draft_sent_len: dict[int, int] = field(default_factory=dict)
    _draft_pending_flush: dict[int, bool] = field(default_factory=dict)
    _draft_inflight: dict[int, bool] = field(default_factory=dict)
    _draft_gen: dict[int, int] = field(default_factory=dict)
    _draft_locks: dict[int, asyncio.Lock] = field(default_factory=dict, repr=False)
    _draft_idle: dict[int, asyncio.Event] = field(default_factory=dict, repr=False)
    _seq: int = 0
    _progress_id: dict[int, int] = field(default_factory=dict)
    _progress_tools: dict[int, dict[str, _ToolLine]] = field(default_factory=dict)
    _progress_heartbeat_min: dict[int, int] = field(default_factory=dict)
    _last_event_at: dict[int, float] = field(default_factory=dict)
    _busy_ack_mode: dict[int, str] = field(default_factory=dict)
    _last_progress_html: dict[int, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.sender = _adapt_sender(self.sender)
        self.locale = _chrome_locale(self.locale)

    def _next_draft_id(self, chat_id: int) -> int:
        self._seq += 1
        self._draft_gen[chat_id] = self._draft_gen.get(chat_id, 0) + 1
        # Telegram requires non-zero draft_id; keep per-chat uniqueness.
        draft_id = (abs(chat_id) % 1_000_000) * 1000 + (self._seq % 1000) or 1
        self._draft_id[chat_id] = draft_id
        self._draft_buf[chat_id] = ""
        self._draft_alive[chat_id] = True
        self._draft_sent_len[chat_id] = 0
        self._draft_pending_flush[chat_id] = False
        self._last_draft_at.pop(chat_id, None)
        return draft_id

    def restart_draft(self, chat_id: int) -> None:
        """Invalidate any live draft after a real message destroyed it (#40)."""
        self._draft_alive[chat_id] = False
        self._draft_buf.pop(chat_id, None)
        self._draft_id.pop(chat_id, None)
        self._draft_sent_len.pop(chat_id, None)
        self._draft_pending_flush.pop(chat_id, None)
        self._last_draft_at.pop(chat_id, None)

    def _lock_for(self, chat_id: int) -> asyncio.Lock:
        lock = self._draft_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._draft_locks[chat_id] = lock
        return lock

    def _idle_event(self, chat_id: int) -> asyncio.Event:
        ev = self._draft_idle.get(chat_id)
        if ev is None:
            ev = asyncio.Event()
            ev.set()
            self._draft_idle[chat_id] = ev
        return ev

    async def _await_inflight(self, chat_id: int) -> None:
        ev = self._draft_idle.get(chat_id)
        if ev is not None:
            await ev.wait()

    def _should_flush_now(self, chat_id: int) -> bool:
        """True when unsent prose should hit Telegram (interval or size)."""
        buf = self._draft_buf.get(chat_id, "")
        pending = len(buf) - self._draft_sent_len.get(chat_id, 0)
        if pending <= 0:
            return False
        last = self._last_draft_at.get(chat_id)
        if last is None:
            return True
        if pending >= self.chunk_threshold:
            return True
        return (self.clock() - last) >= self.flush_interval_s

    def progress_message_id(self, chat_id: int) -> int | None:
        """Live progress-bubble message id, or ``None`` when no bubble exists."""
        return self._progress_id.get(chat_id)

    def _mark_activity(self, chat_id: int) -> None:
        self._last_event_at[chat_id] = self.clock()

    async def on_event(
        self,
        chat_id: int,
        *,
        kind: str,
        text: str = "",
        data: dict[str, Any] | None = None,
        progress: str | None = None,
    ) -> None:
        """Consume one worker/agent event and update Telegram accordingly."""
        data = data or {}
        if progress is not None:
            self.progress = progress
        density = self.progress
        if kind == "busy_ack":
            mode = str(data.get("mode") or "steer")
            await self.ack_busy(chat_id, mode=mode)
            return
        if should_drop_event(kind, data, progress=density):
            return
        self._mark_activity(chat_id)
        if kind in {"tool_start", "tool_end", "tool_progress"}:
            await self._on_tool_event(chat_id, kind, text, data, density)
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
        if kind == "yolo_confirm":
            await self.send_yolo_confirm(chat_id, token=str(data.get("token") or "yolo"))
            return
        if kind == "undo_confirm":
            await self.send_undo_confirm(chat_id, token=str(data.get("token") or "undo"))
            return
        if kind == "thread_switch":
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
                filename=data.get("filename") if isinstance(data.get("filename"), str) else None,
            )
            await self.send_html(chat_id, html)
            return

    async def _on_tool_event(
        self,
        chat_id: int,
        kind: str,
        text: str,
        data: dict[str, Any],
        progress: str,
    ) -> None:
        tools = self._progress_tools.setdefault(chat_id, {})
        key = str(data.get("tool_call_id") or text or "tool")
        row = tools.get(key)
        if kind == "tool_start":
            if row is None:
                raw_args = data.get("args")
                args = raw_args if isinstance(raw_args, dict) else {}
                tools[key] = _ToolLine(name=text or "tool", args=args)
        elif kind == "tool_end":
            if row is None:
                tools[key] = _ToolLine(name=text or "tool")
                row = tools[key]
            row.summary = str(data.get("summary") or text or "")
            elapsed = data.get("elapsed")
            if progress == "verbose" and isinstance(elapsed, (int, float)) and elapsed:
                row.duration = f" · {float(elapsed):.1f}s"
        elif kind == "tool_progress":
            if progress not in _PROGRESS_TAILS:
                return
            if row is None:
                tools[key] = _ToolLine(name=text or "tool")
                row = tools[key]
            tail = str(data.get("tail") or data.get("chunk") or "")
            if tail:
                row.tail = tail
        await self._upsert_progress_bubble(chat_id, self._progress_html(chat_id, progress))

    def _progress_html(self, chat_id: int, progress: str) -> str:
        lines: list[str] = []
        mode = self._busy_ack_mode.get(chat_id)
        if mode:
            lines.append(layout.muted(BUSY_ACK_TEXT, dialect="html"))
            if self.busy_ack_detail:
                extra = _BUSY_ACK_DETAIL.get(mode, _BUSY_ACK_DETAIL["steer"])
                lines.append(layout.escape(extra, dialect="html"))
        minutes = self._progress_heartbeat_min.get(chat_id)
        if minutes:
            lines.append(layout.muted(f"Working — {minutes} min", dialect="html"))
        loc = _chrome_locale(self.locale)
        for tool in self._progress_tools.get(chat_id, {}).values():
            verb, obj = activity_open(tool.name, tool.args, locale=loc)
            lines.append(layout.tool_open(verb, obj, dialect="html"))
            if tool.summary:
                shown = activity_result(tool.summary, locale=loc)
                duration = tool.duration if progress == "verbose" else ""
                lines.append(layout.tool_result(shown, duration=duration, dialect="html"))
            elif progress in _PROGRESS_TAILS and tool.tail:
                clipped = _clip_tail(tool.tail)
                if clipped:
                    lines.append(layout.muted(clipped, dialect="html"))
        return "\n".join(lines)

    async def _upsert_progress_bubble(self, chat_id: int, html: str) -> None:
        """Create or edit the single progress bubble. Does **not** restart draft."""
        if not html.strip():
            return
        if len(html) > TELEGRAM_MESSAGE_MAX:
            html = html[: TELEGRAM_MESSAGE_MAX - 1] + "…"
        if self._last_progress_html.get(chat_id) == html:
            return
        self._last_progress_html[chat_id] = html
        mid = self._progress_id.get(chat_id)
        if mid is None:
            msg = await self.sender.send_message(
                chat_id=chat_id, text=html, parse_mode=self.parse_mode
            )
            extracted = _coerce_message_id(msg)
            if extracted is not None:
                self._progress_id[chat_id] = extracted
            return
        edit = getattr(self.sender, "edit_message", None)
        if not callable(edit):
            return
        try:
            await edit(
                chat_id=chat_id,
                message_id=mid,
                text=html,
                parse_mode=self.parse_mode,
            )
        except Exception:  # noqa: BLE001 — best-effort in-place edit
            _log.debug("progress bubble edit failed chat=%s id=%s", chat_id, mid, exc_info=True)

    async def maybe_long_running(
        self,
        chat_id: int,
        *,
        turn_in_flight: bool,
        now: float | None = None,
    ) -> None:
        """Emit ``Working — N min`` after a quiet interval while a turn runs.

        Independent of ``keepalive_draft`` (draft TTL). Gated by
        ``long_running_notifications``.
        """
        instant = self.clock() if now is None else now
        if not self.long_running_notifications:
            return
        if not turn_in_flight:
            self._last_event_at.pop(chat_id, None)
            self._progress_heartbeat_min.pop(chat_id, None)
            return
        last = self._last_event_at.get(chat_id)
        if last is None:
            self._last_event_at[chat_id] = instant
            return
        quiet = instant - last
        if quiet < self.long_running_interval_s:
            return
        minutes = max(1, int(quiet // 60))
        if self._progress_heartbeat_min.get(chat_id) == minutes:
            return
        self._progress_heartbeat_min[chat_id] = minutes
        await self._upsert_progress_bubble(chat_id, self._progress_html(chat_id, self.progress))

    async def ack_busy(self, chat_id: int, *, mode: str = "steer") -> None:
        """One short ``Working…`` edit. Detail paragraph only when enabled."""
        from jarn.config.schema import TELEGRAM_BUSY_INPUT_DEFAULT, TELEGRAM_BUSY_INPUT_MODES

        self._busy_ack_mode[chat_id] = (
            mode if mode in TELEGRAM_BUSY_INPUT_MODES else TELEGRAM_BUSY_INPUT_DEFAULT
        )
        await self._upsert_progress_bubble(chat_id, self._progress_html(chat_id, self.progress))

    async def _cleanup_progress(self, chat_id: int) -> None:
        mid = self._progress_id.pop(chat_id, None)
        self._progress_tools.pop(chat_id, None)
        self._progress_heartbeat_min.pop(chat_id, None)
        self._last_event_at.pop(chat_id, None)
        self._busy_ack_mode.pop(chat_id, None)
        self._last_progress_html.pop(chat_id, None)
        if mid is None or self.tool_progress_cleanup != "delete":
            return
        delete = getattr(self.sender, "delete_message", None)
        if not callable(delete):
            return
        try:
            await delete(chat_id=chat_id, message_id=mid)
        except Exception:  # noqa: BLE001 — best-effort cleanup
            _log.debug("progress bubble delete failed chat=%s id=%s", chat_id, mid, exc_info=True)

    async def _append_draft(self, chat_id: int, chunk: str) -> None:
        if not chunk:
            return
        start_flush = False
        async with self._lock_for(chat_id):
            if not self._draft_alive.get(chat_id):
                self._next_draft_id(chat_id)
            buf = self._draft_buf.get(chat_id, "") + chunk
            # Draft preview is ephemeral; keep under Telegram draft text budget.
            if len(buf) > TELEGRAM_MESSAGE_MAX:
                buf = buf[-TELEGRAM_MESSAGE_MAX:]
            self._draft_buf[chat_id] = buf
            if self._draft_inflight.get(chat_id):
                self._draft_pending_flush[chat_id] = True
                return
            if not self._should_flush_now(chat_id):
                return
            self._draft_inflight[chat_id] = True
            self._idle_event(chat_id).clear()
            start_flush = True
        if start_flush:
            await self._run_flush(chat_id)

    async def _run_flush(self, chat_id: int) -> None:
        """One ``sendMessageDraft`` HTTP; then at most one trailing coalesced edit."""
        try:
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
            self._last_draft_at[chat_id] = self.clock()
            self._draft_sent_len[chat_id] = len(buf)
        finally:
            trailing = False
            async with self._lock_for(chat_id):
                self._draft_inflight[chat_id] = False
                if self._draft_alive.get(chat_id) and self._draft_pending_flush.get(chat_id):
                    self._draft_pending_flush[chat_id] = False
                    self._draft_inflight[chat_id] = True
                    trailing = True
                if not trailing:
                    self._idle_event(chat_id).set()
            if trailing:
                await self._run_flush(chat_id)

    async def _commit_draft(self, chat_id: int) -> str:
        """Stop draft HTTP and return buffered prose so a real message can keep it."""
        async with self._lock_for(chat_id):
            gen = self._draft_gen.get(chat_id, 0)
            buf = self._draft_buf.get(chat_id, "")
            self._draft_alive[chat_id] = False
            self._draft_pending_flush[chat_id] = False
        await self._await_inflight(chat_id)
        async with self._lock_for(chat_id):
            if self._draft_gen.get(chat_id, 0) == gen:
                self.restart_draft(chat_id)
        return buf

    async def _invalidate_draft(self, chat_id: int) -> None:
        """Drop the live draft without persisting it (notices / explicit send)."""
        async with self._lock_for(chat_id):
            gen = self._draft_gen.get(chat_id, 0)
            self._draft_alive[chat_id] = False
            self._draft_pending_flush[chat_id] = False
        await self._await_inflight(chat_id)
        async with self._lock_for(chat_id):
            if self._draft_gen.get(chat_id, 0) == gen:
                self.restart_draft(chat_id)

    async def _emit_plain(self, chat_id: int, text: str) -> None:
        for piece in chunk_html(escape_html(text)):
            await self.sender.send_message(
                chat_id=chat_id, text=piece, parse_mode=self.parse_mode
            )

    async def keepalive_draft(self, chat_id: int) -> None:
        """Re-send the same draft_id to reset the ~30s TTL (#40 / #32)."""
        start_flush = False
        async with self._lock_for(chat_id):
            if not self._draft_alive.get(chat_id):
                return
            if self._draft_id.get(chat_id) is None:
                return
            if self._draft_inflight.get(chat_id):
                self._draft_pending_flush[chat_id] = True
                return
            self._draft_inflight[chat_id] = True
            self._idle_event(chat_id).clear()
            start_flush = True
        if start_flush:
            await self._run_flush(chat_id)

    async def finalize(self, chat_id: int) -> None:
        """Persist the draft buffer via ``sendMessage``, then drop the bubble."""
        buf = await self._commit_draft(chat_id)
        if buf.strip():
            await self._emit_plain(chat_id, buf)
        await self._cleanup_progress(chat_id)

    async def send_plain(self, chat_id: int, text: str) -> None:
        """Send escaped HTML chunks as real messages (destroys any live draft)."""
        await self._invalidate_draft(chat_id)
        await self._emit_plain(chat_id, text)

    async def send_html(self, chat_id: int, html: str) -> None:
        """Send already-built HTML; restarts draft (#40)."""
        await self._invalidate_draft(chat_id)
        for piece in chunk_html(html):
            await self.sender.send_message(chat_id=chat_id, text=piece, parse_mode=self.parse_mode)

    async def send_notice(self, chat_id: int, text: str) -> None:
        from jarn.tui import layout

        if layout.looks_like_layout_markup(text):
            await self.send_html(chat_id, layout.to_html(text))
            return
        await self.send_plain(chat_id, text)

    async def _send_card(self, chat_id: int, html: str, markup: dict[str, Any]) -> Any:
        """Persist live prose, then post a real card (Telegram dismisses the draft)."""
        buf = await self._commit_draft(chat_id)
        if buf.strip():
            await self._emit_plain(chat_id, buf)
        return await self.sender.send_message(
            chat_id=chat_id,
            text=html,
            parse_mode=self.parse_mode,
            reply_markup=self._coerce_markup(markup),
        )

    async def send_approval_card(self, chat_id: int, **kwargs: Any) -> Any:
        kwargs.setdefault("locale", _chrome_locale(self.locale))
        html, markup = build_approval_card(**kwargs)
        return await self._send_card(chat_id, html, markup)

    async def send_yolo_confirm(self, chat_id: int, *, token: str = "yolo") -> Any:
        html, markup = build_yolo_confirm_card(token=token)
        return await self._send_card(chat_id, html, markup)

    async def send_undo_confirm(self, chat_id: int, *, token: str = "undo") -> Any:
        html, markup = build_undo_confirm_card(token=token)
        return await self._send_card(chat_id, html, markup)

    async def settle_approval_card(
        self,
        chat_id: int,
        message_id: int,
        *,
        html: str = "",
        text: str = "",
    ) -> None:
        """Delete the tapped card from the chat (Hermes-style).

        Telegram may refuse delete (too old, already gone). Then strip the
        buttons so the leftover card cannot be tapped again.
        """
        delete = getattr(self.sender, "delete_message", None)
        if callable(delete):
            try:
                await delete(chat_id=chat_id, message_id=message_id)
                return
            except Exception:  # noqa: BLE001 — fall back to disarming buttons
                _log.debug(
                    "approval card delete failed chat=%s id=%s",
                    chat_id,
                    message_id,
                    exc_info=True,
                )
        edit = getattr(self.sender, "edit_message", None)
        if not callable(edit):
            return
        body = html.strip() if html.strip() else escape_html(text)
        if not body:
            return
        await edit(
            chat_id=chat_id,
            message_id=message_id,
            text=body,
            parse_mode=self.parse_mode,
            reply_markup=None,
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
