"""Unified live/cache/fallback model discovery service."""

from __future__ import annotations

import hashlib
import math
import os
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jarn.catalog.cache import ModelCatalogCache
from jarn.catalog.models import (
    CatalogError,
    CatalogSource,
    ModelCatalogEntry,
    ModelCatalogSnapshot,
    ReasoningEffort,
)
from jarn.config.defaults import DEFAULT_MODELS
from jarn.config.schema import Config, ProviderConfig, ProviderType
from jarn.config.secrets import redact_secrets
from jarn.errors import ErrorCode
from jarn.providers import (
    RemoteModelCatalog,
    RemoteModelDiscoveryError,
    RemoteModelRecord,
    fetch_remote_model_catalog,
    parse_model_ref,
    qualify_model_ref,
    remote_catalog_account_fingerprint,
    strip_profile,
    supports_remote_model_catalog,
)
from jarn.providers.codex_subscription import (
    CodexAppServer,
    CodexProtocolError,
    CodexSubscriptionError,
    require_chatgpt_subscription,
)

_DEFAULT_TTL_SECONDS = 3600
_DEFAULT_CATALOG_TIMEOUT_SECONDS = 20.0
_MIN_CATALOG_TIMEOUT_SECONDS = 1.0
_MAX_CATALOG_TIMEOUT_SECONDS = 120.0
_MAX_CODEX_PAGES = 100


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def catalog_timeout_seconds(value: float | str | None = None) -> float:
    """Return the bounded catalog timeout from an explicit value or the env.

    ``JARN_CATALOG_TIMEOUT_SECONDS`` is intentionally process-wide so setup,
    ``/model``, doctor, and pre-turn checks share one operator-controlled upper
    bound. Invalid and non-finite values fall back to 20 seconds; valid values
    are clamped to 1..120 seconds.
    """

    raw: float | str | None = (
        os.environ.get("JARN_CATALOG_TIMEOUT_SECONDS") if value is None else value
    )
    if raw is None or raw == "":
        return _DEFAULT_CATALOG_TIMEOUT_SECONDS
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_CATALOG_TIMEOUT_SECONDS
    if not math.isfinite(parsed):
        return _DEFAULT_CATALOG_TIMEOUT_SECONDS
    return min(
        _MAX_CATALOG_TIMEOUT_SECONDS,
        max(_MIN_CATALOG_TIMEOUT_SECONDS, parsed),
    )


def account_fingerprint(provider_profile: str, account: dict[str, Any]) -> str:
    """Hash a non-token account scope so cache identities are not exposed."""

    workspace = account.get("workspace")
    workspace = workspace if isinstance(workspace, dict) else {}
    stable_scope = (
        workspace.get("id")
        or account.get("workspaceId")
        or account.get("accountId")
        or account.get("id")
        or account.get("email")
        or f"{account.get('type', 'unknown')}:{account.get('planType', 'unknown')}"
    )
    material = f"{provider_profile}\0{stable_scope}".encode()
    return hashlib.sha256(material).hexdigest()[:20]


def _endpoint_fingerprint(provider_profile: str, provider: ProviderConfig) -> str:
    material = f"{provider_profile}\0{provider.type.value}\0{provider.base_url or ''}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def _reasoning_efforts(raw: Any) -> tuple[ReasoningEffort, ...]:
    if not isinstance(raw, list):
        raise CodexProtocolError("model/list supportedReasoningEfforts must be an array")
    out: list[ReasoningEffort] = []
    seen: set[str] = set()
    for item in raw:
        value: Any
        description: Any
        if isinstance(item, str):
            value, description = item, None
        elif isinstance(item, dict):
            value = item.get("reasoningEffort") or item.get("value")
            description = item.get("description")
        else:
            raise CodexProtocolError("model/list reasoning effort must be a string or object")
        if not isinstance(value, str) or not value:
            raise CodexProtocolError("model/list reasoning effort has no value")
        if value not in seen:
            seen.add(value)
            out.append(
                ReasoningEffort(
                    value=value,
                    description=str(description) if description else None,
                )
            )
    return tuple(out)


