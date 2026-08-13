from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

import jarn.catalog.service as catalog_module
from jarn.catalog import (
    CatalogSource,
    ModelCatalogCache,
    ModelCatalogService,
    catalog_timeout_seconds,
)
from jarn.config.schema import Config, ProviderConfig, ProviderType, RoutingConfig
from jarn.providers import (
    RemoteModelCatalog,
    RemoteModelDiscoveryError,
    RemoteModelRecord,
    fetch_remote_model_catalog,
)

FAKE_SERVER = Path(__file__).with_name("codex_fake_app_server.py")
FAKE_COMMAND = (sys.executable, str(FAKE_SERVER))
START = datetime(2026, 8, 9, 12, tzinfo=UTC)


def codex_provider() -> ProviderConfig:
    return ProviderConfig(type=ProviderType.CODEX_SUBSCRIPTION)


def catalog_service(tmp_path: Path, now: list[datetime]) -> ModelCatalogService:
    cache = ModelCatalogCache(tmp_path / "cache", clock=lambda: now[0].timestamp())
    return ModelCatalogService(
        cache=cache,
        ttl_seconds=3600,
        clock=lambda: now[0],
        timeout_seconds=5,
    )


def get_codex(service: ModelCatalogService, *, include_hidden: bool = False):
    return service.get_catalog(
        "codex_subscription",
        codex_provider(),
        include_hidden=include_hidden,
        codex_command=FAKE_COMMAND,
    )


def test_one_page_codex_catalog_is_live_and_versioned(tmp_path):
    now = [START]
    snapshot = get_codex(catalog_service(tmp_path, now))
    payload = snapshot.to_dict()

    assert snapshot.source is CatalogSource.CODEX_LIVE
    assert snapshot.availability_verified is True
    assert snapshot.stale is False
    assert len(snapshot.models) == 1
    assert snapshot.default_entry() == snapshot.models[0]
    assert snapshot.models[0].model_id == "gpt-default"
    assert snapshot.models[0].account_available is True
    assert payload["schema_version"] == 1
    assert payload["ttl_seconds"] == 3600
    assert payload["account_fingerprint"]
    json.dumps(payload)


def test_codex_catalog_paginates_and_filters_hidden(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "model_paginated")
    now = [START]
    service = catalog_service(tmp_path, now)

    standard = get_codex(service)
    advanced = get_codex(service, include_hidden=True)

    assert [entry.model_id for entry in standard.models] == ["gpt-default", "gpt-fast"]
    assert [entry.model_id for entry in advanced.models] == [
        "gpt-default",
        "gpt-hidden",
        "gpt-fast",
    ]
    assert advanced.models[1].hidden is True


def test_codex_pagination_shares_one_timeout_budget_and_closes_server(monkeypatch, tmp_path):
    """A slow first page cannot grant every later page a fresh full timeout."""

    class FakeServer:
        instance = None

        def __init__(self, **kwargs):
            self.timeout_seconds = kwargs["timeout_seconds"]
            self.request_timeouts = []
            self.model_calls = 0
            self.closed = False
            FakeServer.instance = self

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self.closed = True

        def account(self, *, refresh):
            assert refresh is False
            self.request_timeouts.append(self.timeout_seconds)
            return {"type": "chatgpt", "planType": "plus"}

        def model_list(self, *, limit, include_hidden, cursor):
            assert limit == 100
            assert include_hidden is False
            self.request_timeouts.append(self.timeout_seconds)
            self.model_calls += 1
            if cursor is None:
                return ([{"model": "gpt-first"}], "page-2")
            raise TimeoutError("second catalog page stalled")

    monotonic_values = iter((0.0, 0.05, 0.10, 0.90))
    monkeypatch.setattr(catalog_module, "CodexAppServer", FakeServer)
    monkeypatch.setattr(catalog_module.time, "monotonic", lambda: next(monotonic_values))
    service = ModelCatalogService(
        cache=ModelCatalogCache(tmp_path / "cache"),
        timeout_seconds=1,
    )

    snapshot = service.get_catalog(
        "codex_subscription",
        codex_provider(),
        allow_stale_cache=False,
    )

    server = FakeServer.instance
    assert server is not None
    assert server.closed is True
    assert server.model_calls == 2
    assert server.request_timeouts == pytest.approx([0.95, 0.90, 0.10])
    assert snapshot.source is CatalogSource.STATIC_FALLBACK
    assert snapshot.availability_verified is False
    assert snapshot.error is not None
    assert "second catalog page stalled" in snapshot.error.message


