"""Cost tracking & budget tests."""

from __future__ import annotations

from jarn.config.schema import BudgetConfig
from jarn.cost import BudgetStatus, CostTracker, Usage
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


# -- P5.C: per-tool cost breakdown ------------------------------------------

def test_per_tool_attribution():
    """Cost is bucketed per tool name alongside per-model, with a default bucket
    for plain replies (no tool)."""
    from jarn.cost.tracker import RESPONSE_TOOL

    t = CostTracker()
    t.record("claude-opus-4-8", 1_000_000, 0, tool="execute")   # priced -> $5.00
    t.record("claude-opus-4-8", 1_000_000, 0, tool="execute")   # $5.00 again
    t.record("claude-opus-4-8", 1_000_000, 0, tool="web_fetch")  # $5.00
    t.record("claude-opus-4-8", 1_000, 500)                      # no tool -> reply
    assert set(t.per_tool) == {"execute", "web_fetch", RESPONSE_TOOL}
    execute = t.per_tool["execute"]
    assert execute.calls == 2 and execute.input_tokens == 2_000_000
    assert t.per_tool["web_fetch"].calls == 1
    assert t.per_tool[RESPONSE_TOOL].calls == 1


def test_per_tool_totals_reconcile_with_grand_total():
    """The per-tool totals must sum EXACTLY to the same grand total — no
    double-counting, no drift — and match the per-model totals too."""
    t = CostTracker()
    t.record("claude-opus-4-8", 1000, 500, tool="execute")
    t.record("claude-haiku-4-5", 200, 100, tool="read_file")
    t.record("claude-opus-4-8", 1000, 500, tool="execute")
    t.record("claude-opus-4-8", 7, 3)  # plain reply

    cost_via_tool = sum(u.cost_usd for u in t.per_tool.values())
    cost_via_model = sum(u.cost_usd for u in t.per_model.values())
    calls_via_tool = sum(u.calls for u in t.per_tool.values())
    in_via_tool = sum(u.input_tokens for u in t.per_tool.values())
    out_via_tool = sum(u.output_tokens for u in t.per_tool.values())

    assert cost_via_tool == t.total.cost_usd == cost_via_model
    assert calls_via_tool == t.total.calls == 4
    assert in_via_tool == t.total.input_tokens
    assert out_via_tool == t.total.output_tokens


def test_top_tools_ranks_by_cost():
    """top_tools returns the biggest cost contributors first, capped to limit."""
    t = CostTracker()
    t.record("claude-opus-4-8", 2_000_000, 0, tool="execute")    # $10.00
    t.record("claude-opus-4-8", 1_000_000, 0, tool="web_fetch")  # $5.00
    t.record("claude-opus-4-8", 100_000, 0, tool="read_file")    # $0.50
    top = t.top_tools(limit=2)
    assert [name for name, _ in top] == ["execute", "web_fetch"]
    assert top[0][1].cost_usd == 10.0


def test_per_tool_default_bucket_when_no_tool():
    """A call with no tool lands in the response bucket so totals still close."""
    from jarn.cost.tracker import RESPONSE_TOOL

    t = CostTracker()
    t.record("claude-opus-4-8", 1000, 500)
    assert list(t.per_tool) == [RESPONSE_TOOL]
    assert t.per_tool[RESPONSE_TOOL].cost_usd == t.total.cost_usd


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


# -- prompt-cache token tracking --------------------------------------------

def test_usage_cache_fields_default_zero():
    """Usage gains cache fields that default to 0 so existing call sites stay valid."""
    u = Usage()
    assert u.cache_read_tokens == 0
    assert u.cache_creation_tokens == 0


def test_record_captures_cache_tokens():
    """record() accumulates cache token counts into every bucket it touches."""
    t = CostTracker()
    t.record(
        "claude-opus-4-8", 1000, 500, tool="execute",
        cache_read_tokens=800, cache_creation_tokens=200,
    )
    assert t.total.cache_read_tokens == 800
    assert t.total.cache_creation_tokens == 200
    assert t.per_model["claude-opus-4-8"].cache_read_tokens == 800
    assert t.per_tool["execute"].cache_creation_tokens == 200