def _string_tuple(raw: Any, field: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise CodexProtocolError(f"model/list {field} must be an array of strings")
    return tuple(dict.fromkeys(raw))


def _service_tiers(raw: Any) -> tuple[str, ...]:
    """Normalize both shipped Codex service-tier protocol shapes.

    Older app-server versions returned tier identifiers as strings.  Codex
    0.147.0 returns objects with ``id``, ``name``, and ``description``.  Keep
    the catalog's versioned public/cache representation string-only by using
    the human-readable name when present and the stable id otherwise.  Unknown
    object fields are forward-compatible metadata, but known fields must keep
    their documented scalar types and every object must identify a tier.
    """

    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise CodexProtocolError("model/list serviceTiers must be an array")
    values: list[str] = []
    for item in raw:
        value: str | None
        if isinstance(item, str):
            value = item
        elif isinstance(item, dict):
            tier_id = item.get("id")
            name = item.get("name")
            description = item.get("description")
            if tier_id is not None and not isinstance(tier_id, str):
                raise CodexProtocolError("model/list service tier id must be a string")
            if name is not None and not isinstance(name, str):
                raise CodexProtocolError("model/list service tier name must be a string")
            if description is not None and not isinstance(description, str):
                raise CodexProtocolError(
                    "model/list service tier description must be a string"
                )
            value = name or tier_id
        else:
            raise CodexProtocolError(
                "model/list service tier must be a string or object"
            )
        if not isinstance(value, str) or not value.strip():
            raise CodexProtocolError("model/list service tier has no id or name")
        normalized = value.strip()
        if normalized not in values:
            values.append(normalized)
    return tuple(values)


def _codex_entry(
    provider_profile: str,
    raw: dict[str, Any],
) -> ModelCatalogEntry:
    model_value = raw.get("model") or raw.get("id")
    if not isinstance(model_value, str) or not model_value:
        raise CodexProtocolError("model/list entry has no model/id")
    catalog_id = raw.get("id")
    if catalog_id is not None and not isinstance(catalog_id, str):
        raise CodexProtocolError("model/list entry id must be a string")
    display = raw.get("displayName") or model_value
    if not isinstance(display, str):
        raise CodexProtocolError("model/list entry displayName must be a string")
    efforts = _reasoning_efforts(raw.get("supportedReasoningEfforts") or [])
    default_effort = raw.get("defaultReasoningEffort")
    if default_effort is not None and not isinstance(default_effort, str):
        raise CodexProtocolError("model/list defaultReasoningEffort must be a string")
    effort_values = {effort.value for effort in efforts}
    if default_effort and default_effort not in effort_values:
        raise CodexProtocolError(
            "model/list defaultReasoningEffort is not in supportedReasoningEfforts"
        )
    context_window = raw.get("contextWindow")
    if context_window is not None and (not isinstance(context_window, int) or context_window <= 0):
        raise CodexProtocolError("model/list contextWindow must be a positive integer")
    personality = raw.get("supportsPersonality")
    if personality is not None and not isinstance(personality, bool):
        raise CodexProtocolError("model/list supportsPersonality must be boolean")
    description = raw.get("description")
    if description is not None and not isinstance(description, str):
        raise CodexProtocolError("model/list description must be a string")
    bool_fields: dict[str, bool] = {}
    for field, default in (
        ("hidden", False),
        ("isDefault", False),
        ("preview", False),
        ("deprecated", False),
    ):
        value = raw.get(field, default)
        if not isinstance(value, bool):
            raise CodexProtocolError(f"model/list {field} must be boolean")
        bool_fields[field] = value
    availability = raw.get("accountAvailable", raw.get("available", True))
    if not isinstance(availability, bool):
        raise CodexProtocolError("model/list account availability must be boolean")
    replacement = raw.get("replacementModel") or raw.get("replacement")
    if replacement is not None and not isinstance(replacement, str):
        raise CodexProtocolError("model/list replacement model must be a string")
    return ModelCatalogEntry(
        provider_profile=provider_profile,
        catalog_id=catalog_id,
        model_id=model_value,
        ref=qualify_model_ref(model_value, provider_profile),
        display_name=display,
        description=description,
        hidden=bool_fields["hidden"],
        is_default=bool_fields["isDefault"],
        account_available=availability,
        default_reasoning_effort=default_effort,
        supported_reasoning_efforts=efforts,
        input_modalities=_string_tuple(raw.get("inputModalities"), "inputModalities"),
        supports_personality=personality,
        preview=bool_fields["preview"],
        deprecated=bool_fields["deprecated"],
        replacement_ref=(qualify_model_ref(replacement, provider_profile) if replacement else None),
        context_window=context_window,
        service_tiers=_service_tiers(raw.get("serviceTiers")),
        billing_mode="chatgpt_subscription",
        availability_label="Available for this ChatGPT account",
    )


class _CatalogFetchError(RuntimeError):
    def __init__(
        self,
        cause: BaseException,
        *,
        account_scope: str | None = None,
    ) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.account_scope = account_scope


class ModelCatalogService:
    """The one model-list abstraction used across every product surface."""

    def __init__(
        self,
        *,
        cache: ModelCatalogCache | None = None,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        clock: Callable[[], datetime] = _utc_now,
        timeout_seconds: float | None = None,
        on_wait: Callable[[str], None] | None = None,
    ) -> None:
        self.cache = cache or ModelCatalogCache()
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.clock = clock
        self.timeout_seconds = catalog_timeout_seconds(timeout_seconds)
        self.on_wait = on_wait
        # Exact billable-validation evidence must remain usable for the current
        # transactional setup even if the optional disk cache cannot be written
        # (read-only home, quota, etc.).  It contains only model metadata and a
        # one-way account/endpoint fingerprint, never a credential.
        self._session_validations: dict[tuple[str, str], ModelCatalogSnapshot] = {}

    @contextmanager
    def _network_wait(self, provider_profile: str):
        """Emit one progress notice when a catalog request exceeds one second."""

        timer: threading.Timer | None = None
        if self.on_wait is not None:
            callback = self.on_wait

            def notify() -> None:
                try:
                    callback(
                        f"Still checking {provider_profile} model availability "
                        f"(timeout {self.timeout_seconds:g}s)…"
                    )
                except Exception:  # noqa: BLE001 - progress must never break discovery
                    return

            timer = threading.Timer(1.0, notify)
            timer.daemon = True
            timer.start()
        try:
            yield
        finally:
            if timer is not None:
                timer.cancel()

    def get_catalog(
        self,
        provider_profile: str,
        provider: ProviderConfig,
        *,
        include_hidden: bool = False,
        allow_stale_cache: bool = True,
        refresh_live: bool = True,
        codex_command: str | Sequence[str] | None = None,
        cwd: str | Path | None = None,
    ) -> ModelCatalogSnapshot:
        """Fetch live data first, then clearly labeled cache/static fallback."""

        if not refresh_live:
            return self._offline_catalog(
                provider_profile,
                provider,
                allow_stale_cache=allow_stale_cache,
                codex_command=codex_command,
                cwd=cwd,
            )

        if provider.type is ProviderType.CODEX_SUBSCRIPTION:
            raw_command = codex_command or provider.extra.get("codex_command")
            command: str | Sequence[str] | None
            if (
                raw_command is None
                or isinstance(raw_command, str)
                or (
                    isinstance(raw_command, (list, tuple))
                    and all(isinstance(part, str) for part in raw_command)
                )
            ):
                command = raw_command
            else:
                return self._fallback(
                    provider_profile,
                    provider,
                    ValueError("codex_command must be a path string or argv string list"),
                    account_scope=None,
                    allow_stale_cache=allow_stale_cache,
                )
            try:
                with self._network_wait(provider_profile):
                    snapshot = self._codex_catalog(
                        provider_profile,
                        include_hidden=include_hidden,
                        command=command,
                        cwd=cwd,
                    )
            except _CatalogFetchError as failure:
                return self._fallback(
                    provider_profile,
                    provider,
                    failure.cause,
                    account_scope=failure.account_scope,
                    allow_stale_cache=allow_stale_cache,
                )
            self.cache.save(snapshot)
            return snapshot

        if provider.type in {
            ProviderType.OLLAMA,
            ProviderType.LMSTUDIO,
        }:
            try:
                with self._network_wait(provider_profile):
                    remote = fetch_remote_model_catalog(
                        provider,
                        timeout_seconds=self.timeout_seconds,
                    )
            except RemoteModelDiscoveryError as exc:
                return self._fallback(
                    provider_profile,
                    provider,
                    exc,
                    account_scope=_endpoint_fingerprint(provider_profile, provider),
                    allow_stale_cache=allow_stale_cache,
                )
            snapshot = self._local_catalog(
                provider_profile,
                provider,
                remote.models,
            )
            self.cache.save(snapshot)
            return snapshot

        account_scope: str | None = None
        with suppress(RemoteModelDiscoveryError):
            account_scope = remote_catalog_account_fingerprint(provider)
        if supports_remote_model_catalog(provider.type):
            try:
                with self._network_wait(provider_profile):
                    remote = fetch_remote_model_catalog(
                        provider,
                        timeout_seconds=self.timeout_seconds,
                    )
            except RemoteModelDiscoveryError as exc:
                return self._fallback(
                    provider_profile,
                    provider,
                    exc,
                    account_scope=account_scope,
                    allow_stale_cache=allow_stale_cache,
                )
            snapshot = self._provider_catalog(provider_profile, provider, remote)
            self.cache.save(snapshot)
            return snapshot

        return self._fallback(
            provider_profile,
            provider,
            RuntimeError(
                "this provider has no documented non-billable model-list adapter; "
                "a recent successful setup validation is required"
            ),
            account_scope=account_scope,
            allow_stale_cache=allow_stale_cache,
        )

    def _codex_catalog(
        self,
        provider_profile: str,
        *,
        include_hidden: bool,
        command: str | Sequence[str] | None,
        cwd: str | Path | None,
    ) -> ModelCatalogSnapshot:
        account_scope: str | None = None
        deadline = time.monotonic() + self.timeout_seconds

        def apply_remaining_timeout(server: CodexAppServer) -> None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Codex model catalog exceeded its {self.timeout_seconds:g}s timeout"
                )
            # CodexAppServer applies its timeout independently to each request.
            # Reset it to this operation's remaining budget so pagination cannot
            # multiply the configured catalog timeout by the page count.
            server.timeout_seconds = remaining

        try:
            with CodexAppServer(
                command=command,
                cwd=cwd,
                timeout_seconds=self.timeout_seconds,
            ) as server:
                apply_remaining_timeout(server)
                account = require_chatgpt_subscription(server.account(refresh=False))
                account_scope = account_fingerprint(provider_profile, account)
                rows: list[dict[str, Any]] = []
                cursor: str | None = None
                seen_cursors: set[str] = set()
                for _page in range(_MAX_CODEX_PAGES):
                    apply_remaining_timeout(server)
                    page, next_cursor = server.model_list(
                        limit=100,
                        include_hidden=include_hidden,
                        cursor=cursor,
                    )
                    rows.extend(page)
                    if next_cursor is None:
                        break
                    if next_cursor in seen_cursors:
                        raise CodexProtocolError("model/list pagination cursor repeated")
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
                else:
                    raise CodexProtocolError("model/list exceeded the pagination limit")
        except (CodexSubscriptionError, TimeoutError) as exc:
            raise _CatalogFetchError(exc, account_scope=account_scope) from exc

        seen_models: set[str] = set()
        entries: list[ModelCatalogEntry] = []
        try:
            for row in rows:
                entry = _codex_entry(provider_profile, row)
                if entry.hidden and not include_hidden:
                    continue
                if entry.account_available is not True:
                    continue
                if entry.model_id in seen_models:
                    continue
                seen_models.add(entry.model_id)
                entries.append(entry)
        except CodexProtocolError as exc:
            raise _CatalogFetchError(exc, account_scope=account_scope) from exc
        return self._snapshot(
            provider_profile=provider_profile,
            provider_type=ProviderType.CODEX_SUBSCRIPTION.value,
            source=CatalogSource.CODEX_LIVE,
            account_scope=account_scope,
            models=tuple(entries),
            verified=True,
            label=f"Live ChatGPT account catalog ({len(entries)} models)",
        )

    def _local_catalog(
        self,
        provider_profile: str,
        provider: ProviderConfig,
        models: Sequence[RemoteModelRecord],
    ) -> ModelCatalogSnapshot:
        defaults_profile = (
            provider_profile if provider_profile in DEFAULT_MODELS else provider.type.value
        )
        raw_default = DEFAULT_MODELS.get(defaults_profile, {}).get("main")
        default_ref = (
            qualify_model_ref(strip_profile(raw_default, defaults_profile), provider_profile)
            if raw_default
            else None
        )
        entries_by_id: dict[str, ModelCatalogEntry] = {}
        for item in models:
            if item.model_id in entries_by_id:
                continue
            ref = qualify_model_ref(item.model_id, provider_profile)
            tool_label = "Reported by the local endpoint"
            if provider.type is ProviderType.OLLAMA:
                if item.supports_tools is True:
                    tool_label = "Reported by Ollama; tool support verified"
                elif item.supports_tools is False:
                    tool_label = "Reported by Ollama; tools are not supported"
                else:
                    tool_label = "Reported by Ollama; tool support unverified"
            entries_by_id[item.model_id] = ModelCatalogEntry(
                provider_profile=provider_profile,
                model_id=item.model_id,
                ref=ref,
                display_name=item.display_name or item.model_id,
                description=item.description,
                is_default=ref == default_ref,
                account_available=True,
                input_modalities=item.input_modalities,
                supports_tools=item.supports_tools,
                preview=item.preview,
                deprecated=item.deprecated,
                context_window=item.context_window,
                billing_mode="local",
                availability_label=tool_label,
            )
        entries = tuple(entries_by_id.values())
        label = f"Live local endpoint catalog ({len(entries)} models)"
        if provider.type is ProviderType.OLLAMA:
            tool_capable = sum(entry.supports_tools is True for entry in entries)
            label = (
                f"Live local endpoint catalog ({len(entries)} installed; "
                f"{tool_capable} tool-capable)"
            )
        return self._snapshot(
            provider_profile=provider_profile,
            provider_type=provider.type.value,
            source=CatalogSource.LOCAL_LIVE,
            account_scope=_endpoint_fingerprint(provider_profile, provider),
            models=entries,
            verified=True,
            label=label,
        )

    def _provider_catalog(
        self,
        provider_profile: str,
        provider: ProviderConfig,
        remote: RemoteModelCatalog,
    ) -> ModelCatalogSnapshot:
        """Convert a configured provider response without static invention."""

        defaults_profile = (
            provider_profile if provider_profile in DEFAULT_MODELS else provider.type.value
        )
        raw_default = DEFAULT_MODELS.get(defaults_profile, {}).get("main")
        default_ref = (
            qualify_model_ref(strip_profile(raw_default, defaults_profile), provider_profile)
            if raw_default
            else None
        )
        entries = tuple(
            ModelCatalogEntry(
                provider_profile=provider_profile,
                model_id=item.model_id,
                ref=qualify_model_ref(item.model_id, provider_profile),
                display_name=item.display_name or item.model_id,
                description=item.description,
                is_default=(qualify_model_ref(item.model_id, provider_profile) == default_ref),
                account_available=True,
                input_modalities=item.input_modalities,
                preview=item.preview,
                deprecated=item.deprecated,
                context_window=item.context_window,
                billing_mode="api_key",
                availability_label="Reported by the configured provider endpoint",
            )
            for item in remote.models
        )
        return self._snapshot(
            provider_profile=provider_profile,
            provider_type=provider.type.value,
            source=CatalogSource.PROVIDER_LIVE,
            account_scope=remote.account_fingerprint,
            models=entries,
            verified=True,
            label=remote.provenance_label,
        )

    def record_billable_validation(
        self,
        provider_profile: str,
        provider: ProviderConfig,
        model_ref: str,
    ) -> ModelCatalogSnapshot:
        """Persist exact, time-bounded evidence from one successful model call.

        This is used only for providers without a documented non-billable list
        endpoint.  It never expands one successful call into a claim about other
        static defaults and is bound to a hash of the endpoint and credential.
        """

        parsed = parse_model_ref(model_ref, default_profile=provider_profile)
        if parsed.profile != provider_profile:
            raise ValueError("validated model does not belong to the provider profile")
        scope = remote_catalog_account_fingerprint(provider)
        snapshot = self._snapshot(
            provider_profile=provider_profile,
            provider_type=provider.type.value,
            source=CatalogSource.BILLABLE_VALIDATION,
            account_scope=scope,
            models=(
                ModelCatalogEntry(
                    provider_profile=provider_profile,
                    model_id=parsed.model_id,
                    ref=parsed.qualified,
                    display_name=parsed.model_id,
                    is_default=True,
                    account_available=True,
                    billing_mode="api_key",
                    availability_label=("Verified by one successful billable provider request"),
                ),
            ),
            verified=True,
            label=(
                "Successful billable validation (exact selected model only; "
                "not a provider-wide catalog)"
            ),
        )
        self._session_validations[(provider_profile, scope)] = snapshot
        self.cache.save(snapshot)
        return snapshot

    @staticmethod
    def supports_live_catalog(provider: ProviderConfig) -> bool:
        return provider.type is ProviderType.CODEX_SUBSCRIPTION or supports_remote_model_catalog(
            provider.type
        )

    @staticmethod
    def configured_routes(config: Config) -> tuple[tuple[str, str, str | None], ...]:
        """Return every runtime route that must be checked before agent work."""

        main = config.resolved_main_model()
        routes: list[tuple[str, str, str | None]] = []
        if main:
            parsed = parse_model_ref(main, default_profile=config.default_profile)
            provider = config.providers.get(parsed.profile)
            effort_raw = provider.extra.get("reasoning_effort") if provider else None
            routes.append(("main", parsed.qualified, str(effort_raw) if effort_raw else None))
        if config.routing.subagent:
            parsed = parse_model_ref(
                config.routing.subagent,
                default_profile=config.default_profile,
            )
            routes.append(("subagent", parsed.qualified, None))
        if config.routing.summarizer:
            parsed = parse_model_ref(
                config.routing.summarizer,
                default_profile=config.default_profile,
            )
            routes.append(("summarizer", parsed.qualified, None))
        for index, ref in enumerate(config.routing.fallback):
            parsed = parse_model_ref(ref, default_profile=config.default_profile)
            routes.append((f"fallback[{index}]", parsed.qualified, None))
        return tuple(routes)

    def get_catalogs_for_routes(
        self,
        config: Config,
        *,
        include_hidden: bool = False,
        allow_stale_cache: bool = True,
        refresh_live: bool = True,
        cwd: str | Path | None = None,
    ) -> dict[str, ModelCatalogSnapshot]:
        """Fetch one shared snapshot for every provider referenced by routing."""

        profiles: list[str] = []
        for _route, ref, _effort in self.configured_routes(config):
            profile = parse_model_ref(ref, default_profile=config.default_profile).profile
            if profile not in profiles:
                profiles.append(profile)
        targets = [
            (profile, config.providers[profile])
            for profile in profiles
            if profile in config.providers
        ]

        def load(target: tuple[str, ProviderConfig]) -> tuple[str, ModelCatalogSnapshot]:
            profile, provider = target
            return profile, self.get_catalog(
                profile,
                provider,
                include_hidden=include_hidden,
                allow_stale_cache=allow_stale_cache,
                refresh_live=refresh_live,
                cwd=cwd,
            )

        if not targets:
            return {}
        with ThreadPoolExecutor(max_workers=min(4, len(targets))) as pool:
            return dict(pool.map(load, targets))

    def _offline_catalog(
        self,
        provider_profile: str,
        provider: ProviderConfig,
        *,
        allow_stale_cache: bool,
        codex_command: str | Sequence[str] | None,
        cwd: str | Path | None,
    ) -> ModelCatalogSnapshot:
        """Load account-scoped evidence without calling a live model-list endpoint.

        Codex ``account/read(refreshToken=false)`` is used only to derive the
        local account scope for its cache. It does not refresh credentials or
        call ``model/list``. Other provider scopes are derived locally from the
        configured endpoint and credential.
        """

        account_scope: str | None = None
        scope_error: BaseException | None = None
        if provider.type is ProviderType.CODEX_SUBSCRIPTION:
            raw_command = codex_command or provider.extra.get("codex_command")
            command: str | Sequence[str] | None
            if (
                raw_command is None
                or isinstance(raw_command, str)
                or (
                    isinstance(raw_command, (list, tuple))
                    and all(isinstance(part, str) for part in raw_command)
                )
            ):
                command = raw_command
            else:
                command = None
                scope_error = ValueError("codex_command must be a path string or argv list")
            if scope_error is None:
                try:
                    with CodexAppServer(
                        command=command,
                        cwd=cwd,
                        timeout_seconds=self.timeout_seconds,
                    ) as server:
                        account = require_chatgpt_subscription(server.account(refresh=False))
                    account_scope = account_fingerprint(provider_profile, account)
                except (CodexSubscriptionError, TimeoutError) as exc:
                    scope_error = exc
        elif provider.type in {ProviderType.OLLAMA, ProviderType.LMSTUDIO}:
            account_scope = _endpoint_fingerprint(provider_profile, provider)
        else:
            try:
                account_scope = remote_catalog_account_fingerprint(provider)
            except (OSError, ValueError, RuntimeError) as exc:
                scope_error = exc

        reason = scope_error or RuntimeError(
            "live model-catalog refresh is disabled for this offline diagnostic"
        )
        return self._fallback(
            provider_profile,
            provider,
            reason,
            account_scope=account_scope,
            allow_stale_cache=allow_stale_cache,
        )

    @classmethod
    def validate_routes(
        cls,
        config: Config,
        snapshots: dict[str, ModelCatalogSnapshot],
    ) -> tuple[bool, tuple[str, ...]]:
        """Validate main/subagent/summarizer/fallback across provider boundaries."""

        errors: list[str] = []
        for route, ref, effort in cls.configured_routes(config):
            parsed = parse_model_ref(ref, default_profile=config.default_profile)
            provider = config.providers.get(parsed.profile)
            if provider is None:
                errors.append(f"{route}: provider profile {parsed.profile!r} is not configured")
                continue
            snapshot = snapshots.get(parsed.profile)
            if snapshot is None:
                errors.append(f"{route}: no catalog was loaded for {parsed.profile}")
                continue
            if not snapshot.availability_verified:
                detail = snapshot.error.message if snapshot.error else snapshot.provenance_label
                errors.append(f"{route}: availability could not be verified for {ref}: {detail}")
                continue
            ok, message = cls.validate_selection(
                snapshot,
                parsed.qualified,
                reasoning_effort=effort,
            )
            if not ok:
                errors.append(f"{route}: {message}")
        return not errors, tuple(errors)

    def _fallback(
        self,
        provider_profile: str,
        provider: ProviderConfig,
        exc: BaseException,
        *,
        account_scope: str | None,
        allow_stale_cache: bool,
    ) -> ModelCatalogSnapshot:
        error = CatalogError(
            code=ErrorCode.MODEL_CATALOG_UNAVAILABLE.value,
            message=redact_secrets(str(exc)),
        )
        # Every saved live snapshot has a deterministic account/endpoint scope.
        # If scope resolution itself failed, never ask the cache for "whatever
        # profile matched" — that could relabel another API key's models as
        # available. Codex has the same invariant when account/read fails.
        cached = None
        if account_scope is not None:
            evidence = self._session_validations.get((provider_profile, account_scope))
            if evidence is not None:
                expires = datetime.fromisoformat(evidence.expires_at.replace("Z", "+00:00"))
                stale = self.clock().astimezone(UTC) >= expires
                if not stale or allow_stale_cache:
                    return evidence.as_cache(
                        stale=stale,
                        availability_verified=not stale,
                        label=(
                            f"Stale successful billable validation from "
                            f"{evidence.retrieved_at}; availability unverified"
                            if stale
                            else f"Current-session successful billable validation from "
                            f"{evidence.retrieved_at} (exact model only)"
                        ),
                        error=error,
                    )
            cached = self.cache.load(
                provider_profile,
                account_fingerprint=account_scope,
                allow_stale=allow_stale_cache,
                fetch_error=error,
            )
        if cached is not None:
            return cached
        return self._static_catalog(provider_profile, provider, error)

    def _static_catalog(
        self,
        provider_profile: str,
        provider: ProviderConfig,
        error: CatalogError,
    ) -> ModelCatalogSnapshot:
        defaults = DEFAULT_MODELS.get(provider_profile) or DEFAULT_MODELS.get(
            provider.type.value, {}
        )
        defaults_profile = (
            provider_profile if provider_profile in DEFAULT_MODELS else provider.type.value
        )
        unique: dict[str, str] = {}
        for route, ref in defaults.items():
            model_id = strip_profile(ref, defaults_profile)
            unique.setdefault(model_id, route)
        entries = tuple(
            ModelCatalogEntry(
                provider_profile=provider_profile,
                model_id=model_id,
                ref=qualify_model_ref(model_id, provider_profile),
                display_name=model_id,
                is_default=route == "main",
                account_available=None,
                billing_mode=(
                    "chatgpt_subscription"
                    if provider.type is ProviderType.CODEX_SUBSCRIPTION
                    else None
                ),
                availability_label="Offline fallback; availability unverified",
            )
            for model_id, route in unique.items()
        )
        return self._snapshot(
            provider_profile=provider_profile,
            provider_type=provider.type.value,
            source=CatalogSource.STATIC_FALLBACK,
            account_scope=None,
            models=entries,
            verified=False,
            label="Offline fallback; availability unverified",
            error=error,
        )

    def _snapshot(
        self,
        *,
        provider_profile: str,
        provider_type: str,
        source: CatalogSource,
        account_scope: str | None,
        models: tuple[ModelCatalogEntry, ...],
        verified: bool,
        label: str,
        error: CatalogError | None = None,
    ) -> ModelCatalogSnapshot:
        now = self.clock().astimezone(UTC)
        return ModelCatalogSnapshot(
            provider_profile=provider_profile,
            provider_type=provider_type,
            source=source,
            retrieved_at=_iso(now),
            ttl_seconds=self.ttl_seconds,
            expires_at=_iso(now + timedelta(seconds=self.ttl_seconds)),
            stale=False,
            account_fingerprint=account_scope,
            models=models,
            availability_verified=verified,
            provenance_label=label,
            error=error,
        )

    @staticmethod
    def validate_selection(
        snapshot: ModelCatalogSnapshot,
        model_ref: str,
        *,
        reasoning_effort: str | None = None,
    ) -> tuple[bool, str]:
        """Validate a model + effort against the same snapshot shown to the user."""

        if not snapshot.availability_verified:
            return (
                False,
                f"{snapshot.provenance_label}; availability is not verified for {model_ref}.",
            )
        entry = next((item for item in snapshot.models if item.ref == model_ref), None)
        if entry is None:
            return False, f"{model_ref} is not in the {snapshot.provenance_label}."
        if entry.hidden:
            return False, f"{model_ref} is hidden; open Advanced to select it explicitly."
        if entry.account_available is False:
            return False, f"{model_ref} is not available for this account."
        if snapshot.provider_type == ProviderType.OLLAMA.value:
            if entry.supports_tools is False:
                return (
                    False,
                    f"{model_ref} is installed but does not support Ollama tools. "
                    "Run `ollama pull <tool-capable-model>`, then `/model refresh` "
                    "or select a model whose capability list includes `tools`.",
                )
            if entry.supports_tools is not True:
                return (
                    False,
                    f"{model_ref} tool support could not be verified through Ollama "
                    "`/api/show`. Upgrade Ollama or select a model whose capability "
                    "list includes `tools`, then run `/model refresh`.",
                )
        if entry.deprecated:
            suggestion = (
                f" Select {entry.replacement_ref} instead."
                if entry.replacement_ref
                else " Refresh the catalog and select a supported replacement."
            )
            return False, f"{model_ref} is retired or deprecated.{suggestion}"
        if reasoning_effort is not None:
            supported = {effort.value for effort in entry.supported_reasoning_efforts}
            if supported and reasoning_effort not in supported:
                choices = ", ".join(sorted(supported))
                return (
                    False,
                    f"Reasoning effort {reasoning_effort!r} is unsupported; use {choices}.",
                )
        return True, "selection valid"


__all__ = ["ModelCatalogService", "account_fingerprint", "catalog_timeout_seconds"]
