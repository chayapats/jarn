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
async def test_compact_summarizes_and_starts_new_thread(tmp_path, monkeypatch):
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
