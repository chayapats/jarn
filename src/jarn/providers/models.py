"""Model resolution & per-task routing.

A J.A.R.N. *model ref* is ``<profile>/<model-id>`` where ``profile`` names an
entry in ``config.providers`` and ``model-id`` is the provider's own identifier
(which may itself contain slashes, e.g. ``openrouter/anthropic/claude-opus-4-8``
→ profile ``openrouter``, model ``anthropic/claude-opus-4-8``).

The :class:`ModelFactory` turns a ref + provider config into a LangChain
``BaseChatModel`` via ``init_chat_model``, mapping each provider type to the
right backend and injecting ``api_key`` / ``base_url``. Built models are cached.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from jarn.config.schema import Config, ProviderConfig, ProviderType
from jarn.config.secrets import SecretResolutionError, resolve

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel


class ModelResolutionError(RuntimeError):
    """Raised when a model ref cannot be turned into a chat model."""


@dataclass(frozen=True, slots=True)
class ModelRef:
    profile: str
    model_id: str

    @property
    def qualified(self) -> str:
        return f"{self.profile}/{self.model_id}"


def parse_model_ref(ref: str, *, default_profile: str | None = None) -> ModelRef:
    """Split a model ref into (profile, model_id).

    If ``ref`` has no ``/`` it is treated as a bare model id under
    ``default_profile`` (e.g. ``"claude-opus-4-8"`` + profile ``anthropic``).
    """
    if "/" in ref:
        profile, model_id = ref.split("/", 1)
        return ModelRef(profile=profile, model_id=model_id)
    if not default_profile:
        raise ModelResolutionError(
            f"Model ref {ref!r} has no profile and no default_profile is set."
        )
    return ModelRef(profile=default_profile, model_id=ref)


def qualify_model_ref(value: str, profile: str) -> str:
    """Ensure ``value`` is a full ``<profile>/<model>`` ref under ``profile``.

    This resolves the common confusion where a provider's model id itself looks
    like ``vendor/model`` (e.g. OpenRouter's ``deepseek/deepseek-v4-flash``).
    The user picks the *provider* separately, then types just the model id; we
    prepend the chosen provider so it routes correctly:

        qualify_model_ref("deepseek/deepseek-v4-flash", "openrouter")
            -> "openrouter/deepseek/deepseek-v4-flash"
        qualify_model_ref("openrouter/anthropic/claude", "openrouter")
            -> "openrouter/anthropic/claude"   (already qualified)
    """
    value = value.strip()
    if value.startswith(f"{profile}/"):
        return value
    return f"{profile}/{value}"


def strip_profile(ref: str, profile: str) -> str:
    """Inverse of :func:`qualify_model_ref` for display: drop a leading profile."""
    prefix = f"{profile}/"
    return ref[len(prefix):] if ref.startswith(prefix) else ref


# Providers served through ChatOpenAI (model_provider="openai") + a base_url.
_OPENAI_COMPATIBLE = {
    ProviderType.OPENAI,
    ProviderType.OPENROUTER,
    ProviderType.LMSTUDIO,
    ProviderType.GROQ,
    ProviderType.DEEPSEEK,
    ProviderType.TOGETHER,
    ProviderType.FIREWORKS,
    ProviderType.XAI,
    ProviderType.OPENAI_COMPATIBLE,
}

# Providers with dedicated LangChain integrations: type -> model_provider string.
_DEDICATED = {
    ProviderType.ANTHROPIC: "anthropic",
    ProviderType.OLLAMA: "ollama",
    ProviderType.GOOGLE: "google_genai",
    ProviderType.MISTRAL: "mistralai",
}


@dataclass(slots=True)
class ModelFactory:
    """Builds and caches chat models for a given :class:`Config`."""

    config: Config
    default_max_retries: int = 2
    _cache: dict[str, Any] = field(default_factory=dict)

    def build(self, ref: str) -> BaseChatModel:
        """Build (or return cached) chat model for a fully/partly-qualified ref."""
        parsed = parse_model_ref(ref, default_profile=self.config.default_profile)
        cache_key = parsed.qualified
        if cache_key in self._cache:
            return self._cache[cache_key]

        provider = self.config.providers.get(parsed.profile)
        if provider is None:
            raise ModelResolutionError(
                f"No provider {parsed.profile!r} configured (referenced by {ref!r})."
            )
        model = self._construct(parsed, provider)
        self._cache[cache_key] = model
        return model

    def build_main(self) -> BaseChatModel:
        ref = self.config.resolved_main_model()
        if not ref:
            raise ModelResolutionError("No main model configured (routing.main/default_model).")
        return self.build(ref)

    def build_subagent(self) -> BaseChatModel | None:
        ref = self.config.resolved_subagent_model()
        return self.build(ref) if ref else None

    def build_summarizer(self) -> BaseChatModel | None:
        ref = self.config.resolved_summarizer_model()
        return self.build(ref) if ref else None

    def fallback_models(self) -> list[BaseChatModel]:
        """Materialize the configured fallback chain (skipping unbuildable ones)."""
        models: list[BaseChatModel] = []
        for ref in self.config.routing.fallback:
            try:
                models.append(self.build(ref))
            except (ModelResolutionError, Exception):
                # A broken fallback entry must not prevent the agent from starting.
                continue
        return models

    # -- internals ----------------------------------------------------------

    def _construct(self, ref: ModelRef, provider: ProviderConfig) -> BaseChatModel:
        from langchain.chat_models import init_chat_model

        kwargs: dict[str, Any] = dict(provider.extra)
        kwargs.setdefault("max_retries", self.default_max_retries)

        try:
            return self._construct_inner(ref, provider, kwargs, init_chat_model)
        except SecretResolutionError as exc:
            raise ModelResolutionError(
                f"Cannot build {ref.qualified!r}: {exc}"
            ) from exc

    def _construct_inner(self, ref, provider, kwargs, init_chat_model) -> BaseChatModel:

        if provider.type in _OPENAI_COMPATIBLE:
            model_provider = "openai"
            api_key = resolve(provider.api_key)
            # LM Studio / some local servers accept any non-empty key.
            if provider.type is ProviderType.LMSTUDIO and not api_key:
                api_key = "lm-studio"
            if api_key:
                kwargs["api_key"] = api_key
            if provider.base_url:
                kwargs["base_url"] = provider.base_url
        elif provider.type is ProviderType.OLLAMA:
            model_provider = "ollama"
            if provider.base_url:
                kwargs["base_url"] = provider.base_url
        elif provider.type in _DEDICATED:
            model_provider = _DEDICATED[provider.type]
            api_key = resolve(provider.api_key)
            if api_key:
                kwargs["api_key"] = api_key
            if provider.base_url:
                kwargs["base_url"] = provider.base_url
        else:  # pragma: no cover - exhaustive by enum
            raise ModelResolutionError(f"Unsupported provider type: {provider.type}")

        try:
            return init_chat_model(ref.model_id, model_provider=model_provider, **kwargs)
        except Exception as exc:  # noqa: BLE001 - surface a clean message
            raise ModelResolutionError(
                f"Failed to initialize model {ref.qualified!r} "
                f"(provider type {provider.type.value}): {exc}"
            ) from exc
