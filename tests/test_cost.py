"""Cost tracking & budget tests."""

from __future__ import annotations

import logging
from contextlib import contextmanager

import pytest

from jarn.config.schema import BudgetConfig
from jarn.cost import BudgetStatus, CostTracker, Usage
from jarn.cost.pricing import Price, cost_of, lookup

# T-1-2 test constants: a main-model ref and a subagent ref.
_MAIN = "anthropic/claude-opus-4"
_SUB = "openrouter/anthropic/claude-haiku-4-5"


@pytest.fixture()
def tracker() -> CostTracker:
    return CostTracker()


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


def test_unpriced_hard_stop_warns():
    """A hard-capped budget with unpriced calls must at least WARN: $0-accrued
    unpriced spend is unknown and could hide overspend the hard stop can't bind."""
    t = CostTracker(budget=BudgetConfig(per_session_usd=10.0, hard_stop=True))
    t.record("mystery-model", 1000, 1000)  # unpriced -> $0 accrued
    assert t.total.unpriced_calls == 1
    assert t.status() is BudgetStatus.WARN


def test_unpriced_without_hard_stop_stays_ok():
    """Without a hard stop, an unpriced call under-limit is not forced to WARN."""
    t = CostTracker(budget=BudgetConfig(per_session_usd=10.0, hard_stop=False))
    t.record("mystery-model", 1000, 1000)
    assert t.total.unpriced_calls == 1
    assert t.status() is BudgetStatus.OK


def test_unpriced_hard_stop_exceeded_still_exceeded():
    """The EXCEEDED verdict outranks the unpriced WARN when the priced spend is over."""
    t = CostTracker(budget=BudgetConfig(per_session_usd=0.01, hard_stop=True))
    t.record("claude-opus-4-8", 1_000_000, 0)  # $5.00 priced, over the $0.01 cap
    t.record("mystery-model", 1000, 1000)       # plus an unpriced call
    assert t.status() is BudgetStatus.EXCEEDED


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


# -- P2.B: unpriced model notice (routed to the jarn log, NOT stderr) -------


@contextmanager
def _capture_cost_logs():
    """Capture WARNING records on the jarn.cost logger directly, so the assertion
    doesn't depend on log propagation/handler setup elsewhere in the suite."""
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    lg = logging.getLogger("jarn.cost")
    lg.addHandler(handler)
    prev = lg.level
    lg.setLevel(logging.WARNING)
    try:
        yield records
    finally:
        lg.removeHandler(handler)
        lg.setLevel(prev)


def test_unpriced_notice_logged_once():
    """cost_of logs the unpriced notice exactly once per unknown model id — to the
    jarn logger (file), never warnings.warn (which leaks into the TUI display)."""
    from jarn.cost.pricing import _WARNED_UNPRICED, cost_of

    model = "totally-unknown-model-p2b-test"
    _WARNED_UNPRICED.discard(model)  # reset dedup state

    with _capture_cost_logs() as records:
        assert cost_of(model, 1000, 1000) is None  # unpriced -> None
        msgs = [r.getMessage() for r in records]
        assert len(msgs) == 1
        assert model in msgs[0] and "$0" in msgs[0]

        cost_of(model, 2000, 2000)  # repeat — must NOT re-log
        assert len(records) == 1, "notice should not repeat for the same model"


def test_unpriced_notice_dedup_per_model():
    """Each distinct unknown model gets its own one-time notice."""
    from jarn.cost.pricing import _WARNED_UNPRICED, cost_of

    for slug in ("unknown-alpha-p2b", "unknown-beta-p2b"):
        _WARNED_UNPRICED.discard(slug)

    with _capture_cost_logs() as records:
        cost_of("unknown-alpha-p2b", 1, 1)
        cost_of("unknown-beta-p2b", 1, 1)
        cost_of("unknown-alpha-p2b", 1, 1)  # repeat — must not re-log
        msgs = [r.getMessage() for r in records]

    assert any("unknown-alpha-p2b" in s for s in msgs)
    assert any("unknown-beta-p2b" in s for s in msgs)
    assert len(msgs) == 2, "two models → two notices, no duplicates"


def test_priced_model_no_notice():
    """No notice is logged for a model whose price is known."""
    from jarn.cost.pricing import cost_of

    with _capture_cost_logs() as records:
        assert cost_of("claude-opus-4-8", 1_000_000, 1_000_000) is not None
    assert records == []


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
    """Price cache-rate fields default to None (fall back to input) when a source omits them."""
    assert Price(1.0, 2.0).cache_read_rate is None
    assert Price(1.0, 2.0).cache_write_rate is None


def test_claude_anchor_declares_cache_rates():
    """Claude anchors now carry explicit cache rates (read 0.1x, write 1.25x input)."""
    price = lookup("openrouter/anthropic/claude-opus-4-8")
    assert price is not None
    assert price.cache_read_rate == 0.5   # 0.1 x 5.0 input
    assert price.cache_write_rate == 6.25  # 1.25 x 5.0 input


