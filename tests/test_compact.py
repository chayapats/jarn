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


def _summ_cfg(*, auto_compact: bool = True, compact_at_pct: int = 85) -> Config:
    """Config with a summarizer distinct from the main model."""
    cfg = Config(
        default_profile="p",
        providers={
            "p": ProviderConfig(
                type=ProviderType.OPENROUTER, api_key="x",
                base_url="http://localhost:9999/v1",
            )
        },
        routing=RoutingConfig(main="p/main-model", summarizer="p/summ-model"),
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


def test_build_passes_single_summarization_middleware(tmp_path):
    """build_runtime passes exactly one SummarizationMiddleware via middleware=,
    built on the resolved *summarizer* model (not the main model)."""
    from jarn.agent import builder

    main_fake, summ_fake = _fake_models()

    def _build(self, ref):
        return summ_fake if "summ" in ref else main_fake

    captured: dict = {}

    def _spy(*a, **kw):
        captured.update(kw)
        return object()  # build_runtime only stores the agent; never calls it

    with patch("jarn.providers.models.ModelFactory.build", _build), \
         patch("deepagents.create_deep_agent", _spy):
        builder.build_runtime(_summ_cfg(), project_root=tmp_path)

    mws = _summarization_mws(captured.get("middleware", ()))
    assert len(mws) == 1
    assert mws[0].model is summ_fake


def test_build_runtime_injects_single_summarization_middleware(tmp_path):
    """Real build (create_deep_agent NOT mocked): the built-in summarization is
    excluded and jarn's single instance survives in the fully assembled stack,
    on the summarizer model. Also a regression guard for the duplicate-middleware
    crash (create_agent rejects two middleware with the same .name)."""
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
        rt = builder.build_runtime(_summ_cfg(), project_root=tmp_path)

    assert type(rt.agent).__name__ == "CompiledStateGraph"
    mws = _summarization_mws(captured["middleware"])
    assert len(mws) == 1  # exactly one — built-in excluded, ours kept
    assert mws[0].model is summ_fake  # and it runs on the summarizer model


def test_build_no_summarization_when_auto_compact_off(tmp_path):
    """With context.auto_compact off, no summarization middleware is injected and
    the built-in stays excluded — the assembled stack has zero auto-summarization."""
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
        rt = builder.build_runtime(_summ_cfg(auto_compact=False), project_root=tmp_path)

    assert type(rt.agent).__name__ == "CompiledStateGraph"
    assert _summarization_mws(captured["middleware"]) == []
