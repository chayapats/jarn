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

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarn.config.schema import Config, ProviderConfig, ProviderType
from jarn.config.secrets import SecretResolutionError, resolve, resolve_secret_mapping

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel


class ModelResolutionError(RuntimeError):
    """Raised when a model ref cannot be turned into a chat model."""


def _slug_hint(provider_type: ProviderType) -> str:
    """A provider-appropriate dot-vs-dash convention note for a slug suggestion."""
    if provider_type is ProviderType.ANTHROPIC:
        return "Anthropic uses dashes; OpenRouter uses dots."
    if provider_type is ProviderType.OPENROUTER:
        return "OpenRouter uses dots; Anthropic API uses dashes."
    return "check the dot-vs-dash version separators for this provider."


def suggest_slug(provider_type: ProviderType, slug: str) -> str | None:
    """Return a corrected slug suggestion when dot/dash confusion is likely.

    OpenRouter uses dots (``claude-opus-4.8``) while the dedicated Anthropic API
    uses dashes (``claude-opus-4-8``).  When the slug contains a dot that looks
    like a version separator, try swapping dots to dashes (and vice-versa) and
    check whether the alternative appears in the provider's known default slugs.
    Returns the suggested slug string if a near-match is found, else ``None``.
    """
    from jarn.config.defaults import DEFAULT_MODELS

    provider_key = provider_type.value
    known_slugs: set[str] = set()
    for models in DEFAULT_MODELS.values():
        for ref in models.values():
            # refs are like "openrouter/anthropic/claude-opus-4.8" — strip profile
            parts = ref.split("/", 1)
            if len(parts) == 2:
                known_slugs.add(parts[1])
            known_slugs.add(ref)

    # Also collect slugs for the specific provider
    provider_defaults = DEFAULT_MODELS.get(provider_key, {})
    provider_slugs: set[str] = set()
    for ref in provider_defaults.values():
        parts = ref.split("/", 1)
        if len(parts) == 2:
            provider_slugs.add(parts[1])

    # Try the simple dot<->dash swap
    import re

    if "." in slug:
        candidate = slug.replace(".", "-")
    elif re.search(r"\d-\d", slug):
        # Swap only digit-separator hyphens (e.g. "4-8" -> "4.8", not word hyphens)
        candidate = re.sub(r"(\d)-(\d)", r"\1.\2", slug)
    else:
        return None

    if candidate == slug:
        return None

    # Check candidate against all known slugs (both provider-specific and global)
    all_known = known_slugs | provider_slugs
    if candidate in all_known:
        return f"did you mean {candidate!r}? ({_slug_hint(provider_type)})"

    # Partial match: check if candidate appears as a substring of a known slug
    for known in all_known:
        if candidate in known or known in candidate:
            return f"did you mean {known!r}? ({_slug_hint(provider_type)})"

    return None


def prompt_cache_strategy(provider_type: ProviderType) -> str:
    """How prompt caching is achieved for ``provider_type``.

    Caching is not one mechanism: Anthropic needs explicit ``cache_control``
    breakpoints (a middleware), the other cloud providers cache by exact prefix
    automatically on their servers, and local llama.cpp servers (Ollama / LM
    Studio) reuse a KV/prefix cache automatically *as long as the model stays
    resident* — so the only lever we have there is keeping it warm.

    Returns one of:
      ``"middleware"``        — Anthropic; cache-control is added by the agent
                                engine (deepagents) itself, so JARN does nothing.
      ``"server_auto"``       — nothing to do; the provider caches server-side.
      ``"ollama_keepalive"``  — pass ``keep_alive`` to keep Ollama's cache warm.
      ``"lmstudio_ttl"``      — pass request ``ttl`` to keep LM Studio loaded.
    """
    if provider_type is ProviderType.ANTHROPIC:
        return "middleware"
    if provider_type is ProviderType.OLLAMA:
        return "ollama_keepalive"
    if provider_type is ProviderType.LMSTUDIO:
        return "lmstudio_ttl"
    # OPENAI_COMPATIBLE is an unknown custom endpoint — don't risk injecting a
    # non-standard ttl into a strict server; treat as automatic/no-op.
    return "server_auto"


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
    return ref[len(prefix) :] if ref.startswith(prefix) else ref


#: How long to wait when probing a local endpoint for its model list. Short so an
#: unreachable endpoint degrades to manual entry quickly rather than hanging setup.
_DISCOVERY_TIMEOUT_SECS = 2.0


