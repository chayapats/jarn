"""Per-root gateway worker process loop (T-WKR-1 / T-WKR-2).

Reads private NDJSON on stdin and writes on stdout using
:mod:`jarn.gateway.protocol`. One long-lived process hosts a single
:class:`~jarn.controller.core.Controller` for one project root and serves N
``thread_id`` conversations serially.

Outbound frames are fail-closed redacted **before** serialization so secrets
never leave the worker (#36 / T-WKR-2). Periodic ``status`` heartbeats supply
eviction inputs; ``parked_approvals`` is informational and must not pin the
worker (#37).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import logging
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any, TextIO

from jarn.agent.events import ApprovalRequest, Event, EventKind
from jarn.agent.media_ingest import MediaInput
from jarn.agent.tool_arg_redact import sanitize_tool_args
from jarn.agent.turn_runner import run_agent_turn
from jarn.config.loader import load_config
from jarn.config.secrets import redact_secrets
from jarn.controller.core import Controller
from jarn.gateway.approvals import (
    ApprovalParked,
    PendingApproval,
    PendingApprovalMap,
    make_park_approver,
    resume_parked_approval,
)
from jarn.gateway.protocol import (
    SCHEMA_VERSION,
    ApprovalAskFrame,
    ApprovalVerdictFrame,
    CancelFrame,
    ErrorFrame,
    EventFrame,
    HandshakeFrame,
    InboundFrame,
    OutboundFrame,
    ProtocolError,
    ShutdownFrame,
    StatusFrame,
    SteerFrame,
    TurnFrame,
    UnsupportedSchemaVersion,
    decode_inbound_line,
    encode_line,
)

_log = logging.getLogger("jarn.gateway.worker")

#: Default seconds between ``status`` heartbeats.
DEFAULT_HEARTBEAT_INTERVAL_S = 2.0

#: Routing placeholder when the daemon has not yet stamped ``chat_id`` on the map.
_PLACEHOLDER_CHAT_ID = 0

#: Max depth when walking event ``data`` / approval payloads for string redaction.
_REDACT_DEPTH = 6

#: Element limit per container during outbound redaction walks.
_REDACT_ITEMS = 100


class WorkerOrderingError(ProtocolError):
    """Worker-side ordering violation (e.g. frames before handshake)."""


def redact_outbound_value(value: Any, *, depth: int = 0) -> Any:
    """Fail-closed redact + bound a value before it crosses the serialize boundary."""
    if isinstance(value, str):
        return redact_secrets(value)
    if depth >= _REDACT_DEPTH:
        return "<omitted: max depth>"
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _REDACT_ITEMS:
                out["__truncated"] = True
                break
            key_s = str(key)
            if key_s == "args" and isinstance(item, dict):
                out[key_s] = sanitize_tool_args(item)
            else:
                out[key_s] = redact_outbound_value(item, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        items: list[Any] = []
        for index, item in enumerate(value):
            if index >= _REDACT_ITEMS:
                break
            items.append(redact_outbound_value(item, depth=depth + 1))
        if len(value) > _REDACT_ITEMS:
            items.append("<truncated>")
        return items
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return redact_outbound_value(asdict(value), depth=depth)
    return value


def redact_outbound_frame(frame: OutboundFrame) -> OutboundFrame:
    """Return a copy of *frame* with secrets scrubbed (T-WKR-2)."""
    if isinstance(frame, EventFrame):
        return EventFrame(
            thread_id=frame.thread_id,
            kind=frame.kind,
            text=redact_secrets(frame.text or ""),
            data=redact_outbound_value(frame.data or {}),
        )
    if isinstance(frame, ApprovalAskFrame):
        mem: dict[str, Any] | None = None
        if frame.suggested_memory is not None:
            redacted = redact_outbound_value(frame.suggested_memory)
            mem = redacted if isinstance(redacted, dict) else {"value": redacted}
        skill: dict[str, Any] | None = None
        if frame.suggested_skill is not None:
            redacted = redact_outbound_value(frame.suggested_skill)
            skill = redacted if isinstance(redacted, dict) else {"value": redacted}
        return ApprovalAskFrame(
            token=frame.token,
            thread_id=frame.thread_id,
            action=redact_secrets(frame.action or ""),
            target=redact_secrets(frame.target or ""),
            description=redact_secrets(frame.description or ""),
            dangerous=frame.dangerous,
            reason=redact_secrets(frame.reason or ""),
            args=sanitize_tool_args(frame.args),
            plan=redact_secrets(frame.plan) if frame.plan is not None else None,
            suggested_memory=mem,
            suggested_skill=skill,
        )
    if isinstance(frame, ErrorFrame):
        return ErrorFrame(
            message=redact_secrets(frame.message or ""),
            code=frame.code,
            thread_id=frame.thread_id,
        )
    # StatusFrame carries only counters / booleans.
    return frame


def event_to_frame(event: Event, *, thread_id: str) -> EventFrame:
    """Map an agent :class:`Event` to a private pipe :class:`EventFrame`."""
    kind = event.kind.value if isinstance(event.kind, EventKind) else str(event.kind)
    return EventFrame(
        thread_id=thread_id,
        kind=kind,
        text=event.text or "",
        data=dict(event.data or {}),
    )


def _media_inputs(frame: TurnFrame) -> list[MediaInput]:
    return [MediaInput(path=ref.path, mime=ref.mime, modality=ref.modality) for ref in frame.media]


def _live_bg_job_count() -> int:
    try:
        from jarn.agent.background import manager

        return sum(1 for row in manager().list() if row.get("running"))
    except Exception:  # noqa: BLE001 — status must never crash the worker
        return 0


def _approval_ask_from_park(record: PendingApproval, request: ApprovalRequest) -> ApprovalAskFrame:
    action = request.action
    tool = getattr(action, "tool", None) or ""
    kind = getattr(getattr(action, "kind", None), "value", None) or ""
    action_name = tool or kind or "tool"
    mem: dict[str, Any] | None = None
    skill: dict[str, Any] | None = None
    if request.suggested_memory is not None:
        mem = asdict(request.suggested_memory)
    if request.suggested_skill is not None:
        skill = asdict(request.suggested_skill)
    args = dict(request.args or {})
    result = request.result
    return ApprovalAskFrame(
        token=record.token,
        thread_id=record.thread_id,
        action=str(action_name),
        target=str(getattr(action, "target", "") or ""),
        description=request.description or "",
        dangerous=bool(getattr(result, "dangerous", False)),
        reason=str(getattr(result, "reason", "") or ""),
        args=args,
        plan=request.plan,
        suggested_memory=mem,
        suggested_skill=skill,
    )


class GatewayWorker:
    """NDJSON worker loop for one project root.

    Construct with an injectable :class:`Controller` for tests, or let
    :meth:`from_root` build a real one. Turns are serialized by construction
    (one in-flight task); inbound ``cancel`` / ``steer`` target that task.
    """

    def __init__(
        self,
        *,
        root: Path | str,
        controller: Controller,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_S,
        approval_store: PendingApprovalMap | None = None,
        chat_id: int = _PLACEHOLDER_CHAT_ID,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.controller = controller
        self._stdin = stdin if stdin is not None else sys.stdin
        self._stdout = stdout if stdout is not None else sys.stdout
        self.heartbeat_interval = max(0.05, float(heartbeat_interval))
        self.approval_store = approval_store or PendingApprovalMap()
        self.chat_id = chat_id
        self._clock = clock or time.monotonic
        self._handshaken = False
        self._shutdown = False
        self._turn_task: asyncio.Task[Any] | None = None
        self._turn_thread_id: str | None = None
        self._last_activity = self._clock()
        self._emit_lock = asyncio.Lock()

    @classmethod
    def from_root(
        cls,
        root: Path | str,
        *,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_S,
        approval_store: PendingApprovalMap | None = None,
        chat_id: int = _PLACEHOLDER_CHAT_ID,
    ) -> GatewayWorker:
        """Load config and construct a :class:`Controller` for *root*."""
        project_root = Path(root).expanduser().resolve()
        config = load_config(project_root=project_root)
        controller = Controller(config, project_root, project_trusted=True)
        return cls(
            root=project_root,
            controller=controller,
            stdin=stdin,
            stdout=stdout,
            heartbeat_interval=heartbeat_interval,
            approval_store=approval_store,
            chat_id=chat_id,
        )

    # -- outbound ------------------------------------------------------------

    def emit(self, frame: OutboundFrame) -> None:
        """Redact, encode, and write one outbound NDJSON line (blocking write)."""
        safe = redact_outbound_frame(frame)
        line = encode_line(safe)
        self._stdout.write(line)
        self._stdout.flush()

    async def aemit(self, frame: OutboundFrame) -> None:
        """Async-safe emit (serializes writers)."""
        async with self._emit_lock:
            await asyncio.to_thread(self.emit, frame)

    def status_frame(self) -> StatusFrame:
        """Build a heartbeat :class:`StatusFrame` for the eviction predicate."""
        turn_in_flight = self._turn_in_flight()
        idle_ms = 0 if turn_in_flight else int(max(0.0, self._clock() - self._last_activity) * 1000)
        parked = self._parked_count_for_root()
        return StatusFrame(
            turn_in_flight=turn_in_flight,
            live_bg_jobs=_live_bg_job_count(),
            idle_ms=idle_ms,
            parked_approvals=parked,
        )

    def _turn_in_flight(self) -> bool:
        task = self._turn_task
        return task is not None and not task.done()

    def _parked_count_for_root(self) -> int:
        root_s = str(self.root)
        n = 0
        for row in self.approval_store.list():
            try:
                if Path(row.root).expanduser().resolve() == self.root or row.root == root_s:
                    n += 1
            except (OSError, ValueError):
                if row.root == root_s:
                    n += 1
        return n

    def _touch(self) -> None:
        self._last_activity = self._clock()

    # -- inbound handlers ----------------------------------------------------

    async def handle_frame(self, frame: InboundFrame) -> None:
        """Dispatch one decoded inbound frame."""
        self._touch()
        if isinstance(frame, HandshakeFrame):
            await self._handle_handshake(frame)
            return
        if not self._handshaken:
            await self.aemit(
                ErrorFrame(
                    message="handshake required before other frames",
                    code="handshake_required",
                )
            )
            raise WorkerOrderingError("handshake required before other frames")
        if isinstance(frame, TurnFrame):
            await self._handle_turn(frame)
        elif isinstance(frame, ApprovalVerdictFrame):
            await self._handle_approval_verdict(frame)
        elif isinstance(frame, CancelFrame):
            await self._handle_cancel(frame)
        elif isinstance(frame, SteerFrame):
            await self._handle_steer(frame)
        elif isinstance(frame, ShutdownFrame):
            await self._handle_shutdown()
        else:  # pragma: no cover - type exhaustiveness
            await self.aemit(
                ErrorFrame(message=f"unsupported frame: {type(frame)!r}", code="unsupported")
            )

    async def _handle_handshake(self, frame: HandshakeFrame) -> None:
        # decode_inbound_line already rejects mismatches; belt-and-suspenders here.
        if frame.schema_version != SCHEMA_VERSION:
            raise UnsupportedSchemaVersion(frame.schema_version)
        self._handshaken = True
        await self.aemit(self.status_frame())

    async def _handle_turn(self, frame: TurnFrame) -> None:
        if self._turn_in_flight():
            await self.aemit(
                ErrorFrame(
                    message="turn already in flight",
                    code="busy",
                    thread_id=frame.thread_id,
                )
            )
            return
        self._turn_thread_id = frame.thread_id
        task = asyncio.create_task(
            self._run_turn(frame),
            name=f"gateway-turn-{frame.thread_id}",
        )
        self._turn_task = task
        self.controller.bind_turn_task(task)
        # Do not await here — the main loop must keep reading cancel/steer/shutdown.
        task.add_done_callback(self._on_turn_done)

    def _on_turn_done(self, task: asyncio.Task[Any]) -> None:
        if self._turn_task is task:
            self._turn_task = None
            self._turn_thread_id = None
            self.controller.bind_turn_task(None)
            self._touch()

    async def _try_local_slash(self, frame: TurnFrame) -> bool:
        """Run registry display commands locally — same pages as the REPL."""
        from jarn.commands.registry import is_gateway_local_command, parse_slash_line
        from jarn.controller.core import CommandResult

        parsed = parse_slash_line(frame.text)
        if parsed is None:
            return False
        name, args = parsed
        if not is_gateway_local_command(name):
            return False
        handler = getattr(self.controller, "handle_command", None)
        if not callable(handler):
            return False
        result = handler(name, args)
        if not isinstance(result, CommandResult) or result.seed_turn:
            return False
        await self.aemit(
            EventFrame(
                thread_id=frame.thread_id,
                kind="notice",
                text=result.text or "",
            )
        )
        await self.aemit(EventFrame(thread_id=frame.thread_id, kind="done"))
        return True

    async def _run_turn(self, frame: TurnFrame) -> None:
        thread_id = frame.thread_id
        try:
            self.controller.resume_thread(thread_id)
            await self.controller.ensure_runtime()
            if not frame.media and await self._try_local_slash(frame):
                return
            enriched = await asyncio.to_thread(self.controller.enrich_turn_input, frame.text)
            approver = self._make_park_approver(
                thread_id,
                chat_id=frame.chat_id,
            )

            async def on_event(event: Event) -> None:
                await self.aemit(event_to_frame(event, thread_id=thread_id))

            result = await run_agent_turn(
                self.controller,
                enriched,
                approver=approver,
                media=_media_inputs(frame) or None,
                on_event=on_event,
            )
            if result.error is not None:
                await self.aemit(
                    ErrorFrame(
                        message=result.error.text or "turn failed",
                        code="turn_error",
                        thread_id=thread_id,
                    )
                )
        except ApprovalParked:
            # approval_ask already emitted via on_park; interrupt stays in state.sqlite.
            _log.info("approval parked thread=%s", thread_id)
        except asyncio.CancelledError:
            await self.aemit(
                ErrorFrame(
                    message="turn cancelled",
                    code="cancelled",
                    thread_id=thread_id,
                )
            )
            raise
        except Exception as exc:  # noqa: BLE001 — fail-loud to daemon
            _log.exception("turn failed thread=%s", thread_id)
            with contextlib.suppress(Exception):
                await self.aemit(
                    ErrorFrame(
                        message=str(exc) or type(exc).__name__,
                        code="turn_failed",
                        thread_id=thread_id,
                    )
                )

    async def _handle_approval_verdict(self, frame: ApprovalVerdictFrame) -> None:
        if self._turn_in_flight():
            await self.aemit(
                ErrorFrame(
                    message="cannot resume approval while a turn is in flight",
                    code="busy",
                )
            )
            return
        record = self.approval_store.get(frame.token)
        if record is None:
            await self.aemit(
                ErrorFrame(
                    message=f"unknown approval token: {frame.token}",
                    code="unknown_token",
                )
            )
            return
        self._turn_thread_id = record.thread_id
        task = asyncio.create_task(
            self._run_approval_verdict(frame, record),
            name=f"gateway-verdict-{frame.token[:8]}",
        )
        self._turn_task = task
        self.controller.bind_turn_task(task)
        task.add_done_callback(self._on_turn_done)

    async def _run_approval_verdict(
        self, frame: ApprovalVerdictFrame, record: PendingApproval
    ) -> None:
        thread_id = record.thread_id
        try:
            self.controller.resume_thread(thread_id)
            await self.controller.ensure_runtime()
            # Park approver is the restored default after the fixed verdict lands,
            # so a later ASK in the same resume stream parks again.
            park = self._make_park_approver(
                thread_id,
                chat_id=record.chat_id,
            )
            driver = self.controller.make_driver(park)
            async for event in resume_parked_approval(
                driver,
                token=frame.token,
                approved=frame.approved,
                scope=frame.scope,
                message=frame.message,
                plan_mode_target=frame.plan_mode_target,
                store=self.approval_store,
            ):
                await self.aemit(event_to_frame(event, thread_id=thread_id))
        except ApprovalParked:
            _log.info("approval parked again thread=%s", thread_id)
        except asyncio.CancelledError:
            await self.aemit(
                ErrorFrame(
                    message="approval resume cancelled",
                    code="cancelled",
                    thread_id=thread_id,
                )
            )
            raise
        except Exception as exc:  # noqa: BLE001
            _log.exception("approval resume failed token=%s", frame.token)
            with contextlib.suppress(Exception):
                await self.aemit(
                    ErrorFrame(
                        message=str(exc) or type(exc).__name__,
                        code="resume_failed",
                        thread_id=thread_id,
                    )
                )

    async def _handle_cancel(self, frame: CancelFrame) -> None:
        if not self._turn_in_flight():
            return
        if self._turn_thread_id and frame.thread_id != self._turn_thread_id:
            await self.aemit(
                ErrorFrame(
                    message=(
                        f"cancel thread_id={frame.thread_id!r} does not match "
                        f"in-flight {self._turn_thread_id!r}"
                    ),
                    code="thread_mismatch",
                    thread_id=frame.thread_id,
                )
            )
            return
        await self.controller.abort()

    async def _handle_steer(self, frame: SteerFrame) -> None:
        if not self._turn_in_flight():
            await self.aemit(
                ErrorFrame(
                    message="no turn in flight to steer",
                    code="idle",
                    thread_id=frame.thread_id,
                )
            )
            return
        if self._turn_thread_id and frame.thread_id != self._turn_thread_id:
            await self.aemit(
                ErrorFrame(
                    message=(
                        f"steer thread_id={frame.thread_id!r} does not match "
                        f"in-flight {self._turn_thread_id!r}"
                    ),
                    code="thread_mismatch",
                    thread_id=frame.thread_id,
                )
            )
            return
        self.controller._steer_slot = frame.text

    async def _handle_shutdown(self) -> None:
        self._shutdown = True
        if self._turn_in_flight():
            try:
                await self.controller.abort()
            except Exception:  # noqa: BLE001
                _log.exception("abort during shutdown")
        # Heartbeat / read loops observe _shutdown and exit.

    def _make_park_approver(
        self,
        thread_id: str,
        *,
        chat_id: int | None = None,
    ):
        async def on_park(record: PendingApproval, request: ApprovalRequest) -> None:
            ask = _approval_ask_from_park(record, request)
            await self.aemit(ask)

        def card_factory(
            record: PendingApproval,
            request: ApprovalRequest,
        ) -> dict[str, Any]:
            ask = redact_outbound_frame(_approval_ask_from_park(record, request))
            assert isinstance(ask, ApprovalAskFrame)
            return {
                "action": ask.action,
                "target": ask.target,
                "description": ask.description,
                "args": dict(ask.args),
                "plan": ask.plan,
                "suggested_memory": ask.suggested_memory,
                "suggested_skill": ask.suggested_skill,
                "dangerous": ask.dangerous,
            }

        return make_park_approver(
            root=self.root,
            thread_id=thread_id,
            # Routing bookkeeping only; LangGraph interrupt in state.sqlite is SoT.
            interrupt_id="parked",
            chat_id=self.chat_id if chat_id is None else chat_id,
            store=self.approval_store,
            card_factory=card_factory,
            on_park=on_park,
        )

    # -- main loop -----------------------------------------------------------

    async def run(self) -> int:
        """Run until ``shutdown``, EOF, or a fatal protocol error. Returns exit code."""
        heartbeat = asyncio.create_task(self._heartbeat_loop(), name="gateway-worker-heartbeat")
        exit_code = 0
        try:
            async for line in self._stdin_lines():
                if self._shutdown:
                    break
                try:
                    frame = decode_inbound_line(line)
                except UnsupportedSchemaVersion as exc:
                    await self.aemit(
                        ErrorFrame(
                            message=str(exc),
                            code="unsupported_schema_version",
                        )
                    )
                    exit_code = 2
                    self._shutdown = True
                    break
                except ProtocolError as exc:
                    await self.aemit(ErrorFrame(message=str(exc), code="protocol_error"))
                    continue
                try:
                    await self.handle_frame(frame)
                except WorkerOrderingError as exc:
                    _log.warning("worker protocol: %s", exc)
                    exit_code = 2
                    self._shutdown = True
                    break
                except UnsupportedSchemaVersion as exc:
                    await self.aemit(
                        ErrorFrame(
                            message=str(exc),
                            code="unsupported_schema_version",
                        )
                    )
                    exit_code = 2
                    self._shutdown = True
                    break
                if self._shutdown:
                    break
        except Exception as exc:  # noqa: BLE001 — fail-loud
            exit_code = 1
            await self._fail_loud(f"worker crashed: {exc}")
        finally:
            self._shutdown = True
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            if self._turn_in_flight():
                await self._fail_loud(
                    "worker exiting with turn in flight",
                    code="worker_death",
                    thread_id=self._turn_thread_id,
                )
                task = self._turn_task
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
            try:
                await self.controller.aclose()
            except Exception:  # noqa: BLE001
                _log.exception("controller.aclose failed")
        return exit_code

    async def _fail_loud(
        self,
        message: str,
        *,
        code: str = "worker_death",
        thread_id: str | None = None,
    ) -> None:
        """Best-effort error emit on worker death mid-turn (no auto-replay)."""
        try:
            await self.aemit(ErrorFrame(message=message, code=code, thread_id=thread_id))
        except Exception:  # noqa: BLE001
            _log.exception("failed to emit death error")

    async def _heartbeat_loop(self) -> None:
        while not self._shutdown:
            if self._handshaken:
                try:
                    await self.aemit(self.status_frame())
                except Exception:  # noqa: BLE001
                    _log.exception("status heartbeat failed")
            try:
                await asyncio.sleep(self.heartbeat_interval)
            except asyncio.CancelledError:
                raise

    async def _stdin_lines(self):
        loop = asyncio.get_running_loop()
        while not self._shutdown:
            line = await loop.run_in_executor(None, self._stdin.readline)
            if line == "":
                # EOF — treat as shutdown. Fail-loud if a turn is still running.
                self._shutdown = True
                break
            yield line


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jarn.gateway.worker",
        description="Per-root gateway worker (private NDJSON on stdin/stdout).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Project root (default: $JARN_GATEWAY_ROOT from the daemon)",
    )
    parser.add_argument(
        "--heartbeat-interval",
        type=float,
        default=DEFAULT_HEARTBEAT_INTERVAL_S,
        help="Seconds between status heartbeats (default: %(default)s)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry: ``python -m jarn.gateway.worker`` (daemon sets ``JARN_GATEWAY_ROOT``)."""
    args = build_arg_parser().parse_args(argv)
    root_raw = args.root or os.environ.get("JARN_GATEWAY_ROOT")
    if not root_raw:
        print(
            "jarn.gateway.worker: pass --root or set JARN_GATEWAY_ROOT",
            file=sys.stderr,
        )
        return 2
    worker = GatewayWorker.from_root(
        root_raw,
        heartbeat_interval=args.heartbeat_interval,
    )
    return asyncio.run(worker.run())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL_S",
    "GatewayWorker",
    "WorkerOrderingError",
    "build_arg_parser",
    "event_to_frame",
    "main",
    "redact_outbound_frame",
    "redact_outbound_value",
]
