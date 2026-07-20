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
  envelope). ``--json`` is a legacy alias for this.
* ``stream-json`` — newline-delimited JSON (NDJSON): one object per Event as the
  turn runs, then a terminal ``{"type": "result", ...}`` object carrying the
  same envelope plus the session ``thread_id`` (and ``transcript_path`` when a
  transcript is being written) so a CI caller can locate/resume the session it
  just ran. This mirrors the spirit of ``claude -p --output-format stream-json``
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
from jarn.config.schema import Config, PermissionMode
from jarn.cost import BudgetExceeded
from jarn.tui.controller import Controller

# Auto-approving modes: the user explicitly opted in, so headless may proceed.
_AUTO_MODES = frozenset({PermissionMode.AUTO_EDIT, PermissionMode.YOLO})

# Exit codes for ``jarn -p`` (documented in CLI --help and docs/CONFIGURATION.md).
EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2
EXIT_TIMEOUT = 124

_TIMEOUT_MSG_HINTS = (
    "timed out",
    "timeout",
    "time out",
)


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


def _classify_exception(exc: BaseException) -> HeadlessFailure:
    if isinstance(exc, HeadlessRefusal):
        return HeadlessFailure(
            exc.kind,
            str(exc),
            exit_code=exc.exit_code,
        )
    if isinstance(exc, BudgetExceeded):
        return HeadlessFailure("budget", str(exc), exit_code=EXIT_REFUSED)
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return HeadlessFailure("timeout", str(exc), exit_code=EXIT_TIMEOUT)
    if isinstance(exc, HeadlessFailure):
        return exc
    message = str(exc)
    if _is_timeout_message(message):
        return HeadlessFailure("timeout", message, exit_code=EXIT_TIMEOUT)
    return HeadlessFailure("error", message, exit_code=EXIT_ERROR)


def _error_from_event(text: str, data: dict[str, Any] | None) -> HeadlessFailure:
    payload = data or {}
    if payload.get("verification"):
        return HeadlessFailure(
            "verification",
            text,
            exit_code=EXIT_ERROR,
            details={"verification": payload["verification"]},
        )
    if payload.get("budget"):
        return HeadlessFailure("budget", text, exit_code=EXIT_REFUSED)
    if _is_timeout_message(text):
        return HeadlessFailure("timeout", text, exit_code=EXIT_TIMEOUT)
    return HeadlessFailure("error", text, exit_code=EXIT_ERROR)


def _emit_failure(
    failure: HeadlessFailure,
    *,
    as_json: bool,
    hint: str | None = None,
) -> int:
    if as_json:
        error = {"kind": failure.kind, "message": failure.message, **failure.details}
        print(json.dumps({"error": error}))
    else:
        print(f"error: {failure.message}", file=sys.stderr)
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
    }


def _event_to_json(event: Event) -> dict[str, Any]:
    """Serialize an :class:`Event` to a JSON-ready dict, generically.

    Emits ``{"type": <kind>}`` plus every other dataclass field by name, read via
    :func:`dataclasses.fields`. This deliberately avoids a per-kind whitelist so a
    new ``EventKind`` or a new ``Event`` attribute streams through untouched
    (nothing to keep in sync). Mirrors the one-event-per-line NDJSON of
    ``claude -p --output-format stream-json``. Non-JSON values inside ``data`` are
    rendered by the emitter's ``default=str`` fallback.
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

    ``default=str`` keeps a rogue non-serialisable value in ``Event.data`` from
    aborting the whole stream — it is rendered as its ``str()`` instead.
    """
    sys.stdout.write(json.dumps(obj, default=str) + "\n")
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

    * ``stream-json`` — a terminal ``{"type": "error", "error": {...}}`` NDJSON
      line (hint, if any, goes to stderr so stdout stays pure NDJSON).
    * ``json`` / ``text`` — delegates to :func:`_emit_failure` unchanged.
    """
    if output_format == "stream-json":
        error = {"kind": failure.kind, "message": failure.message, **failure.details}
        _emit_ndjson({"type": "error", "error": error})
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
                "error",
                "no sessions to resume",
                exit_code=EXIT_ERROR,
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
            "error",
            f"--max-turns must be >= 1, got {max_turns}",
            exit_code=EXIT_ERROR,
        )
    if max_turns > 1:
        # Honest failure over a silent no-op: headless runs exactly one complete
        # turn (the SessionDriver already drives the full model/tool graph to
        # completion), so it cannot honour a request for more than one turn.
        raise HeadlessFailure(
            "error",
            (
                f"--max-turns > 1 is not supported in headless mode (got {max_turns}). "
                "A headless invocation runs exactly one complete turn — the agent's "
                "model/tool graph already loops to completion within it. "
                "Re-run without --max-turns (or with --max-turns 1)."
            ),
            exit_code=EXIT_ERROR,
        )

    controller = Controller(
        config, project_root, project_trusted=project_trusted,
        system_prompt_override=system_prompt_override,
        response_format=response_format,
        extra_roots=add_dirs,
    )
    try:
        ok, message = controller.validate()
        if not ok:
            raise HeadlessFailure("error", f"provider not ready: {message}")

        await controller.ensure_runtime()

        if resume_session:
            thread_id = _resolve_resume_session(controller, resume_session)
            controller.resume_thread(thread_id)

        mode = config.permission_mode
        approver: Approver = _make_fail_closed_approver(mode)
        driver = controller.make_driver(approver)

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

        # One SessionDriver invocation already runs the complete LangGraph agent/tool
        # loop through DONE (including bounded verification repairs). Re-invoking a
        # completed graph because it happened to use a tool duplicated final answers
        # and cost, so a headless prompt is one complete user turn.
        async for event in driver.run_turn(turn_input, resume=resume):
            if on_event is not None:
                # Stream every event generically (stream-json). Runs before the
                # per-kind handling so an ERROR event is emitted before we raise.
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

        reply_text = "".join(text_parts)

        # Record per-turn telemetry now that the driver's complete model/tool graph
        # has run to DONE — mirroring the REPL turn path (repl/turn.py). Headless is
        # the most common real usage (unattended / CI), so without this the buffer
        # flushed at aclose() is always empty and telemetry never fires. record_turn
        # is a hard no-op when telemetry is opt-out/off, so this respects the
        # default-OFF policy exactly like the REPL (the gate lives in Telemetry).
        controller.record_turn(when=time.time())

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
                    exit_code=EXIT_ERROR,
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
        transcript = getattr(driver, "transcript", None)
        transcript_path = str(transcript.path) if transcript is not None else None

        return HeadlessResult(
            result=result_value,
            tokens=tokens,
            cost=cost,
            turns=turns_completed,
            tool_calls=tool_calls,
            verification=verification,
            thread_id=controller.thread_id,
            transcript_path=transcript_path,
        )
    finally:
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

    Exit codes:
        0 — success
        1 — generic error
        2 — approval refused or session budget hard-stop
        124 — timeout
    """
    fmt = output_format if output_format is not None else ("json" if as_json else "text")
    streaming = fmt == "stream-json"

    refusal_hint = (
        "hint: pass --permission-mode auto-edit or yolo to allow unattended tool use "
        "(at your own risk)."
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
    except Exception as exc:  # noqa: BLE001
        failure = _classify_exception(exc)
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
        print(json.dumps(_result_payload(result)))
    else:
        if isinstance(result.result, str):
            print(result.result, end="" if result.result.endswith("\n") else "\n")
        else:
            print(json.dumps(result.result))

    return EXIT_SUCCESS