def test_cost_of_cache_uses_anchor_rates():
    """Cache tokens on a claude anchor price at its explicit rates, not the input rate."""
    # opus input $5/Mtok → cache_read 0.5, cache_write 6.25.
    # 1M cache-read @0.5 + 1M cache-creation @6.25 = 6.75.
    cost = cost_of(
        "claude-opus-4-8", 0, 0,
        cache_read_tokens=1_000_000, cache_creation_tokens=1_000_000,
    )
    assert cost == 6.75


def test_cost_of_unchanged_without_cache_tokens():
    """cost_of with no cache tokens matches the original input+output formula exactly."""
    # 1M input @5 + 1M output @25 = 30
    assert cost_of("claude-opus-4-8", 1_000_000, 1_000_000) == 30.0


def test_cost_of_cache_falls_back_to_input_rate():
    """With no explicit cache rates, cache tokens are priced at the input rate."""
    from jarn.cost import pricing
    from jarn.cost.pricing import cost_of

    # A rate-less anchor: input $5/Mtok, no cache rates → cache priced at input.
    # 1M cache-read + 1M cache-creation -> $10 added.
    orig = pricing._BUILTIN
    pricing._BUILTIN = {**orig, "rateless-model": Price(5.0, 25.0)}
    try:
        cost = cost_of(
            "rateless-model", 0, 0,
            cache_read_tokens=1_000_000, cache_creation_tokens=1_000_000,
        )
        assert cost == 10.0
    finally:
        pricing._BUILTIN = orig


def test_cost_of_does_not_double_charge_cached_input():
    """Regression: input_tokens is the FULL provider total (LangChain folds the
    cache counts back in), so the cached subset is repriced, not added on top.

    The bug billed cached tokens at the input rate AND again as a cache line."""
    from jarn.cost import pricing
    from jarn.cost.pricing import cost_of

    # Rate-less anchor input $5/Mtok. Full input 1M, of which 0.8M is a cache read
    # (no explicit cache rate → cache also $5). Correct: plain 0.2M@5 + cache
    # 0.8M@5 = $1 + $4 = $5, i.e. exactly 1M@5 counted ONCE. The bug gave $9.
    orig = pricing._BUILTIN
    pricing._BUILTIN = {**orig, "rateless-model": Price(5.0, 25.0)}
    try:
        assert cost_of("rateless-model", 1_000_000, 0, cache_read_tokens=800_000) == 5.0
    finally:
        pricing._BUILTIN = orig


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


def test_streaming_usage_dedup():
    """Cumulative usage on repeated chunks records final totals once, not N×."""
    from types import SimpleNamespace

    from jarn.agent.session import SessionDriver
    from jarn.cost import CostTracker

    tracker = CostTracker()
    driver = SessionDriver(
        agent=None,
        engine=None,  # type: ignore[arg-type]
        tracker=tracker,
        thread_id="t1",
    )

    for i in range(1, 11):
        cumulative_in = i * 100
        cumulative_out = i * 10
        msg = SimpleNamespace(
            usage_metadata={
                "input_tokens": cumulative_in,
                "output_tokens": cumulative_out,
                "input_token_details": {},
            },
            response_metadata={},
            tool_calls=[],
            tool_call_chunks=[],
        )
        driver._record_usage(msg)

    assert tracker.total.input_tokens == 1000
    assert tracker.total.output_tokens == 100
    assert tracker.total.calls == 1


def test_concurrent_record():
    """Concurrent record() calls produce deterministic totals under the tracker lock."""
    import threading

    tracker = CostTracker()
    errors: list[str] = []

    def _worker() -> None:
        try:
            for _ in range(50):
                tracker.record("claude-opus-4-8", 10, 5, tool="execute")
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    assert tracker.total.calls == 200
    assert tracker.total.input_tokens == 2000
    assert tracker.total.output_tokens == 1000


def test_parallel_tool_cost_split():
    """Parallel tool calls split per-tool attribution evenly."""
    tracker = CostTracker()
    tracker.record(
        "claude-opus-4-8",
        1000,
        200,
        tools=["execute", "read_file"],
    )
    assert tracker.total.input_tokens == 1000
    assert tracker.per_tool["execute"].input_tokens == 500
    assert tracker.per_tool["read_file"].input_tokens == 500
    cost_sum = sum(u.cost_usd for u in tracker.per_tool.values())
    assert abs(cost_sum - tracker.total.cost_usd) < 1e-9


def test_pricing_network_opt_out(monkeypatch, tmp_path):
    """Network pricing fetch is skipped when config/env disables it."""
    from jarn.cost import pricing

    monkeypatch.setenv("JARN_HOME", str(tmp_path))
    fetched: list[int] = []
    monkeypatch.setattr(
        pricing,
        "_fetch_openrouter",
        lambda: fetched.append(1) or {"x/y": {"input": 1.0, "output": 2.0, "context": 0}},
    )
    monkeypatch.setattr(pricing, "_disk_cache_fresh", lambda: False)

    pricing.warm_catalog(force=True, network=False)
    assert fetched == []

    pricing.warm_catalog(force=True, network=True)
    assert fetched == [1]

    fetched.clear()
    monkeypatch.setenv("JARN_NO_NETWORK_PRICING", "1")
    pricing.warm_catalog(force=True, network=True)
    assert fetched == []

    assert pricing.lookup("claude-opus-4-8") is not None


