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

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(slots=True)
class BudgetExceeded(RuntimeError):
    spent: float
    limit: float

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"Session budget exceeded: ${self.spent:.4f} / ${self.limit:.2f}"


@dataclass(slots=True)
class CostTracker:
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    total: Usage = field(default_factory=Usage)
    per_model: dict[str, Usage] = field(default_factory=dict)
    context_tokens: int = 0

    def record(self, model_id: str, input_tokens: int, output_tokens: int) -> Usage:
        """Record one model call; returns the updated total usage."""
        cost = pricing.cost_of(model_id, input_tokens, output_tokens)
        unpriced = cost is None
        cost = cost or 0.0

        bucket = self.per_model.setdefault(model_id, Usage())
        for u in (bucket, self.total):
            u.input_tokens += input_tokens
            u.output_tokens += output_tokens
            u.cost_usd += cost
            u.calls += 1
            if unpriced:
                u.unpriced_calls += 1
        # Largest single prompt seen ~= current context fill (reset per thread).
        self.context_tokens = max(self.context_tokens, input_tokens)
        return self.total

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
