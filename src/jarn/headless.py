"""Headless one-shot runner for non-interactive / CI use.

``jarn -p "do X"`` drives one or more agent turns through the same controller +
session path the REPL uses, prints the assistant's final text to stdout, and exits.

Fail-closed safety: in headless mode there is no human to approve a gated tool.
If the effective permission mode is ``ask`` or ``plan`` and an approval is
required, the run refuses the action and exits non-zero rather than silently
auto-approving. Callers that want unattended execution must opt in explicitly
via ``--permission-mode auto-edit`` or ``yolo``.

Output formats (``--output-format text|json|stream-json``):

* ``text`` — the assistant's final reply as plain text (the default).
* ``json`` — a single buffered final object (the :func:`_result_payload`
  envelope), including the session ``thread_id``. ``--json`` is a legacy alias
  for this.
* ``stream-json`` — newline-delimited JSON (NDJSON): one object per Event as the
  turn runs, then a terminal ``{"type": "result", ...}`` object carrying the
  same envelope (plus ``transcript_path`` when a transcript is being written),
  so a CI caller can locate/resume the session it just ran. This mirrors the
  spirit of ``claude -p --output-format stream-json``
  (one event per line, a terminal result object); each line is flushed as it is
  emitted so the stream is live.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from jarn.agent.session import (
    ApprovalReply,
    ApprovalRequest,
    Approver,
    Event,
    EventKind,
)
from jarn.agent.turn_runner import run_agent_turn
from jarn.config.schema import Config, PermissionMode
from jarn.cost import BudgetExceeded
from jarn.errors import ErrorCode, JarnUserError, error_detail
from jarn.exit_codes import (
    EXIT_AUTH,
    EXIT_BUDGET_EXCEEDED,
    EXIT_CANCELLED,
    EXIT_INTERNAL,
    EXIT_MODEL_UNAVAILABLE,
    EXIT_NETWORK_PROVIDER,
    EXIT_PERMISSION_DENIED,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
    EXIT_USAGE_CONFIG,
    EXIT_VERIFICATION_FAILED,
)
from jarn.tui.controller import Controller

# Auto-approving modes: the user explicitly opted in, so headless may proceed.
_AUTO_MODES = frozenset({PermissionMode.AUTO_EDIT, PermissionMode.YOLO})

# Backward-compatible import names.  The values now participate in the public
# version-1 taxonomy in :mod:`jarn.exit_codes`.
EXIT_ERROR = EXIT_INTERNAL
EXIT_REFUSED = EXIT_PERMISSION_DENIED

_TIMEOUT_MSG_HINTS = (
    "timed out",
    "timeout",
    "time out",
)

_UNSERIALIZABLE = "<unserializable>"


def _json_dumps(value: Any) -> str:
    """Serialize headless output without exposing non-JSON object representations."""
    return json.dumps(value, default=lambda _value: _UNSERIALIZABLE)


@dataclass(slots=True)
class HeadlessResult:
    """The outcome of a headless run."""

    result: Any
    """The assistant's final text reply, or a parsed dict when ``--output-schema`` is used."""
    tokens: dict[str, Any] = field(default_factory=dict)
    """Per-model token counts (input/output/total), keyed by model ref."""
    cost: float = 0.0
    """Total session cost in USD."""
    turns: int = 1
    """How many complete user turns ran (one per headless invocation)."""
    tool_calls: int = 0
    """How many tool invocations the agent made across all turns."""
    verification: dict[str, Any] | None = None
    """Structured final verification outcome, when verification was requested."""
    project_trusted: bool = True
    """Whether project-level configuration was trusted for this run."""
    permission_mode: str = PermissionMode.ASK.value
    """The effective permission mode after presets, overrides, and trust clamping."""
    thread_id: str = ""
    """The session thread id, so a CI caller can locate/resume this run."""
    transcript_path: str | None = None
    """Path to the JSONL transcript, when one was written (else ``None``)."""


