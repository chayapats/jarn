"""Telegram gateway process boundary — daemon, workers, root leases, private pipe.

The NDJSON schema in ``jarn.gateway.protocol`` is **private** (daemon ↔ per-root
worker) and deliberately distinct from ``jarn -p --output-format stream-json``.
It may change in one commit and is not a public embedding API.

See docs/TELEGRAM_GATEWAY_PLAN.md and GitHub #60 / #52.
"""

from jarn.gateway.approvals import (
    ApprovalParked,
    PendingApproval,
    PendingApprovalMap,
    make_park_approver,
    make_verdict_approver,
    mint_approval_token,
    pending_approvals_path,
    record_pending_approval,
    resume_parked_approval,
)
from jarn.gateway.daemon import (
    DEFAULT_IDLE_TIMEOUT_MS,
    DEFAULT_WORKER_COMMAND,
    DaemonSupervisor,
    WorkerDeadError,
    WorkerHandle,
    WorkerProtocolError,
)
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
from jarn.gateway.sessions import (
    QUEUED_NOTICE,
    ForbiddenRootError,
    QueuedTurn,
    RootBusyLeaseError,
    SessionRouter,
    UnknownRepoError,
    validate_gateway_root,
)
from jarn.gateway.worker import (
    DEFAULT_HEARTBEAT_INTERVAL_S,
    GatewayWorker,
    event_to_frame,
    redact_outbound_frame,
    redact_outbound_value,
)

__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL_S",
    "DEFAULT_IDLE_TIMEOUT_MS",
    "DEFAULT_WORKER_COMMAND",
    "SCHEMA_VERSION",
    "QUEUED_NOTICE",
    "ApprovalAskFrame",
    "ApprovalParked",
    "ApprovalVerdictFrame",
    "CancelFrame",
    "DaemonSupervisor",
    "ErrorFrame",
    "EventFrame",
    "ForbiddenRootError",
    "GatewayWorker",
    "HandshakeFrame",
    "InboundFrame",
    "MediaRef",
    "OutboundFrame",
    "PendingApproval",
    "PendingApprovalMap",
    "ProtocolError",
    "QueuedTurn",
    "RootBusyLeaseError",
    "RootLease",
    "RootLeaseHeldError",
    "SessionRouter",
    "ShutdownFrame",
    "StatusFrame",
    "SteerFrame",
    "TurnFrame",
    "UnknownRepoError",
    "UnsupportedSchemaVersion",
    "WorkerDeadError",
    "WorkerHandle",
    "WorkerProtocolError",
    "decode_inbound_line",
    "decode_outbound_line",
    "encode_line",
    "event_to_frame",
    "make_park_approver",
    "make_verdict_approver",
    "mint_approval_token",
    "pending_approvals_path",
    "record_pending_approval",
    "redact_outbound_frame",
    "redact_outbound_value",
    "resume_parked_approval",
    "validate_gateway_root",
]