def test_codex_entry_carries_reasoning_and_capability_metadata(tmp_path):
    now = [START]
    entry = get_codex(catalog_service(tmp_path, now)).models[0]

    assert entry.default_reasoning_effort == "medium"
    assert [effort.value for effort in entry.supported_reasoning_efforts] == [
        "low",
        "medium",
        "high",
    ]
    assert entry.input_modalities == ("text", "image")
    assert entry.supports_personality is True
    assert entry.billing_mode == "chatgpt_subscription"


def test_codex_catalog_accepts_current_service_tier_objects(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "model_service_tier_objects")

    snapshot = get_codex(catalog_service(tmp_path, [START]))

    assert snapshot.source is CatalogSource.CODEX_LIVE
    assert snapshot.availability_verified is True
    assert snapshot.models[0].service_tiers == ("Priority", "Flex")
    assert snapshot.to_dict()["models"][0]["service_tiers"] == ["Priority", "Flex"]


def test_codex_catalog_rejects_malformed_service_tier_object(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "model_bad_service_tier")

    snapshot = get_codex(catalog_service(tmp_path, [START]))

    assert snapshot.source is CatalogSource.STATIC_FALLBACK
    assert snapshot.availability_verified is False
    assert snapshot.error is not None
    assert "service tier name must be a string" in snapshot.error.message


def test_live_empty_catalog_is_honest_not_replaced_by_static(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "model_empty")
    now = [START]
    snapshot = get_codex(catalog_service(tmp_path, now))

    assert snapshot.source is CatalogSource.CODEX_LIVE
    assert snapshot.models == ()
    assert snapshot.default_entry() is None


@pytest.mark.parametrize("mode", ["model_malformed", "model_bad_effort", "model_cycle"])
def test_malformed_catalog_uses_labeled_static_fallback(monkeypatch, tmp_path, mode):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", mode)
    now = [START]
    snapshot = get_codex(catalog_service(tmp_path, now))

    assert snapshot.source is CatalogSource.STATIC_FALLBACK
    assert snapshot.availability_verified is False
    assert snapshot.provenance_label == "Offline fallback; availability unverified"
    assert snapshot.error is not None
    assert all(entry.account_available is None for entry in snapshot.models)
    assert all(
        entry.availability_label == "Offline fallback; availability unverified"
        for entry in snapshot.models
    )


def test_live_failure_uses_fresh_cache_with_provenance(monkeypatch, tmp_path):
    now = [START]
    service = catalog_service(tmp_path, now)
    live = get_codex(service)
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "model_network_failure")

    cached = get_codex(service)

    assert cached.source is CatalogSource.CACHE
    assert cached.origin_source is CatalogSource.CODEX_LIVE
    assert cached.stale is False
    assert cached.availability_verified is True
    assert live.retrieved_at in cached.provenance_label
    assert cached.error is not None


def test_account_read_failure_never_reuses_another_account_cache(monkeypatch, tmp_path):
    now = [START]
    service = catalog_service(tmp_path, now)
    get_codex(service)
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "network_failure")

    snapshot = get_codex(service)

    assert snapshot.source is CatalogSource.STATIC_FALLBACK
    assert snapshot.availability_verified is False


def test_stale_cache_is_visible_and_no_longer_verified(monkeypatch, tmp_path):
    now = [START]
    service = catalog_service(tmp_path, now)
    get_codex(service)
    now[0] = START.replace(hour=14)
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "model_network_failure")

    cached = get_codex(service)

    assert cached.source is CatalogSource.CACHE
    assert cached.stale is True
    assert cached.availability_verified is False
    assert "Stale cached catalog" in cached.provenance_label


def test_stale_cache_can_be_refused(monkeypatch, tmp_path):
    now = [START]
    service = catalog_service(tmp_path, now)
    get_codex(service)
    now[0] = START.replace(hour=14)
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "model_network_failure")

    snapshot = service.get_catalog(
        "codex_subscription",
        codex_provider(),
        allow_stale_cache=False,
        codex_command=FAKE_COMMAND,
    )

    assert snapshot.source is CatalogSource.STATIC_FALLBACK
    assert snapshot.availability_verified is False


def test_account_cache_identity_mismatch_is_refused(tmp_path):
    now = [START]
    service = catalog_service(tmp_path, now)
    live = get_codex(service)

    loaded = service.cache.load(
        "codex_subscription",
        account_fingerprint="different-account",
    )

    assert live.account_fingerprint != "different-account"
    assert loaded is None


def test_corrupt_cache_degrades_to_static_fallback(monkeypatch, tmp_path):
    now = [START]
    service = catalog_service(tmp_path, now)
    path = service.cache.path_for("codex_subscription")
    path.parent.mkdir(parents=True)
    path.write_text("not-json", encoding="utf-8")
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "model_network_failure")

    snapshot = get_codex(service)

    assert snapshot.source is CatalogSource.STATIC_FALLBACK


