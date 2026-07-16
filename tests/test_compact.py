"""Tests for the summarize-and-continue /compact flow."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from jarn.tui.controller import Controller


class _FakeSummarizer:
    async def ainvoke(self, prompt):
        return AIMessage(content="SUMMARY: goal was X, changed a.py, next step Y.")


class _FakeAgent:
    def __init__(self, messages):
        self._messages = messages
        self.updated = None

    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": self._messages})

    async def aupdate_state(self, config, values):
        self.updated = values


def _controller(tmp_path, monkeypatch, messages):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    from jarn.config.schema import Config

    ctrl = Controller(Config(), root)
    ctrl.runtime = SimpleNamespace(
        agent=_FakeAgent(messages),
        factory=SimpleNamespace(
            build_summarizer=lambda: _FakeSummarizer(),
            build_main=lambda: _FakeSummarizer(),
        ),
        main_model_ref="x",
    )
    return ctrl


@pytest.mark.asyncio
async def test_manual_compact_still_forks_thread(tmp_path, monkeypatch):
    """Manual compaction (summarize + continue in a fresh thread) is unchanged by
    the move of *auto* compaction into the in-graph summarization middleware."""
    messages = [HumanMessage(content="fix the bug"), AIMessage(content="fixed it")]
    ctrl = _controller(tmp_path, monkeypatch, messages)
    old_thread = ctrl.thread_id

    summary = await ctrl.compact()

    assert "SUMMARY" in summary
    assert ctrl.thread_id != old_thread  # fresh thread
    # new thread seeded with the summary
    seeded = ctrl.runtime.agent.updated["messages"][0]
    assert isinstance(seeded, HumanMessage)
    assert "SUMMARY" in seeded.content
    ctrl.close()


@pytest.mark.asyncio
async def test_compact_empty_thread_returns_blank(tmp_path, monkeypatch):
    ctrl = _controller(tmp_path, monkeypatch, [])
    assert await ctrl.compact() == ""
    ctrl.close()


# -- P4.D: preview/apply split (manual /compact reviews before applying) -----


@pytest.mark.asyncio
async def test_compact_preview_generates_without_applying(tmp_path, monkeypatch):
    """compact_preview returns the summary but must NOT replace the thread."""
    messages = [HumanMessage(content="fix the bug"), AIMessage(content="fixed it")]
    ctrl = _controller(tmp_path, monkeypatch, messages)
    old_thread = ctrl.thread_id

    summary = await ctrl.compact_preview()

    assert "SUMMARY" in summary
    assert ctrl.thread_id == old_thread  # thread untouched
    assert ctrl.runtime.agent.updated is None  # nothing seeded/applied
    ctrl.close()


@pytest.mark.asyncio
async def test_compact_preview_empty_thread_returns_blank(tmp_path, monkeypatch):
    ctrl = _controller(tmp_path, monkeypatch, [])
    assert await ctrl.compact_preview() == ""
    ctrl.close()


# -- FIX C: bounded transcript (per-message cap + window trim) ----------------


class _CapturingSummarizer:
    """Records the prompt it is handed so tests can inspect what the summarizer
    actually received."""

    def __init__(self):
        self.prompt: str | None = None

    async def ainvoke(self, prompt):
        self.prompt = prompt
        return AIMessage(content="SUMMARY: ok.")


def _capturing_controller(tmp_path, monkeypatch, messages):
    ctrl = _controller(tmp_path, monkeypatch, messages)
    cap = _CapturingSummarizer()
    ctrl.runtime.factory = SimpleNamespace(
        build_summarizer=lambda: cap, build_main=lambda: cap
    )
    return ctrl, cap


@pytest.mark.asyncio
async def test_compact_preview_caps_oversized_message(tmp_path, monkeypatch):
    """A single huge message (e.g. a giant tool result) is capped per-message so it
    cannot dominate the summarizer prompt; the drop is announced inline."""
    body = "START" + ("x" * 5000) + "ENDMARKER"  # 5014 chars → capped at 4000
    ctrl, cap = _capturing_controller(
        tmp_path, monkeypatch, [HumanMessage(content=body)]
    )

    await ctrl.compact_preview()

    assert cap.prompt is not None
    assert "…[+1014 chars]" in cap.prompt  # 5014 - 4000 dropped chars announced
    assert "ENDMARKER" not in cap.prompt   # tail beyond the cap never sent
    ctrl.close()


@pytest.mark.asyncio
async def test_compact_preview_trims_long_thread_under_budget(tmp_path, monkeypatch):
    """A 150k+ token thread is window-trimmed so the summarizer prompt stays within
    budget (head + tail kept, middle dropped behind a marker) rather than overflowing
    the summarizer exactly when /compact is most needed."""
    from jarn.memory.tokens import count_tokens

    # summarizer runs on the unknown ref "x" → context_window 0 → 60_000 budget.
    messages = []
    for i in range(300):
        line = f"turn {i}: " + " ".join(f"token{i}_{j}" for j in range(400))
        messages.append(
            HumanMessage(content=line) if i % 2 == 0 else AIMessage(content=line)
        )
    ctrl, cap = _capturing_controller(tmp_path, monkeypatch, messages)

    await ctrl.compact_preview()

    assert cap.prompt is not None
    assert "…[trimmed" in cap.prompt  # the dropped middle is marked
    # The full untrimmed transcript is far over budget; the sent prompt is not.
    assert count_tokens(cap.prompt) <= 62_000  # ~60k budget + instruction preamble
    # Head (original goal) and tail (latest state) both survive the trim.
    assert "turn 0:" in cap.prompt
    assert "turn 299:" in cap.prompt
    ctrl.close()


def test_trim_to_window_never_exceeds_budget(monkeypatch):
    """BLOCKER 3: _trim_to_window GUARANTEES count_tokens(result) <= budget AND
    the shrink loop preserves the NEWEST lines (the 70% tail's whole purpose).

    Determinism: _trim_to_window imports count_tokens from jarn.memory.tokens, and
    the batch-shrink path only triggers under the len//4 fallback tokenizer (with
    tiktoken loaded the per-line accounting is exact and the loop never runs). We
    monkeypatch that counter to the len//4 approximation so the shrink path ALWAYS
    runs — otherwise the reviewer's bug (shrinking the tail from its END, dropping
    the newest lines) hides on machines where tiktoken loads."""
    import jarn.memory.tokens as tokens_mod
    from jarn.controller.core import _trim_to_window

    def _approx(text: str) -> int:
        return len(text) // 4  # non-additive under floor division → shrink triggers

    monkeypatch.setattr(tokens_mod, "count_tokens", _approx)

    budget = 60_000
    lines = [f"line {i}: " + " ".join(f"w{i}_{j}" for j in range(30)) for i in range(4000)]
    text = "\n".join(lines)
    assert _approx(text) > budget  # the input genuinely overflows

    result = _trim_to_window(text, budget)

    assert _approx(result) <= budget  # the invariant the reviewer's repro broke
    assert "…[trimmed" in result  # the dropped middle is marked
    assert result.strip()  # non-empty for non-empty input
    # The LAST source line survives — the newest state the tail exists to keep.
    assert "line 3999:" in result
    # Head (beginning) precedes tail (latest state).
    assert result.index("line 0:") < result.index("line 3999:")


def test_trim_to_window_single_oversized_line_char_cut():
    """A single line far over budget is hard-cut by chars (no newline to split on),
    still yielding non-empty content within budget."""
    from jarn.controller.core import _trim_to_window
    from jarn.memory.tokens import count_tokens

    budget = 1_000
    text = "x" * 500_000  # one giant line, no separators
    result = _trim_to_window(text, budget)
    assert count_tokens(result) <= budget
    assert result  # non-empty


def test_trim_to_window_char_cut_keeps_newest_suffix(monkeypatch):
    """BLOCKER 3: when no whole line fits, the char-cut fallback keeps the NEWEST
    SUFFIX (the newest-tail contract) — NOT the oldest prefix. Determinism: force
    the len//4 fallback tokenizer so the char-cut path always runs."""
    import jarn.memory.tokens as tokens_mod
    from jarn.controller.core import _trim_to_window

    def _approx(text: str) -> int:
        return len(text) // 4

    monkeypatch.setattr(tokens_mod, "count_tokens", _approx)

    budget = 50
    oldest = "OLDEST_MARKER " + "a" * 4000
    newest = "b" * 4000 + " NEWEST_MARKER"
    text = oldest + "\n" + newest  # two oversized "messages", neither line fits

    result = _trim_to_window(text, budget)

    assert _approx(result) <= budget  # within budget
    assert result.strip()  # non-empty for non-empty input
    assert "NEWEST_MARKER" in result  # newest content survives
    assert "OLDEST_MARKER" not in result  # oldest prefix discarded