# -- override cache rates + mtime-memoized loader ---------------------------


def test_price_override_with_cache_keys(tmp_path, monkeypatch):
    """pricing.yaml entries may declare optional cache_read/cache_write; they flow
    into the resolved Price (absent → None)."""
    from jarn.cost import pricing

    monkeypatch.setenv("JARN_HOME", str(tmp_path))
    pricing._YAML_CACHE.clear()
    (tmp_path / "pricing.yaml").write_text(
        "custom-model:\n"
        "  input: 2.0\n"
        "  output: 8.0\n"
        "  cache_read: 0.2\n"
        "  cache_write: 2.5\n",
        encoding="utf-8",
    )
    price = pricing.lookup("vendor/custom-model")
    assert price is not None
    assert price.input_per_mtok == 2.0
    assert price.cache_read_rate == 0.2
    assert price.cache_write_rate == 2.5


def test_price_override_without_cache_keys_leaves_none(tmp_path, monkeypatch):
    """An override omitting cache keys resolves to None cache rates (input fallback)."""
    from jarn.cost import pricing

    monkeypatch.setenv("JARN_HOME", str(tmp_path))
    pricing._YAML_CACHE.clear()
    (tmp_path / "pricing.yaml").write_text(
        "plain-model:\n  input: 1.0\n  output: 3.0\n", encoding="utf-8"
    )
    price = pricing.lookup("plain-model")
    assert price is not None
    assert price.cache_read_rate is None and price.cache_write_rate is None


def test_yaml_cache_invalidates_on_mtime_change(tmp_path, monkeypatch):
    """_cached_load re-parses only when st_mtime_ns changes; a same-mtime rewrite
    is served from cache, and a new mtime (even within one second) invalidates."""
    import os

    from jarn.cost import pricing

    monkeypatch.setenv("JARN_HOME", str(tmp_path))
    pricing._YAML_CACHE.clear()
    pf = tmp_path / "pricing.yaml"

    pf.write_text("mtok-model:\n  input: 1.0\n  output: 2.0\n", encoding="utf-8")
    os.utime(pf, ns=(1_000_000_000, 1_000_000_000))
    assert pricing.lookup("mtok-model").input_per_mtok == 1.0

    # New content, SAME mtime_ns → memoized old value is served (proves caching).
    pf.write_text("mtok-model:\n  input: 5.0\n  output: 2.0\n", encoding="utf-8")
    os.utime(pf, ns=(1_000_000_000, 1_000_000_000))
    assert pricing.lookup("mtok-model").input_per_mtok == 1.0

    # New content, NEW mtime_ns → cache invalidates. Jump a full 2 seconds, not
    # 1 ns: NTFS quantizes timestamps to 100 ns ticks (and FAT to 2 s), so a
    # sub-tick bump reads back as the SAME st_mtime_ns on Windows CI.
    pf.write_text("mtok-model:\n  input: 9.0\n  output: 2.0\n", encoding="utf-8")
    os.utime(pf, ns=(3_000_000_000, 3_000_000_000))
    assert pricing.lookup("mtok-model").input_per_mtok == 9.0


def test_price_override_parse_error_logged(tmp_path, monkeypatch):
    """A malformed pricing.yaml is swallowed (no overrides) but names the file in a log."""
    from jarn.cost import pricing

    monkeypatch.setenv("JARN_HOME", str(tmp_path))
    pricing._YAML_CACHE.clear()
    (tmp_path / "pricing.yaml").write_text("model: [unterminated\n", encoding="utf-8")
    with _capture_cost_logs() as records:
        assert pricing._load_price_overrides() == {}
    assert any("pricing.yaml" in r.getMessage() for r in records)


def test_price_override_bad_cache_value_skips_entry_keeps_rest(tmp_path, monkeypatch):
    """A bad cache_read on one entry skips only that entry (with a warning) — the
    other entries still resolve, and a lookup never raises. Regression: the float()
    of the optional cache rates used to run outside the parse boundary."""
    from jarn.cost import pricing

    monkeypatch.setenv("JARN_HOME", str(tmp_path))
    pricing._YAML_CACHE.clear()
    (tmp_path / "pricing.yaml").write_text(
        "bad-model:\n"
        "  input: 1.0\n"
        "  output: 3.0\n"
        "  cache_read: nope\n"
        "good-model:\n"
        "  input: 2.0\n"
        "  output: 8.0\n",
        encoding="utf-8",
    )
    with _capture_cost_logs() as records:
        # Must not raise (pre-fix this raised ValueError on every lookup).
        assert pricing.lookup("bad-model") is None
        good = pricing.lookup("good-model")
    assert good is not None and good.input_per_mtok == 2.0
    assert any("pricing.yaml" in r.getMessage() for r in records)