def test_catalog_cache_is_private_and_leaves_no_partial_files(tmp_path):
    now = [START]
    service = catalog_service(tmp_path, now)
    get_codex(service)
    path = service.cache.path_for("codex_subscription")

    assert path.read_text(encoding="utf-8").endswith("\n")
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    assert not list(path.parent.glob("*.tmp"))


def test_verified_catalog_removes_retired_current_and_static_choices(monkeypatch, tmp_path):
    from jarn.config.schema import Config, RoutingConfig
    from jarn.tui.controller import Controller

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    root.mkdir()
    config = Config(
        default_profile="codex_subscription",
        default_model="codex_subscription/gpt-removed",
        providers={"codex_subscription": codex_provider()},
        routing=RoutingConfig(
            main="codex_subscription/gpt-removed",
            fallback=["codex_subscription/gpt-also-removed"],
        ),
    )
    controller = Controller(config, root)
    controller._model_catalog_snapshot = get_codex(catalog_service(tmp_path, [START]))

    choices = dict(controller.model_choices())

    assert set(choices) == {"codex_subscription/gpt-default"}
    assert "account default" in choices["codex_subscription/gpt-default"]
    controller.close()


def test_local_model_removed_after_update_fails_before_turn(monkeypatch, tmp_path):
    from jarn.config.schema import Config, RoutingConfig
    from jarn.tui.controller import Controller

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    root.mkdir()
    config = Config(
        default_profile="ollama",
        default_model="ollama/removed-model",
        providers={
            "ollama": ProviderConfig(
                type=ProviderType.OLLAMA,
                base_url="http://127.0.0.1:11434",
            )
        },
        routing=RoutingConfig(main="ollama/removed-model"),
    )
    controller = Controller(config, root)
    service = catalog_service(tmp_path, [START])
    monkeypatch.setattr(
        "jarn.catalog.service.fetch_remote_model_catalog",
        lambda _provider, **_kwargs: RemoteModelCatalog(
            (RemoteModelRecord("available-model"),),
            "live local fixture",
            "local-scope",
        ),
    )
    controller._model_catalog_service = service

    ok, message = controller.validate_selected_model_catalog()

    assert ok is False
    assert "removed-model" in message
    assert "/model refresh" in message
    assert controller.runtime is None
    controller.close()


@pytest.mark.asyncio
async def test_runtime_catalog_gate_raises_stable_model_error(monkeypatch, tmp_path):
    from jarn.config.schema import Config, RoutingConfig
    from jarn.errors import JarnUserError
    from jarn.tui.controller import Controller

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    root.mkdir()
    config = Config(
        default_profile="ollama",
        default_model="ollama/completion-only",
        providers={
            "ollama": ProviderConfig(
                type=ProviderType.OLLAMA,
                base_url="http://127.0.0.1:11434",
            )
        },
        routing=RoutingConfig(main="ollama/completion-only"),
    )
    controller = Controller(config, root)
    monkeypatch.setattr(
        controller,
        "validate_selected_model_catalog",
        lambda: (False, "completion-only does not support Ollama tools"),
    )

    with pytest.raises(JarnUserError) as raised:
        await controller.ensure_runtime()

    assert raised.value.code == "JARN-MODEL-001"
    assert raised.value.detail.component == "model catalog"
    assert "pull a model" in raised.value.detail.action
    await controller.aclose()


def test_local_catalog_uses_same_service_and_reports_live_source(monkeypatch, tmp_path):
    now = [START]
    service = catalog_service(tmp_path, now)
    provider = ProviderConfig(type=ProviderType.OLLAMA, base_url="http://localhost:11434")
    monkeypatch.setattr(
        "jarn.catalog.service.fetch_remote_model_catalog",
        lambda _provider, **_kwargs: RemoteModelCatalog(
            (
                RemoteModelRecord("qwen-thai", supports_tools=True),
                RemoteModelRecord("qwen-thai", supports_tools=True),
                RemoteModelRecord("coder", supports_tools=False),
            ),
            "live local fixture",
            "local-scope",
        ),
    )

    snapshot = service.get_catalog("ollama", provider)

    assert snapshot.source is CatalogSource.LOCAL_LIVE
    assert [entry.ref for entry in snapshot.models] == ["ollama/qwen-thai", "ollama/coder"]
    assert all(entry.account_available is True for entry in snapshot.models)
    assert [entry.supports_tools for entry in snapshot.models] == [True, False]
    assert snapshot.provenance_label == (
        "Live local endpoint catalog (2 installed; 1 tool-capable)"
    )
    cached = service.cache.load(
        "ollama",
        account_fingerprint=snapshot.account_fingerprint,
    )
    assert cached is not None
    assert [entry.supports_tools for entry in cached.models] == [True, False]


