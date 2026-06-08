"""Model ref parsing & factory mapping tests (init_chat_model is mocked)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from jarn.providers import (
    ModelFactory,
    ModelResolutionError,
    parse_model_ref,
    qualify_model_ref,
    strip_profile,
)


def test_parse_qualified_ref():
    ref = parse_model_ref("openrouter/anthropic/claude-opus-4-8")
    assert ref.profile == "openrouter"
    assert ref.model_id == "anthropic/claude-opus-4-8"


def test_parse_bare_with_default_profile():
    ref = parse_model_ref("claude-opus-4-8", default_profile="anthropic")
    assert ref.profile == "anthropic"
    assert ref.model_id == "claude-opus-4-8"


def test_parse_bare_without_default_raises():
    with pytest.raises(ModelResolutionError):
        parse_model_ref("claude-opus-4-8")


def test_qualify_prepends_provider():
    # The user's bug: an OpenRouter model id that itself contains a slash.
    assert qualify_model_ref("deepseek/deepseek-v4-flash", "openrouter") == \
        "openrouter/deepseek/deepseek-v4-flash"


def test_qualify_idempotent_when_already_prefixed():
    assert qualify_model_ref("openrouter/anthropic/claude", "openrouter") == \
        "openrouter/anthropic/claude"


def test_qualify_then_parse_routes_to_openrouter(base_config):
    ref = qualify_model_ref("deepseek/deepseek-v4-flash", "openrouter")
    parsed = parse_model_ref(ref, default_profile="openrouter")
    assert parsed.profile == "openrouter"
    assert parsed.model_id == "deepseek/deepseek-v4-flash"


def test_strip_profile():
    assert strip_profile("openrouter/deepseek/x", "openrouter") == "deepseek/x"
    assert strip_profile("deepseek/x", "openrouter") == "deepseek/x"


def test_factory_unknown_profile_raises(base_config):
    factory = ModelFactory(base_config)
    with pytest.raises(ModelResolutionError):
        factory.build("nosuchprofile/model")


def test_factory_openrouter_maps_to_openai_backend(base_config):
    factory = ModelFactory(base_config)
    with patch("langchain.chat_models.init_chat_model") as m:
        m.return_value = object()
        factory.build("openrouter/anthropic/claude-opus-4-8")
    _, kwargs = m.call_args
    assert kwargs["model_provider"] == "openai"
    assert kwargs["base_url"] == "http://localhost:9999/v1"
    assert kwargs["api_key"] == "sk-test"


def test_factory_ollama_maps_to_ollama_backend(base_config):
    factory = ModelFactory(base_config)
    with patch("langchain.chat_models.init_chat_model") as m:
        m.return_value = object()
        factory.build("ollama/qwen3-coder:30b")
    _, kwargs = m.call_args
    assert kwargs["model_provider"] == "ollama"
    assert "api_key" not in kwargs


def test_factory_caches(base_config):
    factory = ModelFactory(base_config)
    sentinel = object()
    with patch("langchain.chat_models.init_chat_model", return_value=sentinel) as m:
        a = factory.build("openrouter/x")
        b = factory.build("openrouter/x")
    assert a is b
    assert m.call_count == 1


def test_missing_key_wrapped_as_resolution_error(base_config, monkeypatch):
    base_config.providers["openrouter"].api_key = "${UNSET_VAR_ABC}"
    factory = ModelFactory(base_config)
    with pytest.raises(ModelResolutionError):
        factory.build("openrouter/x")