@dataclass(frozen=True, slots=True)
class RemoteModelRecord:
    """One model returned by a provider's read-only catalog endpoint.

    The fields intentionally describe only facts present in the response.  A
    missing value stays ``None``/empty rather than being filled from
    ``DEFAULT_MODELS`` and accidentally presented as live provider metadata.
    """

    model_id: str
    display_name: str | None = None
    description: str | None = None
    context_window: int | None = None
    input_modalities: tuple[str, ...] = ()
    supports_tools: bool | None = None
    preview: bool = False
    deprecated: bool = False


@dataclass(frozen=True, slots=True)
class RemoteModelCatalog:
    """A successful, non-billable provider model-list response."""

    models: tuple[RemoteModelRecord, ...]
    provenance_label: str
    account_fingerprint: str


class RemoteModelDiscoveryError(RuntimeError):
    """A provider model list could not be obtained or safely interpreted."""


# Provider documentation checked for these endpoints in August 2026.  DeepSeek
# intentionally stays out: its OpenAI-compatible inference API does not publish
# a model-list contract.  A static default is not evidence, so callers receive a
# clearly unverified fallback instead of a fabricated live catalog.
_CLOUD_MODEL_LIST_TYPES: frozenset[ProviderType] = frozenset(
    {
        ProviderType.OPENAI,
        ProviderType.OPENROUTER,
        ProviderType.ANTHROPIC,
        ProviderType.GOOGLE,
        ProviderType.MISTRAL,
        ProviderType.GROQ,
        ProviderType.TOGETHER,
        ProviderType.FIREWORKS,
        ProviderType.XAI,
        ProviderType.OPENCODE,
    }
)
_API_KEY_PROVIDER_TYPES = _CLOUD_MODEL_LIST_TYPES | {ProviderType.DEEPSEEK}


def supports_remote_model_catalog(provider_type: ProviderType) -> bool:
    """Whether J.A.R.N. has a read-only live-list adapter for this type."""

    return provider_type in _CLOUD_MODEL_LIST_TYPES or provider_type in {
        ProviderType.OLLAMA,
        ProviderType.LMSTUDIO,
        ProviderType.OPENAI_COMPATIBLE,
    }


def _model_catalog_base(provider: ProviderConfig) -> str:
    if provider.base_url:
        return provider.base_url.strip().rstrip("/")
    defaults = {
        ProviderType.OPENAI: "https://api.openai.com/v1",
        ProviderType.ANTHROPIC: "https://api.anthropic.com",
        ProviderType.GOOGLE: "https://generativelanguage.googleapis.com/v1beta",
        ProviderType.MISTRAL: "https://api.mistral.ai/v1",
        ProviderType.OPENROUTER: "https://openrouter.ai/api/v1",
        ProviderType.GROQ: "https://api.groq.com/openai/v1",
        ProviderType.DEEPSEEK: "https://api.deepseek.com",
        ProviderType.TOGETHER: "https://api.together.xyz/v1",
        ProviderType.FIREWORKS: "https://api.fireworks.ai/inference/v1",
        ProviderType.XAI: "https://api.x.ai/v1",
        ProviderType.OPENCODE: "https://opencode.ai/zen/go/v1",
    }
    return defaults.get(provider.type, "")


def _remote_catalog_key(provider: ProviderConfig) -> str | None:
    try:
        key = resolve(provider.api_key) if provider.api_key else None
    except SecretResolutionError as exc:
        raise RemoteModelDiscoveryError(
            f"{provider.type.value} model-list credential could not be resolved"
        ) from exc
    if provider.type in _API_KEY_PROVIDER_TYPES and not key:
        raise RemoteModelDiscoveryError(
            f"{provider.type.value} model-list requires the configured API key"
        )
    return key


def _remote_scope(provider: ProviderConfig, base: str, key: str | None) -> str:
    # The secret is one-way hashed with endpoint/type scope and is never emitted.
    # This prevents a fresh catalog fetched with one API key from being reused by
    # another account configured under the same profile name.
    material = f"{provider.type.value}\0{base}\0{key or 'no-key'}".encode()
    return hashlib.sha256(material).hexdigest()[:20]


def remote_catalog_account_fingerprint(provider: ProviderConfig) -> str:
    """Return the privacy-preserving identity used to bind provider caches."""

    base = _model_catalog_base(provider)
    if not base:
        raise RemoteModelDiscoveryError(f"{provider.type.value} endpoint is not configured")
    return _remote_scope(provider, base, _remote_catalog_key(provider))


