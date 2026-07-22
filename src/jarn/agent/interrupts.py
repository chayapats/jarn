"""Interrupt resolution helpers for the session driver."""

from __future__ import annotations

import asyncio
import logging
import shlex
from typing import TYPE_CHECKING, Any

from langgraph.types import Command

from jarn.agent.events import (
    ApprovalRequest,
    Event,
    EventKind,
    SuggestedMemory,
)
from jarn.agent.permissions_bridge import tool_to_action
from jarn.permissions import (
    Action,
    ActionKind,
    Decision,
    PermissionResult,
    RememberScope,
)

if TYPE_CHECKING:
    from jarn.agent.session import SessionDriver

_log = logging.getLogger("jarn")


async def resolve_interrupts(driver: SessionDriver, interrupts: list[Any]):
    """For each gated tool call, yield (event, decision-dict)."""
    for intr in interrupts:
        for req in _action_requests(intr):
            name = req.get("action") or req.get("name") or "tool"
            args = req.get("args", {}) or {}

            # Plan-mode handoff: exit_plan_mode is callable *in* plan mode, so
            # it bypasses the engine (which would deny it like any non-read
            # action). The approver shows the plan and escalates the mode on
            # approval; an approve resumes the tool (its return is the model's
            # "go" signal), a reject keeps the agent planning.
            if name == "exit_plan_mode":
                plan_text = str(args.get("plan", "")).strip()
                reply = await driver.approver(
                    ApprovalRequest(
                        action=Action(ActionKind.READ, target="plan", tool=name),
                        result=PermissionResult(Decision.ASK, "plan ready for review"),
                        description=req.get("description", ""),
                        args=args,
                        plan=plan_text,
                    )
                )
                if reply.approved:
                    yield (
                        Event(EventKind.APPROVAL, text="plan approved — executing",
                              data={"target": "plan"}),
                        {"type": "approve"},
                    )
                else:
                    yield (
                        Event(EventKind.APPROVAL, text="plan not approved — still planning",
                              data={"target": "plan"}),
                        {"type": "reject",
                         "message": reply.message
                         or "Keep refining the plan; call exit_plan_mode again when ready."},
                    )
                continue

            # Memory suggestion: suggest_memory proposes a memory for the user
            # to approve. It never mutates the world on its own (the approver
            # does the write on approval), so it bypasses the engine and routes
            # straight to the approver — an approve resumes the tool (its return
            # is the model's confirmation), a reject keeps the agent going
            # without writing anything.
            if name == "suggest_memory":
                suggestion = SuggestedMemory(
                    name=str(args.get("name", "")).strip(),
                    description=str(args.get("description", "")).strip(),
                    body=str(args.get("body", "")).strip(),
                    type=str(args.get("type", "project")).strip() or "project",
                    scope=str(args.get("scope", "project")).strip() or "project",
                )
                reply = await driver.approver(
                    ApprovalRequest(
                        action=Action(ActionKind.READ, target="memory", tool=name),
                        result=PermissionResult(Decision.ASK, "memory suggested"),
                        description=req.get("description", ""),
                        args=args,
                        suggested_memory=suggestion,
                    )
                )
                if reply.approved:
                    yield (
                        Event(EventKind.APPROVAL, text="memory saved",
                              data={"target": "memory"}),
                        {"type": "approve"},
                    )
                else:
                    yield (
                        Event(EventKind.APPROVAL, text="memory not saved",
                              data={"target": "memory"}),
                        {"type": "reject",
                         "message": reply.message or "User declined to save the memory."},
                    )
                continue

            action = tool_to_action(name, args)

            # Pre-tool / pre-commit hooks run before the tool: a blocking
            # hook that fails rejects the call (e.g. tests fail → no commit).
            if driver.hooks is not None:
                abort, hook_notices = await run_pre_hooks(driver, name, action)
                for note in hook_notices:
                    # `resolve_interrupts` yields ``(event, decision)`` tuples;
                    # a NOTICE carries no decision, so pair it with ``None``.
                    yield (Event(EventKind.NOTICE, text=note), None)
                if abort is not None:
                    yield (
                        Event(EventKind.APPROVAL, text=f"blocked by hook: {name} ({abort})",
                              data={"target": action.target}),
                        {"type": "reject", "message": abort},
                    )
                    continue

            result = driver.engine.evaluate(action)

            if result.decision is Decision.ALLOW:
                yield (
                    Event(EventKind.APPROVAL, text=f"auto-allowed: {name}",
                          data={"target": action.target}),
                    {"type": "approve"},
                )
            elif result.decision is Decision.DENY:
                yield (
                    Event(EventKind.APPROVAL, text=f"blocked: {name} ({result.reason})",
                          data={"target": action.target, "dangerous": result.dangerous}),
                    {"type": "reject", "message": result.reason},
                )
            else:  # ASK
                reply = await driver.approver(
                    ApprovalRequest(action=action, result=result,
                                    description=req.get("description", ""),
                                    args=args)
                )
                if reply.approved:
                    # A guard-dangerous action can never be remembered as
                    # ALWAYS — downgrade to SESSION so it isn't persisted.
                    scope = reply.scope
                    if result.block_remember_always and scope is RememberScope.ALWAYS:
                        scope = RememberScope.SESSION
                    driver.engine.remember(action, scope)
                    if reply.edited_args is not None:
                        # Edit-before-apply: resume with a LangGraph ``edit``
                        # decision so the tool runs with the user-edited args —
                        # the edited content is what lands on disk.
                        yield (
                            Event(EventKind.APPROVAL, text=f"approved (edited): {name}",
                                  data={"target": action.target, "scope": scope.value}),
                            {"type": "edit",
                             "edited_action": {"name": name, "args": reply.edited_args}},
                        )
                    else:
                        yield (
                            Event(EventKind.APPROVAL, text=f"approved: {name}",
                                  data={"target": action.target, "scope": scope.value}),
                            {"type": "approve"},
                        )
                else:
                    driver.engine.deny_session(action)
                    yield (
                        Event(EventKind.APPROVAL, text=f"rejected: {name}",
                              data={"target": action.target}),
                        {"type": "reject", "message": reply.message or "rejected by user"},
                    )