def test_window_override_bad_value_skips_entry_keeps_rest(tmp_path, monkeypatch):
    """A non-numeric window value skips only that entry (with a warning); the rest survive."""
    from jarn.cost import pricing

    monkeypatch.setenv("JARN_HOME", str(tmp_path))
    pricing._YAML_CACHE.clear()
    (tmp_path / "context_windows.yaml").write_text(
        "bad-win: nope\ngood-win: 128000\n", encoding="utf-8"
    )
    with _capture_cost_logs() as records:
        assert pricing.context_window("bad-win") == 0
        assert pricing.context_window("good-win") == 128_000
    assert any("bad-win" in r.getMessage() for r in records)


def test_catalog_malformed_entry_skipped_healthy_survive():
    """One malformed catalog record (context_length: "unknown") is skipped with a
    warning — the healthy entries still parse; the whole catalog is not discarded."""
    from jarn.cost import pricing

    payload = {"data": [
        {"id": "vendor/bad", "pricing": {"prompt": "0.000001"}, "context_length": "unknown"},
        {"id": "vendor/good", "pricing": {"prompt": "0.000002", "completion": "0.000004"},
         "context_length": 1000},
    ]}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    import sys
    import types

    fake_httpx = types.ModuleType("httpx")
    fake_httpx.get = lambda *a, **k: _Resp()  # type: ignore[attr-defined]
    monkey = pytest.MonkeyPatch()
    monkey.setitem(sys.modules, "httpx", fake_httpx)
    try:
        with _capture_cost_logs() as records:
            cat = pricing._fetch_openrouter()
    finally:
        monkey.undo()
    assert "vendor/bad" not in cat
    assert cat["vendor/good"]["context"] == 1000
    assert cat["vendor/good"]["input"] == pytest.approx(2.0)
    assert any("vendor/bad" in r.getMessage() for r in records)


def test_price_override_list_root_returns_empty_no_crash(tmp_path, monkeypatch):
    """A valid-YAML non-mapping root (a top-level list) yields {} + a warning
    naming the file — never an AttributeError from raw.items(), and lookups still
    work (fall through to builtin/catalog)."""
    from jarn.cost import pricing

    monkeypatch.setenv("JARN_HOME", str(tmp_path))
    pricing._YAML_CACHE.clear()
    (tmp_path / "pricing.yaml").write_text("- not-a-mapping\n", encoding="utf-8")
    with _capture_cost_logs() as records:
        assert pricing._load_price_overrides() == {}  # no exception
        # A builtin lookup still resolves (overrides simply contributed nothing).
        assert pricing.lookup("claude-opus-4-8") is not None
    assert any("pricing.yaml" in r.getMessage() for r in records)


def test_price_override_numeric_key_skipped_others_survive(tmp_path, monkeypatch):
    """A numeric YAML key (``1:``) is skipped with a warning — it would otherwise be
    admitted and crash every _match_substr lookup (``k in model_id``). Other entries
    survive and lookups still work."""
    from jarn.cost import pricing

    monkeypatch.setenv("JARN_HOME", str(tmp_path))
    pricing._YAML_CACHE.clear()
    (tmp_path / "pricing.yaml").write_text(
        "1:\n  input: 1.0\n  output: 2.0\n"
        "good-model:\n  input: 2.0\n  output: 8.0\n",
        encoding="utf-8",
    )
    with _capture_cost_logs() as records:
        # Must not raise (pre-fix: `1 in model_id` -> TypeError on every lookup).
        good = pricing.lookup("good-model")
    assert good is not None and good.input_per_mtok == 2.0
    assert any("pricing.yaml" in r.getMessage() for r in records)


def test_window_override_list_root_returns_empty_no_crash(tmp_path, monkeypatch):
    """A non-mapping context_windows.yaml root yields {} + a warning, not a crash."""
    from jarn.cost import pricing

    monkeypatch.setenv("JARN_HOME", str(tmp_path))
    pricing._YAML_CACHE.clear()
    (tmp_path / "context_windows.yaml").write_text("- 1\n- 2\n", encoding="utf-8")
    with _capture_cost_logs() as records:
        assert pricing._load_window_overrides() == {}
        assert pricing.context_window("claude-opus-4-8") == 200_000
    assert any("context-window" in r.getMessage() for r in records)


def test_catalog_non_mapping_element_skipped_healthy_survive():
    """A non-mapping element in the catalog ``data`` list is skipped — the whole
    catalog is not aborted, and the healthy entry still parses. Regression: id
    extraction ran before the per-entry try, so one bad element crashed the fetch."""
    from jarn.cost import pricing

    payload = {"data": [
        "not-a-mapping",
        {"id": "vendor/good", "pricing": {"prompt": "0.000002", "completion": "0.000004"},
         "context_length": 1000},
    ]}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    import sys
    import types

    fake_httpx = types.ModuleType("httpx")
    fake_httpx.get = lambda *a, **k: _Resp()  # type: ignore[attr-defined]
    monkey = pytest.MonkeyPatch()
    monkey.setitem(sys.modules, "httpx", fake_httpx)
    try:
        with _capture_cost_logs() as records:
            cat = pricing._fetch_openrouter()  # must not raise
    finally:
        monkey.undo()
    assert cat["vendor/good"]["context"] == 1000
    assert cat["vendor/good"]["input"] == pytest.approx(2.0)
    assert any(r.getMessage() for r in records)  # the bad element was warned


