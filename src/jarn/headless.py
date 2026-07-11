"""Headless one-shot runner for non-interactive / CI use.

``jarn -p "do X"`` drives one or more agent turns through the same controller +
session path the REPL uses, prints the assistant's final text to stdout, and exits.

Fail-closed safety: in headless mode there is no human to approve a gated tool.
If the effective permission mode is ``ask`` or ``plan`` and an approval is
required, the run refuses the action and exits non-zero rather than silently
auto-approving. Callers that want unattended execution must opt in explicitly
via ``--permission-mode auto-edit`` or ``yolo``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jarn.agent.session import (
    ApprovalReply,
    ApprovalRequest,
    Approver,
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
) -> HeadlessResult:
    """Async core: build the runtime, run one complete user turn, return results.

    ``max_turns`` remains accepted for CLI compatibility. A SessionDriver call
    already contains the complete model/tool graph loop, so completed graphs are
    never reinvoked based on whether they happened to use a tool.

    ``system_prompt_override`` is forwarded to the Controller / build_runtime for
    the eval harness's harness-prompt A/B (see build_runtime).
    """
    if max_turns < 1:
        raise HeadlessFailure(
            "error",
            f"--max-turns must be >= 1, got {max_turns}",
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

        enriched = controller.enrich_turn_input(prompt) if prompt else ""

        text_parts: list[str] = []
        tool_calls = 0
        turns_completed = 1
        verification: dict[str, Any] | None = None
        resume = bool(resume_session and not prompt)
        turn_input = "" if resume else enriched

        # One SessionDriver invocation already runs the complete LangGraph agent/tool
        # loop through DONE (including bounded verification repairs). Re-invoking a
        # completed graph because it happened to use a tool duplicated final answers
        # and cost. ``max_turns`` remains accepted for CLI compatibility, but a
        # headless prompt is one complete user turn.
        async for event in driver.run_turn(turn_input, resume=resume):
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

        return HeadlessResult(
            result=result_value,
            tokens=tokens,
            cost=cost,
            turns=turns_completed,
            tool_calls=tool_calls,
            verification=verification,
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
    max_turns: int = 1,
    resume_session: str | None = None,
    response_format: Any | None = None,
    add_dirs: list[Path] | None = None,
) -> int:
    """Synchronous entry point called by the CLI.

    Runs the headless turn(s), writes output to stdout, and returns an exit code.

    Exit codes:
        0 — success
        1 — generic error
        2 — approval refused or session budget hard-stop
        124 — timeout
    """
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
            )
        )
    except Exception as exc:  # noqa: BLE001
        failure = _classify_exception(exc)
        hint = refusal_hint if failure.kind == "refusal" else None
        return _emit_failure(failure, as_json=as_json, hint=hint)

    if as_json:
        print(json.dumps(_result_payload(result)))
    else:
        if isinstance(result.result, str):
            print(result.result, end="" if result.result.endswith("\n") else "\n")
        else:
            print(json.dumps(result.result))

    return EXIT_SUCCESS