def test_ollama_selection_rejects_completion_only_and_unknown_tools(monkeypatch, tmp_path):
    from jarn.config.schema import Config, RoutingConfig
    from jarn.onboarding.model_catalog import selectable_setup_models
    from jarn.tui.controller import Controller

    service = catalog_service(tmp_path, [START])
    provider = ProviderConfig(type=ProviderType.OLLAMA, base_url="http://localhost:11434")
    monkeypatch.setattr(
        "jarn.catalog.service.fetch_remote_model_catalog",
        lambda _provider, **_kwargs: RemoteModelCatalog(
            (
                RemoteModelRecord("completion-only", supports_tools=False),
                RemoteModelRecord("unknown-tools"),
                RemoteModelRecord("agent-model", supports_tools=True),
            ),
            "live local fixture",
            "local-scope",
        ),
    )

    snapshot = service.get_catalog("ollama", provider)

    assert [entry.ref for entry in selectable_setup_models(snapshot)] == [
        "ollama/agent-model"
    ]

    ok, message = service.validate_selection(snapshot, "ollama/completion-only")
    assert ok is False
    assert "does not support Ollama tools" in message
    assert "ollama pull" in message
    ok, message = service.validate_selection(snapshot, "ollama/unknown-tools")
    assert ok is False
    assert "could not be verified" in message
    assert service.validate_selection(snapshot, "ollama/agent-model") == (
        True,
        "selection valid",
    )

    project = tmp_path / "project"
    project.mkdir()
    controller = Controller(
        Config(
            default_profile="ollama",
            default_model="ollama/completion-only",
            providers={"ollama": provider},
            routing=RoutingConfig(main="ollama/completion-only"),
        ),
        project,
    )
    controller._model_catalog_snapshot = snapshot
    assert set(dict(controller.model_choices())) == {"ollama/agent-model"}
    controller.close()


def test_cloud_without_adapter_is_explicit_unverified_fallback(tmp_path):
    now = [START]
    provider = ProviderConfig(type=ProviderType.ANTHROPIC)

    snapshot = catalog_service(tmp_path, now).get_catalog("anthropic", provider)

    assert snapshot.source is CatalogSource.STATIC_FALLBACK
    assert snapshot.availability_verified is False
    assert snapshot.error is not None
    ok, message = ModelCatalogService.validate_selection(snapshot, snapshot.models[0].ref)
    assert ok is False
    assert "not verified" in message


def test_provider_alias_requalifies_static_defaults(tmp_path):
    now = [START]
    provider = ProviderConfig(type=ProviderType.ANTHROPIC)

    snapshot = catalog_service(tmp_path, now).get_catalog("work", provider)

    assert snapshot.models
    assert all(entry.ref.startswith("work/") for entry in snapshot.models)
    assert all(not entry.model_id.startswith("anthropic/") for entry in snapshot.models)


def test_selection_validation_uses_catalog_reasoning_metadata(tmp_path):
    now = [START]
    service = catalog_service(tmp_path, now)
    snapshot = get_codex(service)
    ref = snapshot.models[0].ref

    assert service.validate_selection(snapshot, ref, reasoning_effort="high") == (
        True,
        "selection valid",
    )
    ok, message = service.validate_selection(snapshot, ref, reasoning_effort="impossible")
    assert ok is False
    assert "unsupported" in message


def test_hidden_model_requires_advanced_even_if_snapshot_contains_it(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "model_paginated")
    now = [START]
    service = catalog_service(tmp_path, now)
    snapshot = get_codex(service, include_hidden=True)
    hidden = next(entry for entry in snapshot.models if entry.hidden)

    ok, message = service.validate_selection(snapshot, hidden.ref)

    assert ok is False
    assert "Advanced" in message


def test_retired_model_has_migration_suggestion_and_unavailable_is_filtered(monkeypatch, tmp_path):
    monkeypatch.setenv("JARN_CODEX_FAKE_MODE", "model_retired")
    now = [START]
    service = catalog_service(tmp_path, now)

    snapshot = get_codex(service)

    assert [entry.model_id for entry in snapshot.models] == ["gpt-old", "gpt-new"]
    retired = snapshot.models[0]
    assert retired.deprecated is True
    assert retired.replacement_ref == "codex_subscription/gpt-new"
    ok, message = service.validate_selection(snapshot, retired.ref)
    assert ok is False
    assert "retired or deprecated" in message
    assert "codex_subscription/gpt-new" in message


class _CatalogResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


