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
