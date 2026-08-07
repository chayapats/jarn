"""Private daemon↔worker NDJSON protocol (#60 / T-PIPE-1)."""

from __future__ import annotations

import json

import pytest

from jarn.gateway import (
    SCHEMA_VERSION,
    ApprovalAskFrame,
    ApprovalVerdictFrame,
    CancelFrame,
    ErrorFrame,
    EventFrame,
    HandshakeFrame,
    MediaRef,
    ProtocolError,
    ShutdownFrame,
    StatusFrame,
    SteerFrame,
    TurnFrame,
    UnsupportedSchemaVersion,
    decode_inbound_line,
    decode_outbound_line,
    encode_line,
)
from jarn.gateway import protocol as protocol_mod


def _round_trip_inbound(frame):
    line = encode_line(frame)
    assert line.endswith("\n")
    assert "\n" not in line[:-1]
    return decode_inbound_line(line)


def _round_trip_outbound(frame):
    line = encode_line(frame)
    assert line.endswith("\n")
    return decode_outbound_line(line)


# ---------------------------------------------------------------------------
# Inbound round-trips
# ---------------------------------------------------------------------------


def test_handshake_round_trip():
    out = _round_trip_inbound(HandshakeFrame(schema_version=SCHEMA_VERSION))
    assert isinstance(out, HandshakeFrame)
    assert out.schema_version == SCHEMA_VERSION


def test_turn_round_trip_with_media():
    frame = TurnFrame(
        thread_id="thr-1",
        text="look at this",
        media=[MediaRef(path="/tmp/a.png", mime="image/png", modality="image")],
    )
    out = _round_trip_inbound(frame)
    assert out.thread_id == "thr-1"
    assert out.text == "look at this"
    assert len(out.media) == 1
    assert out.media[0].path == "/tmp/a.png"
    assert out.media[0].mime == "image/png"
    assert out.media[0].modality == "image"


def test_turn_round_trip_empty_media_default():
    out = _round_trip_inbound(TurnFrame(thread_id="t", text="hi"))
    assert out.media == []


def test_approval_verdict_round_trip():
    frame = ApprovalVerdictFrame(
        token="tok-1",
        approved=True,
        scope="session",
        message="",
        plan_mode_target="auto-edit",
    )
    out = _round_trip_inbound(frame)
    assert out.token == "tok-1"
    assert out.approved is True
    assert out.scope == "session"
    assert out.plan_mode_target == "auto-edit"


def test_cancel_steer_shutdown_round_trip():
    cancel = _round_trip_inbound(CancelFrame(thread_id="t1"))
    assert cancel.thread_id == "t1"
    steer = _round_trip_inbound(SteerFrame(thread_id="t1", text="pivot"))
    assert steer.text == "pivot"
    assert isinstance(_round_trip_inbound(ShutdownFrame()), ShutdownFrame)


# ---------------------------------------------------------------------------
# Outbound round-trips
# ---------------------------------------------------------------------------


def test_event_round_trip():
    frame = EventFrame(
        thread_id="t1",
        kind="text",
        text="hello",
        data={"n": 1},
    )
    out = _round_trip_outbound(frame)
    assert out.kind == "text"
    assert out.text == "hello"
    assert out.data == {"n": 1}


def test_status_heartbeat_fields_round_trip():
    frame = StatusFrame(
        turn_in_flight=True,
        live_bg_jobs=2,
        idle_ms=0,
        parked_approvals=1,
    )
    out = _round_trip_outbound(frame)
    assert out.turn_in_flight is True
    assert out.live_bg_jobs == 2
    assert out.idle_ms == 0
    assert out.parked_approvals == 1


def test_status_omits_optional_parked_approvals_when_unset():
    line = encode_line(
        StatusFrame(turn_in_flight=False, live_bg_jobs=0, idle_ms=500)
    )
    obj = json.loads(line)
    assert "parked_approvals" not in obj
    out = decode_outbound_line(line)
    assert out.parked_approvals is None


def test_approval_ask_and_error_round_trip():
    ask = _round_trip_outbound(
        ApprovalAskFrame(
            token="tok",
            thread_id="t1",
            action="execute",
            target="rm -rf /",
            description="shell",
            dangerous=True,
            reason="destructive",
            args={"command": "rm -rf /"},
        )
    )
    assert ask.dangerous is True
    assert ask.args["command"] == "rm -rf /"

    err = _round_trip_outbound(
        ErrorFrame(message="boom", code="worker_crash", thread_id="t1")
    )
    assert err.code == "worker_crash"
    assert err.thread_id == "t1"


# ---------------------------------------------------------------------------
# Schema / wire shape invariants
# ---------------------------------------------------------------------------


def test_wire_uses_frame_discriminator_not_stream_json_type():
    """Private schema must not reuse headless stream-json's ``type`` key."""
    line = encode_line(HandshakeFrame())
    obj = json.loads(line)
    assert obj["frame"] == "handshake"
    assert "type" not in obj
    assert obj["schema_version"] == SCHEMA_VERSION


def test_schema_version_only_on_handshake():
    for frame in (
        TurnFrame(thread_id="t", text="x"),
        CancelFrame(thread_id="t"),
        EventFrame(thread_id="t", kind="text"),
        StatusFrame(turn_in_flight=False, live_bg_jobs=0, idle_ms=1),
        ErrorFrame(message="x"),
    ):
        obj = json.loads(encode_line(frame))
        assert "schema_version" not in obj


def test_unknown_schema_version_handshake_rejected():
    line = json.dumps({"frame": "handshake", "schema_version": SCHEMA_VERSION + 99}) + "\n"
    with pytest.raises(UnsupportedSchemaVersion) as excinfo:
        decode_inbound_line(line)
    assert excinfo.value.got == SCHEMA_VERSION + 99
    assert excinfo.value.expected == SCHEMA_VERSION


def test_missing_schema_version_on_handshake_rejected():
    with pytest.raises(ProtocolError, match="schema_version"):
        decode_inbound_line('{"frame":"handshake"}\n')


def test_direction_mismatch_rejected():
    """An outbound-only frame must not decode as inbound (and vice versa)."""
    event_line = encode_line(EventFrame(thread_id="t", kind="text", text="x"))
    with pytest.raises(ProtocolError, match="unknown frame"):
        decode_inbound_line(event_line)

    turn_line = encode_line(TurnFrame(thread_id="t", text="x"))
    with pytest.raises(ProtocolError, match="unknown frame"):
        decode_outbound_line(turn_line)


def test_empty_and_non_object_rejected():
    with pytest.raises(ProtocolError, match="empty"):
        decode_inbound_line("\n")
    with pytest.raises(ProtocolError, match="JSON object"):
        decode_inbound_line("[1]\n")
    with pytest.raises(ProtocolError, match="invalid JSON"):
        decode_inbound_line("{nope\n")


def test_encode_rejects_non_frame():
    with pytest.raises(ProtocolError, match="not a gateway frame"):
        encode_line(object())  # type: ignore[arg-type]


def test_schema_version_constant_is_v1():
    assert SCHEMA_VERSION == 1
    assert protocol_mod.SCHEMA_VERSION == 1
