"""build_runtime's system_prompt_override — the seam the eval harness uses to
A/B J.A.R.N.'s harness prompt against a bare tool-using agent (same model/tools)."""

from __future__ import annotations

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
