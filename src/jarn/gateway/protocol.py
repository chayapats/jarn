"""Private NDJSON frame types for the daemon↔worker pipe (#60).

Framing: one JSON object per line. The discriminator is ``"frame"`` (not the
headless stream-json ``"type"``). ``schema_version`` appears only on the
handshake frame; mismatched versions fail fast via
:class:`UnsupportedSchemaVersion`.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any

# Bump only when the private pipe schema is intentionally incompatible.
SCHEMA_VERSION = 1

# Wire discriminator — kept distinct from headless stream-json's ``"type"``.
_FRAME_KEY = "frame"


class ProtocolError(ValueError):
    """Malformed, unknown, or direction-invalid frame on the private pipe."""


class UnsupportedSchemaVersion(ProtocolError):
    """Handshake carried a ``schema_version`` this process does not speak."""

    def __init__(self, got: int, *, expected: int = SCHEMA_VERSION) -> None:
        self.got = got
        self.expected = expected
        super().__init__(
            f"unsupported gateway schema_version {got} (expected {expected})"
        )


# ---------------------------------------------------------------------------
# Shared / inbound (daemon → worker)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MediaRef:
    """Staged media attached to a turn (paths owned by the daemon staging area)."""

    path: str
    mime: str
    modality: str  # e.g. "image", "document"


@dataclass(slots=True)
class HandshakeFrame:
    """First frame after spawn; carries ``schema_version`` and nothing else versioned."""

    schema_version: int = SCHEMA_VERSION


@dataclass(slots=True)
class TurnFrame:
    """Run one user turn on ``thread_id`` (text + optional staged media refs)."""

    thread_id: str
    text: str
    media: list[MediaRef] = field(default_factory=list)


@dataclass(slots=True)
class ApprovalVerdictFrame:
    """Resume a parked approval (#37) identified by gateway routing ``token``."""

    token: str
    approved: bool
    scope: str = "once"  # once | session (remote ALWAYS is out of v1)
    message: str = ""
    plan_mode_target: str | None = None


@dataclass(slots=True)
class CancelFrame:
    """Cancel the in-flight turn for ``thread_id`` (if any)."""

    thread_id: str


@dataclass(slots=True)
class SteerFrame:
    """Inject steer text into the active turn for ``thread_id``."""

    thread_id: str
    text: str


@dataclass(slots=True)
class ShutdownFrame:
    """Ask the worker to exit cleanly."""


# ---------------------------------------------------------------------------
# Outbound (worker → daemon)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EventFrame:
    """One worker-side agent event (already redacted at the serialize boundary)."""

    thread_id: str
    kind: str
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StatusFrame:
    """Periodic heartbeat with eviction-predicate inputs (#37 / #60)."""

    turn_in_flight: bool
    live_bg_jobs: int
    idle_ms: int
    parked_approvals: int | None = None  # optional; does not pin the worker


@dataclass(slots=True)
class ApprovalAskFrame:
    """Park-compatible approval request; daemon cards it, later sends verdict."""

    token: str
    thread_id: str
    action: str
    target: str
    description: str = ""
    dangerous: bool = False
    reason: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    plan: str | None = None
    suggested_memory: dict[str, Any] | None = None


@dataclass(slots=True)
class ErrorFrame:
    """Worker- or protocol-level failure surfaced to the daemon."""

    message: str
    code: str = "error"
    thread_id: str | None = None


type InboundFrame = (
    HandshakeFrame
    | TurnFrame
    | ApprovalVerdictFrame
    | CancelFrame
    | SteerFrame
    | ShutdownFrame
)

type OutboundFrame = EventFrame | StatusFrame | ApprovalAskFrame | ErrorFrame

type Frame = InboundFrame | OutboundFrame

_INBOUND_TYPES: dict[str, type] = {
    "handshake": HandshakeFrame,
    "turn": TurnFrame,
    "approval_verdict": ApprovalVerdictFrame,
    "cancel": CancelFrame,
    "steer": SteerFrame,
    "shutdown": ShutdownFrame,
}

_OUTBOUND_TYPES: dict[str, type] = {
    "event": EventFrame,
    "status": StatusFrame,
    "approval_ask": ApprovalAskFrame,
    "error": ErrorFrame,
}

_FRAME_NAME: dict[type, str] = {
    **{cls: name for name, cls in _INBOUND_TYPES.items()},
    **{cls: name for name, cls in _OUTBOUND_TYPES.items()},
}


def encode_line(frame: Frame) -> str:
    """Serialize *frame* to one NDJSON line (including the trailing newline)."""
    name = _FRAME_NAME.get(type(frame))
    if name is None:
        raise ProtocolError(f"not a gateway frame: {type(frame)!r}")
    payload = _to_wire_dict(frame)
    payload[_FRAME_KEY] = name
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"


def decode_inbound_line(line: str) -> InboundFrame:
    """Parse one NDJSON line as a daemon→worker frame."""
    return _decode_line(line, _INBOUND_TYPES)  # type: ignore[return-value]


def decode_outbound_line(line: str) -> OutboundFrame:
    """Parse one NDJSON line as a worker→daemon frame."""
    return _decode_line(line, _OUTBOUND_TYPES)  # type: ignore[return-value]


def _decode_line(line: str, registry: dict[str, type]) -> Frame:
    text = line.strip()
    if not text:
        raise ProtocolError("empty NDJSON line")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("frame must be a JSON object")
    name = obj.get(_FRAME_KEY)
    if not isinstance(name, str):
        raise ProtocolError(f"missing or invalid {_FRAME_KEY!r} discriminator")
    cls = registry.get(name)
    if cls is None:
        known = ", ".join(sorted(registry))
        raise ProtocolError(f"unknown frame {name!r} (expected one of: {known})")
    body = {k: v for k, v in obj.items() if k != _FRAME_KEY}
    try:
        frame = _from_wire_dict(cls, body)
    except (TypeError, KeyError, ValueError) as exc:
        raise ProtocolError(f"invalid {name} frame: {exc}") from exc
    if isinstance(frame, HandshakeFrame):
        _check_handshake_version(frame)
    return frame


def _check_handshake_version(frame: HandshakeFrame) -> None:
    if frame.schema_version != SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(frame.schema_version)


def _to_wire_dict(frame: Frame) -> dict[str, Any]:
    if isinstance(frame, TurnFrame):
        return {
            "thread_id": frame.thread_id,
            "text": frame.text,
            "media": [asdict(m) for m in frame.media],
        }
    if isinstance(frame, StatusFrame):
        out: dict[str, Any] = {
            "turn_in_flight": frame.turn_in_flight,
            "live_bg_jobs": frame.live_bg_jobs,
            "idle_ms": frame.idle_ms,
        }
        if frame.parked_approvals is not None:
            out["parked_approvals"] = frame.parked_approvals
        return out
    if isinstance(frame, ShutdownFrame):
        return {}
    return asdict(frame)


def _from_wire_dict(cls: type, body: dict[str, Any]) -> Frame:
    if cls is HandshakeFrame:
        if "schema_version" not in body:
            raise ProtocolError("handshake requires schema_version")
        version = body["schema_version"]
        if not isinstance(version, int) or isinstance(version, bool):
            raise ProtocolError("schema_version must be an int")
        return HandshakeFrame(schema_version=version)

    if cls is TurnFrame:
        media_raw = body.get("media", [])
        if not isinstance(media_raw, list):
            raise ProtocolError("turn.media must be a list")
        media = [
            MediaRef(
                path=str(item["path"]),
                mime=str(item["mime"]),
                modality=str(item["modality"]),
            )
            for item in media_raw
        ]
        return TurnFrame(
            thread_id=str(body["thread_id"]),
            text=str(body["text"]),
            media=media,
        )

    if cls is ShutdownFrame:
        return ShutdownFrame()

    if cls is StatusFrame:
        parked = body.get("parked_approvals")
        if parked is not None and not isinstance(parked, int):
            raise ProtocolError("status.parked_approvals must be an int when set")
        return StatusFrame(
            turn_in_flight=bool(body["turn_in_flight"]),
            live_bg_jobs=int(body["live_bg_jobs"]),
            idle_ms=int(body["idle_ms"]),
            parked_approvals=parked,
        )

    # Generic path for the remaining simple dataclasses.
    allowed = {f.name for f in fields(cls)}
    kwargs = {k: v for k, v in body.items() if k in allowed}
    return cls(**kwargs)  # type: ignore[operator]