class HeadlessRefusal(Exception):
    """Raised when fail-closed safety blocks a gated tool.

    Carries the tool name and reason so the caller can emit a clear message.
    """

    kind = "refusal"
    exit_code = EXIT_REFUSED

    def __init__(self, tool: str, reason: str) -> None:
        super().__init__(f"headless: gated tool refused — {tool!r}: {reason}")
        self.tool = tool
        self.reason = reason


class HeadlessFailure(Exception):
    """Structured headless failure with a stable exit code and error kind."""

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        exit_code: int = EXIT_ERROR,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.exit_code = exit_code
        self.details = details or {}


def _is_timeout_message(text: str) -> bool:
    lowered = text.lower()
    return any(hint in lowered for hint in _TIMEOUT_MSG_HINTS)


def _looks_like_model_failure(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "model not found",
            "model is not available",
            "model unavailable",
            "no main model configured",
            "unknown model",
            "unsupported model",
        )
    )


def _looks_like_auth_failure(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "api key",
            "no key",
            "authentication",
            "not signed in",
            "login required",
            "credential",
            "unauthorized",
        )
    )


def _public_failure_details(failure: HeadlessFailure) -> dict[str, Any]:
    """Return the stable public error anatomy for a headless failure."""
    existing = dict(failure.details)
    if all(
        key in existing
        for key in ("code", "summary", "cause", "component", "retryable", "action")
    ):
        return existing

    contracts: dict[str, tuple[ErrorCode, bool, str]] = {
        "usage": (
            ErrorCode.CLI_USAGE,
            False,
            "Correct the command or configuration, then run it again.",
        ),
        "config": (
            ErrorCode.CONFIG_INVALID_SCHEMA,
            False,
            "Run `jarn config validate`, correct the reported setting, then retry.",
        ),
        "auth": (
            ErrorCode.AUTH_FAILED,
            False,
            "Run `jarn auth status`, then `jarn auth repair` if needed.",
        ),
        "model": (
            ErrorCode.MODEL_UNAVAILABLE,
            False,
            "Run `jarn model refresh` and select an available model.",
        ),
        "permission": (
            ErrorCode.PERMISSION_DENIED,
            False,
            "Review the requested action and choose an appropriate permission mode.",
        ),
        "refusal": (
            ErrorCode.PERMISSION_DENIED,
            False,
            "Review the requested action and choose an appropriate permission mode.",
        ),
        "provider": (
            ErrorCode.NETWORK_FAILED,
            True,
            "Check provider status, network, proxy, and CA settings, then retry.",
        ),
        "network": (
            ErrorCode.NETWORK_FAILED,
            True,
            "Check the network, proxy, and CA settings, then retry.",
        ),
        "timeout": (
            ErrorCode.NETWORK_FAILED,
            True,
            "Check connectivity and retry; increase the timeout if appropriate.",
        ),
        "budget": (
            ErrorCode.BUDGET_EXCEEDED,
            False,
            "Increase the explicit session budget or reduce the requested work.",
        ),
        "verification": (
            ErrorCode.VERIFICATION_FAILED,
            False,
            "Inspect the verification result, fix the reported failure, and retry.",
        ),
        "cancelled": (
            ErrorCode.CANCELLED,
            True,
            "Run the command again when ready.",
        ),
        "schema": (
            ErrorCode.VERIFICATION_FAILED,
            False,
            "Correct the output schema or task, then retry.",
        ),
    }
    code, retryable, action = contracts.get(
        failure.kind,
        (
            ErrorCode.INTERNAL,
            False,
            "Run `jarn doctor --report jarn-support-report.json` and inspect the local log.",
        ),
    )
    public = error_detail(
        code,
        failure.message,
        cause=failure.message,
        component="headless",
        retryable=retryable,
        action=action,
        details=existing or None,
    ).to_dict()
    nested = public.pop("details", None)
    if isinstance(nested, dict):
        public.update(nested)
    return public


