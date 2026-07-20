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


# -- default general-purpose subagent runs on routing.subagent (cost routing) --
#
# deepagents auto-adds the general-purpose subagent (the one the `task` tool
# spawns) on the MAIN model. build_runtime instead injects its own spec on the
# configured routing.subagent model so delegated tasks bill at the cheaper rate.


def _build_runtime_capture(cfg, tmp_path):
    """Run build_runtime with model construction + create_deep_agent stubbed,
    capturing the ``subagents`` deepagents receives. Returns (subagents, models
    keyed by ref) so a test can assert *which* model each subagent runs on."""
    from unittest.mock import MagicMock

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from jarn.agent import builder

    models: dict[str, object] = {}

    def fake_build(self, ref):  # noqa: ANN001
        if ref not in models:
            models[ref] = GenericFakeChatModel(messages=iter([]))
        return models[ref]

    captured: dict[str, object] = {}

    def fake_cda(**kwargs):
        captured["subagents"] = kwargs.get("subagents")
        return MagicMock(name="compiled-agent")

    with (
        patch("jarn.providers.models.ModelFactory.build", fake_build),
        patch("deepagents.create_deep_agent", fake_cda),
    ):
        builder.build_runtime(cfg, project_root=tmp_path)
    return captured["subagents"], models


def test_general_purpose_subagent_uses_routing_subagent_model(base_config, tmp_path):
    subagents, models = _build_runtime_capture(base_config, tmp_path)
    sub_ref = base_config.resolved_subagent_model()
    main_ref = base_config.resolved_main_model()
    assert sub_ref == "openrouter/anthropic/claude-haiku-4-5" and sub_ref != main_ref
    gp = next((s for s in (subagents or []) if s.get("name") == "general-purpose"), None)
    assert gp is not None, "build_runtime must inject a general-purpose subagent spec"
    assert gp["model"] is models[sub_ref]
    assert gp["model"] is not models[main_ref]


def test_no_general_purpose_injection_when_subagent_matches_main(base_config, tmp_path):
    base_config.routing.subagent = None  # resolved_subagent_model() -> main
    subagents, _ = _build_runtime_capture(base_config, tmp_path)
    names = {s.get("name") for s in (subagents or [])}
    assert "general-purpose" not in names


def test_custom_general_purpose_subagent_not_clobbered(base_config, tmp_path):
    """A user-defined `general-purpose` subagent (.md) wins: jarn must not inject
    its own, so the user's model/prompt override is preserved."""
    from jarn.extensibility.subagents import CustomSubagent

    custom = {
        "general-purpose": CustomSubagent(
            name="general-purpose",
            description="user gp",
            system_prompt="do the thing",
            model="openrouter/anthropic/claude-opus-4-8",
        )
    }
    with patch("jarn.agent.runtime.load_subagents", return_value=custom):
        subagents, _ = _build_runtime_capture(base_config, tmp_path)
    gps = [s for s in (subagents or []) if s.get("name") == "general-purpose"]
    assert len(gps) == 1  # only the user's — no jarn-injected duplicate
    assert gps[0]["system_prompt"] == "do the thing"


def test_general_purpose_injection_compiles(base_config, tmp_path):
    """Real build (create_deep_agent NOT mocked): the injected general-purpose
    subagent spec must be accepted by deepagents and compile to a graph."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from jarn.agent import builder

    fake = GenericFakeChatModel(messages=iter([]))
    with patch("jarn.providers.models.ModelFactory.build", return_value=fake):
        rt = builder.build_runtime(base_config, project_root=tmp_path)
    assert type(rt.agent).__name__ == "CompiledStateGraph"
    # The subagent model ref is now a known usage-attribution target (billed cheap).
    assert base_config.resolved_subagent_model() in rt.known_model_refs
