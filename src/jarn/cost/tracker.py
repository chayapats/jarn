"""Per-session token & cost accounting with budget enforcement.

The tracker accumulates usage across the main loop and all subagents, computes
USD cost from the pricing table, and exposes budget status so the UI can warn
(at ``warn_at_pct``) and the agent loop can hard-stop when configured.
"""

from __future__ import annotations

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

    def record(
        self,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        tool: str | None = None,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> Usage:
        """Record one model call; returns the updated total usage.

        Each call is attributed to exactly one ``per_model`` bucket *and* exactly
        one ``per_tool`` bucket (the tool this call requested, or ``RESPONSE_TOOL``
        for a plain reply). Attributing to a single tool bucket — never one per
        tool-call — keeps ``sum(per_tool) == total`` exactly, the same invariant
        ``per_model`` already holds, so the breakdown never double-counts.

        ``cache_read_tokens`` / ``cache_creation_tokens`` are the prompt-cache
        portions of this call's input; they are priced into the returned cost and
        tracked per bucket so ``/cost`` can surface cache reuse. When both are 0
        (no cache usage), totals are identical to the pre-cache behavior.
        """
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
        tool_bucket = self.per_tool.setdefault(tool or RESPONSE_TOOL, Usage())
        for u in (model_bucket, tool_bucket, self.total):
            u.input_tokens += input_tokens
            u.output_tokens += output_tokens
            u.cost_usd += cost
            u.calls += 1
            u.cache_read_tokens += cache_read_tokens
            u.cache_creation_tokens += cache_creation_tokens
            if unpriced:
                u.unpriced_calls += 1
        # Largest single prompt seen ~= current context fill (reset per thread).
        self.context_tokens = max(self.context_tokens, input_tokens)
        return self.total

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
        if not self.limit:
            return 0.0
        return self.total.cost_usd / self.limit

    def status(self) -> BudgetStatus:
        if not self.limit:
            return BudgetStatus.OK
        frac = self.fraction_used()
        if frac >= 1.0:
            return BudgetStatus.EXCEEDED
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
        t = self.total
        base = f"${t.cost_usd:.4f} · {t.total_tokens:,} tok · {t.calls} calls"
        if self.limit:
            base += f" · {self.fraction_used() * 100:.0f}% of ${self.limit:.2f}"
        if t.unpriced_calls:
            base += f" · {t.unpriced_calls} unpriced"
        if len(self.per_model) > 1:
            parts = [
                f"{model} ${u.cost_usd:.4f}"
                for model, u in self.per_model.items()
            ]
            base += " · " + ", ".join(parts)
        return base
