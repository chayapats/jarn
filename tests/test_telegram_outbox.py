"""T-TG-3: draft→finalize HTML, cards, drop progress/subagent (#40/#37/#39)."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from jarn.telegram.outbox import (
    BUSY_ACK_TEXT,
    LONG_RUNNING_INTERVAL_S,
    Outbox,
    build_approval_card,
    build_media_refusal_card,
    build_undo_confirm_card,
    build_yolo_confirm_card,
    effective_telegram_busy_ack_detail,
    effective_telegram_busy_input_mode,
    effective_telegram_tool_progress,
    encode_callback,
    parse_callback,
    should_drop_event,
)


@dataclass
class FakeSender:
    drafts: list[tuple[int, int, str]] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    edits: list[dict] = field(default_factory=list)
    deletes: list[tuple[int, int]] = field(default_factory=list)

    async def send_message_draft(self, chat_id, draft_id, text=None, parse_mode=None, **kw):
        self.drafts.append((chat_id, draft_id, text or ""))
        return True

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None, **kw):
        message_id = len(self.messages) + 1
        self.messages.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
            }
        )
        return {"message_id": message_id}

    async def edit_message(self, chat_id, message_id, text, parse_mode=None, **kw):
        self.edits.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": parse_mode,
            }
        )
        return {"message_id": message_id}

    async def delete_message(self, chat_id, message_id, **kw):
        self.deletes.append((chat_id, message_id))
        return True


class _Clock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_should_drop_tool_progress_and_subagent():
    assert should_drop_event("tool_progress")
    assert should_drop_event("tool_start")
    assert should_drop_event("tool_end")
    assert should_drop_event("text", {"agent": "researcher"})
    assert not should_drop_event("text", {})


def test_should_drop_event_honors_progress_density():
    assert should_drop_event("tool_start", progress="off")
    assert not should_drop_event("tool_start", progress="new")
    assert not should_drop_event("tool_end", progress="all")
    assert not should_drop_event("tool_progress", progress="verbose")
    assert should_drop_event("tool_call", progress="verbose")
    assert should_drop_event("tool_start", {"agent": "researcher"}, progress="new")
    assert not should_drop_event("text", progress="off")
    assert not should_drop_event("done", progress="off")
    assert not should_drop_event("approval_ask", progress="off")


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
    assert "<code>execute</code>" in text
    assert "<pre>" in text
    assert "<span" not in text
    labels = [btn["text"] for row in markup["inline_keyboard"] for btn in row]
    assert labels == ["Once", "Session", "Deny"]
    assert "Always" not in labels


def test_memory_and_skill_save_decline():
    mem_text, m = build_approval_card(
        token="m1",
        suggested_memory={"name": "n", "body": "b", "description": "d"},
    )
    assert [b["text"] for r in m["inline_keyboard"] for b in r] == ["Save", "Decline"]
    assert "<pre>" in mem_text
    assert "<span" not in mem_text
    skill_text, s = build_approval_card(
        token="s1",
        suggested_skill={"name": "n", "body": "b", "description": "d"},
    )
    assert [b["text"] for r in s["inline_keyboard"] for b in r] == ["Save", "Decline"]
    assert "<pre>" in skill_text
    assert "<span" not in skill_text


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
    assert "<code>note.ogg</code>" in html
    assert "<span" not in html


def test_cards_do_not_double_escape_user_text():
    text, _ = build_approval_card(
        token="tok1",
        action="bash <x>",
        description="run a & b",
        args={"cmd": "echo <hi>"},
        target="path & file",
    )
    assert "<code>bash &lt;x&gt;</code>" in text
    assert "run a &amp; b" in text
    assert "<code>path &amp; file</code>" in text
    assert "&amp;amp;" not in text
    assert "&amp;lt;" not in text
    assert "<span" not in text
    mem, _ = build_approval_card(
        token="m1",
        suggested_memory={
            "name": "n&n",
            "body": "<script>",
            "description": "a < b",
        },
    )
    assert "n&amp;n" in mem
    assert "&lt;script&gt;" in mem
    assert "a &lt; b" in mem
    assert "&amp;amp;" not in mem


@pytest.mark.asyncio
async def test_draft_finalize_and_restart_after_card():
    sender = FakeSender()
    out = Outbox(sender=sender)
    await out.on_event(7, kind="text", text="Hello")
    assert sender.drafts
    draft_id_1 = sender.drafts[-1][1]
    assert "Hello" in sender.drafts[-1][2]

    await out.send_approval_card(7, token="t1", action="bash", description="danger")
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
    assert sender.edits == []


@pytest.mark.asyncio
async def test_off_emits_zero_tool_messages():
    sender = FakeSender()
    out = Outbox(sender=sender, progress="off")
    await out.on_event(1, kind="tool_start", text="bash", data={"args": {"cmd": "ls"}})
    await out.on_event(1, kind="tool_progress", text="bash", data={"tail": "out"})
    await out.on_event(1, kind="tool_end", text="bash", data={"summary": "ok"})
    await out.on_event(1, kind="done")
    assert sender.messages == []
    assert sender.edits == []
    assert sender.deletes == []


@pytest.mark.asyncio
async def test_new_density_one_bubble_edited_in_place():
    sender = FakeSender()
    out = Outbox(sender=sender, progress="new")
    await out.on_event(1, kind="tool_start", text="bash", data={"args": {"cmd": "ls"}})
    await out.on_event(
        1, kind="tool_start", text="read", data={"tool_call_id": "c2", "args": {"path": "a"}}
    )
    assert len(sender.messages) == 1
    progress_id = sender.messages[0]["message_id"]
    assert out.progress_message_id(1) == progress_id
    assert sender.edits
    assert all(e["message_id"] == progress_id for e in sender.edits)
    assert "bash" in sender.edits[-1]["text"]
    assert "read" in sender.edits[-1]["text"]
    assert sender.messages[0]["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_verbose_includes_tails():
    sender = FakeSender()
    out = Outbox(sender=sender, progress="verbose")
    await out.on_event(1, kind="tool_start", text="execute", data={"tool_call_id": "c1"})
    await out.on_event(
        1,
        kind="tool_progress",
        text="execute",
        data={"tool_call_id": "c1", "tail": "compiling foo.c\nok"},
    )
    html = sender.edits[-1]["text"] if sender.edits else sender.messages[0]["text"]
    assert "compiling foo.c" in html
    assert "execute" in html


@pytest.mark.asyncio
async def test_new_density_does_not_dump_tails():
    sender = FakeSender()
    out = Outbox(sender=sender, progress="new")
    await out.on_event(1, kind="tool_start", text="execute", data={"tool_call_id": "c1"})
    await out.on_event(
        1,
        kind="tool_progress",
        text="execute",
        data={"tool_call_id": "c1", "tail": "compiling foo.c"},
    )
    html = sender.messages[0]["text"]
    assert "compiling foo.c" not in html
    assert sender.edits == []


@pytest.mark.asyncio
async def test_cleanup_deletes_progress_bubble_on_done():
    sender = FakeSender()
    out = Outbox(sender=sender, progress="verbose")
    await out.on_event(1, kind="tool_start", text="bash")
    progress_id = sender.messages[0]["message_id"]
    await out.on_event(1, kind="text", text="done-prose")
    await out.on_event(1, kind="done")
    assert sender.deletes == [(1, progress_id)]
    assert any("done-prose" in m["text"] for m in sender.messages)
    assert out.progress_message_id(1) is None


@pytest.mark.asyncio
async def test_cleanup_keep_leaves_progress_bubble():
    sender = FakeSender()
    out = Outbox(sender=sender, progress="new", tool_progress_cleanup="keep")
    await out.on_event(1, kind="tool_start", text="bash")
    await out.on_event(1, kind="done")
    assert sender.deletes == []


@pytest.mark.asyncio
async def test_verbose_then_tool_start_appears():
    sender = FakeSender()
    out = Outbox(sender=sender, progress="off")
    await out.on_event(1, kind="tool_start", text="bash")
    assert sender.messages == []
    await out.on_event(1, kind="tool_start", text="bash", progress="new")
    assert len(sender.messages) == 1
    assert "bash" in sender.messages[0]["text"]


@pytest.mark.asyncio
async def test_subagent_inner_stream_dropped():
    sender = FakeSender()
    out = Outbox(sender=sender)
    await out.on_event(1, kind="text", text="secret", data={"agent": "worker-1"})
    assert sender.drafts == []


@pytest.mark.asyncio
async def test_subagent_tools_dropped_even_when_progress_new():
    sender = FakeSender()
    out = Outbox(sender=sender, progress="new")
    await out.on_event(
        1,
        kind="tool_start",
        text="bash",
        data={"agent": "researcher", "args": {"cmd": "ls"}},
    )
    assert sender.messages == []
    assert sender.edits == []
    assert out.progress_message_id(1) is None


@pytest.mark.asyncio
async def test_progress_bubble_does_not_destroy_prose_draft():
    sender = FakeSender()
    out = Outbox(sender=sender, progress="new")
    await out.on_event(1, kind="text", text="Hello")
    draft_id = sender.drafts[-1][1]
    assert out._draft_alive.get(1) is True
    await out.on_event(1, kind="tool_start", text="bash")
    progress_id = out.progress_message_id(1)
    assert progress_id is not None
    assert progress_id != draft_id
    assert out._draft_alive.get(1) is True
    assert sender.drafts[-1][1] == draft_id


@pytest.mark.asyncio
async def test_approval_card_still_destroys_draft_not_progress():
    sender = FakeSender()
    out = Outbox(sender=sender, progress="new")
    await out.on_event(1, kind="text", text="Hello")
    await out.on_event(1, kind="tool_start", text="bash")
    progress_id = out.progress_message_id(1)
    await out.send_approval_card(1, token="t1", action="bash", description="danger")
    assert out._draft_alive.get(1) is False
    assert out.progress_message_id(1) == progress_id


@pytest.mark.asyncio
async def test_long_running_heartbeat_after_interval():
    clock = _Clock(0.0)
    sender = FakeSender()
    out = Outbox(
        sender=sender,
        clock=clock,
        long_running_interval_s=LONG_RUNNING_INTERVAL_S,
    )
    await out.maybe_long_running(1, turn_in_flight=True)
    clock.t = LONG_RUNNING_INTERVAL_S - 1
    await out.maybe_long_running(1, turn_in_flight=True)
    assert sender.messages == []
    clock.t = LONG_RUNNING_INTERVAL_S
    await out.maybe_long_running(1, turn_in_flight=True)
    assert len(sender.messages) == 1
    assert "Working — 3 min" in sender.messages[0]["text"]
    clock.t = LONG_RUNNING_INTERVAL_S
    await out.maybe_long_running(1, turn_in_flight=True)
    assert len(sender.messages) == 1
    assert sender.edits == []


@pytest.mark.asyncio
async def test_long_running_heartbeat_disabled():
    clock = _Clock(0.0)
    sender = FakeSender()
    out = Outbox(
        sender=sender,
        clock=clock,
        long_running_notifications=False,
        long_running_interval_s=LONG_RUNNING_INTERVAL_S,
    )
    await out.maybe_long_running(1, turn_in_flight=True)
    clock.t = LONG_RUNNING_INTERVAL_S * 4
    await out.maybe_long_running(1, turn_in_flight=True)
    assert sender.messages == []
    assert sender.edits == []


@pytest.mark.asyncio
async def test_keepalive_draft_does_not_count_as_activity():
    clock = _Clock(0.0)
    sender = FakeSender()
    out = Outbox(sender=sender, clock=clock)
    await out.on_event(1, kind="text", text="hi")
    clock.t = LONG_RUNNING_INTERVAL_S
    await out.keepalive_draft(1)
    await out.maybe_long_running(1, turn_in_flight=True)
    assert any("Working — 3 min" in m["text"] for m in sender.messages)


def test_effective_telegram_tool_progress_does_not_inherit_ui():
    cfg = SimpleNamespace(
        ui=SimpleNamespace(tool_progress="new"),
        gateway=SimpleNamespace(telegram=SimpleNamespace()),
    )
    assert effective_telegram_tool_progress(cfg) == "off"
    cfg.gateway.telegram.tool_progress = "new"
    assert effective_telegram_tool_progress(cfg) == "new"
    cfg.gateway.telegram.tool_progress = "nope"
    assert effective_telegram_tool_progress(cfg) == "off"
    assert effective_telegram_tool_progress(None) == "off"


def test_effective_telegram_busy_input_mode_does_not_inherit_ui_queue():
    cfg = SimpleNamespace(
        ui=SimpleNamespace(busy_input_mode="queue"),
        gateway=SimpleNamespace(telegram=SimpleNamespace()),
    )
    assert effective_telegram_busy_input_mode(cfg) == "steer"
    cfg.gateway.telegram.busy_input_mode = "queue"
    assert effective_telegram_busy_input_mode(cfg) == "queue"
    cfg.gateway.telegram.busy_input_mode = "interrupt"
    assert effective_telegram_busy_input_mode(cfg) == "steer"
    assert effective_telegram_busy_input_mode(None) == "steer"


def test_effective_telegram_busy_ack_detail_defaults_off():
    cfg = SimpleNamespace(
        ui=SimpleNamespace(busy_ack_detail=False),
        gateway=SimpleNamespace(telegram=SimpleNamespace(busy_ack_detail=False)),
    )
    assert effective_telegram_busy_ack_detail(cfg) is False
    assert effective_telegram_busy_ack_detail(None) is False
    cfg.ui.busy_ack_detail = True
    assert effective_telegram_busy_ack_detail(cfg) is True


@pytest.mark.asyncio
async def test_busy_ack_is_short_working_without_detail():
    sender = FakeSender()
    out = Outbox(sender=sender)
    assert out.busy_ack_detail is False
    await out.ack_busy(1, mode="steer")
    assert len(sender.messages) == 1
    html = sender.messages[0]["text"]
    assert BUSY_ACK_TEXT in html
    assert "Steering" not in html
    assert "Queued" not in html
    await out.ack_busy(1, mode="steer")
    assert len(sender.messages) == 1
    assert sender.edits == []


@pytest.mark.asyncio
async def test_busy_ack_detail_off_by_default_on_event():
    sender = FakeSender()
    out = Outbox(sender=sender)
    await out.on_event(1, kind="busy_ack", text=BUSY_ACK_TEXT, data={"mode": "queue"})
    assert len(sender.messages) == 1
    html = sender.messages[0]["text"]
    assert BUSY_ACK_TEXT in html
    assert "Queued" not in html
    assert "Steering" not in html


@pytest.mark.asyncio
async def test_busy_ack_detail_adds_paragraph_when_enabled():
    sender = FakeSender()
    out = Outbox(sender=sender, busy_ack_detail=True)
    await out.ack_busy(1, mode="steer")
    html = sender.messages[0]["text"]
    assert BUSY_ACK_TEXT in html
    assert "Steering" in html


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


def test_undo_confirm_card_uses_yolo_callback_pattern():
    text, markup = build_undo_confirm_card(token="undo-abc")
    labels = [b["text"] for r in markup["inline_keyboard"] for b in r]
    assert labels == ["Confirm", "Cancel"]
    parsed = parse_callback(markup["inline_keyboard"][0][0]["callback_data"])
    assert parsed is not None
    assert parsed.kind == "undo"
    assert parsed.token == "undo-abc"
    assert parsed.action == "ok"
    cancel = parse_callback(markup["inline_keyboard"][0][1]["callback_data"])
    assert cancel is not None and cancel.action == "no"
    assert "Restore" in text


@pytest.mark.asyncio
async def test_yolo_confirm_event_sends_existing_card():
    sender = FakeSender()
    out = Outbox(sender=sender)
    await out.on_event(7, kind="yolo_confirm", data={"token": "yolo-tok"})
    assert sender.messages
    assert "Enable yolo" in sender.messages[0]["text"]
    assert sender.messages[0]["reply_markup"] is not None
    assert sender.messages[0]["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_undo_confirm_event_sends_card():
    sender = FakeSender()
    out = Outbox(sender=sender)
    await out.on_event(7, kind="undo_confirm", data={"token": "undo-tok"})
    assert sender.messages
    assert "Restore checkpoint" in sender.messages[0]["text"]
    assert sender.messages[0]["reply_markup"] is not None


def test_chunk_html_splits_under_limit_without_overflow():
    from jarn.telegram.htmlutil import TELEGRAM_MESSAGE_MAX, chunk_html

    short = "<b>Status</b>"
    assert chunk_html(short) == [short]
    blob = "\n".join(f"<b>row-{i}</b> " + ("x" * 80) for i in range(80))
    parts = chunk_html(blob)
    assert len(parts) > 1
    assert all(len(part) <= TELEGRAM_MESSAGE_MAX for part in parts)
    reconstructed = "\n".join(parts)
    assert "row-0" in reconstructed
    assert "row-79" in reconstructed