@pytest.mark.parametrize("bad", [".nan", ".inf", "-1"])
def test_price_override_nonfinite_cache_read_skipped(tmp_path, monkeypatch, bad):
    """A NaN/inf/negative cache_read is skipped with a warning — the entry does not
    resolve (so a poisoned rate can never enter cost)."""
    from jarn.cost import pricing

    monkeypatch.setenv("JARN_HOME", str(tmp_path))
    pricing._YAML_CACHE.clear()
    (tmp_path / "pricing.yaml").write_text(
        f"poison-model:\n  input: 5.0\n  output: 5.0\n  cache_read: {bad}\n",
        encoding="utf-8",
    )
    with _capture_cost_logs() as records:
        assert pricing.lookup("poison-model") is None
    assert any("pricing.yaml" in r.getMessage() for r in records)


def test_nonfinite_cache_override_does_not_disable_hard_stop(tmp_path, monkeypatch):
    """A .nan cache_read on the opus override would poison cost to NaN, making every
    budget comparison False so a $1 hard stop NEVER fires. The fix skips the bad
    override -> opus resolves to its finite builtin price -> cost stays finite and
    the cap still EXCEEDs once spend passes $1."""
    import math

    from jarn.config.schema import BudgetConfig
    from jarn.cost import CostTracker, pricing

    monkeypatch.setenv("JARN_HOME", str(tmp_path))
    pricing._YAML_CACHE.clear()
    (tmp_path / "pricing.yaml").write_text(
        "claude-opus-4-8:\n  input: 5.0\n  output: 25.0\n  cache_read: .nan\n",
        encoding="utf-8",
    )
    t = CostTracker(budget=BudgetConfig(per_session_usd=1.0, hard_stop=True))
    t.record("claude-opus-4-8", 1_000_000, 0, cache_read_tokens=500_000)  # ~$2.75
    assert math.isfinite(t.total.cost_usd)
    assert t.should_stop() is True
    assert t.status() is BudgetStatus.EXCEEDED


def test_catalog_cache_rates_flow_into_price(tmp_path, monkeypatch):
    """OpenRouter per-model cache rates carry through the catalog into the Price."""
    from jarn.cost import pricing

    monkeypatch.setenv("JARN_HOME", str(tmp_path))
    monkeypatch.setattr(pricing, "_MEM_CATALOG", {
        "vendor/cached-model": {
            "input": 1.0, "output": 4.0, "context": 1000,
            "cache_read": 0.1, "cache_write": 1.25,
        },
    })
    price = pricing.lookup("vendor/cached-model")
    assert price is not None
    assert price.cache_read_rate == 0.1
    assert price.cache_write_rate == 1.25


def test_openrouter_parses_per_token_cache_rates():
    """_fetch_openrouter converts input_cache_read/write per-token USD to $/Mtok."""
    from jarn.cost import pricing

    payload = {"data": [{
        "id": "vendor/m",
        "pricing": {
            "prompt": "0.000001", "completion": "0.000004",
            "input_cache_read": "0.0000001", "input_cache_write": "0.00000125",
        },
        "context_length": 1000,
    }]}

    class _Resp:
        def raise_for_status(self):  # noqa: D401
            pass

        def json(self):
            return payload

    import sys
    import types

    fake_httpx = types.ModuleType("httpx")
    fake_httpx.get = lambda *a, **k: _Resp()  # type: ignore[attr-defined]
    monkey = pytest.MonkeyPatch()
    monkey.setitem(sys.modules, "httpx", fake_httpx)
    try:
        cat = pricing._fetch_openrouter()
    finally:
        monkey.undo()
    entry = cat["vendor/m"]
    assert entry["cache_read"] == pytest.approx(0.1)    # 0.0000001 * 1e6
    assert entry["cache_write"] == pytest.approx(1.25)  # 0.00000125 * 1e6


def test_openrouter_absent_cache_fields_stay_none():
    """When the catalog omits cache pricing, the entry's cache rates stay None."""
    from jarn.cost import pricing

    payload = {"data": [{
        "id": "vendor/nocache",
        "pricing": {"prompt": "0.000001", "completion": "0.000004"},
        "context_length": 1000,
    }]}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    import sys
    import types

    fake_httpx = types.ModuleType("httpx")
    fake_httpx.get = lambda *a, **k: _Resp()  # type: ignore[attr-defined]
    monkey = pytest.MonkeyPatch()
    monkey.setitem(sys.modules, "httpx", fake_httpx)
    try:
        cat = pricing._fetch_openrouter()
    finally:
        monkey.undo()
    entry = cat["vendor/nocache"]
    assert entry["cache_read"] is None and entry["cache_write"] is None


