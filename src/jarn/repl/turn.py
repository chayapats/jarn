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
from pathlib import Path
from typing import TYPE_CHECKING, cast

from rich.console import Console
from rich.markdown import Markdown

from jarn.agent.session import ApprovalReply, ApprovalRequest, Event, EventKind
from jarn.agent.turn_runner import run_agent_turn
from jarn.agent.turn_runner import select_inline_images as select_inline_images
from jarn.config.schema import PermissionMode
from jarn.permissions import ActionKind, RememberScope
from jarn.repl.auth_errors import _friendly_auth_error, _provider_hint
from jarn.repl_renderer import TurnRenderer
from jarn.tui import grammar, layout
from jarn.tui.controller import Controller
from jarn.tui.notify import notify
from jarn.util.process_env import external_command_env

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

#: Sentinel for the "edit then save" action on a suggested-skill prompt — same
#: shape as :data:`_EDIT_MEMORY`, for skill body editing before write.
_EDIT_SKILL = object()


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
    todos_sink: Callable[[], Awaitable[None]] | None = None,
    title_hook: Callable[[str], None] | None = None,
    queue_sink: Callable[..., int] | None = None,
    images: list[Path] | None = None,
    pending_only: bool = False,
) -> list[tuple[str, str]]:
    """Stream a turn; return the turn's expandable ``(tool, full output)`` pairs.

    Thin REPL adapter over :func:`jarn.agent.turn_runner.run_agent_turn` — owns
    Rich rendering, approval prompts, and cancel UX. Retry / fallback / T-3-7
    policy lives in the shared runner (no settle/abort/yolo wrappers here).

    If ``tool_sink`` is given, tool outputs are appended to it live (so a pager
    can read them mid-turn). If ``todos_sink`` is given, it is awaited on every
    ``write_todos`` tool completion so the front-end can refresh the live plan
    checklist in place as the agent flips items. If ``queue_sink`` is given
    (the REPL's ``InputQueue.append``), a diagnostics auto-fix round is queued
    through it as an *internal* item (``internal=True``) so the drain runs it
    without a ``» queued:`` / ``› …`` user-line echo."""
    renderer = TurnRenderer(
        console, lambda: controller.tracker.total.total_tokens,
        live_sink=live_sink, spinner=spinner, tool_sink=tool_sink,
        tool_progress=getattr(controller, "tool_progress", controller.config.ui.tool_progress),
        wrap_at=controller.config.ui.wrap_at,
        show_reasoning=controller.config.ui.show_reasoning,
    )
    # ONE cancellation handler spans the whole turn — runtime warm-up, enrich, AND
    # streaming — so a cancel during the PRE-STREAM awaits (``ensure_runtime`` or the
    # off-thread ``enrich_turn_input``) still routes through ``renderer.cancel()``.
    # The caller (``InlineApp._handle``) suppresses a turn's ``CancelledError`` on the
    # assumption the renderer already printed the stop message; a cancel that escaped
    # this handler (as one during setup used to, when the try started only at the
    # stream loop) was therefore SILENT — the user got no feedback at all.
    had_events = False
    turn_failed = False
    try:
        try:
            await controller.ensure_runtime()
        except Exception as exc:  # noqa: BLE001  (CancelledError is BaseException → the outer handler)
            console.print(
                layout.err(f"agent not ready: {exc}")
                + "  "
                + layout.muted("· /help or jarn setup")
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
            if controller.health == "error":
                console.print(
                    layout.err(f"{grammar.GLYPH_FAIL} {controller.last_error}")
                    + " "
                    + layout.muted("— run /doctor"),
                    highlight=False,
                )
            else:
                console.print(
                    layout.warn(f"{grammar.GLYPH_WARN} {controller.last_error}"),
                    highlight=False,
                )

        if not pending_only:
            controller.record_session_title(text, when=time.time())
            # enrich_turn_input does synchronous memory-file reads + vector-index
            # builds; run it off the event loop so the REPL stays responsive.
            turn_text = await asyncio.to_thread(controller.enrich_turn_input, text)
        else:
            turn_text = ""

        async def approver(req: ApprovalRequest) -> ApprovalReply:
            if title_hook is not None:
                title_hook("approval")
            result = await _approve(console, controller, req, ask=ask, pick=pick, view=view, edit=edit)
            if title_hook is not None:
                title_hook("working")
            return result

        async def on_event(event: Event) -> None:
            nonlocal turn_failed
            if event.kind is EventKind.TEXT:
                renderer.on_text(event.text, agent=event.data.get("agent"))
                if token_sink is not None:
                    token_sink(event.text)
            elif event.kind is EventKind.REASONING:
                renderer.on_reasoning(event.text)
                if token_sink is not None:
                    token_sink(event.text)
            elif event.kind is EventKind.TOOL_START:
                renderer.on_tool(
                    event.text,
                    event.data.get("args", {}),
                    tool_call_id=event.data.get("tool_call_id"),
                    agent=event.data.get("agent"),
                )
            elif event.kind is EventKind.TOOL_PROGRESS:
                # Live foreground-execute tail: render the running command's
                # output tail + heartbeat into the transient live region (never
                # scrollback); on_tool_end clears it and commits the final result.
                renderer.on_tool_progress(
                    event.text,
                    event.data.get("tail", ""),
                    event.data.get("elapsed", 0.0),
                    tool_call_id=event.data.get("tool_call_id"),
                    heartbeat=event.data.get("heartbeat", False),
                    agent=event.data.get("agent"),
                )
            elif event.kind is EventKind.TOOL_END:
                renderer.on_tool_end(
                    event.text,
                    event.data.get("summary", ""),
                    event.data.get("full", ""),
                    tool_call_id=event.data.get("tool_call_id"),
                    agent=event.data.get("agent"),
                )
                # Refresh the live plan checklist the moment a todo write lands,
                # so it re-renders in place mid-turn (not only after the turn).
                if todos_sink is not None and event.text == "write_todos":
                    await todos_sink()
            elif event.kind is EventKind.NOTICE and event.data.get(
                "diagnostics_auto_queue"
            ):
                # Runner already bumped the chain counter + queued via queue_sink;
                # only render the banner when a sink was wired (interactive REPL).
                if queue_sink is not None:
                    renderer.on_notice(
                        layout.muted("diagnostics: errors in edited files — auto-fix round queued")
                    )
            elif event.kind is EventKind.NOTICE and event.data.get("steer"):
                # Mid-turn steering (T-4-6): mark where the steer landed in
                # scrollback so the transcript shows it interleaved at its true
                # position, distinct from a queued (» queued) or normal (›) line.
                renderer.on_notice(layout.muted(event.text))
            elif event.kind is EventKind.NOTICE and event.data.get("diagnostics"):
                # Diagnostics suggest-mode NOTICE: plain notice listing findings.
                d = event.data["diagnostics"]
                renderer.on_notice(
                    layout.notice(f"diagnostics: {d.get('count', 0)} issue(s) in edited files")
                    + "\n"
                    + layout.muted(str(d.get("text", "")))
                )
            elif event.kind is EventKind.NOTICE or (
                event.kind is EventKind.APPROVAL
                and event.text.startswith(("blocked", "rejected"))
            ):
                if event.kind is EventKind.NOTICE and event.data.get("verify"):
                    renderer.on_verify_badge(event.data["verify"])
                elif event.kind is EventKind.NOTICE and event.data.get("severity") == "error":
                    renderer.on_notice(layout.err(event.text))
                else:
                    renderer.on_notice(layout.notice(event.text))
            elif event.kind is EventKind.APPROVAL:
                pass  # authorized tool — side effect already happened; no UI line
            elif event.kind is EventKind.ERROR:
                turn_failed = True
                if event.data.get("auth"):
                    provider = event.data.get("provider") or _provider_hint(controller)
                    renderer.on_notice(_friendly_auth_error(event.text, provider))
                else:
                    renderer.on_notice(layout.err(event.text))
            # DONE and unknown kinds: no UI

        turn_result = await run_agent_turn(
            controller,
            turn_text,
            approver=approver,
            images=images,
            pending_only=pending_only,
            on_event=on_event,
            queue_sink=queue_sink,
        )
        had_events = turn_result.had_events
    except (KeyboardInterrupt, asyncio.CancelledError) as _exc:
        renderer.cancel()
        # Re-raise asyncio cancellations so the event loop knows the task was
        # cancelled.  KeyboardInterrupt is NOT re-raised: the turn function
        # absorbs it and returns normally, letting the REPL keep running.
        if isinstance(_exc, asyncio.CancelledError):
            raise
    finally:
        renderer.finish()

    if not pending_only or had_events:
        controller.record_turn(when=time.time())
    if not pending_only and had_events and not turn_failed:
        controller.mark_session_complete(when=time.time())
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
            proc = subprocess.run(
                [*shlex.split(editor), path],
                check=False,
                env=external_command_env(),
            )
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
    if request.suggested_skill is not None:
        return await _approve_suggested_skill(
            console, controller, request, ask=ask, pick=pick, edit=edit
        )
    a = request.action
    what = (f"run: {a.target}" if a.kind is ActionKind.SHELL
            else f"write: {a.target}" if a.kind is ActionKind.WRITE
            else f"{a.kind.value}: {a.target}")
    danger = (
        f"{layout.err(grammar.GLYPH_WARN + ' DANGEROUS — ')}"
        if request.result.dangerous
        else ""
    )
    console.print(
        f"\n{danger}{layout.strong('Approve?')} {what}  "
        f"{layout.muted('(' + request.result.reason + ')')}"
    )
    console.print(
        layout.muted(f"working directory: {controller.project_root}")
    )
    if a.kind is ActionKind.NETWORK:
        console.print(layout.muted(f"network destination: {a.target}"))
    console.print(
        layout.muted(
            "Choose whether this approval applies once or to a scoped remembered rule."
        )
    )
    console.print(
        layout.muted(
            f"remembered scope: {controller.engine.remember_scope_summary(a)}"
        )
    )
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
                    console.print(layout.muted("edit aborted — nothing applied"))
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
    console.print("\n" + layout.notice(f"{grammar.GLYPH_PLAY} Plan ready for review"))
    if plan.strip():
        console.print(Markdown(plan))
    if not controller.project_trusted:
        console.print(
            layout.warn(
                f"{grammar.GLYPH_WARN} Project is untrusted — approving keeps read-only "
                "plan mode; run /trust to allow edits."
            )
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
                layout.warn(
                    f"mode clamped to {applied} — project untrusted (/trust to allow edits)."
                )
            )
        else:
            console.print(layout.notice(f"plan approved → {applied} mode"))
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
    console.print(
        f"\n{layout.notice(f'{grammar.GLYPH_PLAY} Suggested memory')} "
        f"{layout.muted('(' + suggestion.scope + ', ' + suggestion.type + ')')}"
    )
    console.print(f"  {layout.strong(suggestion.name)} — {layout.escape(suggestion.description)}")
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
            console.print(layout.muted("edit aborted — memory not saved"))
            return ApprovalReply(False, message="User declined to save the memory.")
        suggestion.body = edited.strip()
        choice = True

    if choice is not True:
        console.print(layout.muted("memory not saved"))
        return ApprovalReply(False, message="User declined to save the memory.")

    saved, message = controller.save_suggested_memory(suggestion)
    printer = layout.notice if saved else layout.warn
    console.print(printer(message))
    return ApprovalReply(saved, message="" if saved else message)


async def _approve_suggested_skill(
    console: Console,
    controller: Controller,
    request: ApprovalRequest,
    *,
    ask: Ask | None = None,
    pick: Pick | None = None,
    edit: Callable[[ApprovalRequest], Awaitable[ApprovalReply | None]] | None = None,
) -> ApprovalReply:
    """Skill-suggestion approval: show it, then save / edit-and-save / decline.

    On approval the skill is written through ``controller.save_suggested_skill``
    (nested ``.jarn/skills/<name>/SKILL.md``, trust-gated); declining writes
    nothing. ``approved`` is set iff the skill was actually saved.
    """
    suggestion = request.suggested_skill
    assert suggestion is not None
    console.print(
        f"\n{layout.notice(f'{grammar.GLYPH_PLAY} Suggested skill')} "
        f"{layout.muted('(trigger=' + suggestion.trigger + ')')}"
    )
    console.print(
        f"  {layout.strong(suggestion.name)} — {layout.escape(suggestion.description)}"
    )
    if suggestion.body.strip():
        console.print(Markdown(suggestion.body))

    save = ("Save this skill", True)
    edit_save = ("Edit, then save", _EDIT_SKILL)
    decline = ("Don't save", False)

    choice: object
    if pick is not None:
        options: list[tuple[str, object]] = [save]
        if edit is not None:
            options.append(edit_save)
        options.append(decline)
        choice = await pick(options)
    elif ask is not None:
        ans = (await ask("  Save this skill? [y/N/edit]: ")).strip().lower()
        choice = (
            _EDIT_SKILL if ans in ("e", "edit")
            else ans in ("y", "yes")
        )
    else:
        return ApprovalReply(False, message="auto-denied (no approver)")

    if choice is _EDIT_SKILL:
        edited = await asyncio.to_thread(
            _edit_text_in_editor, suggestion.body, suffix=".md"
        )
        if edited is None:
            console.print(layout.muted("edit aborted — skill not saved"))
            return ApprovalReply(False, message="User declined to save the skill.")
        suggestion.body = edited.strip()
        choice = True

    if choice is not True:
        console.print(layout.muted("skill not saved"))
        return ApprovalReply(False, message="User declined to save the skill.")

    saved, message = controller.save_suggested_skill(suggestion)
    printer = layout.notice if saved else layout.warn
    console.print(printer(message))
    return ApprovalReply(saved, message="" if saved else message)


def _apply_model_ref(
    controller: Controller,
    console: Console,
    chosen: str,
    *,
    reasoning_effort: str | None = None,
) -> None:
    from jarn.providers import qualify_model_ref

    # Treat the ref as already-qualified only when its first segment names a
    # configured provider profile. Otherwise it's a bare model id whose own
    # vendor prefix happens to contain a "/" (e.g. "deepseek/deepseek-chat")
    # — qualify it under the default profile so it routes correctly.
    first = chosen.split("/", 1)[0]
    ref = chosen if first in controller.config.providers else qualify_model_ref(
        chosen, controller.config.default_profile
    )
    controller.apply_model(ref, reasoning_effort=reasoning_effort)
    effort_note = f" · reasoning {reasoning_effort}" if reasoning_effort else ""
    console.print(
        layout.notice(f"model → {controller.config.resolved_main_model()}{effort_note}")
    )


def _apply_mode_ref(controller: Controller, console: Console, chosen: str) -> None:
    try:
        applied = controller.apply_mode(PermissionMode(chosen).value)
        if applied != chosen:
            console.print(
                layout.notice(f"mode → {applied}")
                + " "
                + layout.muted("(clamped — project untrusted)")
            )
        else:
            console.print(layout.notice(f"mode → {applied}"))
    except ValueError:
        console.print(layout.err(f"unknown mode {chosen!r}"))