def test_trim_to_window_zero_budget_never_empty(monkeypatch):
    """BLOCKER 3: a zero budget (int(window*0.6) can round to 0 for a tiny window)
    is treated as 1 and NEVER trims a non-empty input to empty."""
    import jarn.memory.tokens as tokens_mod
    from jarn.controller.core import _trim_to_window

    def _approx(text: str) -> int:
        return len(text) // 4

    monkeypatch.setattr(tokens_mod, "count_tokens", _approx)

    result = _trim_to_window("nonempty", 0)

    assert result  # non-empty in → non-empty out
    assert _approx(result) <= 1  # within the max(1, budget) floor


@pytest.mark.asyncio
async def test_compact_apply_seeds_fresh_thread(tmp_path, monkeypatch):
    """compact_apply starts a fresh thread seeded with the given summary."""
    ctrl = _controller(tmp_path, monkeypatch, [HumanMessage(content="x")])
    old_thread = ctrl.thread_id

    await ctrl.compact_apply("MY SUMMARY")

    assert ctrl.thread_id != old_thread
    seeded = ctrl.runtime.agent.updated["messages"][0]
    assert isinstance(seeded, HumanMessage)
    assert "MY SUMMARY" in seeded.content
    ctrl.close()


# -- T-1-1: single in-graph summarization path -------------------------------
#
# deepagents adds a SummarizationMiddleware unconditionally on the MAIN model at a
# fixed 85% trigger. T-1-1 replaces it with ONE jarn-configured instance on the
# SUMMARIZER model triggered at context.compact_at_pct: build_runtime registers a
# HarnessProfile that excludes the built-in and injects our own via middleware=.

