"""Cost tracking & budget tests."""

from __future__ import annotations

from jarn.config.schema import BudgetConfig
from jarn.cost import BudgetStatus, CostTracker
from jarn.cost.pricing import cost_of, lookup


def test_pricing_known_model():
    price = lookup("openrouter/anthropic/claude-opus-4-8")
    assert price is not None
    assert price.input_per_mtok == 5.0


def test_pricing_unknown_returns_none():
    assert lookup("some/unknown-model-xyz") is None


def test_cost_of_computation():
    # 1M input @5 + 1M output @25 = 30
    assert cost_of("claude-opus-4-8", 1_000_000, 1_000_000) == 30.0


def test_local_model_is_free():
    assert cost_of("ollama/qwen3-coder:30b", 1_000_000, 1_000_000) == 0.0


def test_context_window_curated_and_unknown():
    from jarn.cost.pricing import context_window

    assert context_window("openrouter/anthropic/claude-opus-4-8") == 200_000
    # Unknown model with no catalog cached -> 0 (caller hides the gauge).
    assert context_window("some/unknown-model-xyz") == 0


def test_catalog_fills_long_tail(tmp_path, monkeypatch):
    """A model absent from the curated tables is priced + sized from the catalog."""
    from jarn.cost import pricing

    monkeypatch.setenv("JARN_HOME", str(tmp_path))
    monkeypatch.setattr(pricing, "_MEM_CATALOG", {
        "deepseek/deepseek-v3.2": {"input": 0.27, "output": 1.1, "context": 163_840},
    })
    price = pricing.lookup("deepseek/deepseek-v3.2")
    assert price is not None and price.input_per_mtok == 0.27
    assert pricing.context_window("deepseek/deepseek-v3.2") == 163_840


def test_curated_anchor_beats_catalog(tmp_path, monkeypatch):
    """Curated values win over the catalog for the headline models (determinism)."""
    from jarn.cost import pricing

    monkeypatch.setenv("JARN_HOME", str(tmp_path))
    monkeypatch.setattr(pricing, "_MEM_CATALOG", {
        "anthropic/claude-opus-4-8": {"input": 99.0, "output": 99.0, "context": 1},
    })
    assert pricing.lookup("openrouter/anthropic/claude-opus-4-8").input_per_mtok == 5.0
    assert pricing.context_window("openrouter/anthropic/claude-opus-4-8") == 200_000


def test_tracker_accumulates():
    t = CostTracker()
    t.record("claude-opus-4-8", 1000, 500)
    t.record("claude-opus-4-8", 1000, 500)
    assert t.total.calls == 2
    assert t.total.input_tokens == 2000
    assert t.total.output_tokens == 1000


def test_budget_status_transitions():
    t = CostTracker(budget=BudgetConfig(per_session_usd=1.0, warn_at_pct=80))
    assert t.status() is BudgetStatus.OK
    t.record("claude-opus-4-8", 160_000, 0)  # $0.80 = 80%
    assert t.status() is BudgetStatus.WARN
    t.record("claude-opus-4-8", 50_000, 0)  # push over $1
    assert t.status() is BudgetStatus.EXCEEDED


def test_hard_stop():
    t = CostTracker(budget=BudgetConfig(per_session_usd=0.01, hard_stop=True))
    t.record("claude-opus-4-8", 1_000_000, 0)
    assert t.should_stop() is True


def test_no_hard_stop_when_disabled():
    t = CostTracker(budget=BudgetConfig(per_session_usd=0.01, hard_stop=False))
    t.record("claude-opus-4-8", 1_000_000, 0)
    assert t.should_stop() is False


def test_no_budget_never_stops():
    t = CostTracker(budget=BudgetConfig(per_session_usd=None))
    t.record("claude-opus-4-8", 10_000_000, 10_000_000)
    assert t.should_stop() is False
    assert t.status() is BudgetStatus.OK


def test_unpriced_tracked():
    t = CostTracker()
    t.record("mystery-model", 1000, 1000)
    assert t.total.unpriced_calls == 1
    assert "unpriced" in t.summary_line()


