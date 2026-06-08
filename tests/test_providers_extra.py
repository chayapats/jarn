"""Extended provider coverage (groq/deepseek/xai openai-compatible; google/mistral)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from jarn.config.schema import Config, ProviderConfig, ProviderType, RoutingConfig
from jarn.providers import ModelFactory


def _factory(ptype, **prov):
    cfg = Config(
        default_profile="p",
        providers={"p": ProviderConfig(type=ptype, **prov)},
        routing=RoutingConfig(main="p/model"),
    )
    return ModelFactory(cfg)


@pytest.mark.parametrize("ptype,base", [
    (ProviderType.GROQ, "https://api.groq.com/openai/v1"),
    (ProviderType.DEEPSEEK, "https://api.deepseek.com"),
    (ProviderType.TOGETHER, "https://api.together.xyz/v1"),
    (ProviderType.FIREWORKS, "https://api.fireworks.ai/inference/v1"),
    (ProviderType.XAI, "https://api.x.ai/v1"),
    (ProviderType.OPENAI_COMPATIBLE, "http://localhost:8000/v1"),
])
def test_openai_compatible_providers(ptype, base):
    factory = _factory(ptype, api_key="k", base_url=base)
    with patch("langchain.chat_models.init_chat_model") as m:
        m.return_value = object()
        factory.build("p/model")
    assert m.call_args.kwargs["model_provider"] == "openai"
    assert m.call_args.kwargs["base_url"] == base
    assert m.call_args.kwargs["api_key"] == "k"


@pytest.mark.parametrize("ptype,provider_str", [
    (ProviderType.GOOGLE, "google_genai"),
    (ProviderType.MISTRAL, "mistralai"),
])
def test_dedicated_providers(ptype, provider_str):
    factory = _factory(ptype, api_key="k")
    with patch("langchain.chat_models.init_chat_model") as m:
        m.return_value = object()
        factory.build("p/model")
    assert m.call_args.kwargs["model_provider"] == provider_str


def test_all_provider_types_have_defaults():
    from jarn.config.defaults import ALL_PROVIDERS, DEFAULT_MODELS, PROVIDER_ENV_VARS

    for p in ALL_PROVIDERS:
        assert p in DEFAULT_MODELS, f"{p} missing default models"
    # Every cloud provider should suggest an env var.
    from jarn.config.defaults import CLOUD_PROVIDERS
    for p in CLOUD_PROVIDERS:
        assert p in PROVIDER_ENV_VARS
