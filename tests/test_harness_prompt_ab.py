"""build_runtime's system_prompt_override — the seam the eval harness uses to
A/B J.A.R.N.'s harness prompt against a bare tool-using agent (same model/tools)."""

from __future__ import annotations

from datetime import UTC
from unittest.mock import patch

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

from jarn.agent.builder import build_runtime
from jarn.config.schema import Config


def _build(tmp_path, override):
    cfg = Config()
    cfg.default_model = "openrouter/test-model"
    fake = GenericFakeChatModel(messages=iter([]))
    with patch("jarn.providers.models.ModelFactory.build", return_value=fake):
        return build_runtime(
            cfg, project_root=tmp_path, system_prompt_override=override
        )


def test_default_uses_jarn_persona(tmp_path):
    rt = _build(tmp_path, None)
    assert "Just A Reliable Nerd" in rt.system_prompt


def test_override_replaces_prompt_wholesale(tmp_path):
    rt = _build(tmp_path, "You are a coding assistant. Use the tools.")
    assert rt.system_prompt == "You are a coding assistant. Use the tools."
    assert "Reliable Nerd" not in rt.system_prompt


def test_empty_override_yields_empty_prompt(tmp_path):
    # "" is distinct from None: an explicit empty prompt (DeepAgents' own default
    # agent instructions still apply downstream — that's the "no harness" arm).
    rt = _build(tmp_path, "")
    assert rt.system_prompt == ""


def test_date_context_states_the_current_date():
    from datetime import datetime

    from jarn.agent.prompts import date_context

    block = date_context(datetime(2026, 6, 17, 9, 30, tzinfo=UTC))
    assert "2026-06-17" in block
    assert "Wednesday" in block
    assert "today" in block.lower()


def test_jarn_prompt_injects_the_current_date(tmp_path):
    """The JARN system prompt tells the agent today's date, so time-sensitive
    requests ("find today's news") aren't anchored to the training cutoff."""
    rt = _build(tmp_path, None)
    assert "Current date and time:" in rt.system_prompt


def test_override_arm_has_no_date_injection(tmp_path):
    """The eval baseline (override) stays the pure controlled prompt — no date —
    so the A/B isolates the harness prompt."""
    rt = _build(tmp_path, "You are a coding assistant. Use the tools.")
    assert "Current date and time:" not in rt.system_prompt
