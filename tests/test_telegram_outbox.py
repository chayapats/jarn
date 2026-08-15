"""T-TG-3: draft→finalize HTML, cards, drop progress/subagent (#40/#37/#39)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from jarn.telegram.outbox import (
    Outbox,
    build_approval_card,
    build_media_refusal_card,
    build_yolo_confirm_card,
    encode_callback,
    parse_callback,
    should_drop_event,
)


@dataclass
class FakeSender:
    drafts: list[tuple[int, int, str]] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)

    async def send_message_draft(self, chat_id, draft_id, text=None, parse_mode=None, **kw):
        self.drafts.append((chat_id, draft_id, text or ""))
        return True

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None, **kw):
        self.messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
            }
        )
        return {"message_id": len(self.messages)}


def test_should_drop_tool_progress_and_subagent():
    assert should_drop_event("tool_progress")
    assert should_drop_event("tool_start")
    assert should_drop_event("tool_end")
    assert should_drop_event("text", {"agent": "researcher"})
    assert not should_drop_event("text", {})


def test_callback_roundtrip_under_64_bytes():
    token = "x" * 24
    payload = encode_callback("t", token, "once")
    assert len(payload.encode()) <= 64
    parsed = parse_callback(payload)
    assert parsed is not None
    assert parsed.kind == "tool"
    assert parsed.token == token
    assert parsed.action == "once"
    assert parse_callback("bogus") is None


def test_tool_card_has_once_session_deny_no_always():
    text, markup = build_approval_card(
        token="tok1",
        action="execute",
        description="run ls",
        args={"cmd": "ls /tmp"},
    )
    assert "Approve" in text
    assert "execute" in text
    labels = [
        btn["text"]
        for row in markup["inline_keyboard"]
        for btn in row
    ]
    assert labels == ["Once", "Session", "Deny"]
    assert "Always" not in labels


def test_memory_and_skill_save_decline():
    _, m = build_approval_card(
        token="m1",
        suggested_memory={"name": "n", "body": "b", "description": "d"},
    )
    assert [b["text"] for r in m["inline_keyboard"] for b in r] == ["Save", "Decline"]
    _, s = build_approval_card(
        token="s1",
        suggested_skill={"name": "n", "body": "b", "description": "d"},
    )
    assert [b["text"] for r in s["inline_keyboard"] for b in r] == ["Save", "Decline"]


def test_plan_three_way_and_yolo():
    _, p = build_approval_card(token="p1", plan="do the thing")
    labels = [b["text"] for r in p["inline_keyboard"] for b in r]
    assert labels == ["auto-edit", "ask", "keep planning"]
    _, y = build_yolo_confirm_card()
    assert [b["text"] for r in y["inline_keyboard"] for b in r] == ["Confirm", "Cancel"]


def test_media_refusal_card_html():
    html = build_media_refusal_card(
        message="Voice not supported",
        reason="unsupported_modality",
        modality="voice",
        filename="note.ogg",
    )
    assert "Media not accepted" in html
    assert "voice" in html
    assert "note.ogg" in html


@pytest.mark.asyncio
async def test_draft_finalize_and_restart_after_card():
    sender = FakeSender()
    out = Outbox(sender=sender)
    await out.on_event(7, kind="text", text="Hello")
    assert sender.drafts
    draft_id_1 = sender.drafts[-1][1]
    assert "Hello" in sender.drafts[-1][2]

    await out.send_approval_card(
        7, token="t1", action="bash", description="danger"
    )
    assert sender.messages
    assert out._draft_alive.get(7) is False

    await out.on_event(7, kind="text", text=" after")
    assert out._draft_alive.get(7) is True
    draft_id_2 = sender.drafts[-1][1]
    assert draft_id_2 != draft_id_1

    await out.on_event(7, kind="done")
    assert any("after" in m["text"] for m in sender.messages)
    assert sender.messages[-1]["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_tool_progress_ignored():
    sender = FakeSender()
    out = Outbox(sender=sender)
    await out.on_event(1, kind="tool_progress", text="...", data={"tail": "x"})
    await out.on_event(1, kind="tool_start", text="bash")
    assert sender.drafts == []
    assert sender.messages == []


@pytest.mark.asyncio
async def test_subagent_inner_stream_dropped():
    sender = FakeSender()
    out = Outbox(sender=sender)
    await out.on_event(1, kind="text", text="secret", data={"agent": "worker-1"})
    assert sender.drafts == []


@pytest.mark.asyncio
async def test_layout_notice_is_sent_as_telegram_html():
    from jarn.tui import layout

    sender = FakeSender()
    out = Outbox(sender=sender)
    await out.on_event(1, kind="notice", text=layout.title("Status"))
    assert sender.messages
    assert "<b>Status</b>" in sender.messages[0]["text"]
    assert "[b]" not in sender.messages[0]["text"]
    assert sender.messages[0]["parse_mode"] == "HTML"
