"""Fail-closed redaction and size-caps for tool args leaving the agent stream.

``TOOL_START`` is emitted from the updates stream *before* HITL interrupt
resolution. A DENY/REJECT therefore arrives only after the outbound event has
already been published — so anything that may leave on the wire (TUI, headless
stream-json, a future gateway worker) must be redacted and size-capped at the
Event-construction boundary, not later at the consumer.

The walk mirrors the transcript writer's contract (depth / width / shared
character budget) so a huge ``write_file`` payload cannot inflate an outbound
frame, and ``.env``-shaped ``NAME=value`` secrets are scrubbed by the central
redactor before serialization.
"""

from __future__ import annotations

from typing import Any

from jarn.config.secrets import redact_secrets

#: Characters retained from one string leaf (matches transcript leaf cap).
_MAX_ARG_CHARS = 2_000

#: Depth limit when walking a tool-argument value.
_MAX_ARG_DEPTH = 6

#: Element limit per container.
_MAX_ARG_ITEMS = 100

#: Characters retained across ALL arguments of one tool call.
_MAX_ARG_TOTAL_CHARS = 20_000


def sanitize_tool_args(args: dict[str, Any] | None) -> dict[str, Any]:
    """Return a redacted, size-capped copy of tool-call args for outbound events.

    Fail-closed: call this *before* putting args into a ``TOOL_START`` Event (or
    any other outbound serialization). Scalars other than strings pass through
    unchanged; truncation is marked with ``<key>__truncated`` so consumers can
    tell the payload is partial.
    """
    if not args:
        return {}
    capped: dict[str, Any] = {}
    budget = [_MAX_ARG_TOTAL_CHARS]
    for key, value in args.items():
        out, truncated = _cap_arg(value, budget=budget)
        capped[str(key)] = out
        if truncated:
            capped[f"{key}__truncated"] = True
    return capped


def _cap_arg(
    value: Any, *, depth: int = 0, budget: list[int] | None = None
) -> tuple[Any, bool]:
    """Redact and size-cap one tool-argument value. Returns ``(value, truncated)``."""
    if budget is None:
        budget = [_MAX_ARG_TOTAL_CHARS]

    if isinstance(value, str):
        redacted = redact_secrets(value)
        allowance = min(_MAX_ARG_CHARS, max(0, budget[0]))
        if len(redacted) > allowance:
            budget[0] -= allowance
            return redacted[:allowance], True
        budget[0] -= len(redacted)
        return redacted, False

    if isinstance(value, dict):
        if depth >= _MAX_ARG_DEPTH:
            return f"<dict omitted: deeper than {_MAX_ARG_DEPTH} levels>", True
        out: dict[str, Any] = {}
        truncated = False
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_ARG_ITEMS or budget[0] <= 0:
                truncated = True
                break
            capped, item_truncated = _cap_arg(item, depth=depth + 1, budget=budget)
            out[str(key)] = capped
            truncated = truncated or item_truncated
        return out, truncated

    if isinstance(value, (list, tuple)):
        if depth >= _MAX_ARG_DEPTH:
            return f"<list omitted: deeper than {_MAX_ARG_DEPTH} levels>", True
        items: list[Any] = []
        truncated = False
        for index, item in enumerate(value):
            if index >= _MAX_ARG_ITEMS or budget[0] <= 0:
                truncated = True
                break
            capped, item_truncated = _cap_arg(item, depth=depth + 1, budget=budget)
            items.append(capped)
            truncated = truncated or item_truncated
        return items, truncated

    return value, False