def test_openrouter_fetch_failure_logs_warning():
    """A failed catalog fetch is logged (not silently swallowed) and returns {}."""
    import sys
    import types

    from jarn.cost import pricing

    fake_httpx = types.ModuleType("httpx")

    def _boom(*a, **k):
        raise RuntimeError("network down")

    fake_httpx.get = _boom  # type: ignore[attr-defined]
    monkey = pytest.MonkeyPatch()
    monkey.setitem(sys.modules, "httpx", fake_httpx)
    try:
        with _capture_cost_logs() as records:
            assert pricing._fetch_openrouter() == {}
    finally:
        monkey.undo()
    assert any("fetch failed" in r.getMessage() for r in records)


# -- T-1-2: accurate context gauge (assignment not max; is_main flag) ---------


# -- budget boundary: a CONFIGURED $0 limit is a real cap, not "no limit" ----


def test_zero_dollar_limit_is_exceeded_and_stops():
    """A configured $0 hard cap means nothing may be spent: any session (even $5
    of real spend) is EXCEEDED and should_stop — ``not self.limit`` used to treat
    a $0 limit as 'no limit' and report OK / never stop."""
    t = CostTracker(budget=BudgetConfig(per_session_usd=0.0, hard_stop=True))
    t.record("claude-opus-4-8", 1_000_000, 0)  # $5.00 under a $0 cap
    assert t.status() is BudgetStatus.EXCEEDED
    assert t.should_stop() is True
    assert t.fraction_used() == 1.0


def test_zero_dollar_limit_exceeded_even_at_zero_spend():
    """Nothing may be spent under a $0 cap — EXCEEDED even before any spend."""
    t = CostTracker(budget=BudgetConfig(per_session_usd=0.0, hard_stop=True))
    assert t.status() is BudgetStatus.EXCEEDED
    assert t.fraction_used() == 1.0


def test_none_limit_is_unset_not_a_cap():
    """``None`` (no limit configured) stays OK / 0% used regardless of spend."""
    t = CostTracker(budget=BudgetConfig(per_session_usd=None))
    t.record("claude-opus-4-8", 10_000_000, 0)
    assert t.status() is BudgetStatus.OK
    assert t.fraction_used() == 0.0
    assert t.should_stop() is False


# -- B2: a FALSY non-mapping YAML root warns + yields {} (not silent {}) -------


def test_price_override_falsy_scalar_root_warns(tmp_path, monkeypatch):
    """A falsy non-mapping root (``0``/``false``) must warn + yield {} — the old
    ``yaml.safe_load(...) or {}`` coerced it to {} without the required warning."""
    from jarn.cost import pricing

    monkeypatch.setenv("JARN_HOME", str(tmp_path))
    pricing._YAML_CACHE.clear()
    (tmp_path / "pricing.yaml").write_text("false\n", encoding="utf-8")
    with _capture_cost_logs() as records:
        assert pricing._load_price_overrides() == {}
        assert pricing.lookup("claude-opus-4-8") is not None  # falls through to builtin
    assert any("pricing.yaml" in r.getMessage() for r in records)


def test_price_override_incomplete_entry_warns(tmp_path, monkeypatch):
    """A dict entry missing the required output key warns + skips (not silent)."""
    from jarn.cost import pricing

    monkeypatch.setenv("JARN_HOME", str(tmp_path))
    pricing._YAML_CACHE.clear()
    (tmp_path / "pricing.yaml").write_text(
        "incomplete-model:\n  input: 1.0\n"
        "good-model:\n  input: 2.0\n  output: 8.0\n",
        encoding="utf-8",
    )
    with _capture_cost_logs() as records:
        assert pricing.lookup("incomplete-model") is None
        assert pricing.lookup("good-model").input_per_mtok == 2.0
    assert any("incomplete-model" in r.getMessage() for r in records)


# -- B3: an invalid REQUIRED catalog rate SKIPS the entry (stays unpriced) -----


def test_catalog_negative_required_rate_skips_entry_model_unpriced():
    """A negative prompt rate must SKIP the whole entry so the model stays UNPRICED
    (counted by the tracker) — it used to be admitted at $0.0/$0.0, leaving a
    hard-stop budget OK under unlimited usage."""
    from jarn.cost import pricing

    payload = {"data": [
        {"id": "vendor/negative", "pricing": {"prompt": "-0.1", "completion": "0.1"},
         "context_length": 1000},
        {"id": "vendor/good", "pricing": {"prompt": "0.000002", "completion": "0.000004"},
         "context_length": 1000},
    ]}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    import sys
    import types

    fake_httpx = types.ModuleType("httpx")
    fake_httpx.get = lambda *a, **k: _Resp()  # type: ignore[attr-defined]
    monkey = pytest.MonkeyPatch()
    monkey.setitem(sys.modules, "httpx", fake_httpx)
    try:
        with _capture_cost_logs() as records:
            cat = pricing._fetch_openrouter()
    finally:
        monkey.undo()
    assert "vendor/negative" not in cat  # skipped, not priced at $0
    assert cat["vendor/good"]["input"] == pytest.approx(2.0)
    assert any("vendor/negative" in r.getMessage() for r in records)


