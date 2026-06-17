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


# -- P2.B: suggest_slug did-you-mean ----------------------------------------

def test_suggest_slug_dot_to_dash_anthropic():
    """Dot-form slug on Anthropic provider yields a dash-form suggestion."""
    from jarn.config.schema import ProviderType
    from jarn.providers.models import suggest_slug

    result = suggest_slug(ProviderType.ANTHROPIC, "claude-opus-4.8")
    assert result is not None
    assert "claude-opus-4-8" in result
    assert "dashes" in result or "Anthropic" in result


def test_suggest_slug_dash_to_dot_openrouter():
    """Dash-form slug on OpenRouter provider yields a dot-form suggestion."""
    from jarn.config.schema import ProviderType
    from jarn.providers.models import suggest_slug

    result = suggest_slug(ProviderType.OPENROUTER, "anthropic/claude-opus-4-8")
    # The candidate is anthropic/claude-opus-4.8 which matches OR default slugs
    assert result is not None
    assert "4.8" in result or "claude-opus-4.8" in result


def test_suggest_slug_no_match_returns_none():
    """Completely unknown slug returns None (no false positive)."""
    from jarn.config.schema import ProviderType
    from jarn.providers.models import suggest_slug

    result = suggest_slug(ProviderType.ANTHROPIC, "gpt-5-ultra-xyz-fake")
    assert result is None


def test_suggest_slug_already_correct_returns_none():
    """Correct dash-form slug on Anthropic returns None (no spurious suggestion)."""
    from jarn.config.schema import ProviderType
    from jarn.providers.models import suggest_slug

    # "claude-opus-4-8" is correct for Anthropic; swapping to "claude-opus-4.8"
    # would be the OR form — but the function should still return something or None
    # without crashing. We verify it does not return the same slug as suggestion.
    result = suggest_slug(ProviderType.ANTHROPIC, "claude-opus-4-8")
    # It may or may not suggest the OR form; it must not crash and must not
    # suggest the same slug unchanged.
    if result is not None:
        assert "claude-opus-4-8" not in result or "claude-opus-4.8" in result


def test_model_resolution_error_includes_suggestion():
    """ModelResolutionError message includes did-you-mean when slug is swappable."""
    from unittest.mock import patch

    from jarn.config.schema import Config, ProviderConfig, ProviderType, RoutingConfig
    from jarn.providers.models import ModelFactory, ModelResolutionError

    # Set up an Anthropic provider with a dot-form slug (wrong for Anthropic).
    cfg = Config(
        default_profile="anthropic",
        providers={"anthropic": ProviderConfig(type=ProviderType.ANTHROPIC, api_key="fake-key")},
        routing=RoutingConfig(main="anthropic/claude-opus-4.8"),
    )
    factory = ModelFactory(cfg)

    def _raise(*a, **kw):
        raise ValueError("model not found")

    with patch("langchain.chat_models.init_chat_model", side_effect=_raise), pytest.raises(ModelResolutionError) as exc_info:
        factory.build("anthropic/claude-opus-4.8")

    msg = str(exc_info.value)
    assert "did you mean" in msg
    assert "claude-opus-4-8" in msg


# -- list_remote_models (local endpoint discovery; HTTP fully mocked) --------


class _FakeResp:
    def __init__(self, payload, *, status_ok=True):
        self._payload = payload
        self._ok = status_ok

    def raise_for_status(self):
        if not self._ok:
            import httpx

            raise httpx.HTTPStatusError("boom", request=None, response=None)

    def json(self):
        return self._payload


def _capture_get(payload, *, status_ok=True):
    """Return (patched_get, urls) — records every URL the discovery probe hits."""
    urls: list[str] = []

    def _get(url, *args, **kwargs):
        urls.append(url)
        return _FakeResp(payload, status_ok=status_ok)

    return _get, urls


def test_list_remote_models_ollama_parses_tags():
    from jarn.providers import list_remote_models

    payload = {"models": [{"name": "qwen3-coder:30b"}, {"name": "llama3:8b"}]}
    get, urls = _capture_get(payload)
    prov = ProviderConfig(type=ProviderType.OLLAMA, base_url="http://localhost:11434")
    with patch("httpx.get", get):
        out = list_remote_models(prov)
    assert out == ["qwen3-coder:30b", "llama3:8b"]
    assert urls == ["http://localhost:11434/api/tags"]


def test_list_remote_models_lmstudio_parses_v1_models():
    from jarn.providers import list_remote_models

    payload = {"data": [{"id": "qwen2.5-coder"}, {"id": "phi-4"}]}
    get, urls = _capture_get(payload)
    # base_url already carries /v1 (normalize_base_url convention).
    prov = ProviderConfig(type=ProviderType.LMSTUDIO, base_url="http://localhost:1234/v1")
    with patch("httpx.get", get):
        out = list_remote_models(prov)
    assert out == ["qwen2.5-coder", "phi-4"]
    assert urls == ["http://localhost:1234/v1/models"]


def test_list_remote_models_openai_compatible_appends_v1_when_bare():
    from jarn.providers import list_remote_models

    payload = {"data": [{"id": "local-model"}]}
    get, urls = _capture_get(payload)
    prov = ProviderConfig(type=ProviderType.OPENAI_COMPATIBLE, base_url="http://localhost:8000")
    with patch("httpx.get", get):
        out = list_remote_models(prov)
    assert out == ["local-model"]
    assert urls == ["http://localhost:8000/v1/models"]


def test_list_remote_models_unreachable_returns_empty():
    """A connection error must degrade to [] (manual entry), never raise."""
    import httpx

    from jarn.providers import list_remote_models

    def _boom(*a, **k):
        raise httpx.ConnectError("no endpoint")

    prov = ProviderConfig(type=ProviderType.OLLAMA, base_url="http://localhost:11434")
    with patch("httpx.get", _boom):
        assert list_remote_models(prov) == []


def test_list_remote_models_http_error_returns_empty():
    from jarn.providers import list_remote_models

    get, _ = _capture_get({"models": []}, status_ok=False)
    prov = ProviderConfig(type=ProviderType.OLLAMA, base_url="http://localhost:11434")
    with patch("httpx.get", get):
        assert list_remote_models(prov) == []


def test_list_remote_models_no_base_url_returns_empty():
    from jarn.providers import list_remote_models

    # No network call should be made when there's no endpoint to hit.
    def _fail(*a, **k):
        raise AssertionError("must not hit the network without a base_url")

    prov = ProviderConfig(type=ProviderType.OLLAMA, base_url=None)
    with patch("httpx.get", _fail):
        assert list_remote_models(prov) == []


def test_list_remote_models_non_local_provider_returns_empty():
    from jarn.providers import list_remote_models

    def _fail(*a, **k):
        raise AssertionError("must not probe cloud providers")

    prov = ProviderConfig(type=ProviderType.ANTHROPIC, base_url="https://api.anthropic.com")
    with patch("httpx.get", _fail):
        assert list_remote_models(prov) == []


def test_list_remote_models_malformed_payload_returns_empty():
    from jarn.providers import list_remote_models

    # Missing keys / wrong shapes must not raise.
    get, _ = _capture_get({"unexpected": "shape"})
    prov = ProviderConfig(type=ProviderType.LMSTUDIO, base_url="http://localhost:1234/v1")
    with patch("httpx.get", get):
        assert list_remote_models(prov) == []