@pytest.mark.parametrize(
    ("provider", "payload", "expected_url", "expected_id"),
    [
        (
            ProviderConfig(type=ProviderType.OPENAI, api_key="key"),
            {"data": [{"id": "gpt-live"}]},
            "https://api.openai.com/v1/models",
            "gpt-live",
        ),
        (
            ProviderConfig(type=ProviderType.OPENROUTER, api_key="key"),
            {"data": [{"id": "vendor/model"}]},
            "https://openrouter.ai/api/v1/models",
            "vendor/model",
        ),
        (
            ProviderConfig(type=ProviderType.MISTRAL, api_key="key"),
            {"data": [{"id": "mistral-live"}]},
            "https://api.mistral.ai/v1/models",
            "mistral-live",
        ),
        (
            ProviderConfig(type=ProviderType.GROQ, api_key="key"),
            {"data": [{"id": "llama-live"}]},
            "https://api.groq.com/openai/v1/models",
            "llama-live",
        ),
        (
            ProviderConfig(type=ProviderType.TOGETHER, api_key="key"),
            [{"id": "Qwen/live", "type": "chat"}],
            "https://api.together.xyz/v1/models",
            "Qwen/live",
        ),
        (
            ProviderConfig(type=ProviderType.FIREWORKS, api_key="key"),
            {"data": [{"id": "accounts/fireworks/models/live"}]},
            "https://api.fireworks.ai/inference/v1/models",
            "accounts/fireworks/models/live",
        ),
        (
            ProviderConfig(type=ProviderType.XAI, api_key="key"),
            {"models": [{"id": "grok-live"}]},
            "https://api.x.ai/v1/language-models",
            "grok-live",
        ),
        (
            ProviderConfig(
                type=ProviderType.OPENAI_COMPATIBLE,
                base_url="https://gateway.example/v1",
            ),
            {"data": [{"id": "custom-live"}]},
            "https://gateway.example/v1/models",
            "custom-live",
        ),
        (
            ProviderConfig(
                type=ProviderType.LMSTUDIO,
                base_url="http://localhost:1234/v1",
            ),
            {"data": [{"id": "local-live"}]},
            "http://localhost:1234/v1/models",
            "local-live",
        ),
        (
            ProviderConfig(
                type=ProviderType.OLLAMA,
                base_url="http://localhost:11434",
            ),
            {"models": [{"name": "qwen-live"}]},
            "http://localhost:11434/api/tags",
            "qwen-live",
        ),
    ],
)
def test_documented_model_list_adapters_use_provider_specific_endpoints(
    monkeypatch,
    provider,
    payload,
    expected_url,
    expected_id,
):
    calls: list[str] = []
    post_calls: list[tuple[str, dict]] = []

    def get(url, **_kwargs):
        calls.append(url)
        return _CatalogResponse(payload)

    def post(url, **kwargs):
        post_calls.append((url, kwargs.get("json") or {}))
        return _CatalogResponse(
            {
                "capabilities": ["completion", "tools"],
                "model_info": {"qwen.context_length": 32768},
            }
        )

    monkeypatch.setattr("httpx.get", get)
    monkeypatch.setattr("httpx.post", post)

    catalog = fetch_remote_model_catalog(provider)

    assert calls == [expected_url]
    assert [entry.model_id for entry in catalog.models] == [expected_id]
    assert "configured provider endpoint" in catalog.provenance_label
    if provider.type is ProviderType.OLLAMA:
        assert post_calls == [
            ("http://localhost:11434/api/show", {"model": "qwen-live"})
        ]
        assert catalog.models[0].supports_tools is True
        assert catalog.models[0].context_window == 32768
    else:
        assert post_calls == []


def test_ollama_catalog_carries_each_models_tool_capability(monkeypatch):
    provider = ProviderConfig(
        type=ProviderType.OLLAMA,
        base_url="http://localhost:11434",
    )
    monkeypatch.setattr(
        "httpx.get",
        lambda *_args, **_kwargs: _CatalogResponse(
            {"models": [{"name": "completion-only"}, {"name": "agent-model"}]}
        ),
    )

    def post(_url, **kwargs):
        model = kwargs["json"]["model"]
        capabilities = ["completion", "tools"] if model == "agent-model" else ["completion"]
        return _CatalogResponse({"capabilities": capabilities})

    monkeypatch.setattr("httpx.post", post)

    catalog = fetch_remote_model_catalog(provider)

    assert [(item.model_id, item.supports_tools) for item in catalog.models] == [
        ("completion-only", False),
        ("agent-model", True),
    ]


def test_ollama_catalog_rejects_malformed_capability_metadata(monkeypatch):
    provider = ProviderConfig(
        type=ProviderType.OLLAMA,
        base_url="http://localhost:11434",
    )
    monkeypatch.setattr(
        "httpx.get",
        lambda *_args, **_kwargs: _CatalogResponse(
            {"models": [{"name": "unverifiable"}]}
        ),
    )
    monkeypatch.setattr(
        "httpx.post",
        lambda *_args, **_kwargs: _CatalogResponse({"capabilities": "tools"}),
    )

    with pytest.raises(RemoteModelDiscoveryError, match="malformed capabilities"):
        fetch_remote_model_catalog(provider)


