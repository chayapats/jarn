"""Turn execution, approvals, and editor helpers."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, cast

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape as _rich_escape

from jarn.agent.session import ApprovalReply, ApprovalRequest, Event, EventKind
from jarn.config.schema import PermissionMode
from jarn.permissions import ActionKind, RememberScope
from jarn.repl.auth_errors import _friendly_auth_error, _provider_hint
from jarn.repl_renderer import TurnRenderer
from jarn.tui import palette
from jarn.tui.controller import Controller
from jarn.tui.notify import notify

if TYPE_CHECKING:
    from rich.text import Text

Ask = Callable[[str], Awaitable[str]]
Pick = Callable[[list[tuple[str, object]]], Awaitable[object]]

#: Sentinel an approval menu carries for the "view full diff" action: choosing
#: it opens the complete diff in the pager and re-shows the same prompt — it is
#: NOT an :class:`ApprovalReply`, so it can never approve or deny.
_VIEW_FULL_DIFF = object()

#: Sentinel for the "edit before apply" action: choosing it opens the proposed
#: new content in ``$EDITOR`` and applies the user-edited result. Like
#: :data:`_VIEW_FULL_DIFF` it is not an :class:`ApprovalReply` — the editor flow
#: produces the actual reply (an approve carrying ``edited_args``, or a deny when
#: the editor is aborted).
_EDIT_BEFORE_APPLY = object()

#: Sentinel for the "edit then save" action on a suggested-memory prompt: choosing
#: it opens the memory body in ``$EDITOR`` and saves the edited result. Like the
#: others it is not an :class:`ApprovalReply` — the editor flow produces the reply.
_EDIT_MEMORY = object()


async def _run_turn(
    console: Console,
    controller: Controller,
    text: str,
    ask: Ask,
    *,
    pick: Pick | None = None,
    view: Callable[[str], Awaitable[None]] | None = None,
    edit: Callable[[ApprovalRequest], Awaitable[ApprovalReply | None]] | None = None,
    live_sink: Callable[[str], None] | None = None,
    spinner: bool = True,
    tool_sink: list[tuple[str, str]] | None = None,
    token_sink: Callable[[str], None] | None = None,
    title_hook: Callable[[str], None] | None = None,
) -> list[tuple[str, str]]:
    """Stream a turn; return the turn's expandable ``(tool, full output)`` pairs.

    If ``tool_sink`` is given, tool outputs are appended to it live (so a pager
    can read them mid-turn)."""
    try:
        await controller.ensure_runtime()
    except Exception as exc:  # noqa: BLE001
        console.print(
            f"[{palette.C_ERROR}]agent not ready: {_rich_escape(str(exc))}[/{palette.C_ERROR}]  "
            f"[{palette.C_DIM}]· /help or jarn setup[/{palette.C_DIM}]"
        )
        return []

    # Surface a degraded/error runtime state once per session (MCP server down,
    # sandbox fell back to host, or an ambient key would leak to a non-local
    # async-subagent url). Without this it lands only in the rotating log file.
    if (
        not controller.health_notice_shown
        and controller.last_error
        and controller.health in ("degraded", "error")
    ):
        controller.health_notice_shown = True
        _warn_color, _glyph = (
            (palette.C_ERROR, "✗") if controller.health == "error" else (palette.C_WARN, "⚠")
        )
        _doctor_hint = (
            f" [{palette.C_DIM}]— run /doctor[/{palette.C_DIM}]"
            if controller.health == "error"
            else ""
        )
        console.print(
            f"[{_warn_color}]{_glyph} {_rich_escape(controller.last_error)}[/{_warn_color}]{_doctor_hint}"
        )

    controller.record_session_title(text, when=time.time())
    turn_text = controller.enrich_turn_input(text)

    async def approver(req: ApprovalRequest) -> ApprovalReply:
        if title_hook is not None:
            title_hook("approval")
        result = await _approve(console, controller, req, ask=ask, pick=pick, view=view, edit=edit)
        if title_hook is not None:
            title_hook("working")
        return result

    renderer = TurnRenderer(
        console, lambda: controller.tracker.total.total_tokens,
        live_sink=live_sink, spinner=spinner, tool_sink=tool_sink,
    )
    try:
        # Turn loop with transparent model fallback: if a turn fails with a
        # retryable provider error *before* emitting any visible output, rotate
        # to the next fallback model and retry; otherwise surface the error.
        # ``produced`` gates on emitted UI events. On retry we resume from the
        # thread's existing state (LangGraph already checkpointed the user
        # message before the failed model call) so the user turn isn't duplicated.
        resume = False
        while True:
            driver = controller.make_driver(approver)
            produced = False
            pending_error: Event | None = None
            payload = "" if resume else turn_text
            async for event in driver.run_turn(payload, resume=resume):
                if event.kind is EventKind.TEXT:
                    renderer.on_text(event.text)
                    if token_sink is not None:
                        token_sink(event.text)
                    produced = True
                elif event.kind is EventKind.REASONING:
                    renderer.on_reasoning(event.text)
                    if token_sink is not None:
                        token_sink(event.text)
                    produced = True
                elif event.kind is EventKind.TOOL_START:
                    renderer.on_tool(
                        event.text,
                        event.data.get("args", {}),
                        tool_call_id=event.data.get("tool_call_id"),
                    )
                    produced = True
                elif event.kind is EventKind.TOOL_END:
                    renderer.on_tool_end(
                        event.text,
                        event.data.get("summary", ""),
                        event.data.get("full", ""),
                        tool_call_id=event.data.get("tool_call_id"),
                    )
                    produced = True
                elif event.kind is EventKind.NOTICE or (
                    event.kind is EventKind.APPROVAL
                    and event.text.startswith(("blocked", "rejected"))
                ):
                    renderer.on_notice(f"[{palette.C_NOTICE}]{event.text}[/{palette.C_NOTICE}]")
                    produced = True
                elif event.kind is EventKind.APPROVAL:
                    produced = True  # a tool was authorized → has a side effect
                elif event.kind is EventKind.ERROR:
                    pending_error = event

            if pending_error is None:
                controller.reset_model_rotation()  # back to primary on success
                break
            if pending_error.data.get("retryable") and not produced:
                new_ref = controller.rotate_to_fallback()
                if new_ref:
                    try:
                        await controller.ensure_runtime()
                    except Exception as exc:  # noqa: BLE001
                        renderer.on_notice(f"[{palette.C_ERROR}]fallback unavailable: {exc}[/{palette.C_ERROR}]")
                        break
                    renderer.on_notice(
                        f"[{palette.C_NOTICE}]model error, retrying with {new_ref}…[/{palette.C_NOTICE}]"
                    )
                    resume = True  # user message is already in state; don't re-send
                    continue
            if pending_error.data.get("auth"):
                # A 401 is non-retryable on the *same* provider (reusing a
                # rejected key just 401s again), but a configured fallback on a
                # different provider with a resolvable key is exactly the case
                # where switching helps — try that before dead-ending.
                if not produced:
                    new_ref = controller.rotate_to_keyed_fallback()
                    if new_ref:
                        try:
                            await controller.ensure_runtime()
                        except Exception as exc:  # noqa: BLE001
                            renderer.on_notice(
                                f"[{palette.C_ERROR}]fallback unavailable: {exc}[/{palette.C_ERROR}]"
                            )
                            break
                        renderer.on_notice(
                            f"[{palette.C_NOTICE}]auth failed, retrying with {new_ref}…"
                            f"[/{palette.C_NOTICE}]"
                        )
                        resume = True  # user message is already in state; don't re-send
                        continue
                provider = pending_error.data.get("provider") or _provider_hint(controller)
                renderer.on_notice(_friendly_auth_error(pending_error.text, provider))
            else:
                renderer.on_notice(
                    f"[{palette.C_ERROR}]{pending_error.text}[/{palette.C_ERROR}]"
                )
            break
    except KeyboardInterrupt:
        renderer.cancel()
    finally:
        renderer.finish()

    controller.record_turn(when=time.time())
    # Auto-compaction is handled in-graph by the summarization middleware wired in
    # build_runtime (summarizer model, context.compact_at_pct) — no controller-side
    # thread-forking trigger here. Manual /compact still forks the thread on demand.
    return renderer.tool_outputs


def _editable_field(args: dict | None) -> str | None:
    """Which arg holds the proposed new content for a write/edit call.

    ``content`` for a ``write_file`` (full file), ``new_string`` for an
    ``edit_file`` (the replacement text). Returns ``None`` when neither is
    present (e.g. a binary write), so edit-before-apply is simply not offered.
    """
    if not args:
        return None
    if "content" in args:
        return "content"
    if "new_string" in args:
        return "new_string"
    return None


def _edit_text_in_editor(text: str, *, suffix: str = ".txt") -> str | None:
    """Open ``text`` in ``$EDITOR`` and return the edited result.

    Returns the edited text on a normal save-quit, or ``None`` when the editor is
    *aborted* (non-zero exit, e.g. vim ``:cq``) so the caller cancels without
    applying anything. Blocking — call via :func:`asyncio.to_thread` so the event
    loop stays live.
    """
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="jarn-edit-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        try:
            proc = subprocess.run([*shlex.split(editor), path], check=False)
        except (OSError, ValueError):
            # Editor missing or unparseable $EDITOR → treat as abort.
            return None
        if proc.returncode != 0:
            return None  # editor aborted — do not apply
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


def _approval_options(
    request: ApprovalRequest, *, view_full_diff: bool = False, edit_before_apply: bool = False
) -> list[tuple[str, object]]:
    """Build the Claude Code-style approval menu for a gated action.

    ``view_full_diff`` appends a non-reply "View full diff" option (carrying the
    :data:`_VIEW_FULL_DIFF` sentinel) for over-cap write diffs. ``edit_before_apply``
    appends an "Edit before apply" option (carrying :data:`_EDIT_BEFORE_APPLY`) for
    writes whose new content can be opened in ``$EDITOR``.
    """
    options: list[tuple[str, object]] = [
        ("Allow once", ApprovalReply(True, RememberScope.ONCE)),
    ]
    if request.result.block_remember_always:
        options.append(("Allow for session", ApprovalReply(True, RememberScope.SESSION)))
    else:
        options.append(("Allow always", ApprovalReply(True, RememberScope.ALWAYS)))
    if edit_before_apply:
        options.append(("Edit before apply", _EDIT_BEFORE_APPLY))
    options.append(("Deny", ApprovalReply(False, message="rejected by user")))
    if view_full_diff:
        options.append(("View full diff", _VIEW_FULL_DIFF))
    return options


async def _approve(
    console: Console,
    controller: Controller,
    request: ApprovalRequest,
    *,
    ask: Ask | None = None,
    pick: Pick | None = None,
    view: Callable[[str], Awaitable[None]] | None = None,
    edit: Callable[[ApprovalRequest], Awaitable[ApprovalReply | None]] | None = None,
) -> ApprovalReply:
    # Fire the approval notification before the prompt renders.  elapsed=0
    # because the threshold check is skipped for "needs_approval" events.
    notify("needs_approval", controller.config.ui, elapsed=0.0, write=console.file.write)
    if request.plan is not None:
        return await _approve_plan(console, controller, request, ask=ask, pick=pick)
    if request.suggested_memory is not None:
        return await _approve_suggested_memory(
            console, controller, request, ask=ask, pick=pick, edit=edit
        )
    a = request.action
    what = (f"run: {a.target}" if a.kind is ActionKind.SHELL
            else f"write: {a.target}" if a.kind is ActionKind.WRITE
            else f"{a.kind.value}: {a.target}")
    danger = "[red]⚠ DANGEROUS — [/red]" if request.result.dangerous else ""
    console.print(f"\n{danger}[bold]Approve?[/bold] {what}  [{palette.C_DIM}]({request.result.reason})[/{palette.C_DIM}]")
    full_diff: Text | None = None
    over_cap = False
    if a.kind is ActionKind.WRITE:
        from jarn.tui.widgets.diff import diff_from_edit_args

        # Cap the inline diff so writing a large file doesn't flood the prompt;
        # the full content is what's being approved, not what needs to be read.
        cap = controller.config.ui.approval_diff_lines
        full_diff = diff_from_edit_args(request.args or {})
        over_cap = full_diff is not None and len(full_diff.plain.splitlines()) > cap
        diff = diff_from_edit_args(request.args or {}, max_lines=cap)
        if diff is not None:
            console.print(diff)
    # Only offer "view full diff" when there's actually more to see *and* a pager
    # to route it through (interactive sessions thread one in via ``view``).
    show_view = over_cap and view is not None and full_diff is not None
    # Offer "edit before apply" only for a write whose new content is editable
    # *and* when an editor launcher is wired (interactive sessions thread one in
    # via ``edit``); headless callers never see it.
    show_edit = (
        a.kind is ActionKind.WRITE
        and edit is not None
        and _editable_field(request.args) is not None
    )
    options = _approval_options(request, view_full_diff=show_view, edit_before_apply=show_edit)
    if pick is not None:
        while True:
            picked = await pick(options)
            if picked is _VIEW_FULL_DIFF:
                # Viewing must NOT decide: scroll the full diff, then re-prompt.
                assert view is not None and full_diff is not None
                await view(full_diff.plain)
                continue
            if picked is _EDIT_BEFORE_APPLY:
                # Open the proposed content in $EDITOR. A clean save → approve with
                # the edited args; aborting the editor cancels cleanly (deny), so
                # nothing is applied. Either way the prompt is not re-shown.
                assert edit is not None
                reply = await edit(request)
                if reply is None:
                    console.print(
                        f"[{palette.C_DIM}]edit aborted — nothing applied[/{palette.C_DIM}]"
                    )
                    return ApprovalReply(False, message="rejected by user")
                return reply
            return cast(ApprovalReply, picked)
    # Text fallback for headless tests / non-interactive callers.
    allow_once = cast(ApprovalReply, options[0][1])
    deny = ApprovalReply(False, message="rejected by user")
    if ask is None:
        return deny
    choices = ("[a]llow once / [s]ession / [r]eject" if request.result.block_remember_always
               else "[a]llow once / [s]ession / [w] always / [r]eject")
    ans = (await ask(f"  {choices}: ")).strip().lower()
    if ans in ("a", "allow", "y", "yes"):
        return allow_once
    if ans in ("s", "session"):
        return ApprovalReply(True, RememberScope.SESSION)
    if ans in ("w", "always") and not request.result.block_remember_always:
        return ApprovalReply(True, RememberScope.ALWAYS)
    return deny


async def _approve_plan(
    console: Console,
    controller: Controller,
    request: ApprovalRequest,
    *,
    ask: Ask | None = None,
    pick: Pick | None = None,
) -> ApprovalReply:
    """Plan-mode handoff approval: show the plan, pick the mode to proceed in.

    On approval the live permission mode is escalated through
    ``controller.apply_mode`` (which clamps to the review-only floor on an
    untrusted project), so the rest of the turn can carry out the plan.
    """
    from rich.markdown import Markdown

    plan = request.plan or ""
    console.print(f"\n[{palette.C_NOTICE}]▶ Plan ready for review[/{palette.C_NOTICE}]")
    if plan.strip():
        console.print(Markdown(plan))
    if not controller.project_trusted:
        console.print(
            f"[{palette.C_WARN}]⚠ Project is untrusted — approving keeps read-only "
            f"plan mode; run /trust to allow edits.[/{palette.C_WARN}]"
        )

    auto = ("Approve → proceed in auto-edit",
            ApprovalReply(True, plan_mode_target="auto-edit"))
    askm = ("Approve → proceed, ask before each action",
            ApprovalReply(True, plan_mode_target="ask"))
    keep = ("Keep planning (don't execute yet)",
            ApprovalReply(False,
                          message="Keep refining the plan; call exit_plan_mode again when ready."))
    ordered: list[tuple[str, object]] = (
        [auto, askm, keep]
        if controller.config.plan.exit_mode == "auto-edit"
        else [askm, auto, keep]
    )

    if pick is not None:
        picked = await pick(ordered)
        reply = cast(ApprovalReply, picked)
    elif ask is not None:
        ans = (await ask("  [a]pprove auto-edit / [k] approve ask / [n] keep planning: ")).strip().lower()
        reply = auto[1] if ans in ("a", "approve", "y", "yes") else askm[1] if ans in ("k", "ask") else keep[1]
    else:
        return ApprovalReply(False, message="auto-denied (no approver)")

    if reply.approved and reply.plan_mode_target:
        applied = controller.apply_mode(reply.plan_mode_target)
        if applied != reply.plan_mode_target:
            console.print(
                f"[{palette.C_WARN}]mode clamped to {applied} — project untrusted "
                f"(/trust to allow edits).[/{palette.C_WARN}]"
            )
        else:
            console.print(
                f"[{palette.C_NOTICE}]plan approved → {applied} mode[/{palette.C_NOTICE}]"
            )
    return reply


async def _approve_suggested_memory(
    console: Console,
    controller: Controller,
    request: ApprovalRequest,
    *,
    ask: Ask | None = None,
    pick: Pick | None = None,
    edit: Callable[[ApprovalRequest], Awaitable[ApprovalReply | None]] | None = None,
) -> ApprovalReply:
    """Memory-suggestion approval: show it, then save / edit-and-save / decline.

    On approval the memory is written through ``controller.save_suggested_memory``
    (same scope + trust gating as ``/memory add``); declining writes nothing. The
    returned :class:`ApprovalReply` only signals the agent — its ``approved`` flag
    is set iff the memory was actually saved.
    """
    suggestion = request.suggested_memory
    assert suggestion is not None
    console.print(f"\n[{palette.C_NOTICE}]▶ Suggested memory[/{palette.C_NOTICE}] "
                  f"[{palette.C_DIM}]({suggestion.scope}, {suggestion.type})[/{palette.C_DIM}]")
    console.print(f"  [b]{_rich_escape(suggestion.name)}[/b] — "
                  f"{_rich_escape(suggestion.description)}")
    if suggestion.body.strip():
        console.print(Markdown(suggestion.body))

    save = ("Save this memory", True)
    edit_save = ("Edit, then save", _EDIT_MEMORY)
    decline = ("Don't save", False)

    choice: object
    if pick is not None:
        # Only offer "edit" when there's an editor wired (interactive sessions),
        # matching how edit-before-apply is gated for writes.
        options: list[tuple[str, object]] = [save]
        if edit is not None:
            options.append(edit_save)
        options.append(decline)
        choice = await pick(options)
    elif ask is not None:
        ans = (await ask("  Save this memory? [y/N/edit]: ")).strip().lower()
        choice = (
            _EDIT_MEMORY if ans in ("e", "edit")
            else ans in ("y", "yes")
        )
    else:
        return ApprovalReply(False, message="auto-denied (no approver)")

    if choice is _EDIT_MEMORY:
        edited = await asyncio.to_thread(
            _edit_text_in_editor, suggestion.body, suffix=".md"
        )
        if edited is None:
            console.print(
                f"[{palette.C_DIM}]edit aborted — memory not saved[/{palette.C_DIM}]"
            )
            return ApprovalReply(False, message="User declined to save the memory.")
        suggestion.body = edited.strip()
        choice = True

    if choice is not True:
        console.print(f"[{palette.C_DIM}]memory not saved[/{palette.C_DIM}]")
        return ApprovalReply(False, message="User declined to save the memory.")

    saved, message = controller.save_suggested_memory(suggestion)
    colour = palette.C_NOTICE if saved else palette.C_WARN
    console.print(f"[{colour}]{_rich_escape(message)}[/{colour}]")
    return ApprovalReply(saved, message="" if saved else message)


def _apply_model_ref(controller: Controller, console: Console, chosen: str) -> None:
    from jarn.providers import qualify_model_ref

    # Treat the ref as already-qualified only when its first segment names a
    # configured provider profile. Otherwise it's a bare model id whose own
    # vendor prefix happens to contain a "/" (e.g. "deepseek/deepseek-chat")
    # — qualify it under the default profile so it routes correctly.
    first = chosen.split("/", 1)[0]
    ref = chosen if first in controller.config.providers else qualify_model_ref(
        chosen, controller.config.default_profile
    )
    controller.apply_model(ref)
    console.print(
        f"[{palette.C_NOTICE}]model → {controller.config.resolved_main_model()}[/{palette.C_NOTICE}]"
    )


def _apply_mode_ref(controller: Controller, console: Console, chosen: str) -> None:
    try:
        applied = controller.apply_mode(PermissionMode(chosen).value)
        if applied != chosen:
            console.print(
                f"[{palette.C_NOTICE}]mode → {applied}[/{palette.C_NOTICE}] "
                f"[{palette.C_DIM}](clamped — project untrusted)[/{palette.C_DIM}]"
            )
        else:
            console.print(f"[{palette.C_NOTICE}]mode → {applied}[/{palette.C_NOTICE}]")
    except ValueError:
        console.print(f"[{palette.C_ERROR}]unknown mode {chosen!r}[/{palette.C_ERROR}]")