def test_per_model_accumulation():
    """Usage is bucketed per model id, independent of the running total."""
    t = CostTracker()
    t.record("claude-opus-4-8", 1000, 500)
    t.record("claude-haiku-4-5", 200, 100)
    t.record("claude-opus-4-8", 1000, 500)
    assert set(t.per_model) == {"claude-opus-4-8", "claude-haiku-4-5"}
    opus = t.per_model["claude-opus-4-8"]
    assert opus.calls == 2
    assert opus.input_tokens == 2000 and opus.output_tokens == 1000
    haiku = t.per_model["claude-haiku-4-5"]
    assert haiku.calls == 1 and haiku.input_tokens == 200
    # The total spans both models.
    assert t.total.calls == 3
    assert t.total.input_tokens == 2200 and t.total.output_tokens == 1100


def test_summary_line_single_model_has_no_breakdown():
    """A single recorded model must keep the original (no per-model) output."""
    t = CostTracker()
    t.record("claude-opus-4-8", 1000, 500)
    line = t.summary_line()
    assert "claude-opus-4-8" not in line  # no breakdown when only one model
    assert "tok" in line and "calls" in line


def test_summary_line_multi_model_breakdown():
    """Two+ models append a per-model breakdown without losing the base line."""
    t = CostTracker()
    t.record("claude-opus-4-8", 1_000_000, 0)   # priced -> $5.00
    t.record("claude-haiku-4-5", 1_000, 1_000)
    line = t.summary_line()
    assert "claude-opus-4-8 $" in line
    assert "claude-haiku-4-5 $" in line
    # The base aggregate line is still present.
    assert "calls" in line and "tok" in line


# -- P2.B: unpriced model warning -------------------------------------------

def test_unpriced_warning_emitted_once(recwarn):
    """cost_of emits UnpricedModelWarning exactly once per unknown model id."""
    from jarn.cost.pricing import _WARNED_UNPRICED, UnpricedModelWarning, cost_of

    model = "totally-unknown-model-p2b-test"
    _WARNED_UNPRICED.discard(model)  # reset dedup state

    result = cost_of(model, 1000, 1000)
    assert result is None  # unpriced -> None

    warns = [w for w in recwarn.list if issubclass(w.category, UnpricedModelWarning)]
    assert len(warns) == 1
    assert model in str(warns[0].message)
    assert "$0" in str(warns[0].message)

    # Second call must NOT emit another warning.
    cost_of(model, 2000, 2000)
    warns_after = [w for w in recwarn.list if issubclass(w.category, UnpricedModelWarning)]
    assert len(warns_after) == 1, "Warning should not repeat for the same model"


def test_unpriced_warning_dedup_per_model(recwarn):
    """Each distinct unknown model gets its own one-time warning."""
    from jarn.cost.pricing import _WARNED_UNPRICED, UnpricedModelWarning, cost_of

    for slug in ("unknown-alpha-p2b", "unknown-beta-p2b"):
        _WARNED_UNPRICED.discard(slug)

    cost_of("unknown-alpha-p2b", 1, 1)
    cost_of("unknown-beta-p2b", 1, 1)
    cost_of("unknown-alpha-p2b", 1, 1)  # repeat — must not re-warn

    warns = [w for w in recwarn.list if issubclass(w.category, UnpricedModelWarning)]
    slugs_warned = [str(w.message) for w in warns]
    assert any("unknown-alpha-p2b" in s for s in slugs_warned)
    assert any("unknown-beta-p2b" in s for s in slugs_warned)
    assert len(warns) == 2, "Two models → two warnings, no duplicates"


def test_priced_model_no_warning(recwarn):
    """No warning is emitted for a model whose price is known."""
    from jarn.cost.pricing import UnpricedModelWarning, cost_of  # noqa: F401

    result = cost_of("claude-opus-4-8", 1_000_000, 1_000_000)
    assert result is not None
    warns = [w for w in recwarn.list if issubclass(w.category, UnpricedModelWarning)]
    assert len(warns) == 0
