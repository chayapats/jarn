"""Private transport-daemon ↔ per-root-worker pipe (gateway-internal).

This package owns the wire between the Telegram transport daemon and a long-lived
worker process. The NDJSON schema here is **private** and deliberately distinct
from ``jarn -p --output-format stream-json`` — it may change in one commit and is
not a public embedding API.

See docs/TELEGRAM_GATEWAY_PLAN.md and GitHub #60.
"""

from jarn.gateway.protocol import (
    SCHEMA_VERSION,
    ApprovalAskFrame,
    ApprovalVerdictFrame,
    CancelFrame,
    ErrorFrame,
    EventFrame,
    HandshakeFrame,
    InboundFrame,
    MediaRef,
    OutboundFrame,
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

__all__ = [
    "SCHEMA_VERSION",
    "ApprovalAskFrame",
    "ApprovalVerdictFrame",
    "CancelFrame",
    "ErrorFrame",
    "EventFrame",
    "HandshakeFrame",
    "InboundFrame",
    "MediaRef",
    "OutboundFrame",
    "ProtocolError",
    "ShutdownFrame",
    "StatusFrame",
    "SteerFrame",
    "TurnFrame",
    "UnsupportedSchemaVersion",
    "decode_inbound_line",
    "decode_outbound_line",
    "encode_line",
]