def _safe_get_json(
    url: str,
    *,
    provider: ProviderConfig,
    headers: dict[str, str],
    params: dict[str, Any] | None,
    timeout_seconds: float,
) -> Any:
    """GET JSON without ever copying a key-bearing URL into an exception."""

    try:
        import httpx

        response = httpx.get(
            url,
            headers=headers or None,
            params=params or None,
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:  # noqa: BLE001 - translate at the secret boundary
        status = getattr(getattr(exc, "response", None), "status_code", None)
        detail = f"HTTP {status}" if isinstance(status, int) else type(exc).__name__
        raise RemoteModelDiscoveryError(
            f"{provider.type.value} model-list request failed ({detail})"
        ) from exc


def _safe_post_json(
    url: str,
    *,
    provider: ProviderConfig,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
) -> Any:
    """POST JSON without exposing a model name or endpoint response in errors."""

    try:
        import httpx

        response = httpx.post(
            url,
            headers=headers or None,
            json=payload,
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:  # noqa: BLE001 - translate at the local API boundary
        status = getattr(getattr(exc, "response", None), "status_code", None)
        detail = f"HTTP {status}" if isinstance(status, int) else type(exc).__name__
        raise RemoteModelDiscoveryError(
            f"{provider.type.value} capability request failed ({detail})"
        ) from exc


def _optional_text(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _positive_int(row: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = row.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def _remote_record(provider_type: ProviderType, row: dict[str, Any]) -> RemoteModelRecord | None:
    raw_id = _optional_text(row, "id", "name")
    if not raw_id:
        return None
    model_id = raw_id.removeprefix("models/") if provider_type is ProviderType.GOOGLE else raw_id

    # When an endpoint exposes a capability discriminator, use it to avoid
    # putting embedding/image-only or archived entries in a coding-agent picker.
    row_type = row.get("type")
    if provider_type is ProviderType.TOGETHER and row_type not in (None, "chat", "language", "code"):
        return None
    capabilities = row.get("capabilities")
    if isinstance(capabilities, dict) and capabilities.get("completion_chat") is False:
        return None
    methods = row.get("supportedGenerationMethods") or row.get("supported_actions")
    if (
        provider_type is ProviderType.GOOGLE
        and isinstance(methods, list)
        and not any(method in {"generateContent", "generate_content"} for method in methods)
    ):
        return None
    if row.get("archived") is True or row.get("active") is False:
        return None

    # The generic OpenAI/Groq model endpoints also return embedding, image,
    # speech, moderation, and realtime-only assets.  Those are account-visible
    # but cannot back J.A.R.N.'s text chat runtime, so keep them out of the
    # standard picker. Manual Advanced entry remains available for new families.
    lowered = model_id.lower()
    non_chat_markers = (
        "embedding",
        "moderation",
        "whisper",
        "transcribe",
        "dall-e",
        "gpt-image",
        "sora",
        "tts",
        "realtime",
        "audio-preview",
        "audio-transcribe",
    )
    if provider_type in {ProviderType.OPENAI, ProviderType.GROQ} and any(
        marker in lowered for marker in non_chat_markers
    ):
        return None

    modalities: tuple[str, ...] = ()
    architecture = row.get("architecture")
    raw_modalities = row.get("input_modalities")
    if isinstance(architecture, dict):
        raw_modalities = architecture.get("input_modalities", raw_modalities)
    if isinstance(raw_modalities, list):
        modalities = tuple(
            dict.fromkeys(item for item in raw_modalities if isinstance(item, str) and item)
        )

    supports_tools: bool | None = None
    if provider_type is ProviderType.OLLAMA:
        raw_capabilities = row.get("_jarn_ollama_capabilities")
        if isinstance(raw_capabilities, list) and all(
            isinstance(item, str) for item in raw_capabilities
        ):
            supports_tools = "tools" in raw_capabilities

    return RemoteModelRecord(
        model_id=model_id,
        display_name=_optional_text(row, "displayName", "display_name", "name") or model_id,
        description=_optional_text(row, "description"),
        context_window=_positive_int(
            row,
            "context_window",
            "context_length",
            "max_context_length",
            "inputTokenLimit",
            "input_token_limit",
        ),
        input_modalities=modalities,
        supports_tools=supports_tools,
        preview="preview" in lowered,
        deprecated=bool(row.get("deprecated", False) or row.get("deprecation_date")),
    )


def fetch_remote_model_catalog(
    provider: ProviderConfig,
    *,
    timeout_seconds: float = _DISCOVERY_TIMEOUT_SECS,
) -> RemoteModelCatalog:
    """Fetch a provider's documented, non-billable live model catalog.

    Unlike :func:`list_remote_models`, this strict API never silently turns a
    failed request into an empty successful catalog.  The unified catalog layer
    needs that distinction to label live/cache/static provenance truthfully.
    """

    if not supports_remote_model_catalog(provider.type):
        raise RemoteModelDiscoveryError(
            f"{provider.type.value} has no documented non-billable model-list adapter"
        )
    base = _model_catalog_base(provider)
    if not base:
        raise RemoteModelDiscoveryError(f"{provider.type.value} model-list endpoint is not configured")
    key = _remote_catalog_key(provider)
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))

    def remaining_timeout() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RemoteModelDiscoveryError(
                f"{provider.type.value} model-list request exceeded its timeout"
            )
        return max(0.1, remaining)

    headers: dict[str, str] = resolve_secret_mapping(provider.headers)
    lower_headers = {name.lower() for name in headers}
    if provider.type is ProviderType.ANTHROPIC:
        if key and "x-api-key" not in lower_headers:
            headers["x-api-key"] = key
        if "anthropic-version" not in lower_headers:
            headers["anthropic-version"] = "2023-06-01"
    elif key and "authorization" not in lower_headers and provider.type is not ProviderType.GOOGLE:
        headers["Authorization"] = f"Bearer {key}"

    rows: list[dict[str, Any]] = []
    if provider.type is ProviderType.ANTHROPIC:
        url = f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
        after_id: str | None = None
        seen: set[str] = set()
        for _ in range(100):
            params: dict[str, Any] = {"limit": 1000}
            if after_id:
                params["after_id"] = after_id
            payload = _safe_get_json(
                url,
                provider=provider,
                headers=headers,
                params=params,
                timeout_seconds=remaining_timeout(),
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise RemoteModelDiscoveryError("anthropic model-list returned malformed JSON")
            rows.extend(item for item in payload["data"] if isinstance(item, dict))
            if payload.get("has_more") is not True:
                break
            next_id = payload.get("last_id")
            if not isinstance(next_id, str) or not next_id or next_id in seen:
                raise RemoteModelDiscoveryError("anthropic model-list pagination was malformed")
            seen.add(next_id)
            after_id = next_id
        else:
            raise RemoteModelDiscoveryError("anthropic model-list exceeded pagination limit")
    elif provider.type is ProviderType.GOOGLE:
        url = f"{base}/models"
        page_token: str | None = None
        seen_tokens: set[str] = set()
        for _ in range(100):
            params = {"pageSize": 1000, "key": key}
            if page_token:
                params["pageToken"] = page_token
            payload = _safe_get_json(
                url,
                provider=provider,
                headers=headers,
                params=params,
                timeout_seconds=remaining_timeout(),
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
                raise RemoteModelDiscoveryError("google model-list returned malformed JSON")
            rows.extend(item for item in payload["models"] if isinstance(item, dict))
            next_token = payload.get("nextPageToken")
            if not next_token:
                break
            if not isinstance(next_token, str) or next_token in seen_tokens:
                raise RemoteModelDiscoveryError("google model-list pagination was malformed")
            seen_tokens.add(next_token)
            page_token = next_token
        else:
            raise RemoteModelDiscoveryError("google model-list exceeded pagination limit")
    else:
        if provider.type is ProviderType.OLLAMA:
            url = f"{base}/api/tags"
        elif provider.type is ProviderType.XAI:
            # Unlike /models, the documented language-model endpoint excludes
            # image/video-only generators and carries modality metadata.
            url = f"{base}/language-models"
        else:
            # Configured OpenAI-compatible/local bases conventionally carry /v1.
            url = f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
        payload = _safe_get_json(
            url,
            provider=provider,
            headers=headers,
            params=None,
            timeout_seconds=remaining_timeout(),
        )
        raw_rows: Any
        if isinstance(payload, list):
            raw_rows = payload
        elif isinstance(payload, dict):
            raw_rows = (
                payload.get("models")
                if provider.type in {ProviderType.OLLAMA, ProviderType.XAI}
                else payload.get("data")
            )
        else:
            raw_rows = None
        if not isinstance(raw_rows, list):
            raise RemoteModelDiscoveryError(
                f"{provider.type.value} model-list returned malformed JSON"
            )
        rows.extend(item for item in raw_rows if isinstance(item, dict))

    # Ollama's /api/tags proves only that a model is installed. J.A.R.N. binds
    # tools on every agent turn, so treating an installed completion-only model
    # as selectable creates a false-success setup followed by an immediate
    # runtime failure. /api/show is local and non-billable and reports the
    # authoritative capability list. Keep all installed models in the catalog,
    # but carry an explicit true/false/unknown tool-support fact for shared
    # setup, /model, doctor, and pre-turn validation.
    if provider.type is ProviderType.OLLAMA:
        enriched_rows: list[dict[str, Any]] = []
        for row in rows:
            model_name = _optional_text(row, "name", "id")
            if not model_name:
                enriched_rows.append(row)
                continue
            show_payload = _safe_post_json(
                f"{base}/api/show",
                provider=provider,
                headers=headers,
                payload={"model": model_name},
                timeout_seconds=remaining_timeout(),
            )
            if not isinstance(show_payload, dict):
                raise RemoteModelDiscoveryError(
                    "ollama capability response returned malformed JSON"
                )
            raw_capabilities = show_payload.get("capabilities")
            if raw_capabilities is not None and (
                not isinstance(raw_capabilities, list)
                or not all(isinstance(item, str) for item in raw_capabilities)
            ):
                raise RemoteModelDiscoveryError(
                    "ollama capability response returned malformed capabilities"
                )
            enriched = {**row, "_jarn_ollama_capabilities": raw_capabilities}
            model_info = show_payload.get("model_info")
            if isinstance(model_info, dict):
                context_window = next(
                    (
                        value
                        for key, value in model_info.items()
                        if isinstance(key, str)
                        and key.endswith(".context_length")
                        and isinstance(value, int)
                        and not isinstance(value, bool)
                        and value > 0
                    ),
                    None,
                )
                if context_window is not None:
                    enriched["context_window"] = context_window
            enriched_rows.append(enriched)
        rows = enriched_rows

    records: list[RemoteModelRecord] = []
    seen_models: set[str] = set()
    for row in rows:
        if provider.type is ProviderType.OLLAMA and "id" not in row and "name" in row:
            row = {**row, "id": row.get("name")}
        record = _remote_record(provider.type, row)
        if record is None or record.model_id in seen_models:
            continue
        seen_models.add(record.model_id)
        records.append(record)
    scope = _remote_scope(provider, base, key)
    return RemoteModelCatalog(
        models=tuple(records),
        provenance_label=(
            f"Live {provider.type.value} catalog from the configured provider endpoint "
            f"({len(records)} models)"
        ),
        account_fingerprint=scope,
    )


def list_remote_models(provider: ProviderConfig) -> list[str]:
    """Query a local provider's endpoint for the model ids it serves.

    - ``ollama``                       → ``GET {base_url}/api/tags`` (``.models[].name``)
    - ``lmstudio`` / ``openai_compatible`` → ``GET {base_url}/v1/models`` (``.data[].id``)

    Used to offer a selectable list instead of blind model-name entry. This must
    *fail open*: on any error (no endpoint, timeout, bad payload, unsupported
    provider type) it returns ``[]`` so the caller falls back to manual entry. It
    never raises and never blocks for long (short timeout).
    """
    base = (provider.base_url or "").strip().rstrip("/")
    if not base:
        return []

    if provider.type is ProviderType.OLLAMA:
        url = f"{base}/api/tags"
    elif provider.type in (ProviderType.LMSTUDIO, ProviderType.OPENAI_COMPATIBLE):
        # base_url for these already carries a ``/v1`` suffix (see normalize_base_url);
        # tolerate a bare host too so discovery still works if the suffix is absent.
        url = f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
    else:
        return []

    try:
        headers = resolve_secret_mapping(provider.headers)
        api_key = resolve(provider.api_key) if provider.api_key else None
    except SecretResolutionError:
        return []
    if api_key and "Authorization" not in headers and "authorization" not in headers:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        import httpx

        resp = httpx.get(url, headers=headers or None, timeout=_DISCOVERY_TIMEOUT_SECS)
        resp.raise_for_status()
        payload = resp.json()
    except Exception:  # noqa: BLE001 - network/parse errors must degrade to manual entry
        return []

    names: list[str] = []
    if provider.type is ProviderType.OLLAMA:
        for entry in payload.get("models", []) or []:
            name = entry.get("name") if isinstance(entry, dict) else None
            if name:
                names.append(name)
    else:
        for entry in payload.get("data", []) or []:
            mid = entry.get("id") if isinstance(entry, dict) else None
            if mid:
                names.append(mid)
    return names


def remote_context_window(provider: ProviderConfig, model_id: str) -> int | None:
    """Query a local provider's endpoint for ``model_id``'s context-window size.

    - ``lmstudio`` → ``GET {host}/api/v0/models`` (LM Studio's native REST API):
      prefer ``loaded_context_length`` (the size actually loaded), else
      ``max_context_length``.
    - ``ollama``   → ``POST {base}/api/show`` (``model_info`` ``"<arch>.context_length"``).

    Lets the toolbar show a real context % for a local model whose window isn't in
    the curated table. Must *fail open*: returns ``None`` on any error (no
    endpoint, timeout, bad payload, unsupported provider) so the caller falls back
    to the curated/override window or simply hides the gauge. Never raises; short
    timeout.
    """
    base = (provider.base_url or "").strip().rstrip("/")
    if not base:
        return None
    try:
        import httpx

        if provider.type is ProviderType.OLLAMA:
            resp = httpx.post(
                f"{base}/api/show", json={"name": model_id}, timeout=_DISCOVERY_TIMEOUT_SECS
            )
            resp.raise_for_status()
            info = (resp.json() or {}).get("model_info", {}) or {}
            for key, val in info.items():
                if key.endswith(".context_length") and isinstance(val, int) and val > 0:
                    return val
            return None

        if provider.type is ProviderType.LMSTUDIO:
            # LM Studio's native REST API (with context lengths) lives at /api/v0;
            # the configured base_url carries the OpenAI-compat /v1 suffix.
            host = base[: -len("/v1")] if base.endswith("/v1") else base
            resp = httpx.get(f"{host}/api/v0/models", timeout=_DISCOVERY_TIMEOUT_SECS)
            resp.raise_for_status()
            for entry in (resp.json() or {}).get("data", []) or []:
                if not isinstance(entry, dict) or entry.get("id") != model_id:
                    continue
                win = entry.get("loaded_context_length") or entry.get("max_context_length")
                return win if isinstance(win, int) and win > 0 else None
            return None
    except Exception:  # noqa: BLE001 - network/parse errors degrade to "unknown"
        return None
    return None


# ---------------------------------------------------------------------------
# Demo provider — canned responses for deterministic VHS recordings.
# Gated EXCLUSIVELY behind the JARN_DEMO=1 environment variable.  This must
# never be reachable through the normal config system or any config key so it
# cannot accidentally activate in a real user session.
# ---------------------------------------------------------------------------

#: Profile name used for the canned-response demo provider.
DEMO_PROFILE: str = "demo"

#: The file the demo tape edits (matches the tape prompt "add input validation
#: to server.py"). Kept as a relative name so the recorder can run in a scratch
#: dir; the demo model only needs it for the canned tool-call args.
_DEMO_TARGET_FILE = "server.py"

#: The validated ``server.py`` the demo "writes" — the content of the money-shot
#: diff. Deterministic so the recorded GIF is reproducible.
_DEMO_SERVER_PY = """\
from pydantic import BaseModel, field_validator


class CreateItem(BaseModel):
    name: str
    price: float

    @field_validator("price")
    @classmethod
    def price_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("price must be positive")
        return v
"""

#: Prose replies returned by the demo model, in order (see ``_demo_messages``).
#: Crafted to match the money-shot tape: plan approval → streamed diff (driven by
#: the write_file tool call below) → verified badge → /cost.
_DEMO_CANNED_RESPONSES: tuple[str, ...] = (
    (
        "Here is my plan:\n"
        "1. Add a typed `pydantic` request model to `server.py`.\n"
        "2. Reject invalid input automatically (422) via a field validator.\n"
        "3. Keep the change minimal and self-contained.\n\n"
        "Shall I proceed?"
    ),
    "Applying the change to `server.py`…",
    "✓ verified — `server.py` updated, 4 tests passing (0.3 s).",
    "Total cost this session: $0.00 (demo mode — no real API calls made).",
)


def _demo_messages() -> list[Any]:
    """Build the ordered script the demo model replays, one message per turn.

    The money-shot DIFF is driven by a real ``write_file`` **tool call** (not
    prose), so the recorded session shows a genuine diff panel.  Returned as
    ``AIMessage`` objects; ``Any`` in the signature avoids importing langchain at
    module import time (kept lazy like the rest of the factory).
    """
    from langchain_core.messages import AIMessage

    plan, applying, verified, cost = _DEMO_CANNED_RESPONSES
    return [
        # 1. Plan (plan-mode approval step in the tape).
        AIMessage(content=plan),
        # 2. The DIFF: a real write_file tool call the front-end renders as a diff.
        AIMessage(
            content=applying,
            tool_calls=[
                {
                    "name": "write_file",
                    "args": {
                        "file_path": _DEMO_TARGET_FILE,
                        "content": _DEMO_SERVER_PY,
                    },
                    "id": "demo_write_1",
                    "type": "tool_call",
                }
            ],
        ),
        # 3. Verified badge (T-3-2) after the edit + self-verify.
        AIMessage(content=verified),
        # 4. Closing summary (the tape then runs /cost, a slash command).
        AIMessage(content=cost),
    ]


def is_demo_active() -> bool:
    """Return ``True`` iff the ``JARN_DEMO=1`` env-var gate is open.

    This is the **only** check used to decide whether demo mode is available.
    No config key, no fallback path.
    """
    return os.environ.get("JARN_DEMO") == "1"


def demo_provider_config() -> ProviderConfig | None:
    """Return a synthetic :class:`ProviderConfig` for the canned-response demo model.

    Returns ``None`` — and therefore makes the demo model **completely
    unreachable** — whenever ``JARN_DEMO`` is not set to ``"1"``.

    The returned config uses ``ProviderType.OPENAI_COMPATIBLE`` as its
    internal type tag so the factory can identify it without adding a new
    ``ProviderType`` enum value to the config schema.  No real API key or
    endpoint is needed or used.

    Security invariant (also verified by ``test_demo_provider_gated``):
      • ``JARN_DEMO=1``  → returns a :class:`ProviderConfig` (demo available)
      • env unset / not "1" → returns ``None``  (demo never reachable)
    """
    if not is_demo_active():
        return None
    return ProviderConfig(type=ProviderType.OPENAI_COMPATIBLE)


def build_demo_model() -> BaseChatModel:
    """Construct the canned-response chat model used when ``JARN_DEMO=1``.

    A tiny :class:`~langchain_core.language_models.fake_chat_models.GenericFakeChatModel`
    subclass that (a) **ignores** ``bind_tools`` (deepagents/langgraph calls it —
    the stock fake would raise ``NotImplementedError``) and (b) streams the
    scripted ``_demo_messages`` including a real ``write_file`` tool call, so the
    session runs with **no network and no API key**.

    Callers must gate on :func:`is_demo_active` first — this builder itself does
    not check the env var (so it stays unit-testable), but the only production
    call sites (:meth:`ModelFactory.build` / :meth:`ModelFactory.build_main`) do.
    """
    import re

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessageChunk
    from langchain_core.messages.tool import tool_call_chunk
    from langchain_core.outputs import ChatGenerationChunk

    class _DemoChatModel(GenericFakeChatModel):  # type: ignore[misc]
        @property
        def _llm_type(self) -> str:
            return "jarn-demo-canned"

        def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
            # The demo replays a fixed script; tool schemas are irrelevant. Return
            # self so deepagents/langgraph can bind without NotImplementedError.
            return self

        def _stream(  # type: ignore[override]
            self,
            messages: Any,
            stop: Any = None,
            run_manager: Any = None,
            **kwargs: Any,
        ) -> Any:
            # GenericFakeChatModel._stream drops ``.tool_calls`` (it only streams
            # additional_kwargs). Re-implement so the money-shot tool call reaches
            # the graph on the streaming path as well as invoke().
            result = self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
            msg = result.generations[0].message
            content = msg.content if isinstance(msg.content, str) else ""
            if content:
                for token in re.split(r"(\s)", content):
                    chunk = ChatGenerationChunk(message=AIMessageChunk(content=token, id=msg.id))
                    if run_manager:
                        run_manager.on_llm_new_token(token, chunk=chunk)
                    yield chunk
            tool_calls = getattr(msg, "tool_calls", None) or []
            if tool_calls:
                tool_call_chunks = [
                    tool_call_chunk(
                        name=tc["name"],
                        args=json.dumps(tc.get("args", {})),
                        id=tc.get("id"),
                        index=idx,
                    )
                    for idx, tc in enumerate(tool_calls)
                ]
                yield ChatGenerationChunk(
                    message=AIMessageChunk(content="", id=msg.id, tool_call_chunks=tool_call_chunks)
                )

    return _DemoChatModel(messages=iter(_demo_messages()))


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
    ProviderType.OPENCODE,
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
    working_directory: Path | None = None
    _cache: dict[str, Any] = field(default_factory=dict)

    #: Cache key for the canned demo model (JARN_DEMO=1).
    _DEMO_CACHE_KEY = "__jarn_demo__"

    def _demo_model(self) -> BaseChatModel:
        """Return (and cache) the canned demo model — no provider/key needed.

        A single instance is cached so successive turns advance through the same
        scripted message iterator (plan → diff → verified → cost).
        """
        cached = self._cache.get(self._DEMO_CACHE_KEY)
        if cached is None:
            cached = build_demo_model()
            self._cache[self._DEMO_CACHE_KEY] = cached
        return cached

    def build(self, ref: str) -> BaseChatModel:
        """Build (or return cached) chat model for a fully/partly-qualified ref."""
        # JARN_DEMO=1: bypass real provider resolution entirely (no key, no
        # endpoint) and return the canned demo model. Gated ONLY by the env var
        # (see is_demo_active); never reachable in a normal user session.
        if is_demo_active():
            return self._demo_model()
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
        # Demo mode needs no configured model — short-circuit before resolving the
        # ref so an empty/keyless config still yields the canned model.
        if is_demo_active():
            return self._demo_model()
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

    def invalidate_cache(self) -> None:
        """Drop cached chat models (e.g. after ``/key`` or config reload)."""
        self._cache.clear()

    # -- internals ----------------------------------------------------------

    def _construct(self, ref: ModelRef, provider: ProviderConfig) -> BaseChatModel:
        from langchain.chat_models import init_chat_model

        try:
            kwargs: dict[str, Any] = dict(provider.extra)
            kwargs.setdefault("max_retries", self.default_max_retries)
            if provider.headers:
                kwargs.setdefault(
                    "default_headers", resolve_secret_mapping(provider.headers)
                )
            return self._construct_inner(ref, provider, kwargs, init_chat_model)
        except SecretResolutionError as exc:
            from jarn.config.secrets import redact_secrets

            raise ModelResolutionError(
                redact_secrets(f"Cannot build {ref.qualified!r}: {exc}")
            ) from exc

    def _construct_inner(self, ref, provider, kwargs, init_chat_model) -> BaseChatModel:

        if provider.type is ProviderType.CODEX_SUBSCRIPTION:
            from jarn.providers.codex_subscription import (
                CodexSubscriptionChatModel,
                ensure_codex_harness_profile,
            )

            # Managed ChatGPT credentials stay inside Codex's own auth store;
            # api_key/base_url/default_headers are intentionally irrelevant here.
            kwargs.pop("max_retries", None)
            kwargs.pop("default_headers", None)
            command = kwargs.pop("codex_command", None)
            effort = kwargs.pop("reasoning_effort", "medium")
            timeout = kwargs.pop("timeout_seconds", kwargs.pop("timeout", 300.0))
            service_name = kwargs.pop("service_name", "jarn")
            if kwargs:
                unknown = ", ".join(sorted(kwargs))
                raise ModelResolutionError(
                    f"Unsupported codex_subscription provider options: {unknown}"
                )
            ensure_codex_harness_profile()
            return CodexSubscriptionChatModel(
                model_name=ref.model_id,
                codex_command=command,
                working_directory=str(self.working_directory or Path.cwd()),
                reasoning_effort=str(effort),
                timeout_seconds=float(timeout),
                service_name=str(service_name),
            )
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
            # Ask for token usage in the STREAMED response (OpenAI
            # stream_options.include_usage). Without it, OpenAI-compatible servers
            # (LM Studio, vLLM, OpenRouter, …) stream no usage metadata, so cost
            # tracking records nothing — /cost and the budget gauge stay at 0 tok.
            kwargs.setdefault("stream_usage", True)
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

        self._inject_keep_warm(provider.type, kwargs)

        try:
            return init_chat_model(ref.model_id, model_provider=model_provider, **kwargs)
        except Exception as exc:  # noqa: BLE001 - surface a clean message
            suggestion = suggest_slug(provider.type, ref.model_id)
            suffix = f" — {suggestion}" if suggestion else ""
            raise ModelResolutionError(
                f"Model {ref.model_id!r} not found for provider {provider.type.value!r}{suffix}"
            ) from exc

    def _inject_keep_warm(self, provider_type: ProviderType, kwargs: dict[str, Any]) -> None:
        """Keep a local model + its prefix cache resident between turns.

        For Ollama this is the ``keep_alive`` kwarg; for LM Studio it is a
        request-body ``ttl`` (merged into ``extra_body`` without clobbering any
        user-provided keys). No-op when prompt caching is off, ``keep_alive`` is
        0, or the provider caches server-side / via middleware.
        """
        routing = self.config.routing
        if routing.prompt_cache == "off" or routing.keep_alive <= 0:
            return
        strategy = prompt_cache_strategy(provider_type)
        if strategy == "ollama_keepalive":
            kwargs.setdefault("keep_alive", routing.keep_alive)
        elif strategy == "lmstudio_ttl":
            extra_body = dict(kwargs.get("extra_body") or {})
            extra_body.setdefault("ttl", routing.keep_alive)
            kwargs["extra_body"] = extra_body