def test_cache_tokens_reconcile_across_buckets():
    """Cache token sums over per-tool / per-model buckets equal the grand total."""
    t = CostTracker()
    t.record("claude-opus-4-8", 1000, 500, cache_read_tokens=100, cache_creation_tokens=10)
    t.record("claude-haiku-4-5", 200, 100, cache_read_tokens=50, cache_creation_tokens=5)
    read_via_tool = sum(u.cache_read_tokens for u in t.per_tool.values())
    write_via_model = sum(u.cache_creation_tokens for u in t.per_model.values())
    assert read_via_tool == t.total.cache_read_tokens == 150
    assert write_via_model == t.total.cache_creation_tokens == 15


def test_no_cache_usage_leaves_totals_unchanged():
    """A turn with no cache usage records zero cache tokens and unchanged cost."""
    t = CostTracker()
    t.record("claude-opus-4-8", 1_000_000, 0)  # $5.00, no cache args
    assert t.total.cache_read_tokens == 0
    assert t.total.cache_creation_tokens == 0
    assert t.total.cost_usd == 5.0


# -- cache-aware pricing ----------------------------------------------------

def test_price_cache_rates_default_none():
    """Price gains optional cache rate fields defaulting to None (fall back to input)."""
    price = lookup("claude-opus-4-8")
    assert price is not None
    assert price.cache_read_rate is None
    assert price.cache_write_rate is None


def test_cost_of_unchanged_without_cache_tokens():
    """cost_of with no cache tokens matches the original input+output formula exactly."""
    # 1M input @5 + 1M output @25 = 30
    assert cost_of("claude-opus-4-8", 1_000_000, 1_000_000) == 30.0


def test_cost_of_cache_falls_back_to_input_rate():
    """With no explicit cache rates, cache tokens are priced at the input rate."""
    from jarn.cost.pricing import cost_of

    # opus input rate = $5/Mtok. 1M cache-read + 1M cache-creation -> $10 added.
    cost = cost_of(
        "claude-opus-4-8", 0, 0,
        cache_read_tokens=1_000_000, cache_creation_tokens=1_000_000,
    )
    assert cost == 10.0


def test_cost_of_does_not_double_charge_cached_input():
    """Regression: input_tokens is the FULL provider total (LangChain folds the
    cache counts back in), so the cached subset is repriced, not added on top.

    The bug billed cached tokens at the input rate AND again as a cache line."""
    from jarn.cost.pricing import cost_of

    # opus input $5/Mtok. Full input 1M, of which 0.8M is a cache read (no explicit
    # cache rate → cache also $5). Correct: plain 0.2M@5 + cache 0.8M@5 = $1 + $4 =
    # $5, i.e. exactly 1M@5 counted ONCE. The bug gave $9 (1M@5 + 0.8M@5).
    assert cost_of("claude-opus-4-8", 1_000_000, 0, cache_read_tokens=800_000) == 5.0


def test_cost_of_uses_explicit_cache_rates_when_present():
    """When a Price carries cache rates, they price cache tokens instead of input."""
    from jarn.cost import pricing
    from jarn.cost.pricing import Price, cost_of

    monkeypatched = {"my-cache-model": Price(10.0, 30.0, cache_read_rate=1.0, cache_write_rate=12.5)}
    orig = pricing._BUILTIN
    pricing._BUILTIN = {**orig, **monkeypatched}
    try:
        # 1M cache-read @1.0 + 1M cache-creation @12.5 = 13.5
        cost = cost_of(
            "my-cache-model", 0, 0,
            cache_read_tokens=1_000_000, cache_creation_tokens=1_000_000,
        )
        assert cost == 13.5
    finally:
        pricing._BUILTIN = orig