def _classify_exception(exc: BaseException) -> HeadlessFailure:
    if isinstance(exc, HeadlessRefusal):
        return HeadlessFailure(
            exc.kind,
            str(exc),
            exit_code=exc.exit_code,
        )
    if isinstance(exc, BudgetExceeded):
        return HeadlessFailure("budget", str(exc), exit_code=EXIT_BUDGET_EXCEEDED)
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return HeadlessFailure("timeout", str(exc), exit_code=EXIT_TIMEOUT)
    if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
        return HeadlessFailure(
            "cancelled",
            "run cancelled by user",
            exit_code=EXIT_CANCELLED,
        )
    if isinstance(exc, HeadlessFailure):
        return exc
    if isinstance(exc, JarnUserError):
        code = exc.detail.code
        if code.startswith("JARN-AUTH-"):
            exit_code = EXIT_AUTH
            kind = "auth"
        elif code.startswith("JARN-MODEL-"):
            exit_code = EXIT_MODEL_UNAVAILABLE
            kind = "model"
        elif code.startswith("JARN-SAFE-"):
            exit_code = EXIT_PERMISSION_DENIED
            kind = "permission"
        elif code.startswith("JARN-NET-"):
            exit_code = EXIT_NETWORK_PROVIDER
            kind = "network"
        elif code.startswith("JARN-CONFIG-"):
            exit_code = EXIT_USAGE_CONFIG
            kind = "config"
        else:
            exit_code = EXIT_INTERNAL
            kind = "internal"
        return HeadlessFailure(
            kind,
            exc.detail.summary,
            exit_code=exit_code,
            details=exc.to_dict(),
        )
    message = str(exc)
    if _is_timeout_message(message):
        return HeadlessFailure("timeout", message, exit_code=EXIT_TIMEOUT)
    if _looks_like_auth_failure(message):
        return HeadlessFailure("auth", message, exit_code=EXIT_AUTH)
    if _looks_like_model_failure(message):
        return HeadlessFailure("model", message, exit_code=EXIT_MODEL_UNAVAILABLE)
    return HeadlessFailure("internal", message, exit_code=EXIT_INTERNAL)


def _error_from_event(text: str, data: dict[str, Any] | None) -> HeadlessFailure:
    payload = data or {}
    if payload.get("verification"):
        return HeadlessFailure(
            "verification",
            text,
            exit_code=EXIT_VERIFICATION_FAILED,
            details={"verification": payload["verification"]},
        )
    if payload.get("budget"):
        return HeadlessFailure("budget", text, exit_code=EXIT_BUDGET_EXCEEDED)
    if _is_timeout_message(text):
        return HeadlessFailure("timeout", text, exit_code=EXIT_TIMEOUT)
    if payload.get("auth"):
        return HeadlessFailure("auth", text, exit_code=EXIT_AUTH)
    if payload.get("permission") or payload.get("denied"):
        return HeadlessFailure("permission", text, exit_code=EXIT_PERMISSION_DENIED)
    if payload.get("model_unavailable") or payload.get("model") == "unavailable":
        return HeadlessFailure("model", text, exit_code=EXIT_MODEL_UNAVAILABLE)
    if _looks_like_model_failure(text):
        return HeadlessFailure("model", text, exit_code=EXIT_MODEL_UNAVAILABLE)
    if payload.get("retryable") or payload.get("network") or payload.get("provider"):
        return HeadlessFailure("provider", text, exit_code=EXIT_NETWORK_PROVIDER)
    return HeadlessFailure("internal", text, exit_code=EXIT_INTERNAL)


def _emit_failure(
    failure: HeadlessFailure,
    *,
    as_json: bool,
    hint: str | None = None,
) -> int:
    public = _public_failure_details(failure)
    if as_json:
        error = {
            "kind": failure.kind,
            "message": failure.message,
            **public,
        }
        print(_json_dumps({"error": error}))
    else:
        retry = "yes" if public.get("retryable") else "no"
        print(
            f"{public['code']}: {public['summary']}\n"
            f"Cause: {public['cause']}\n"
            f"Component: {public['component']} (retryable: {retry})\n"
            f"Next: {public['action']}\n"
            f"Log: {public['log_path']}",
            file=sys.stderr,
        )
        if hint:
            print(hint, file=sys.stderr)
    return failure.exit_code