async def run_pre_hooks(
    driver: SessionDriver, name: str, action: Action
) -> tuple[str | None, list[str]]:
    """Run ``pre_tool`` (and ``pre_commit`` for git commits) before a gated
    tool. Returns ``(abort_reason, notices)``: ``abort_reason`` is set only
    if a *blocking* hook failed (the tool call is rejected); ``notices``
    carries non-fatal warnings for non-blocking failures so they're surfaced
    to the user instead of being swallowed.

    NOTE: gated tools reach this — mutating + network/MCP, plus reads (so a
    read of a secret store can be gated against ``sensitive_read_globs``). An
    ordinary read still resolves to ALLOW and auto-resumes silently, but it now
    routes through here, so ``pre_tool`` fires for reads too. Hooks run off the
    event loop (``to_thread``) so a slow hook doesn't freeze the UI.
    """
    from jarn.extensibility.hooks import HookEvent

    notices: list[str] = []
    events = [HookEvent.PRE_TOOL]
    if name == "execute" and _looks_like_git_commit(action.target):
        events.append(HookEvent.PRE_COMMIT)
    for event in events:
        results = await asyncio.to_thread(driver.hooks.run, event, target=action.target)
        for result in results:
            if result.should_abort:
                _log.warning(
                    "hooks: blocking %s hook failed (exit %s): %s",
                    event.value,
                    result.exit_code,
                    _hook_detail(result),
                )
                return f"{event.value}: {_hook_detail(result)}", notices
            if not result.ok:
                # Non-blocking failure: don't abort, but don't stay silent.
                detail = _hook_detail(result)
                _log.warning(
                    "hooks: %s hook failed (exit %s): %s",
                    event.value,
                    result.exit_code,
                    detail,
                )
                notices.append(f"{event.value} hook failed: {detail}")
    return None, notices


def _resume_payload(resolved: list[tuple[Any, list[Any]]]) -> Command:
    """Build the LangGraph resume ``Command`` for the pending interrupt(s).

    ``resolved`` is ``[(interrupt_id, decisions), ...]`` — one entry per unique
    pending interrupt, in resolution order.

    A SINGLE pending interrupt resumes with the bundled ``{"decisions": [...]}``
    value its HITL middleware expects. With MULTIPLE pending interrupts (each
    delegated subagent raises its own), LangGraph requires the resume to be a map
    keyed by interrupt id — otherwise it raises *"When there are multiple pending
    interrupts, you must specify the interrupt id when resuming."*
    (``langgraph/pregel/_loop.py``). The id is an xxh3-128 hexdigest, which is how
    LangGraph recognises the value as a per-interrupt resume map.
    """
    keyed = [(iid, decisions) for iid, decisions in resolved if iid is not None]
    if len(resolved) > 1 and len(keyed) == len(resolved):
        return Command(resume={iid: {"decisions": decisions} for iid, decisions in keyed})
    # Single interrupt (or, defensively, ids unavailable): the legacy bundled
    # resume — flatten every decision into one list.
    decisions = [d for _, ds in resolved for d in ds]
    return Command(resume={"decisions": decisions})


def _action_requests(interrupt: Any) -> list[dict[str, Any]]:
    """Extract action-request dicts from a LangGraph interrupt value."""
    value = getattr(interrupt, "value", interrupt)
    if isinstance(value, dict) and "action_requests" in value:
        return list(value["action_requests"])
    if isinstance(value, list):
        items: list[dict[str, Any]] = []
        for v in value:
            if isinstance(v, dict) and "action_requests" in v:
                items.extend(v["action_requests"])
        return items
    return []


def _looks_like_git_commit(command: str) -> bool:
    """True if ``command`` actually invokes ``git commit`` (not a quoted string
    or another git subcommand). Tolerates global flags like ``-C dir`` / ``-c k=v``."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    i = 0
    while i < len(tokens):
        if tokens[i] == "git" or tokens[i].endswith("/git"):
            j = i + 1
            while j < len(tokens):
                tok = tokens[j]
                if tok in ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"):
                    j += 2  # flag that takes a value
                    continue
                if tok.startswith("-"):
                    j += 1
                    continue
                return tok == "commit"
            return False
        i += 1
    return False


def _hook_detail(result: Any) -> str:
    """Last meaningful line of a hook's output, for a compact abort/notice msg."""
    text = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()
    if text:
        return text.splitlines()[-1]
    return f"exit {getattr(result, 'exit_code', '?')}"


async def run_post_hooks(driver: SessionDriver, name: str):
    """Run ``post_tool`` (and ``post_edit`` for writes/edits) after a tool
    completes. Report-only — the tool already ran — so a failing hook is
    surfaced as a NOTICE rather than aborting the turn. ``post_edit`` is
    scoped by the edited *file path* (so a ``matcher: "*.py"`` glob works),
    ``post_tool`` by the tool name."""
    from jarn.extensibility.hooks import HookEvent

    targets = [(HookEvent.POST_TOOL, name)]
    if name in ("write_file", "edit_file"):
        targets.append((HookEvent.POST_EDIT, driver._last_edit_target or name))
    for event, target in targets:
        results = await asyncio.to_thread(driver.hooks.run, event, target=target)
        for result in results:
            if not result.ok:
                yield Event(
                    EventKind.NOTICE,
                    text=f"{event.value} hook failed: {_hook_detail(result)}",
                )
