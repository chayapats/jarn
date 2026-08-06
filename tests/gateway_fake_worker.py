#!/usr/bin/env python3
"""Minimal fake gateway worker for daemon/session tests.

Speaks ``jarn.gateway.protocol`` over stdin/stdout. Controlled via env:

* ``FAKE_WORKER_SCHEMA`` — schema_version to expect (default: real SCHEMA_VERSION)
* ``FAKE_WORKER_IDLE_MS`` — idle_ms reported in status (default: 0)
* ``FAKE_WORKER_BG_JOBS`` — live_bg_jobs in status (default: 0)
* ``FAKE_WORKER_DIE_ON_TURN`` — if set, exit(1) after reading a turn (mid-turn death)
* ``FAKE_WORKER_TURN_HOLD_SECS`` — sleep while "in flight" before emitting done
* ``FAKE_WORKER_PARKED`` — parked_approvals field on status (optional)
"""

from __future__ import annotations

import os
import select
import sys
import time

# Ensure src layout works when invoked as a script path.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from jarn.gateway.protocol import (  # noqa: E402
    SCHEMA_VERSION,
    CancelFrame,
    ErrorFrame,
    EventFrame,
    HandshakeFrame,
    ShutdownFrame,
    StatusFrame,
    TurnFrame,
    UnsupportedSchemaVersion,
    decode_inbound_line,
    encode_line,
)


def select_stdin_ready() -> bool:
    try:
        ready, _, _ = select.select([sys.stdin], [], [], 0)
        return bool(ready)
    except (OSError, ValueError):
        return False


def _emit(frame) -> None:
    sys.stdout.write(encode_line(frame))
    sys.stdout.flush()


def _status(
    *,
    turn_in_flight: bool = False,
    idle_ms: int | None = None,
) -> None:
    parked_raw = os.environ.get("FAKE_WORKER_PARKED")
    parked = int(parked_raw) if parked_raw is not None else None
    _emit(
        StatusFrame(
            turn_in_flight=turn_in_flight,
            live_bg_jobs=int(os.environ.get("FAKE_WORKER_BG_JOBS", "0")),
            idle_ms=int(
                os.environ["FAKE_WORKER_IDLE_MS"]
                if idle_ms is None and "FAKE_WORKER_IDLE_MS" in os.environ
                else (idle_ms if idle_ms is not None else 0)
            ),
            parked_approvals=parked,
        )
    )


def main() -> int:
    expected = int(os.environ.get("FAKE_WORKER_SCHEMA", str(SCHEMA_VERSION)))
    # First frame must be handshake.
    first = sys.stdin.readline()
    if not first:
        return 2
    try:
        frame = decode_inbound_line(first)
    except UnsupportedSchemaVersion as exc:
        _emit(
            ErrorFrame(
                message=f"unsupported schema_version {exc.got}",
                code="unsupported_schema_version",
            )
        )
        return 3
    if not isinstance(frame, HandshakeFrame):
        _emit(ErrorFrame(message="expected handshake", code="protocol"))
        return 4
    if frame.schema_version != expected:
        _emit(
            ErrorFrame(
                message=f"unsupported schema_version {frame.schema_version}",
                code="unsupported_schema_version",
            )
        )
        return 3

    # Prefer FAKE_WORKER_IDLE_MS when set so eviction tests can drive idle_ms.
    _status(turn_in_flight=False)

    for line in sys.stdin:
        try:
            frame = decode_inbound_line(line)
        except Exception as exc:  # noqa: BLE001
            _emit(ErrorFrame(message=str(exc), code="protocol"))
            continue

        if isinstance(frame, ShutdownFrame):
            return 0

        if isinstance(frame, CancelFrame):
            _emit(
                EventFrame(
                    thread_id=frame.thread_id, kind="cancelled", text="cancelled"
                )
            )
            _status(turn_in_flight=False, idle_ms=0)
            continue

        if isinstance(frame, TurnFrame):
            if os.environ.get("FAKE_WORKER_DIE_ON_TURN"):
                # Mid-turn death: report in-flight then exit without done.
                _status(turn_in_flight=True, idle_ms=0)
                time.sleep(0.05)
                return 1
            _status(turn_in_flight=True, idle_ms=0)
            hold = float(os.environ.get("FAKE_WORKER_TURN_HOLD_SECS", "0"))
            # Sleep in slices so a CancelFrame buffered on stdin can land.
            cancelled = False
            end = time.monotonic() + hold
            while time.monotonic() < end:
                time.sleep(min(0.05, max(0.0, end - time.monotonic())))
                if select_stdin_ready():
                    peek = sys.stdin.readline()
                    if peek:
                        try:
                            nxt = decode_inbound_line(peek)
                        except Exception:  # noqa: BLE001
                            continue
                        if isinstance(nxt, CancelFrame):
                            _emit(
                                EventFrame(
                                    thread_id=nxt.thread_id,
                                    kind="cancelled",
                                    text="cancelled",
                                )
                            )
                            _status(turn_in_flight=False, idle_ms=0)
                            cancelled = True
                            break
                        if isinstance(nxt, ShutdownFrame):
                            return 0
            if cancelled:
                continue
            _emit(
                EventFrame(
                    thread_id=frame.thread_id,
                    kind="done",
                    text=f"echo:{frame.text}",
                )
            )
            _status(turn_in_flight=False, idle_ms=0)
            continue

        # ignore steer / approval_verdict in the fake
        _status(turn_in_flight=False)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