def _result_payload(result: HeadlessResult) -> dict[str, Any]:
    return {
        "result": result.result,
        "tokens": result.tokens,
        "cost": result.cost,
        "turns": result.turns,
        "tool_calls": result.tool_calls,
        "verification": result.verification,
        "project_trusted": result.project_trusted,
        "permission_mode": result.permission_mode,
        "thread_id": result.thread_id,
    }


def _event_to_json(event: Event) -> dict[str, Any]:
    """Serialize an :class:`Event` to a JSON-ready dict, generically.

    Emits ``{"type": <kind>}`` plus every other dataclass field by name, read via
    :func:`dataclasses.fields`. This deliberately avoids a per-kind whitelist so a
    new ``EventKind`` or a new ``Event`` attribute streams through untouched
    (nothing to keep in sync). Mirrors the one-event-per-line NDJSON of
    ``claude -p --output-format stream-json``. Non-JSON values inside ``data`` are
    replaced with a non-sensitive sentinel by the emitter.
    """
    out: dict[str, Any] = {}
    for f in fields(event):
        value = getattr(event, f.name)
        if f.name == "kind":
            out["type"] = value.value if isinstance(value, EventKind) else str(value)
        else:
            out[f.name] = value
    return out


def _emit_ndjson(obj: dict[str, Any]) -> None:
    """Write one NDJSON line to stdout and flush so the stream is live.

    A rogue non-serialisable value is replaced with a fixed sentinel so it cannot
    abort the stream or expose its potentially sensitive string representation.
    """
    sys.stdout.write(_json_dumps(obj) + "\n")
    sys.stdout.flush()


def _stream_emit(event: Event) -> None:
    """Per-event sink for ``stream-json``: one NDJSON line per Event."""
    _emit_ndjson(_event_to_json(event))


def _emit_headless_failure(
    failure: HeadlessFailure,
    *,
    output_format: str,
    hint: str | None = None,
) -> int:
    """Emit a failure in the requested output format and return its exit code.

    * ``stream-json`` — a terminal ``{"type": "run_error", "error": {...}}`` NDJSON
      line (hint, if any, goes to stderr so stdout stays pure NDJSON).
    * ``json`` / ``text`` — delegates to :func:`_emit_failure` unchanged.
    """
    if output_format == "stream-json":
        error = {
            "kind": failure.kind,
            "message": failure.message,
            **_public_failure_details(failure),
        }
        _emit_ndjson({"type": "run_error", "error": error})
        if hint:
            print(hint, file=sys.stderr)
        return failure.exit_code
    return _emit_failure(failure, as_json=output_format == "json", hint=hint)


def _make_fail_closed_approver(_mode: PermissionMode) -> Approver:
    """Return an :class:`Approver` that implements the fail-closed rule.

    For auto-approving modes (auto-edit / yolo) the engine already resolves
    most actions to ALLOW before the approver is reached; the few that still
    hit ASK (e.g. danger-guard DANGEROUS) are denied here — they require a
    human regardless. For non-auto modes any ASK raises :class:`HeadlessRefusal`
    so the run exits non-zero with a clear message rather than silently doing
    nothing.
    """

    async def _approver(req: ApprovalRequest) -> ApprovalReply:
        tool = req.action.tool or "tool"
        reason = req.result.reason or "requires confirmation"
        # An ASK that reaches the approver means no human is available.
        raise HeadlessRefusal(tool, reason)

    return _approver


def _resolve_resume_session(controller: Controller, resume_session: str) -> str:
    """Map ``last`` or an explicit thread id to a concrete thread id."""
    if resume_session == "last":
        sessions = controller.sessions.list(limit=1)
        if not sessions:
            raise HeadlessFailure(
                "usage",
                "no sessions to resume",
                exit_code=EXIT_USAGE_CONFIG,
            )
        return sessions[0].thread_id
    return resume_session


