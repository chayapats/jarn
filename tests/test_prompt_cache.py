"""Prompt caching (F1): provider strategy, Anthropic middleware wiring, local
keep-warm (Ollama keep_alive / LM Studio ttl), config validation, and a
system-prompt prefix-stability regression guard.

All model construction is mocked — no live calls.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

from jarn.config.loader import ConfigError, load_config
from jarn.config.schema import (
    Config,
    ProviderConfig,
    ProviderType,
    RoutingConfig,
)
from jarn.providers.models import ModelFactory, prompt_cache_strategy


def _cfg(ptype, main_ref, *, prompt_cache="auto", keep_alive=1800, base_url=None, api_key=None):
    return Config(
        default_profile="p",
        providers={"p": ProviderConfig(type=ptype, api_key=api_key, base_url=base_url)},
        routing=RoutingConfig(main=main_ref, prompt_cache=prompt_cache, keep_alive=keep_alive),
    )


# -- strategy mapping --------------------------------------------------------

@pytest.mark.parametrize(
    "ptype,expected",
    [
        (ProviderType.ANTHROPIC, "middleware"),
        (ProviderType.OPENAI, "server_auto"),
        (ProviderType.OPENROUTER, "server_auto"),
        (ProviderType.DEEPSEEK, "server_auto"),
        (ProviderType.GROQ, "server_auto"),
        (ProviderType.TOGETHER, "server_auto"),
        (ProviderType.FIREWORKS, "server_auto"),
        (ProviderType.XAI, "server_auto"),
        (ProviderType.GOOGLE, "server_auto"),
        (ProviderType.MISTRAL, "server_auto"),
        (ProviderType.OLLAMA, "ollama_keepalive"),
        (ProviderType.LMSTUDIO, "lmstudio_ttl"),
        (ProviderType.OPENAI_COMPATIBLE, "server_auto"),
    ],
)
def test_strategy_mapping(ptype, expected):
    assert prompt_cache_strategy(ptype) == expected


# -- local keep-warm injection ----------------------------------------------

def test_ollama_construct_sets_keep_alive():
    factory = ModelFactory(_cfg(ProviderType.OLLAMA, "p/qwen3", base_url="http://localhost:11434"))
    with patch("langchain.chat_models.init_chat_model") as m:
        m.return_value = object()
        factory.build("p/qwen3")
    _, kwargs = m.call_args
    assert kwargs.get("keep_alive") == 1800


def test_ollama_keep_alive_zero_left_to_provider():
    factory = ModelFactory(
        _cfg(ProviderType.OLLAMA, "p/qwen3", base_url="http://localhost:11434", keep_alive=0)
    )
    with patch("langchain.chat_models.init_chat_model") as m:
        m.return_value = object()
        factory.build("p/qwen3")
    _, kwargs = m.call_args
    assert "keep_alive" not in kwargs


def test_lmstudio_construct_sets_ttl():
    factory = ModelFactory(
        _cfg(ProviderType.LMSTUDIO, "p/qwen3", base_url="http://localhost:1234/v1")
    )
    with patch("langchain.chat_models.init_chat_model") as m:
        m.return_value = object()
        factory.build("p/qwen3")
    _, kwargs = m.call_args
    assert kwargs.get("extra_body", {}).get("ttl") == 1800


def test_prompt_cache_off_skips_local_keepalive():
    factory = ModelFactory(
        _cfg(ProviderType.OLLAMA, "p/qwen3", base_url="http://localhost:11434", prompt_cache="off")
    )
    with patch("langchain.chat_models.init_chat_model") as m:
        m.return_value = object()
        factory.build("p/qwen3")
    _, kwargs = m.call_args
    assert "keep_alive" not in kwargs


def test_lmstudio_ttl_does_not_clobber_user_extra_body():
    cfg = _cfg(ProviderType.LMSTUDIO, "p/qwen3", base_url="http://localhost:1234/v1")
    cfg.providers["p"].extra = {"extra_body": {"foo": "bar"}}
    factory = ModelFactory(cfg)
    with patch("langchain.chat_models.init_chat_model") as m:
        m.return_value = object()
        factory.build("p/qwen3")
    _, kwargs = m.call_args
    assert kwargs["extra_body"]["foo"] == "bar"
    assert kwargs["extra_body"]["ttl"] == 1800


# -- Anthropic middleware wiring in build_runtime ----------------------------

def _anthropic_cfg():
    return Config(
        default_profile="anthropic",
        providers={"anthropic": ProviderConfig(type=ProviderType.ANTHROPIC, api_key="sk-ant")},
        routing=RoutingConfig(main="anthropic/claude-opus-4-8"),
    )


def _build_capture(cfg, tmp_path):
    captured: dict = {}

    def fake_cda(**kwargs):
        captured.update(kwargs)
        return object()

    fake = GenericFakeChatModel(messages=iter([]))
    from jarn.agent import builder

    with patch("jarn.providers.models.ModelFactory.build", return_value=fake), patch(
        "deepagents.create_deep_agent", side_effect=fake_cda
    ):
        builder.build_runtime(cfg, project_root=tmp_path)
    return captured


def _has_caching_mw(middleware):
    from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware

    return any(isinstance(x, AnthropicPromptCachingMiddleware) for x in (middleware or ()))


def test_build_runtime_anthropic_adds_caching_middleware(tmp_path):
    captured = _build_capture(_anthropic_cfg(), tmp_path)
    assert _has_caching_mw(captured.get("middleware"))


def test_build_runtime_off_omits_middleware(tmp_path):
    cfg = _anthropic_cfg()
    cfg.routing.prompt_cache = "off"
    captured = _build_capture(cfg, tmp_path)
    assert not _has_caching_mw(captured.get("middleware"))


def test_build_runtime_server_auto_omits_middleware(base_config, tmp_path):
    # base_config main is openrouter/... -> server_auto, no Anthropic middleware.
    captured = _build_capture(base_config, tmp_path)
    assert not _has_caching_mw(captured.get("middleware"))


# -- config validation -------------------------------------------------------

def test_loader_accepts_prompt_cache_and_keep_alive(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("routing:\n  prompt_cache: off\n  keep_alive: 600\n")
    cfg = load_config(global_path=p, project_path=None)
    assert cfg.routing.prompt_cache == "off"
    assert cfg.routing.keep_alive == 600


def test_loader_rejects_bad_prompt_cache(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("routing:\n  prompt_cache: maybe\n")
    with pytest.raises(ConfigError):
        load_config(global_path=p, project_path=None)


def test_loader_rejects_negative_keep_alive(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("routing:\n  keep_alive: -5\n")
    with pytest.raises(ConfigError):
        load_config(global_path=p, project_path=None)


def test_routing_defaults():
    r = RoutingConfig()
    assert r.prompt_cache == "auto"
    assert r.keep_alive == 1800


# -- prefix stability regression --------------------------------------------

def test_system_prompt_is_pure_and_stable():
    """The cached prefix only pays off if the system prompt is byte-identical
    across turns. build_system_prompt must be pure, and the date stamp must be
    frozen for a fixed `now` (it's computed once at session build)."""
    from jarn.agent import prompts

    now = datetime(2026, 6, 17, 9, 0).astimezone()
    stamp = prompts.date_context(now)
    blocks = (stamp, "PROJECT CONTEXT", "SKILLS")
    assert prompts.build_system_prompt(*blocks) == prompts.build_system_prompt(*blocks)
    # The stamp itself is deterministic for a fixed instant (no per-call drift).
    assert prompts.date_context(now) == stamp
