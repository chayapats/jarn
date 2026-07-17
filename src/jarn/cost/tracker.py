"""Per-session token & cost accounting with budget enforcement.

The tracker accumulates usage across the main loop and all subagents, computes
USD cost from the pricing table, and exposes budget status so the UI can warn
(at ``warn_at_pct``) and the agent loop can hard-stop when configured.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum

from jarn.config.schema import BudgetConfig
from jarn.cost import pricing


class BudgetStatus(str, Enum):
    OK = "ok"
    WARN = "warn"
    EXCEEDED = "exceeded"


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    unpriced_calls: int = 0
    # Prompt-cache tokens (subset of the provider's reported input). Tracked
    # separately so the breakdown can show cache reuse; they are already priced
    # into ``cost_usd`` by ``pricing.cost_of``.
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(slots=True)
class BudgetExceeded(RuntimeError):
    spent: float
    limit: float

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"Session budget exceeded: ${self.spent:.4f} / ${self.limit:.2f}"


# Bucket label for model calls that produced a plain answer rather than a tool
# call (so per-tool totals still reconcile to the grand total).
RESPONSE_TOOL = "(response)"


@dataclass(slots=True)
class CostTracker:
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    total: Usage = field(default_factory=Usage)
    per_model: dict[str, Usage] = field(default_factory=dict)
    per_tool: dict[str, Usage] = field(default_factory=dict)
    context_tokens: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(
        self,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        tool: str | None = None,
        tools: list[str] | None = None,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        increment_call: bool = True,
        is_main: bool = False,
    ) -> Usage:
        """Record one model call; returns the updated total usage.

        Each call is attributed to exactly one ``per_model`` bucket. Cost is
        split evenly across ``tools`` when a turn batches parallel tool calls;
        otherwise it lands in a single ``per_tool`` bucket (or ``RESPONSE_TOOL``
        for a plain reply). Splitting keeps ``sum(per_tool) == total`` exactly.

        ``cache_read_tokens`` / ``cache_creation_tokens`` are the prompt-cache
        portions of this call's input; they are priced into the returned cost and
        tracked per bucket so ``/cost`` can surface cache reuse. When both are 0
        (no cache usage), totals are identical to the pre-cache behavior.

        ``is_main`` must be ``True`` when this call is attributed to the *main*
        model (not a subagent or summarizer). Only then is the ctx% gauge updated.
        The attribution is resolved at the call site (``stream_handlers.record_usage``)
        by comparing the message's reported model ref to ``driver.main_model_ref``.
        Limitation: a same-model subagent shares the main model's ref and therefore
        cannot be distinguished here — the caller documents this where relevant.
        """
        tool_names = [t for t in (tools or []) if t]
        if not tool_names:
            tool_names = [tool or RESPONSE_TOOL]

        n = len(tool_names)
        in_each, in_rem = divmod(input_tokens, n)
        out_each, out_rem = divmod(output_tokens, n)
        cache_r_each, cache_r_rem = divmod(cache_read_tokens, n)
        cache_w_each, cache_w_rem = divmod(cache_creation_tokens, n)
        call_each, call_rem = divmod(1, n)

        with self._lock:
            self._record_aggregate(
                model_id,
                input_tokens,
                output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_creation_tokens=cache_creation_tokens,
                increment_call=increment_call,
                is_main=is_main,
            )
            for i, tool_name in enumerate(tool_names):
                extra_in = 1 if i < in_rem else 0
                extra_out = 1 if i < out_rem else 0
                extra_cr = 1 if i < cache_r_rem else 0
                extra_cw = 1 if i < cache_w_rem else 0
                calls_add = call_each + (1 if i < call_rem else 0)
                self._record_tool_share(
                    tool_name,
                    in_each + extra_in,
                    out_each + extra_out,
                    calls_add if increment_call else 0,
                    cache_read_tokens=cache_r_each + extra_cr,
                    cache_creation_tokens=cache_w_each + extra_cw,
                    model_id=model_id,
                )
            return self.total

    def _record_aggregate(
        self,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        *,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        increment_call: bool = True,
        is_main: bool = False,
    ) -> None:
        """Update session total and per-model buckets (caller holds ``_lock``)."""
        cost = pricing.cost_of(
            model_id,
            input_tokens,
            output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
        )
        unpriced = cost is None
        cost = cost or 0.0

        model_bucket = self.per_model.setdefault(model_id, Usage())
        for u in (model_bucket, self.total):
            u.input_tokens += input_tokens
            u.output_tokens += output_tokens
            u.cost_usd += cost
            if increment_call:
                u.calls += 1
            u.cache_read_tokens += cache_read_tokens
            u.cache_creation_tokens += cache_creation_tokens
            if unpriced and increment_call:
                u.unpriced_calls += 1
        # Update the ctx% gauge only for main-model calls (assignment so the gauge
        # drops correctly after summarization shrinks the prompt; subagent traffic
        # must not inflate it).  Guard: only update when the prompt is non-zero —
        # continuation chunks (cumulative input unchanged → delta input = 0) and
        # Anthropic-style split chunks (output-only final chunk, input = 0 on the
        # non-monotonic new-call path) must not clobber a previously recorded value.
        if is_main and (p := input_tokens + cache_read_tokens + cache_creation_tokens) > 0:
            self.context_tokens = p

    def _record_tool_share(
        self,
        tool: str,
        input_tokens: int,
        output_tokens: int,
        calls: int,
        *,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        model_id: str,
    ) -> None:
        """Attribute a share of one call to a per-tool bucket (caller holds ``_lock``)."""
        cost = pricing.cost_of(
            model_id,
            input_tokens,
            output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
        )
        unpriced = cost is None
        cost = cost or 0.0

        tool_bucket = self.per_tool.setdefault(tool, Usage())
        tool_bucket.input_tokens += input_tokens
        tool_bucket.output_tokens += output_tokens
        tool_bucket.cost_usd += cost
        tool_bucket.calls += calls
        tool_bucket.cache_read_tokens += cache_read_tokens
        tool_bucket.cache_creation_tokens += cache_creation_tokens
        if unpriced:
            tool_bucket.unpriced_calls += calls

    def top_tools(self, limit: int = 5) -> list[tuple[str, Usage]]:
        """The biggest per-tool cost contributors, most expensive first."""
        ranked = sorted(
            self.per_tool.items(), key=lambda kv: kv[1].cost_usd, reverse=True
        )
        return ranked[:limit]

    # -- budget -------------------------------------------------------------

    @property
    def limit(self) -> float | None:
        return self.budget.per_session_usd

    def fraction_used(self) -> float:
        with self._lock:
            # ``None`` means "no limit configured"; a CONFIGURED $0 limit is a real
            # constraint ("nothing may be spent"), so it is fully used at 100% —
            # ``not self.limit`` would wrongly conflate the two.
            if self.limit is None:
                return 0.0
            if self.limit == 0:
                return 1.0
            return self.total.cost_usd / self.limit

    def status(self) -> BudgetStatus:
        with self._lock:
            if self.limit is None:
                return BudgetStatus.OK
            # A configured $0 hard cap means nothing may be spent — any session is
            # already EXCEEDED (and avoids a divide-by-zero below).
            if self.limit == 0:
                return BudgetStatus.EXCEEDED
            frac = self.total.cost_usd / self.limit
            if frac >= 1.0:
                return BudgetStatus.EXCEEDED
            # Invariant: unpriced calls accrue $0, so the real spend is unknown —
            # a hard-capped budget must at least WARN rather than report OK, since
            # a silent $0 could hide true overspend the hard stop can never bind.
            if self.total.unpriced_calls > 0 and self.budget.hard_stop:
                return BudgetStatus.WARN
            if frac * 100 >= self.budget.warn_at_pct:
                return BudgetStatus.WARN
            return BudgetStatus.OK

    def should_stop(self) -> bool:
        """True when a hard stop must be enforced before the next model call."""
        return (
            self.limit is not None
            and self.budget.hard_stop
            and self.status() is BudgetStatus.EXCEEDED
        )

    def check_or_raise(self) -> None:
        if self.should_stop():
            raise BudgetExceeded(spent=self.total.cost_usd, limit=self.limit or 0.0)

    def summary_line(self) -> str:
        """Compact one-line summary for the status bar / ``/cost``.

        When more than one model has been recorded (e.g. a subagent or the
        summarizer used a different model than the main loop), a per-model
        breakdown is appended so the cost can be attributed. With a single
        model the output is unchanged.
        """
        with self._lock:
            t = self.total
            base = f"${t.cost_usd:.4f} · {t.total_tokens:,} tok · {t.calls} calls"
            if self.limit:
                frac = t.cost_usd / self.limit
                base += f" · {frac * 100:.0f}% of ${self.limit:.2f}"
            if t.unpriced_calls:
                base += f" · {t.unpriced_calls} unpriced"
            if len(self.per_model) > 1:
                parts = [
                    f"{model} ${u.cost_usd:.4f}"
                    for model, u in self.per_model.items()
                ]
                base += " · " + ", ".join(parts)
            return base