from unittest.mock import patch  # noqa: E402

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel  # noqa: E402

from jarn.config.schema import (  # noqa: E402
    Config,
    ProviderConfig,
    ProviderType,
    RoutingConfig,
)


def _summ_cfg(
    *, auto_compact: bool = True, compact_at_pct: int = 85, main: str = "p/main-model"
) -> Config:
    """Config with a summarizer distinct from the main model.

    ``main`` picks the main-model ref; use a ref jarn's window table knows (e.g.
    ``p/claude-opus-4-8`` → 200k) to exercise the resolved-tokens trigger, or an
    unknown ref to exercise the deepagents-default fallback."""
    cfg = Config(
        default_profile="p",
        providers={
            "p": ProviderConfig(
                type=ProviderType.OPENROUTER, api_key="x",
                base_url="http://localhost:9999/v1",
            )
        },
        routing=RoutingConfig(main=main, summarizer="p/summ-model"),
    )
    cfg.context.auto_compact = auto_compact
    cfg.context.compact_at_pct = compact_at_pct
    return cfg


def _fake_models():
    """Distinct fakes so the injected middleware's model can be identified."""
    return GenericFakeChatModel(messages=iter([])), GenericFakeChatModel(messages=iter([]))


def _summarization_mws(middleware):
    from deepagents.middleware.summarization import SummarizationMiddleware

    return [m for m in middleware if isinstance(m, SummarizationMiddleware)]


def _real_build(cfg, tmp_path):
    """Run a real ``build_runtime`` (``create_agent`` NOT mocked) with distinct fake
    main/summarizer models. Returns ``(runtime, assembled_main_middleware, main_fake,
    summ_fake)``. The main agent's ``create_agent`` fires last, so the spy's captured
    ``middleware`` is the fully assembled main stack (post-exclusion)."""
    import deepagents.graph as g

    from jarn.agent import builder

    main_fake, summ_fake = _fake_models()

    def _build(self, ref):
        return summ_fake if "summ" in ref else main_fake

    captured: dict = {}
    real_create_agent = g.create_agent

    def _spy(*a, **kw):
        captured["middleware"] = kw.get("middleware", ())
        return real_create_agent(*a, **kw)

    with patch("jarn.providers.models.ModelFactory.build", _build), \
         patch.object(g, "create_agent", _spy):
        rt = builder.build_runtime(cfg, project_root=tmp_path)
    return rt, captured["middleware"], main_fake, summ_fake


def _gp_subagent_middleware(main_middleware):
    """The general-purpose subagent's assembled (post-exclusion) middleware list,
    read from the ``SubAgentMiddleware`` in the main stack — no subagent graph is
    compiled to inspect it."""
    sub_mw = next(
        m for m in main_middleware if type(m).__name__ == "SubAgentMiddleware"
    )
    gp = next(s for s in sub_mw._subagents if s["name"] == "general-purpose")
    return gp["middleware"]


def _trigger(mw):
    """The effective summarization trigger of a (Jarn)SummarizationMiddleware."""
    return mw._lc_helper.trigger


def _trim_limit(mw):
    """The trim_tokens_to_summarize setting of a (Jarn)SummarizationMiddleware."""
    return mw._lc_helper.trim_tokens_to_summarize