async def _run_headless(
    prompt: str,
    config: Config,
    project_root: Path | None,
    *,
    project_trusted: bool = True,
    max_turns: int = 1,
    system_prompt_override: str | None = None,
    resume_session: str | None = None,
    response_format: Any | None = None,
    add_dirs: list[Path] | None = None,
    on_event: Callable[[Event], None] | None = None,
) -> HeadlessResult:
    """Async core: build the runtime, run one complete user turn, return results.

    Headless is single-turn BY DESIGN: a SessionDriver call already contains the
    complete model/tool graph loop and runs it to DONE, so ``--max-turns`` can only
    ever be ``1``. Rather than silently accepting ``--max-turns > 1`` and still
    reporting ``turns == 1`` (which would misrepresent what ran), values other than
    1 are rejected up front with a clear message.

    ``system_prompt_override`` is forwarded to the Controller / build_runtime for
    the eval harness's harness-prompt A/B (see build_runtime).

    ``on_event`` (when set) is called with every :class:`Event` as it streams —
    the ``stream-json`` output mode uses it to emit one NDJSON line per event.
    It runs before the existing per-kind handling, so an ERROR event streams as a
    line first and then raises (which the caller turns into a terminal error
    line). Serialisation stays generic (see :func:`_event_to_json`).
    """
    if max_turns < 1:
        raise HeadlessFailure(
            "usage",
            f"--max-turns must be >= 1, got {max_turns}",
            exit_code=EXIT_USAGE_CONFIG,
        )
    if max_turns > 1:
        # Honest failure over a silent no-op: headless runs exactly one complete
        # turn (the SessionDriver already drives the full model/tool graph to
        # completion), so it cannot honour a request for more than one turn.
        raise HeadlessFailure(
            "usage",
            (
                f"--max-turns > 1 is not supported in headless mode (got {max_turns}). "
                "A headless invocation runs exactly one complete turn — the agent's "
                "model/tool graph already loops to completion within it. "
                "Re-run without --max-turns (or with --max-turns 1)."
            ),
            exit_code=EXIT_USAGE_CONFIG,
        )

    controller = Controller(
        config, project_root, project_trusted=project_trusted,
        system_prompt_override=system_prompt_override,
        response_format=response_format,
        extra_roots=add_dirs,
    )
    # Once the driver's turn has begun, EXACTLY ONE telemetry turn must be recorded
    # regardless of outcome — success, ERROR event, approval refusal, or
    # cancellation — mirroring the REPL turn path (repl/turn.py records the turn in a
    # finally after the stream ends). Pre-runtime validation failures below (before
    # this flag flips) still record nothing, exactly as before.
    driver_started = False
    try:
        ok, message = controller.validate()
        if not ok:
            failure_message = f"provider not ready: {message}"
            if _looks_like_auth_failure(message):
                raise HeadlessFailure("auth", failure_message, exit_code=EXIT_AUTH)
            if _looks_like_model_failure(message):
                raise HeadlessFailure(
                    "model", failure_message, exit_code=EXIT_MODEL_UNAVAILABLE
                )
            raise HeadlessFailure(
                "config", failure_message, exit_code=EXIT_USAGE_CONFIG
            )

        await controller.ensure_runtime()

        if resume_session:
            thread_id = _resolve_resume_session(controller, resume_session)
            controller.resume_thread(thread_id)

        # Persist the session locus before the first model step, matching the
        # interactive path. A crash/refusal/cancellation therefore leaves an
        # honest `incomplete` row that `jarn sessions list` can recover. On a
        # resume, SessionIndex preserves the original title on conflict.
        existing = controller.sessions.get(controller.thread_id)
        title = prompt or (existing.title if existing is not None else "Resumed session")
        controller.record_session_title(title, when=time.time())

        mode = config.permission_mode
        approver: Approver = _make_fail_closed_approver(mode)

        # Off the event loop: enrich_turn_input does synchronous memory-file reads
        # + vector-index builds (mirrors the REPL turn path).
        enriched = (
            await asyncio.to_thread(controller.enrich_turn_input, prompt)
            if prompt
            else ""
        )

        text_parts: list[str] = []
        tool_calls = 0
        turns_completed = 1
        verification: dict[str, Any] | None = None
        resume = bool(resume_session and not prompt)
        turn_input = "" if resume else enriched

        def _handle_event(event: Event) -> None:
            nonlocal tool_calls, verification
            if on_event is not None:
                # Stream every event generically (stream-json). Intermediate
                # provider ERRORs are absorbed by the shared runner for retry
                # policy; only a terminal ERROR is forwarded here.
                on_event(event)
            if event.kind is EventKind.TEXT:
                text_parts.append(event.text)
            elif event.kind is EventKind.TOOL_START:
                tool_calls += 1
            elif event.kind is EventKind.NOTICE:
                if event.data.get("verify"):
                    verification = dict(event.data["verify"])
                if event.data.get("verification_repair"):
                    # The prose before this marker was generated before acceptance
                    # checks failed. Return only the repaired/final answer in headless
                    # mode; the transcript still retains the full audit trail.
                    text_parts.clear()
            elif event.kind is EventKind.ERROR:
                raise _error_from_event(event.text, event.data)
            elif event.kind is EventKind.APPROVAL:
                lowered = event.text.lower()
                if lowered.startswith(("rejected", "blocked")):
                    raise HeadlessRefusal(
                        event.data.get("target", "tool"),
                        event.text,
                    )
                if "auto-denied" in lowered:
                    raise HeadlessRefusal(
                        event.data.get("target", "tool"),
                        event.text,
                    )

        # Shared turn runner: one complete user turn with the same retry /
        # fallback / T-3-7 policy as the REPL (bridge-ready; no TUI deps).
        driver_started = True
        turn_result = await run_agent_turn(
            controller,
            turn_input,
            approver=approver,
            resume=resume,
            on_event=_handle_event,
        )
        if turn_result.error is not None:
            # Terminal failure where the runner surfaced a NOTICE only (e.g.
            # fallback unavailable) — still fail the headless run.
            raise _error_from_event(
                turn_result.error.text, turn_result.error.data
            )

        reply_text = "".join(text_parts)

        # When a schema was requested, extract the structured result from the
        # agent's final graph state instead of using the free-text reply.
        if response_format is not None:
            rt = controller.runtime
            assert rt is not None, "runtime must be set after ensure_runtime()"
            state = await rt.agent.aget_state(
                {"configurable": {"thread_id": controller.thread_id}}
            )
            structured = (getattr(state, "values", {}) or {}).get("structured_response")
            if structured is None:
                raise HeadlessFailure(
                    "schema",
                    "agent did not produce a structured response; "
                    "the schema constraint was not satisfied",
                    exit_code=EXIT_VERIFICATION_FAILED,
                )
            result_value: Any = structured
        else:
            result_value = reply_text

        tracker = controller.tracker
        tokens: dict[str, Any] = {}
        for ref, usage in tracker.per_model.items():
            tokens[ref] = {
                "input": usage.input_tokens,
                "output": usage.output_tokens,
                "total": usage.total_tokens,
            }
        cost = tracker.total.cost_usd

        # Surface the session locus so a CI caller can resume/inspect this run
        # (the transcript writer, when present, is the single source of truth for
        # the JSONL path; None when observability.transcript is disabled).
        transcript = getattr(turn_result.driver, "transcript", None)
        transcript_path = str(transcript.path) if transcript is not None else None

        controller.mark_session_complete(when=time.time())

        return HeadlessResult(
            result=result_value,
            tokens=tokens,
            cost=cost,
            turns=turns_completed,
            tool_calls=tool_calls,
            verification=verification,
            project_trusted=project_trusted,
            permission_mode=config.permission_mode.value,
            thread_id=controller.thread_id,
            transcript_path=transcript_path,
        )
    finally:
        # Record per-turn telemetry once the driver's turn ran — success OR failure
        # (ERROR event / approval refusal / cancellation) — mirroring the REPL turn
        # path (repl/turn.py), which records the turn in a finally after the stream
        # ends regardless of outcome. Headless is the most common real usage
        # (unattended / CI): recording only on success (as before) undercounted every
        # failed run to zero. record_turn is a hard no-op when telemetry is
        # opt-out/off, so this respects the default-OFF policy exactly like the REPL
        # (the gate lives in Telemetry). Recorded before aclose() so the turn event is
        # in the buffer aclose() flushes; the driver_started guard keeps pre-runtime
        # validation failures recording nothing.
        if driver_started:
            controller.record_turn(when=time.time())
        await controller.aclose()