def test_valid_rate_rejects_bool():
    """float(False)==0.0 / float(True)==1.0 would admit a bool as a valid rate;
    reject bools explicitly so a bool REQUIRED rate is skipped, never priced."""
    from jarn.cost.pricing import _valid_rate

    with pytest.raises(ValueError):
        _valid_rate(True)
    with pytest.raises(ValueError):
        _valid_rate(False)
    # A genuine present-zero rate still parses to $0 (free-model price).
    assert _valid_rate(0) == 0.0
    assert _valid_rate("0") == 0.0


def _fetch_with_payload(payload: dict):
    """Run _fetch_openrouter against a fake httpx returning *payload*, capturing logs."""
    from jarn.cost import pricing

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    import sys
    import types

    fake_httpx = types.ModuleType("httpx")
    fake_httpx.get = lambda *a, **k: _Resp()  # type: ignore[attr-defined]
    monkey = pytest.MonkeyPatch()
    monkey.setitem(sys.modules, "httpx", fake_httpx)
    try:
        with _capture_cost_logs() as records:
            cat = pricing._fetch_openrouter()
    finally:
        monkey.undo()
    return cat, records


def test_catalog_missing_required_prompt_skips_entry_unpriced():
    """An entry with ONLY a completion rate (``prompt`` key absent) must SKIP so the
    model stays UNPRICED — the old ``pr.get("prompt", 0) or 0`` coerced the missing
    rate to a valid $0, silently pricing an unknown model at $0 and leaving a
    hard-stop budget OK under unlimited real usage."""
    cat, records = _fetch_with_payload({"data": [
        {"id": "vendor/only-completion", "pricing": {"completion": "0.1"},
         "context_length": 1000},
        {"id": "vendor/good", "pricing": {"prompt": "0.000002", "completion": "0.000004"},
         "context_length": 1000},
    ]})
    assert "vendor/only-completion" not in cat  # skipped, not priced at $0
    assert cat["vendor/good"]["input"] == pytest.approx(2.0)
    assert any("vendor/only-completion" in r.getMessage() for r in records)


def test_catalog_present_zero_rate_is_priced_as_free_model():
    """A PRESENT rate of "0" is a legitimate free-model price (OpenRouter ``:free``
    variants) and must stay PRICED at $0 — missing means unknown, present-zero
    means free. This is the distinction the presence check must preserve."""
    cat, _records = _fetch_with_payload({"data": [
        {"id": "vendor/free", "pricing": {"prompt": "0", "completion": "0"},
         "context_length": 1000},
    ]})
    assert "vendor/free" in cat  # present-zero -> priced, NOT skipped
    assert cat["vendor/free"]["input"] == 0.0
    assert cat["vendor/free"]["output"] == 0.0


def test_catalog_bool_required_rate_skips_entry():
    """A boolean required rate is skipped (float(False)==0.0 would price at $0)."""
    cat, _records = _fetch_with_payload({"data": [
        {"id": "vendor/boolrate", "pricing": {"prompt": False, "completion": "0.1"},
         "context_length": 1000},
    ]})
    assert "vendor/boolrate" not in cat


def test_window_override_zero_or_negative_skipped(tmp_path, monkeypatch):
    """A 0 / negative context window is invalid (a <=0 window collapses the compact
    budget to 0); it is skipped with a warning while positive windows survive."""
    from jarn.cost import pricing

    monkeypatch.setenv("JARN_HOME", str(tmp_path))
    pricing._YAML_CACHE.clear()
    (tmp_path / "context_windows.yaml").write_text(
        "zero-win: 0\nneg-win: -5\ngood-win: 128000\n", encoding="utf-8"
    )
    with _capture_cost_logs() as records:
        overrides = pricing._load_window_overrides()
    assert "zero-win" not in overrides
    assert "neg-win" not in overrides
    assert overrides["good-win"] == 128_000
    assert any("zero-win" in r.getMessage() for r in records)


def test_catalog_nonstring_id_skipped():
    """A non-string id must be skipped (would crash substring lookups otherwise)."""
    from jarn.cost import pricing

    payload = {"data": [
        {"id": 123, "pricing": {"prompt": "0.1", "completion": "0.1"}},
        {"id": "vendor/good", "pricing": {"prompt": "0.000002", "completion": "0.000004"},
         "context_length": 1000},
    ]}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    import sys
    import types

    fake_httpx = types.ModuleType("httpx")
    fake_httpx.get = lambda *a, **k: _Resp()  # type: ignore[attr-defined]
    monkey = pytest.MonkeyPatch()
    monkey.setitem(sys.modules, "httpx", fake_httpx)
    try:
        with _capture_cost_logs() as records:
            cat = pricing._fetch_openrouter()
    finally:
        monkey.undo()
    assert 123 not in cat and "123" not in cat
    assert "vendor/good" in cat
    assert any(r.getMessage() for r in records)


# -- B4: a poisoned legacy disk cache is validated, not trusted wholesale ------