def test_anthropic_and_google_catalog_adapters_paginate(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def get(url, **kwargs):
        params = kwargs.get("params") or {}
        calls.append((url, params))
        if "anthropic" in url:
            if params.get("after_id"):
                return _CatalogResponse({"data": [{"id": "claude-two"}], "has_more": False})
            return _CatalogResponse(
                {
                    "data": [{"id": "claude-one"}],
                    "has_more": True,
                    "last_id": "claude-one",
                }
            )
        if params.get("pageToken"):
            return _CatalogResponse(
                {
                    "models": [
                        {
                            "name": "models/gemini-two",
                            "supportedGenerationMethods": ["generateContent"],
                        }
                    ]
                }
            )
        return _CatalogResponse(
            {
                "models": [
                    {
                        "name": "models/gemini-one",
                        "supportedGenerationMethods": ["generateContent"],
                    }
                ],
                "nextPageToken": "page-two",
            }
        )

    monkeypatch.setattr("httpx.get", get)

    anthropic = fetch_remote_model_catalog(
        ProviderConfig(type=ProviderType.ANTHROPIC, api_key="ant-key")
    )
    google = fetch_remote_model_catalog(
        ProviderConfig(type=ProviderType.GOOGLE, api_key="google-key")
    )

    assert [entry.model_id for entry in anthropic.models] == ["claude-one", "claude-two"]
    assert [entry.model_id for entry in google.models] == ["gemini-one", "gemini-two"]
    assert calls[1][1]["after_id"] == "claude-one"
    assert calls[2][1]["key"] == "google-key"
    assert calls[3][1]["pageToken"] == "page-two"


def test_model_list_failure_never_exposes_key_bearing_error(monkeypatch):
    secret = "super-secret-google-key"

    def fail(*_args, **_kwargs):
        raise RuntimeError(f"transport included {secret}")

    monkeypatch.setattr("httpx.get", fail)

    with pytest.raises(RemoteModelDiscoveryError) as caught:
        fetch_remote_model_catalog(ProviderConfig(type=ProviderType.GOOGLE, api_key=secret))

    assert secret not in str(caught.value)
    assert "RuntimeError" in str(caught.value)


def test_authenticated_openai_catalog_is_live_rich_and_non_billable(monkeypatch, tmp_path):
    captured: list[tuple[str, dict[str, str]]] = []

    def get(url, **kwargs):
        captured.append((url, kwargs.get("headers") or {}))
        return _CatalogResponse(
            {
                "data": [
                    {
                        "id": "gpt-account-model",
                        "context_window": 200_000,
                        "input_modalities": ["text", "image"],
                    }
                ]
            }
        )

    monkeypatch.setattr("httpx.get", get)
    provider = ProviderConfig(type=ProviderType.OPENAI, api_key="account-secret")
    snapshot = catalog_service(tmp_path, [START]).get_catalog("openai", provider)

    assert snapshot.source is CatalogSource.PROVIDER_LIVE
    assert snapshot.availability_verified is True
    assert [entry.ref for entry in snapshot.models] == ["openai/gpt-account-model"]
    assert snapshot.models[0].context_window == 200_000
    assert snapshot.models[0].input_modalities == ("text", "image")
    assert snapshot.models[0].availability_label == "Reported by the configured provider endpoint"
    assert captured == [
        ("https://api.openai.com/v1/models", {"Authorization": "Bearer account-secret"})
    ]


def test_provider_cache_is_credential_scoped_and_never_crosses_accounts(monkeypatch, tmp_path):
    mode = ["live"]

    def get(_url, **_kwargs):
        if mode[0] == "live":
            return _CatalogResponse({"data": [{"id": "gpt-account-a"}]})
        raise OSError("offline")

    monkeypatch.setattr("httpx.get", get)
    service = catalog_service(tmp_path, [START])
    account_a = ProviderConfig(type=ProviderType.OPENAI, api_key="account-a")
    account_b = ProviderConfig(type=ProviderType.OPENAI, api_key="account-b")
    missing_account = ProviderConfig(type=ProviderType.OPENAI)
    live = service.get_catalog("work", account_a)
    mode[0] = "offline"

    same_account = service.get_catalog("work", account_a)
    other_account = service.get_catalog("work", account_b)
    unresolved_account = service.get_catalog("work", missing_account)

    assert live.source is CatalogSource.PROVIDER_LIVE
    assert same_account.source is CatalogSource.CACHE
    assert same_account.availability_verified is True
    assert other_account.source is CatalogSource.STATIC_FALLBACK
    assert other_account.availability_verified is False
    assert unresolved_account.source is CatalogSource.STATIC_FALLBACK
    assert unresolved_account.availability_verified is False


def test_offline_catalog_uses_scoped_cache_without_live_http(monkeypatch, tmp_path):
    calls = 0

    def get(_url, **_kwargs):
        nonlocal calls
        calls += 1
        return _CatalogResponse({"data": [{"id": "gpt-offline-ready"}]})

    monkeypatch.setattr("httpx.get", get)
    service = catalog_service(tmp_path, [START])
    account_a = ProviderConfig(type=ProviderType.OPENAI, api_key="account-a")
    account_b = ProviderConfig(type=ProviderType.OPENAI, api_key="account-b")

    live = service.get_catalog("work", account_a)
    cached = service.get_catalog("work", account_a, refresh_live=False)
    wrong_account = service.get_catalog("work", account_b, refresh_live=False)

    assert calls == 1
    assert live.source is CatalogSource.PROVIDER_LIVE
    assert cached.source is CatalogSource.CACHE
    assert cached.availability_verified is True
    assert wrong_account.source is CatalogSource.STATIC_FALLBACK
    assert wrong_account.availability_verified is False
    assert "offline diagnostic" in (wrong_account.error.message if wrong_account.error else "")


def test_route_catalog_offline_flag_is_forwarded_without_remote_fetch(monkeypatch, tmp_path):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("offline doctor attempted a live provider catalog")

    monkeypatch.setattr("jarn.catalog.service.fetch_remote_model_catalog", forbidden)
    config = Config(
        default_profile="openai",
        default_model="openai/gpt-configured",
        providers={
            "openai": ProviderConfig(type=ProviderType.OPENAI, api_key="account-a"),
        },
        routing=RoutingConfig(main="openai/gpt-configured"),
    )

    snapshots = catalog_service(tmp_path, [START]).get_catalogs_for_routes(
        config,
        refresh_live=False,
    )

    assert snapshots["openai"].source is CatalogSource.STATIC_FALLBACK
    assert snapshots["openai"].availability_verified is False


def test_billable_validation_cache_is_exact_scoped_and_time_bounded(tmp_path):
    now = [START]
    service = catalog_service(tmp_path, now)
    account_a = ProviderConfig(type=ProviderType.DEEPSEEK, api_key="account-a")
    account_b = ProviderConfig(type=ProviderType.DEEPSEEK, api_key="account-b")

    recorded = service.record_billable_validation(
        "deepseek",
        account_a,
        "deepseek/validated-only",
    )
    cached = service.get_catalog("deepseek", account_a)
    wrong_account = service.get_catalog("deepseek", account_b)
    now[0] = START.replace(hour=14)
    stale = service.get_catalog("deepseek", account_a)

    assert recorded.source is CatalogSource.BILLABLE_VALIDATION
    assert [entry.ref for entry in cached.models] == ["deepseek/validated-only"]
    assert cached.source is CatalogSource.CACHE
    assert cached.availability_verified is True
    assert "exact model only" in cached.provenance_label
    assert wrong_account.source is CatalogSource.STATIC_FALLBACK
    assert stale.source is CatalogSource.CACHE
    assert stale.availability_verified is False
    assert "Stale successful billable validation" in stale.provenance_label


def test_cross_provider_routes_share_catalogs_and_fail_closed(monkeypatch, tmp_path):
    from jarn.tui.controller import Controller

    def remote(provider, **_kwargs):
        if provider.type is ProviderType.OPENAI:
            records = (RemoteModelRecord("gpt-main"),)
            scope = "openai-scope"
        else:
            records = (
                RemoteModelRecord("claude-old", deprecated=True),
                RemoteModelRecord("claude-good"),
            )
            scope = "anthropic-scope"
        return RemoteModelCatalog(records, f"live {provider.type.value}", scope)

    monkeypatch.setattr("jarn.catalog.service.fetch_remote_model_catalog", remote)
    config = Config(
        default_profile="openai",
        default_model="openai/gpt-main",
        providers={
            "openai": ProviderConfig(type=ProviderType.OPENAI, api_key="a"),
            "anthropic": ProviderConfig(type=ProviderType.ANTHROPIC, api_key="b"),
        },
        routing=RoutingConfig(
            main="openai/gpt-main",
            subagent="anthropic/claude-old",
            summarizer="openai/gpt-removed",
            fallback=["anthropic/claude-good"],
        ),
    )
    service = catalog_service(tmp_path, [START])
    snapshots = service.get_catalogs_for_routes(config)

    ok, errors = service.validate_routes(config, snapshots)

    assert set(snapshots) == {"openai", "anthropic"}
    assert ok is False
    assert any(error.startswith("subagent:") and "deprecated" in error for error in errors)
    assert any(error.startswith("summarizer:") and "gpt-removed" in error for error in errors)
    assert not any(error.startswith("fallback[0]:") for error in errors)

    root = tmp_path / "project"
    root.mkdir()
    controller = Controller(config, root)
    controller._model_catalog_service = service
    preturn_ok, message = controller.validate_selected_model_catalog()
    assert preturn_ok is False
    assert "subagent" in message
    assert "summarizer" in message
    assert "Advanced config" in message
    assert controller.runtime is None
    controller.close()


def test_model_picker_uses_all_live_provider_snapshots_without_static_invention(
    monkeypatch, tmp_path
):
    from jarn.tui.controller import Controller

    def remote(provider, **_kwargs):
        model = "gpt-live" if provider.type is ProviderType.OPENAI else "claude-live"
        return RemoteModelCatalog(
            (RemoteModelRecord(model),),
            f"live {provider.type.value}",
            f"{provider.type.value}-scope",
        )

    monkeypatch.setattr("jarn.catalog.service.fetch_remote_model_catalog", remote)
    root = tmp_path / "project"
    root.mkdir()
    config = Config(
        default_profile="openai",
        default_model="openai/gpt-live",
        providers={
            "openai": ProviderConfig(type=ProviderType.OPENAI, api_key="a"),
            "anthropic": ProviderConfig(type=ProviderType.ANTHROPIC, api_key="b"),
        },
        routing=RoutingConfig(
            main="openai/gpt-live",
            fallback=["anthropic/claude-live"],
        ),
    )
    controller = Controller(config, root)
    service = catalog_service(tmp_path, [START])
    controller._model_catalog_service = service

    active = controller.refresh_model_catalog()
    choices = dict(controller.model_choices())

    assert active.default_entry() is not None
    assert set(choices) == {"openai/gpt-live", "anthropic/claude-live"}
    assert active.default_entry().ref in choices
    assert all("live " in label for label in choices.values())
    controller.close()


def test_cloud_setup_and_model_picker_use_identical_selectable_catalog(monkeypatch, tmp_path):
    from jarn.onboarding.model_catalog import (
        load_setup_catalog,
        selectable_setup_models,
    )
    from jarn.tui.controller import Controller

    monkeypatch.setattr(
        "jarn.catalog.service.fetch_remote_model_catalog",
        lambda _provider, **_kwargs: RemoteModelCatalog(
            (
                RemoteModelRecord("retired", deprecated=True),
                RemoteModelRecord("gpt-live"),
            ),
            "live openai fixture",
            "openai-scope",
        ),
    )
    service = catalog_service(tmp_path, [START])
    setup_snapshot = load_setup_catalog(
        "openai",
        credential="account-key",
        service=service,
    )
    setup_refs = {entry.ref for entry in selectable_setup_models(setup_snapshot)}
    config = Config(
        default_profile="openai",
        default_model="openai/gpt-live",
        providers={"openai": ProviderConfig(type=ProviderType.OPENAI, api_key="account-key")},
        routing=RoutingConfig(main="openai/gpt-live"),
    )
    project = tmp_path / "project"
    project.mkdir()
    controller = Controller(config, project)
    controller._model_catalog_service = service

    controller.refresh_model_catalog()

    assert setup_refs == {"openai/gpt-live"}
    assert set(dict(controller.model_choices())) == setup_refs
    controller.close()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 20.0),
        ("", 20.0),
        ("not-a-number", 20.0),
        ("nan", 20.0),
        ("0.1", 1.0),
        ("30", 30.0),
        ("999", 120.0),
    ],
)
def test_catalog_timeout_env_is_validated_and_bounded(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("JARN_CATALOG_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("JARN_CATALOG_TIMEOUT_SECONDS", raw)

    assert catalog_timeout_seconds() == expected
    assert ModelCatalogService().timeout_seconds == expected


def test_catalog_wait_hook_reports_requests_over_progress_boundary(monkeypatch, tmp_path):
    callbacks: list[Callable[[], None]] = []

    class ImmediateTimer:
        daemon = False

        def __init__(self, _seconds, callback):
            callbacks.append(callback)

        def start(self):
            callbacks[-1]()

        def cancel(self):
            return None

    monkeypatch.setattr("jarn.catalog.service.threading.Timer", ImmediateTimer)
    monkeypatch.setattr(
        "jarn.catalog.service.fetch_remote_model_catalog",
        lambda _provider, **_kwargs: RemoteModelCatalog(
            (RemoteModelRecord("gpt-live"),),
            "live openai fixture",
            "openai-scope",
        ),
    )
    notices: list[str] = []
    service = ModelCatalogService(
        cache=ModelCatalogCache(tmp_path / "cache"),
        timeout_seconds=17,
        on_wait=notices.append,
    )

    service.get_catalog(
        "openai",
        ProviderConfig(type=ProviderType.OPENAI, api_key="test-key"),
    )

    assert notices == ["Still checking openai model availability (timeout 17s)…"]