def run_headless(
    prompt: str,
    config: Config,
    project_root: Path | None,
    *,
    project_trusted: bool = True,
    as_json: bool = False,
    output_format: str | None = None,
    max_turns: int = 1,
    resume_session: str | None = None,
    response_format: Any | None = None,
    add_dirs: list[Path] | None = None,
) -> int:
    """Synchronous entry point called by the CLI.

    Runs the headless turn(s), writes output to stdout, and returns an exit code.

    ``output_format`` selects ``text`` (plain reply), ``json`` (a single buffered
    envelope), or ``stream-json`` (NDJSON: one line per event, then a terminal
    ``{"type": "result", ...}`` line — see the module docstring). ``as_json`` is
    the legacy boolean alias for ``json``; when ``output_format`` is ``None`` it
    is derived from ``as_json`` so existing callers keep working unchanged.

    Exit codes use the stable taxonomy in :mod:`jarn.exit_codes`.
    """
    fmt = output_format if output_format is not None else ("json" if as_json else "text")
    streaming = fmt == "stream-json"

    if project_trusted:
        refusal_hint = (
            "hint: pass --permission-mode auto-edit or yolo to allow unattended tool use "
            "(at your own risk)."
        )
    else:
        # Suggesting --permission-mode here would be useless advice: on an
        # untrusted project the untrusted floor clamps an explicit mode back down,
        # so the operator may well have passed it already. Name the actual remedy.
        refusal_hint = (
            "hint: this project is untrusted, so --permission-mode is clamped to plan. "
            "Run `jarn trust .` to lift the clamp, or --ignore-project-config to run "
            "without the project's config at all."
        )
    try:
        result = asyncio.run(
            _run_headless(
                prompt,
                config,
                project_root,
                project_trusted=project_trusted,
                max_turns=max_turns,
                resume_session=resume_session,
                response_format=response_format,
                add_dirs=add_dirs,
                on_event=_stream_emit if streaming else None,
            )
        )
    except (KeyboardInterrupt, asyncio.CancelledError) as exc:
        failure = _classify_exception(exc)
        failure.details.setdefault("project_trusted", project_trusted)
        failure.details.setdefault("permission_mode", config.permission_mode.value)
        return _emit_headless_failure(failure, output_format=fmt)
    except Exception as exc:  # noqa: BLE001
        failure = _classify_exception(exc)
        # The trust locus travels with failures too, not just successes. The
        # untrusted floor silently clamps an explicit --permission-mode down to
        # plan, and that clamp is a leading cause of the refusal below — without
        # these fields automation cannot tell "I was downgraded" from any other
        # refusal, which is the exact distinction they were added to expose.
        failure.details.setdefault("project_trusted", project_trusted)
        failure.details.setdefault("permission_mode", config.permission_mode.value)
        hint = refusal_hint if failure.kind == "refusal" else None
        return _emit_headless_failure(failure, output_format=fmt, hint=hint)

    if streaming:
        # Terminal result line: the envelope + the session locus (thread_id /
        # transcript_path) so a CI caller can resume/inspect the run it just did.
        terminal: dict[str, Any] = {"type": "result", **_result_payload(result)}
        terminal["thread_id"] = result.thread_id
        if result.transcript_path is not None:
            terminal["transcript_path"] = result.transcript_path
        _emit_ndjson(terminal)
    elif fmt == "json":
        print(_json_dumps(_result_payload(result)))
    else:
        if isinstance(result.result, str):
            print(result.result, end="" if result.result.endswith("\n") else "\n")
        else:
            print(_json_dumps(result.result))

    return EXIT_SUCCESS
