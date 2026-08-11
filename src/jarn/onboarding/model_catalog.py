"""Setup-facing adapter for the unified model catalog.

The onboarding UIs collect credentials in memory before the configuration is
committed.  This module is the narrow bridge between that transient state and
the same :class:`~jarn.catalog.service.ModelCatalogService` used by ``/model``,
doctor, routing, and the pre-turn gate.  It deliberately returns no selectable
models when availability is unverified: static defaults remain useful as input
placeholders, but are never presented as account availability evidence.
"""

from __future__ import annotations

from collections.abc import Callable

from jarn.catalog import ModelCatalogEntry, ModelCatalogService, ModelCatalogSnapshot
from jarn.config.defaults import PROVIDER_BASE_URLS
from jarn.config.schema import ProviderConfig, ProviderType


def load_setup_catalog(
    provider_profile: str,
    *,
    credential: str | None = None,
    base_url: str | None = None,
    include_hidden: bool = False,
    service: ModelCatalogService | None = None,
    on_wait: Callable[[str], None] | None = None,
) -> ModelCatalogSnapshot:
    """Return a fresh setup catalog for the selected provider/profile.

    ``credential`` may be either a process-memory key or a secret reference.
    It is passed directly to :class:`ProviderConfig`, whose catalog adapter
    resolves references at the secret boundary.  No credential is logged,
    persisted in setup state, or included in returned provenance.

    Stale cache is refused during setup.  A fresh account-scoped cache may still
    be returned if a live request fails, which is the catalog service's normal
    verified-cache contract.
    """

    try:
        provider_type = ProviderType(provider_profile)
    except ValueError as exc:
        raise ValueError(f"unknown setup provider profile {provider_profile!r}") from exc
    provider = ProviderConfig(
        type=provider_type,
        api_key=credential,
        base_url=base_url or PROVIDER_BASE_URLS.get(provider_profile),
    )
    catalog_service = service or ModelCatalogService(on_wait=on_wait)
    return catalog_service.get_catalog(
        provider_profile,
        provider,
        include_hidden=include_hidden,
        allow_stale_cache=False,
    )


def selectable_setup_models(
    snapshot: ModelCatalogSnapshot,
) -> tuple[ModelCatalogEntry, ...]:
    """Return only models proven selectable by this exact catalog snapshot."""

    if not snapshot.availability_verified:
        return ()
    return tuple(
        entry
        for entry in snapshot.visible_models()
        if entry.account_available is not False
        and not entry.deprecated
        and not (
            snapshot.provider_type == ProviderType.OLLAMA.value
            and entry.supports_tools is not True
        )
    )


def recommended_setup_model(snapshot: ModelCatalogSnapshot) -> ModelCatalogEntry | None:
    """Choose the provider-reported/default selectable entry, then the first."""

    entries = selectable_setup_models(snapshot)
    return next((entry for entry in entries if entry.is_default), entries[0] if entries else None)


def setup_catalog_status(snapshot: ModelCatalogSnapshot) -> str:
    """Render provenance without implying static or stale availability."""

    choices = selectable_setup_models(snapshot)
    if choices:
        return snapshot.provenance_label
    if snapshot.availability_verified:
        if snapshot.provider_type == ProviderType.OLLAMA.value and snapshot.models:
            return (
                f"{snapshot.provenance_label}; no installed model has verified "
                "Ollama tool support — run `ollama pull <tool-capable-model>` "
                "and refresh"
            )
        return f"{snapshot.provenance_label}; no selectable chat models were reported"
    detail = snapshot.error.message if snapshot.error else snapshot.provenance_label
    return f"availability unverified: {detail}"


__all__ = [
    "load_setup_catalog",
    "recommended_setup_model",
    "selectable_setup_models",
    "setup_catalog_status",
]
