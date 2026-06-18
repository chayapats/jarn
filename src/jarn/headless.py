"""Headless one-shot runner for non-interactive / CI use.

``jarn -p "do X"`` drives ONE user turn through the same controller + session
path the REPL uses, prints the assistant's final text to stdout, and exits.

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
from jarn.tui.controller import Controller

# Auto-approving modes: the user explicitly opted in, so headless may proceed.
_AUTO_MODES = frozenset({PermissionMode.AUTO_EDIT, PermissionMode.YOLO})


@dataclass(slots=True)
class HeadlessResult:
    """The outcome of a single headless turn."""

    result: str
    """The assistant's final text reply."""
    tokens: dict[str, Any] = field(default_factory=dict)
    """Per-model token counts (input/output/total), keyed by model ref."""
    cost: float = 0.0
    """Total session cost in USD."""
    turns: int = 1
    tool_calls: int = 0
    """How many tool invocations the agent made during the turn (diagnostic)."""


class HeadlessRefusal(Exception):
    """Raised when fail-closed safety blocks a gated tool in a non-auto mode.

    Carries the tool name and reason so the caller can emit a clear message.
    """

    def __init__(self, tool: str, reason: str) -> None:
        super().__init__(f"headless: gated tool refused — {tool!r}: {reason}")
        self.tool = tool
        self.reason = reason


def _make_fail_closed_approver(mode: PermissionMode) -> Approver:
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
        # Either mode: an ASK that reaches the approver means no human is
        # available. Always deny, but the message differs for UX.
        if mode in _AUTO_MODES:
            # Even in auto mode, danger-guard items require a human.
            return ApprovalReply(
                approved=False,
                message=f"headless auto-denied ({reason})",
            )
        # Non-auto modes: surface a clear error instead of silently denying.
        raise HeadlessRefusal(tool, reason)

    return _approver


async def _run_headless(
    prompt: str,
    config: Config,
    project_root: Path | None,
    *,
    project_trusted: bool = True,
    max_turns: int = 1,
    system_prompt_override: str | None = None,
) -> HeadlessResult:
    """Async core: build the runtime, run one turn, return results.

    ``max_turns`` is reserved for future multi-turn headless workflows; the
    current implementation always completes in one model turn (the agent
    itself may still use multiple tool calls internally — that is one "turn"
    from the user's perspective).

    ``system_prompt_override`` is forwarded to the Controller / build_runtime for
    the eval harness's harness-prompt A/B (see build_runtime).
    """
    controller = Controller(
        config, project_root, project_trusted=project_trusted,
        system_prompt_override=system_prompt_override,
    )
    try:
        ok, message = controller.validate()
        if not ok:
            raise RuntimeError(f"provider not ready: {message}")

        await controller.ensure_runtime()

        mode = config.permission_mode
        approver: Approver = _make_fail_closed_approver(mode)
        driver = controller.make_driver(approver)

        enriched = controller.enrich_turn_input(prompt)

        text_parts: list[str] = []
        tool_calls = 0
        async for event in driver.run_turn(enriched):
            if event.kind is EventKind.TEXT:
                text_parts.append(event.text)
            elif event.kind is EventKind.TOOL_START:
                tool_calls += 1
            elif event.kind is EventKind.ERROR:
                raise RuntimeError(event.text)

        reply_text = "".join(text_parts)

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
            result=reply_text, tokens=tokens, cost=cost, turns=1,
            tool_calls=tool_calls,
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
) -> int:
    """Synchronous entry point called by the CLI.

    Runs the headless turn, writes output to stdout, and returns an exit code.
    Returns 0 on success, 1 on any error or fail-closed refusal.
    """
    try:
        result = asyncio.run(
            _run_headless(
                prompt,
                config,
                project_root,
                project_trusted=project_trusted,
                max_turns=max_turns,
            )
        )
    except HeadlessRefusal as exc:
        print(
            f"error: {exc}",
            file=sys.stderr,
        )
        print(
            "hint: pass --permission-mode auto-edit or yolo to allow unattended tool use "
            "(at your own risk).",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if as_json:
        out: dict[str, Any] = {
            "result": result.result,
            "tokens": result.tokens,
            "cost": result.cost,
            "turns": result.turns,
        }
        print(json.dumps(out))
    else:
        print(result.result, end="" if result.result.endswith("\n") else "\n")

    return 0
