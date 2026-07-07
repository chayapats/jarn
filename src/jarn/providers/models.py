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

import json
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from jarn.config.loader import ConfigError
from jarn.config.schema import Config, ProviderConfig, ProviderType
from jarn.config.secrets import SecretResolutionError, resolve

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
    return ref[len(prefix):] if ref.startswith(prefix) else ref


#: How long to wait when probing a local endpoint for its model list. Short so an
#: unreachable endpoint degrades to manual entry quickly rather than hanging setup.
_DISCOVERY_TIMEOUT_SECS = 2.0


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

    headers: dict[str, str] = dict(provider.headers)
    try:
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
_DEMO_SERVER_PY = '''\
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
'''

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
                    chunk = ChatGenerationChunk(
                        message=AIMessageChunk(content=token, id=msg.id)
                    )
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
                    message=AIMessageChunk(
                        content="", id=msg.id, tool_call_chunks=tool_call_chunks
                    )
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

    def fallback_models(self) -> list[BaseChatModel]:
        """Materialize the configured fallback chain (skipping unbuildable ones)."""
        models: list[BaseChatModel] = []
        for ref in self.config.routing.fallback:
            try:
                models.append(self.build(ref))
            except SecretResolutionError:
                raise
            except ConfigError:
                raise
            except ModelResolutionError as exc:
                if isinstance(exc.__cause__, SecretResolutionError):
                    raise exc.__cause__ from exc
                # A broken fallback ref must not prevent the agent from starting.
                continue
            except Exception as exc:
                logger.warning("Skipping fallback model %r: %s", ref, exc)
                continue
        return models

    # -- internals ----------------------------------------------------------

    def _construct(self, ref: ModelRef, provider: ProviderConfig) -> BaseChatModel:
        from langchain.chat_models import init_chat_model

        kwargs: dict[str, Any] = dict(provider.extra)
        kwargs.setdefault("max_retries", self.default_max_retries)
        if provider.headers:
            kwargs.setdefault("default_headers", dict(provider.headers))

        try:
            return self._construct_inner(ref, provider, kwargs, init_chat_model)
        except SecretResolutionError as exc:
            from jarn.config.secrets import redact_secrets

            raise ModelResolutionError(
                redact_secrets(f"Cannot build {ref.qualified!r}: {exc}")
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
