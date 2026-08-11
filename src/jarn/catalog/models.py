"""Versioned model-catalog data shared by setup, REPL, doctor, and routing."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

MODEL_CATALOG_SCHEMA_VERSION = 1


class CatalogSource(str, Enum):
    CODEX_LIVE = "codex_live"
    PROVIDER_LIVE = "provider_live"
    LOCAL_LIVE = "local_live"
    BILLABLE_VALIDATION = "billable_validation"
    CACHE = "cache"
    STATIC_FALLBACK = "static_fallback"


@dataclass(frozen=True, slots=True)
class CatalogError:
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class ReasoningEffort:
    value: str
    description: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {"value": self.value, "description": self.description}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ReasoningEffort:
        value = raw.get("value")
        if not isinstance(value, str) or not value:
            raise ValueError("reasoning effort requires a value")
        description = raw.get("description")
        return cls(value=value, description=str(description) if description else None)


@dataclass(frozen=True, slots=True)
class ModelCatalogEntry:
    provider_profile: str
    model_id: str
    ref: str
    display_name: str
    catalog_id: str | None = None
    description: str | None = None
    hidden: bool = False
    is_default: bool = False
    account_available: bool | None = None
    default_reasoning_effort: str | None = None
    supported_reasoning_efforts: tuple[ReasoningEffort, ...] = ()
    input_modalities: tuple[str, ...] = ()
    supports_tools: bool | None = None
    supports_personality: bool | None = None
    preview: bool = False
    deprecated: bool = False
    replacement_ref: str | None = None
    context_window: int | None = None
    service_tiers: tuple[str, ...] = ()
    billing_mode: str | None = None
    availability_label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_profile": self.provider_profile,
            "catalog_id": self.catalog_id,
            "model_id": self.model_id,
            "ref": self.ref,
            "display_name": self.display_name,
            "description": self.description,
            "hidden": self.hidden,
            "is_default": self.is_default,
            "account_available": self.account_available,
            "default_reasoning_effort": self.default_reasoning_effort,
            "supported_reasoning_efforts": [
                effort.to_dict() for effort in self.supported_reasoning_efforts
            ],
            "input_modalities": list(self.input_modalities),
            "supports_tools": self.supports_tools,
            "supports_personality": self.supports_personality,
            "preview": self.preview,
            "deprecated": self.deprecated,
            "replacement_ref": self.replacement_ref,
            "context_window": self.context_window,
            "service_tiers": list(self.service_tiers),
            "billing_mode": self.billing_mode,
            "availability_label": self.availability_label,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ModelCatalogEntry:
        required = ("provider_profile", "model_id", "ref", "display_name")
        if any(not isinstance(raw.get(key), str) or not raw.get(key) for key in required):
            raise ValueError("cached model entry is missing required strings")
        efforts_raw = raw.get("supported_reasoning_efforts") or []
        if not isinstance(efforts_raw, list) or any(
            not isinstance(item, dict) for item in efforts_raw
        ):
            raise ValueError("cached reasoning efforts must be objects")
        modalities = raw.get("input_modalities") or []
        tiers = raw.get("service_tiers") or []
        if not isinstance(modalities, list) or not all(isinstance(v, str) for v in modalities):
            raise ValueError("cached input modalities must be strings")
        if not isinstance(tiers, list) or not all(isinstance(v, str) for v in tiers):
            raise ValueError("cached service tiers must be strings")
        context_window = raw.get("context_window")
        if context_window is not None and not isinstance(context_window, int):
            raise ValueError("cached context window must be an integer or null")
        available = raw.get("account_available")
        if available is not None and not isinstance(available, bool):
            raise ValueError("cached account availability must be boolean or null")
        personality = raw.get("supports_personality")
        if personality is not None and not isinstance(personality, bool):
            raise ValueError("cached personality support must be boolean or null")
        supports_tools = raw.get("supports_tools")
        if supports_tools is not None and not isinstance(supports_tools, bool):
            raise ValueError("cached tool support must be boolean or null")

        def optional_text(key: str) -> str | None:
            value = raw.get(key)
            return str(value) if value is not None else None

        return cls(
            provider_profile=str(raw["provider_profile"]),
            catalog_id=optional_text("catalog_id"),
            model_id=str(raw["model_id"]),
            ref=str(raw["ref"]),
            display_name=str(raw["display_name"]),
            description=optional_text("description"),
            hidden=bool(raw.get("hidden", False)),
            is_default=bool(raw.get("is_default", False)),
            account_available=available,
            default_reasoning_effort=optional_text("default_reasoning_effort"),
            supported_reasoning_efforts=tuple(
                ReasoningEffort.from_dict(item) for item in efforts_raw
            ),
            input_modalities=tuple(modalities),
            supports_tools=supports_tools,
            supports_personality=personality,
            preview=bool(raw.get("preview", False)),
            deprecated=bool(raw.get("deprecated", False)),
            replacement_ref=optional_text("replacement_ref"),
            context_window=context_window,
            service_tiers=tuple(tiers),
            billing_mode=optional_text("billing_mode"),
            availability_label=optional_text("availability_label"),
        )


@dataclass(frozen=True, slots=True)
class ModelCatalogSnapshot:
    provider_profile: str
    provider_type: str
    source: CatalogSource
    retrieved_at: str
    ttl_seconds: int
    expires_at: str
    stale: bool
    account_fingerprint: str | None
    models: tuple[ModelCatalogEntry, ...]
    availability_verified: bool
    provenance_label: str
    origin_source: CatalogSource | None = None
    error: CatalogError | None = None
    schema_version: int = MODEL_CATALOG_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "provider_profile": self.provider_profile,
            "provider_type": self.provider_type,
            "source": self.source.value,
            "origin_source": self.origin_source.value if self.origin_source else None,
            "retrieved_at": self.retrieved_at,
            "ttl_seconds": self.ttl_seconds,
            "expires_at": self.expires_at,
            "stale": self.stale,
            "account_fingerprint": self.account_fingerprint,
            "availability_verified": self.availability_verified,
            "provenance_label": self.provenance_label,
            "models": [entry.to_dict() for entry in self.models],
            "error": self.error.to_dict() if self.error else None,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ModelCatalogSnapshot:
        if raw.get("schema_version") != MODEL_CATALOG_SCHEMA_VERSION:
            raise ValueError("unsupported model catalog cache schema")
        models = raw.get("models")
        if not isinstance(models, list) or any(not isinstance(item, dict) for item in models):
            raise ValueError("cached model catalog models must be objects")
        source = CatalogSource(str(raw.get("source")))
        origin_raw = raw.get("origin_source")
        error_raw = raw.get("error")
        error = None
        if error_raw is not None:
            if not isinstance(error_raw, dict):
                raise ValueError("cached model catalog error must be an object")
            error = CatalogError(
                code=str(error_raw.get("code") or "CATALOG_ERROR"),
                message=str(error_raw.get("message") or "catalog error"),
            )
        return cls(
            provider_profile=str(raw["provider_profile"]),
            provider_type=str(raw["provider_type"]),
            source=source,
            origin_source=CatalogSource(str(origin_raw)) if origin_raw else None,
            retrieved_at=str(raw["retrieved_at"]),
            ttl_seconds=int(raw["ttl_seconds"]),
            expires_at=str(raw["expires_at"]),
            stale=bool(raw["stale"]),
            account_fingerprint=(
                str(raw["account_fingerprint"]) if raw.get("account_fingerprint") else None
            ),
            models=tuple(ModelCatalogEntry.from_dict(item) for item in models),
            availability_verified=bool(raw["availability_verified"]),
            provenance_label=str(raw["provenance_label"]),
            error=error,
        )

    def visible_models(self) -> tuple[ModelCatalogEntry, ...]:
        return tuple(model for model in self.models if not model.hidden)

    def default_entry(self) -> ModelCatalogEntry | None:
        visible = self.visible_models()
        return next(
            (model for model in visible if model.is_default), visible[0] if visible else None
        )

    def as_cache(
        self,
        *,
        stale: bool,
        availability_verified: bool,
        label: str,
        error: CatalogError | None,
    ) -> ModelCatalogSnapshot:
        origin = self.origin_source or self.source
        return replace(
            self,
            source=CatalogSource.CACHE,
            origin_source=origin,
            stale=stale,
            availability_verified=availability_verified,
            provenance_label=label,
            error=error,
        )


__all__ = [
    "MODEL_CATALOG_SCHEMA_VERSION",
    "CatalogError",
    "CatalogSource",
    "ModelCatalogEntry",
    "ModelCatalogSnapshot",
    "ReasoningEffort",
]