def test_poisoned_disk_cache_entry_dropped_cost_finite(tmp_path, monkeypatch):
    """A legacy on-disk cache with a JSON NaN rate must have the poisoned entry
    dropped (with a warning) — it used to bypass ALL validation and poison cost to
    NaN forever, silently disabling the budget. Healthy entries still resolve."""
    import math

    from jarn.cost import pricing

    monkeypatch.setenv("JARN_HOME", str(tmp_path))
    monkeypatch.setattr(pricing, "_MEM_CATALOG", None)
    cache = tmp_path / "cache" / "openrouter_models.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    # Raw JSON NaN token (json.loads accepts it by default) — the poison.
    cache.write_text(
        '{"vendor/poison": {"input": NaN, "output": 1.0, "context": 100}, '
        '"vendor/good": {"input": 1.0, "output": 2.0, "context": 100}}',
        encoding="utf-8",
    )
    with _capture_cost_logs() as records:
        assert pricing.lookup("vendor/poison") is None  # dropped
        good = pricing.lookup("vendor/good")
        assert good is not None and good.input_per_mtok == 1.0
        cost = cost_of("vendor/good", 1_000_000, 0)
    assert cost is not None and math.isfinite(cost)
    assert any("vendor/poison" in r.getMessage() for r in records)


def test_catalog_negative_rate_unpriced_budget_warns():
    """End-to-end: a catalog model with an invalid required rate stays UNPRICED, so
    a hard-stop budget takes the unpriced-WARN path rather than a silent $0 OK."""
    with pricing_mem_catalog({
        "vendor/negative": {"input": float("nan"), "output": 1.0, "context": 100},
    }) as valid:
        # The poisoned entry is dropped by the validator -> model unpriced.
        assert "vendor/negative" not in valid
        t = CostTracker(budget=BudgetConfig(per_session_usd=10.0, hard_stop=True))
        t.record("vendor/negative", 1_000_000, 0)
        assert t.total.unpriced_calls == 1
        assert t.status() is BudgetStatus.WARN


@contextmanager
def pricing_mem_catalog(raw: dict):
    """Install a validated in-memory catalog for the body; yields the validated dict."""
    from jarn.cost import pricing

    valid = pricing._validate_catalog(raw)
    prev = pricing._MEM_CATALOG
    pricing._MEM_CATALOG = valid
    try:
        yield valid
    finally:
        pricing._MEM_CATALOG = prev


def test_context_gauge_tracks_latest_prompt_not_max(tracker: CostTracker) -> None:
    """After summarization the prompt shrinks; the gauge must drop to the latest value."""
    tracker.record(model_id=_MAIN, input_tokens=10_000, output_tokens=1, is_main=True)
    tracker.record(model_id=_MAIN, input_tokens=2_000, output_tokens=1, is_main=True)
    assert tracker.context_tokens == 2_000  # latest wins, not max


def test_subagent_calls_do_not_move_gauge(tracker: CostTracker) -> None:
    """Subagent traffic (is_main=False) must not inflate the ctx% gauge."""
    tracker.record(model_id=_SUB, input_tokens=50_000, output_tokens=1, is_main=False)
    assert tracker.context_tokens == 0


def test_gauge_includes_cache_tokens(tracker: CostTracker) -> None:
    """prompt_tokens = input + cache_read + cache_creation; the gauge tracks that sum."""
    tracker.record(
        model_id=_MAIN,
        input_tokens=1_000,
        output_tokens=1,
        cache_read_tokens=800,
        cache_creation_tokens=200,
        is_main=True,
    )
    assert tracker.context_tokens == 1_000 + 800 + 200


# -- T-1-2 final-review: gauge must not be clobbered by zero-input chunks ----


def test_gauge_not_clobbered_by_continuation_chunk(tracker: CostTracker) -> None:
    """Continuation chunk with input=0 (cumulative input unchanged) must not reset the gauge.

    Providers that stream cumulative totals resend the same input count on every
    chunk; after dedup the delta has input=0, output>0.  The gauge must keep the
    value set by the first (real-prompt) chunk.
    """
    tracker.record(model_id=_MAIN, input_tokens=5_000, output_tokens=0, is_main=True)
    assert tracker.context_tokens == 5_000
    # Continuation: only new output tokens in this delta
    tracker.record(model_id=_MAIN, input_tokens=0, output_tokens=200, is_main=True)
    assert tracker.context_tokens == 5_000  # must stay 5000, not drop to 0


def test_gauge_not_clobbered_by_split_output_chunk(tracker: CostTracker) -> None:
    """Anthropic-style split: message_start carries input=8000 output=0, final chunk
    carries input=0 output=500 (non-monotonic new-call path).  The gauge must hold the
    value from the message_start chunk.
    """
    tracker.record(model_id=_MAIN, input_tokens=8_000, output_tokens=0, is_main=True)
    assert tracker.context_tokens == 8_000
    # Output-only final chunk: non-monotonic path passes input=0 to record()
    tracker.record(model_id=_MAIN, input_tokens=0, output_tokens=500, is_main=True)
    assert tracker.context_tokens == 8_000  # must stay 8000, not drop to 0
