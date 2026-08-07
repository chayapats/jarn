"""Shared turn runner — retry / fallback / T-3-7 policy for every front-end.

Owns the turn-level loop that was previously trapped in ``repl/turn.py``:

* model-fallback rotation on retryable provider errors (before any visible output)
* auth-error rotation to a keyed fallback on a different provider
* T-3-7 image-capability text-only retry (one-shot per turn, session-sticky)
* diagnostics auto-fix requeue (via an injectable ``queue_sink``)

Front-ends (REPL, headless, Telegram bridge) supply an :class:`Approver` and an
``on_event`` sink. This module must not import TUI widgets — media ingest and
approvals stay injectable through ``SessionDriver.run_turn`` / the approver.

Turn re-entrancy (T-QA-1)
-------------------------
**Policy: refuse.** A second concurrent ``run_agent_turn`` / ``make_driver`` on
the same :class:`~jarn.controller.Controller` raises
:class:`~jarn.controller.TurnBusyError` (surfaced here as an ERROR event with
``data.code == "busy"``). Turns are never silently interleaved and never queued
at this layer — ``make_driver`` would otherwise overwrite ``_active_driver`` and
break ``settle_snapshot`` / undo. Gateway chat routing already queues-when-busy
in :mod:`jarn.gateway.sessions`; the worker/controller still needs this hard
guard so a buggy double-entry fails loud.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarn.agent.session import Approver, Event, EventKind

#: Substrings that mark a provider ERROR as an image/vision/multimodal capability
#: rejection (T-3-7). Matched case-insensitively against the error text, and only
#: consulted on a turn that actually inlined images, so a false positive is bounded
#: to one harmless text-only re-send.
_IMAGE_ERROR_MARKERS: tuple[str, ...] = ("image", "vision", "multimodal", "modalit")

OnEvent = Callable[[Event], Awaitable[None] | None]
QueueSink = Callable[..., Any]


@dataclass(slots=True)
class TurnResult:
    """Outcome of :func:`run_agent_turn` after retries are exhausted or succeed."""

    had_events: bool = False
    """True when the driver emitted at least one event (any attempt)."""
    error: Event | None = None
    """Final ERROR event when the turn ended in failure (after policy retries)."""
    driver: Any = None
    """The last :class:`SessionDriver` minted for this turn (transcript locus)."""


def select_inline_images(controller: Any, text: str) -> list[Path]:
    """Return the image paths to inline for this turn's user message (T-3-7).

    Empty when ``execution.inline_images`` is ``off`` or the session-level fallback
    has already fired (``controller.inline_images_disabled``). Otherwise scans
    ``text`` for qualifying image ``@``-mentions via the completion scanner
    (exists + image mimetype + ≤ 5 MB). Called at submit; the result is threaded
    into :func:`run_agent_turn` / ``SessionDriver.run_turn``.

    The ``@``-mention scanner lives under ``jarn.tui.completion`` (shared with the
    completer); bridge callers pass ``images`` / ``media`` explicitly instead.
    """
    if (
        controller.config.execution.inline_images != "auto"
        or controller.inline_images_disabled
    ):
        return []
    from jarn.tui.completion import scan_image_mentions

    return scan_image_mentions(text, controller.project_root or Path.cwd())


def is_image_capability_error(event: Event) -> bool:
    """True when a provider ERROR looks like an image/vision/multimodal rejection."""
    text = (event.text or "").lower()
    return any(marker in text for marker in _IMAGE_ERROR_MARKERS)


def _counts_as_produced(event: Event) -> bool:
    """Whether *event* counts as visible output that blocks model-fallback retry."""
    if event.kind is EventKind.ERROR:
        return False
    if event.kind is EventKind.DONE:
        return False
    # TEXT / REASONING / TOOL_* / NOTICE / APPROVAL (incl. blocked) all count —
    # matching the historical REPL turn loop.
    return event.kind in {
        EventKind.TEXT,
        EventKind.REASONING,
        EventKind.TOOL_START,
        EventKind.TOOL_PROGRESS,
        EventKind.TOOL_END,
        EventKind.NOTICE,
        EventKind.APPROVAL,
    }


async def _emit(on_event: OnEvent | None, event: Event) -> None:
    if on_event is None:
        return
    result = on_event(event)
    if inspect.isawaitable(result):
        await result


async def run_agent_turn(
    controller: Any,
    text: str,
    *,
    approver: Approver,
    images: list[Path] | None = None,
    media: Sequence[Any] | None = None,
    pending_only: bool = False,
    resume: bool = False,
    on_event: OnEvent | None = None,
    queue_sink: QueueSink | None = None,
) -> TurnResult:
    """Drive one user turn with shared retry / fallback / T-3-7 policy.

    Caller is responsible for ``ensure_runtime``, input enrichment, and
    ``record_turn`` / telemetry. ``text`` is the payload passed to
    ``SessionDriver.run_turn`` on a fresh (non-resume) attempt — typically
    already enriched by the front-end.

    Intermediate ERROR events are absorbed for policy decisions; only a
    terminal ERROR (retries exhausted) is forwarded via ``on_event`` and
    returned on :attr:`TurnResult.error`. Synthetic NOTICE events are emitted
    for image / model / auth fallback attempts.
    
    Acquires the controller's exclusive turn slot for the whole retry loop
    (T-QA-1 refuse policy); see the module docstring.
    """
    from jarn.controller import TurnBusyError

    result = TurnResult()
    try:
        acquired = controller.acquire_turn()
    except TurnBusyError as exc:
        err = Event(
            EventKind.ERROR,
            str(exc),
            data={"code": "busy", "retryable": False},
        )
        await _emit(on_event, err)
        result.error = err
        return result

    try:
        attempt_resume = resume
        image_fallback_done = False

        while True:
            driver = controller.make_driver(approver)
            result.driver = driver
            produced = False
            pending_error: Event | None = None
            payload = "" if attempt_resume else text
            # Inline images only on a fresh (non-resume) attempt while the session
            # fallback hasn't disabled them. A model-rotation resume re-runs on the
            # already-checkpointed state (which still holds the image blocks), so it
            # must not re-inline; only pass the kwarg when there is something to send,
            # so drivers with the old signature (tests) are unaffected.
            turn_images = (
                images
                if (images and not attempt_resume and not controller.inline_images_disabled)
                else None
            )
            run_kwargs: dict[str, Any] = {"resume": attempt_resume}
            if turn_images:
                run_kwargs["images"] = turn_images
            # ``media`` is the preferred multimodal seam (#54); only on fresh attempts.
            if media and not attempt_resume:
                run_kwargs["media"] = media
            if pending_only:
                run_kwargs["pending_only"] = True

            async for event in driver.run_turn(payload, **run_kwargs):
                result.had_events = True
                if event.kind is EventKind.ERROR:
                    pending_error = event
                    continue
                if (
                    event.kind is EventKind.NOTICE
                    and event.data.get("diagnostics_auto_queue")
                    and queue_sink is not None
                ):
                    # Diagnostics auto-fix (T-3-3): queue ONE internal follow-up
                    # turn. The chain-round counter is bumped BEFORE the round runs
                    # so its driver sees round>=1 and the cap holds even if the auto
                    # round introduces new errors.
                    controller._diag_chain_round += 1
                    queue_sink("", event.data["diagnostics_auto_queue"], internal=True)
                await _emit(on_event, event)
                if _counts_as_produced(event):
                    produced = True

            if pending_error is None:
                controller.reset_model_rotation()  # back to primary on success
                return result

            # T-3-7 image fallback: a provider that rejects the inlined image gets
            # ONE same-model text-only retry (do NOT rotate). One-shot per turn and
            # one-way per session — the flag makes `auto` behave like `off` for the
            # rest of the session, and the guard stops a second image error from
            # re-triggering (it then surfaces normally / falls to the branches below).
            if (
                turn_images
                and not image_fallback_done
                and not produced
                and is_image_capability_error(pending_error)
            ):
                controller.inline_images_disabled = True
                image_fallback_done = True
                # Strip the rejected image message from state so the text-only
                # re-send doesn't leave the model re-seeing the image.
                await controller.drop_pending_image_message()
                await _emit(
                    on_event,
                    Event(
                        EventKind.NOTICE,
                        "image not accepted by this model — "
                        "retrying without the inline image (text-only)",
                        data={"turn_policy": "image_fallback"},
                    ),
                )
                attempt_resume = False  # re-send the turn text; images are dropped
                continue

            if pending_error.data.get("retryable") and not produced:
                new_ref = controller.rotate_to_fallback()
                if new_ref:
                    try:
                        await controller.ensure_runtime()
                    except Exception as exc:  # noqa: BLE001
                        await _emit(
                            on_event,
                            Event(
                                EventKind.NOTICE,
                                f"fallback unavailable: {exc}",
                                data={
                                    "turn_policy": "fallback_unavailable",
                                    "severity": "error",
                                },
                            ),
                        )
                        # Match historical REPL: surface only the unavailable notice
                        # (not the original provider error) and stop retrying.
                        result.error = pending_error
                        return result
                    await _emit(
                        on_event,
                        Event(
                            EventKind.NOTICE,
                            f"model error, retrying with {new_ref}…",
                            data={"turn_policy": "model_fallback", "model": new_ref},
                        ),
                    )
                    attempt_resume = True  # user message is already in state
                    continue

            # A 401 is non-retryable on the *same* provider (reusing a
            # rejected key just 401s again), but a configured fallback on a
            # different provider with a resolvable key is exactly the case
            # where switching helps — try that before dead-ending.
            if pending_error.data.get("auth") and not produced:
                new_ref = controller.rotate_to_keyed_fallback()
                if new_ref:
                    try:
                        await controller.ensure_runtime()
                    except Exception as exc:  # noqa: BLE001
                        await _emit(
                            on_event,
                            Event(
                                EventKind.NOTICE,
                                f"fallback unavailable: {exc}",
                                data={
                                    "turn_policy": "fallback_unavailable",
                                    "severity": "error",
                                },
                            ),
                        )
                        result.error = pending_error
                        return result
                    await _emit(
                        on_event,
                        Event(
                            EventKind.NOTICE,
                            f"auth failed, retrying with {new_ref}…",
                            data={"turn_policy": "auth_fallback", "model": new_ref},
                        ),
                    )
                    attempt_resume = True
                    continue

            result.error = pending_error
            await _emit(on_event, pending_error)
            return result
    finally:
        if acquired:
            controller.release_turn()