def test_build_runtime_injects_single_summarization_middleware(tmp_path):
    """Real build (create_deep_agent NOT mocked): the built-in summarization is
    excluded and jarn's single instance survives in the fully assembled stack,
    on the summarizer model. Also a regression guard for the duplicate-middleware
    crash (create_agent rejects two middleware with the same .name).

    trim_tokens_to_summarize must be None (not the 4000-token default) so the
    summarizer sees the FULL evicted history — mirroring create_summarization_middleware."""
    rt, main_mw, _main_fake, summ_fake = _real_build(_summ_cfg(), tmp_path)

    assert type(rt.agent).__name__ == "CompiledStateGraph"
    mws = _summarization_mws(main_mw)
    assert len(mws) == 1  # exactly one — built-in excluded, ours kept
    assert mws[0].model is summ_fake  # and it runs on the summarizer model
    assert _trim_limit(mws[0]) is None  # T-1-1 fix: full history, not 4k default


def test_build_no_summarization_when_auto_compact_off(tmp_path):
    """With context.auto_compact off, no summarization middleware is injected and
    the built-in stays excluded — the assembled stack has zero auto-summarization."""
    rt, main_mw, _main_fake, _summ_fake = _real_build(
        _summ_cfg(auto_compact=False), tmp_path
    )

    assert type(rt.agent).__name__ == "CompiledStateGraph"
    assert _summarization_mws(main_mw) == []


# -- T-1-1 fix round 1: resolve the trigger against jarn's OWN main window -----
#
# deepagents resolves a ("fraction", …) trigger against the SUMMARIZER model's
# window and raises for profile-less models, so it silently degrades to
# ("tokens", 170000) for jarn's OpenRouter defaults — making compact_at_pct inert.
# jarn instead computes ("tokens", pct% of the MAIN model's window) from its own
# window table (the ctx% gauge's source), and falls back to the deepagents default
# only when that window is unknown.


def test_auto_summarize_trigger_uses_main_window_tokens(tmp_path):
    """Finding 1: when jarn knows the MAIN model's window, the injected trigger is an
    explicit ("tokens", pct% of that window) — proving compact_at_pct actually drives
    auto-summarization instead of deepagents' inert fraction path."""
    cfg = _summ_cfg(main="p/claude-opus-4-8", compact_at_pct=50)  # 200k window
    _, main_mw, _main_fake, summ_fake = _real_build(cfg, tmp_path)

    mws = _summarization_mws(main_mw)
    assert len(mws) == 1
    assert mws[0].model is summ_fake
    assert _trigger(mws[0]) == ("tokens", 100_000)  # 50% of 200k


def test_auto_summarize_trigger_falls_back_when_window_unknown(tmp_path):
    """Finding 1: an unknown main window falls back to deepagents' computed default
    trigger (no crash, no silent fraction)."""
    cfg = _summ_cfg(main="p/mystery-model-xyz", compact_at_pct=50)
    _, main_mw, _main_fake, _summ_fake = _real_build(cfg, tmp_path)

    mws = _summarization_mws(main_mw)
    assert len(mws) == 1
    assert _trigger(mws[0]) == ("tokens", 170_000)  # deepagents windowless default


def test_gp_subagent_keeps_summarization(tmp_path):
    """Finding 2: the auto-added general-purpose subagent must NOT lose auto-
    summarization. Excluding deepagents' built-in on the main-model key also strips
    it from the GP subagent (it shares the main model's profile); jarn re-injects via
    the profile's extra_middleware so the GP stack keeps exactly one summarization,
    on the summarizer model, with the same resolved trigger."""
    cfg = _summ_cfg(main="p/claude-opus-4-8", compact_at_pct=50)
    _, main_mw, _main_fake, summ_fake = _real_build(cfg, tmp_path)

    gp_summ = _summarization_mws(_gp_subagent_middleware(main_mw))
    assert len(gp_summ) == 1
    assert gp_summ[0].model is summ_fake
    assert _trigger(gp_summ[0]) == ("tokens", 100_000)


def test_gp_subagent_no_summarization_when_auto_compact_off(tmp_path):
    """With auto_compact off, the GP subagent has zero summarization (built-in
    excluded, nothing re-injected) — the same honest 'off' state as the main agent,
    with no double-summarization creeping back in via deepagents' defaults."""
    cfg = _summ_cfg(auto_compact=False, main="p/claude-opus-4-8")
    _, main_mw, _main_fake, _summ_fake = _real_build(cfg, tmp_path)

    assert _summarization_mws(_gp_subagent_middleware(main_mw)) == []
