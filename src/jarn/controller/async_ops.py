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
from jarn.tui.i18n import resolve_locale, t

if TYPE_CHECKING:
    from jarn.agent.checkpoint import RestorePreview
    from jarn.controller.core import Controller

#: Async confirm callback for yolo escalation (Telegram card, REPL `_ask`, …).
YoloConfirm = Callable[[], Awaitable[bool]]
#: Receives the exact read-only undo preview and returns True only after the user
#: explicitly approves that restore.
UndoConfirm = Callable[["RestorePreview"], Awaitable[bool]]


def bind_turn_task(ctrl: Controller, task: asyncio.Task[Any] | None) -> None:
    """Register (or clear) the front end's in-flight turn task."""
    ctrl._turn_task = task


def turn_running(ctrl: Controller) -> bool:
    """True while a front-end-bound turn task is still in flight."""
    task = ctrl._turn_task
    return task is not None and not task.done()


async def undo(
    ctrl: Controller,
    *,
    confirm: UndoConfirm | None = None,
) -> CommandResult:
    """Preview, confirm, then revert the last turn's file edits.

    A missing or declined confirmation is a safe no-op. The confirmed restore is
    pinned to the previewed checkpoint and current working-tree fingerprint, so
    it also fails closed if either changes while the prompt is open.
    """
    await ctrl.settle_snapshot()
    from jarn.controller.commands import session as session_cmds

    if not ctrl.checkpoint_manager.enabled:
        return await asyncio.to_thread(session_cmds.cmd_undo, ctrl, "")
    preview = await asyncio.to_thread(
        ctrl.checkpoint_manager.preview_undo,
        ctrl.thread_id,
    )
    if not preview.ok:
        return CommandResult(f"Cannot undo: {preview.message}")
    if confirm is None:
        return CommandResult(
            session_cmds.format_undo_preview(preview)
            + "\nConfirmation required before restore; no files were changed."
        )
    if not await confirm(preview):
        return CommandResult("Undo cancelled — no files were changed.")
    return await asyncio.to_thread(session_cmds.cmd_undo_confirmed, ctrl, preview)


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
                "Nothing to abort — no turn is running. Use /undo to revert the last turn's edits."
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
    loc = resolve_locale(ctrl.config)
    try:
        target = PermissionMode(value)
    except ValueError:
        valid = ", ".join(m.value for m in PermissionMode)
        return CommandResult(t("mode.unknown", loc, valid=valid))

    # Real escalate = would land on yolo. Untrusted clamps >plan → plan, so
    # a "yolo" request there is not a silent escalate and needs no confirm.
    escalating_to_yolo = (
        target == PermissionMode.YOLO
        and ctrl.config.permission_mode != PermissionMode.YOLO
        and ctrl.project_trusted
    )
    if escalating_to_yolo:
        if confirm is None:
            return CommandResult(t("mode.yolo_async_refused", loc))
        if not await confirm():
            return CommandResult(t("mode.yolo_cancelled", loc))

    applied = ctrl.apply_mode(target.value)
    if applied != target.value:
        return CommandResult(
            t("mode.untrusted", loc, mode=applied),
            rebuilt=True,
        )
    return CommandResult(t("mode.set_id", loc, mode=applied), rebuilt=True)
