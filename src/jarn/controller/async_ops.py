"""Async controller mutation APIs (T-CTRL-1 / #59).

Embeds settle/compose invariants that used to live only in the REPL wrappers
so every front end (REPL, Telegram, …) gets them by construction.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from jarn.config.schema import PermissionMode
from jarn.controller.core import CommandResult

if TYPE_CHECKING:
    from jarn.controller.core import Controller

#: Async confirm callback for yolo escalation (Telegram card, REPL `_ask`, …).
YoloConfirm = Callable[[], Awaitable[bool]]


def bind_turn_task(ctrl: Controller, task: asyncio.Task[Any] | None) -> None:
    """Register (or clear) the front end's in-flight turn task."""
    ctrl._turn_task = task


def turn_running(ctrl: Controller) -> bool:
    """True while a front-end-bound turn task is still in flight."""
    task = ctrl._turn_task
    return task is not None and not task.done()


async def undo(ctrl: Controller) -> CommandResult:
    """Settle any in-flight snapshot, then revert the last turn's file edits."""
    await ctrl.settle_snapshot()
    from jarn.controller.commands import session as session_cmds

    return await asyncio.to_thread(session_cmds.cmd_undo, ctrl, "")


async def redo(ctrl: Controller) -> CommandResult:
    """Settle any in-flight snapshot, then re-apply the most recent undo."""
    await ctrl.settle_snapshot()
    from jarn.controller.commands import session as session_cmds

    return await asyncio.to_thread(session_cmds.cmd_redo, ctrl, "")


async def abort(ctrl: Controller) -> CommandResult:
    """Cancel the in-flight turn, settle its snapshot, then roll back edits.

    Serializes cancel against the bound turn task: cancel → await turn
    completion → settle → rollback. Idle (no running turn) is a no-op that
    does **not** undo the previous turn. Concurrent abort callers share one
    lock so a fire-and-forget ``create_task`` from the REPL key handler
    cannot interleave settle/rollback with another cancel.

    When ``/abort`` itself is the bound turn task (idle submit path), treat
    as idle — cancelling the current task would deadlock on the await.
    """
    async with ctrl._abort_lock:
        task = ctrl._turn_task
        current = asyncio.current_task()
        if task is None or task.done() or task is current:
            return CommandResult(
                "Nothing to abort — no turn is running. "
                "Use /undo to revert the last turn's edits."
            )
        # Drop any unapplied steer so it cannot resurface as the next turn.
        ctrl._steer_slot = None
        task.cancel()
        killed = ctrl.terminate_shells()
        # Await the cancelled turn so its finally (detach snapshot, cleanup)
        # finishes BEFORE settle/rollback. Always await (even if already
        # done-after-cancel) so a just-cancelled task's cleanup is observed.
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — turn may have failed; still roll back
            pass
        await ctrl.settle_snapshot()
        msg = await asyncio.to_thread(ctrl.abort_rollback)
        if killed:
            msg = f"stopped {killed} running command(s)\n{msg}"
        return CommandResult(msg)


async def set_permission_mode(
    ctrl: Controller,
    value: str,
    *,
    confirm: YoloConfirm | None = None,
) -> CommandResult:
    """Set the permission mode with a controller-owned yolo escalate gate.

    Escalating *to* yolo on a trusted project (not already in yolo) requires a
    ``confirm`` callback that returns True. Without a successful confirm,
    the mode is unchanged — silent remote escalate is impossible. Untrusted
    projects still clamp via :meth:`Controller.apply_mode` (yolo → plan)
    without requiring confirm. Already-in-yolo is a no-op transition.
    """
    try:
        target = PermissionMode(value)
    except ValueError:
        valid = ", ".join(m.value for m in PermissionMode)
        return CommandResult(f"Unknown mode. Choose one of: {valid}")

    # Real escalate = would land on yolo. Untrusted clamps >plan → plan, so
    # a "yolo" request there is not a silent escalate and needs no confirm.
    escalating_to_yolo = (
        target == PermissionMode.YOLO
        and ctrl.config.permission_mode != PermissionMode.YOLO
        and ctrl.project_trusted
    )
    if escalating_to_yolo:
        if confirm is None:
            return CommandResult(
                "Escalating to yolo requires confirmation — "
                "pass confirm=… to set_permission_mode "
                "(sync handle_command('mode','yolo') refuses this path)."
            )
        if not await confirm():
            return CommandResult("yolo cancelled — mode unchanged.")

    applied = ctrl.apply_mode(target.value)
    if applied != target.value:
        return CommandResult(
            f"Project untrusted — mode clamped to {applied}. "
            "Run `jarn trust` to unlock other modes. (rebuilding)",
            rebuilt=True,
        )
    return CommandResult(
        f"Permission mode set to {applied} (rebuilding).", rebuilt=True
    )
