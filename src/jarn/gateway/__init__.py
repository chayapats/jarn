"""Telegram gateway process boundary — daemon, workers, root leases, private pipe.

The NDJSON schema in ``jarn.gateway.protocol`` is **private** (daemon ↔ per-root
worker) and deliberately distinct from ``jarn -p --output-format stream-json``.
It may change in one commit and is not a public embedding API.

See docs/TELEGRAM_GATEWAY_PLAN.md and GitHub #60 / #52.
"""

from jarn.gateway.lease import RootLease, RootLeaseHeldError
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
    "RootLease",
    "RootLeaseHeldError",
    "ShutdownFrame",
    "StatusFrame",
    "SteerFrame",
    "TurnFrame",
    "UnsupportedSchemaVersion",
    "decode_inbound_line",
    "decode_outbound_line",
    "encode_line",
]
