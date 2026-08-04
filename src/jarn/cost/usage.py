"""One reading of a provider's ``usage_metadata`` — shared by every consumer.

Two paths price a model call from LangChain's ``usage_metadata``: the streaming
handler (:mod:`jarn.agent.stream_handlers`) and the parallel fan-out roll-up
(:mod:`jarn.agent.fanout`). They feed the same :class:`~jarn.cost.tracker.Usage`
buckets through the same :func:`~jarn.cost.pricing.cost_of`, so a disagreement
between them is a cost error, not a style difference — and they disagreed:
fan-out read only the generic ``cache_creation`` field and so undercharged every
Anthropic turn that reported TTL-specific cache writes (#82).

Extracting the reading here is the point: a second copy is what drifted.

The Anthropic quirk this exists to absorb
-----------------------------------------
Current ``langchain-anthropic``, when TTL-specific cache fields are present, puts
the cache-WRITE tokens under ``ephemeral_5m_input_tokens`` /
``ephemeral_1h_input_tokens`` and **zeroes** the generic ``cache_creation``.
:func:`~jarn.cost.pricing.cost_of` subtracts cache tokens from the plain-input
charge and reprices them at the model's cache rate, so a reader that sees only
``cache_creation`` leaves those tokens in the plain-input bucket and bills a
cache write at the *input* rate — an undercount, since a cache write costs more
than plain input everywhere it is priced separately (1.25x on Anthropic).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NamedTuple


class TokenCounts(NamedTuple):
    """The four token figures every cost path needs, in ``cost_of`` order."""

    input_tokens: int
    output_tokens: int
    cache_read: int
    cache_creation: int


def _as_int(value: Any) -> int:
    """Coerce a reported count to a non-negative int, never raising.

    Providers have shipped ``None`` here, and a future one may ship a string.
    These figures drive observability and a soft budget, never the correctness of
    the turn itself, so a malformed field must degrade to 0 rather than take down a
    turn that is otherwise fine — the same best-effort policy the write locks follow.

    Negatives are clamped for the same reason. A token count below zero is
    malformed by definition, and letting one through would flow into ``cost_of`` as
    a negative charge — the streaming path's monotonic check reads a negative
    cumulative as a fresh API call, so the bogus figure would be recorded outright
    rather than differenced away.
    """
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def normalize_usage(usage: Mapping[str, Any] | None) -> TokenCounts | None:
    """Read one message's ``usage_metadata`` into :class:`TokenCounts`.

    Returns ``None`` when there is nothing to record (absent or empty metadata),
    so callers can skip without inventing a zero call.
    """
    if not usage:
        return None
    details = usage.get("input_token_details") or {}
    # Prefer the generic field when it is nonzero; fall back to the TTL-specific
    # pair, which is where langchain-anthropic puts cache writes when it uses them
    # (and it zeroes the generic one in that case — see the module docstring).
    cache_creation = _as_int(details.get("cache_creation"))
    if not cache_creation:
        cache_creation = _as_int(details.get("ephemeral_5m_input_tokens")) + _as_int(
            details.get("ephemeral_1h_input_tokens")
        )
    return TokenCounts(
        input_tokens=_as_int(usage.get("input_tokens")),
        output_tokens=_as_int(usage.get("output_tokens")),
        cache_read=_as_int(details.get("cache_read")),
        cache_creation=cache_creation,
    )
