"""Atomic, privacy-aware cache for model catalog snapshots."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from jarn.catalog.models import CatalogError, CatalogSource, ModelCatalogSnapshot
from jarn.config import paths
from jarn.util.atomic import atomic_write_text, file_lock

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _epoch_from_iso(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class ModelCatalogCache:
    def __init__(
        self,
        root: Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = root or (paths.cachedir() / "model-catalog")
        self.clock = clock

    def path_for(self, provider_profile: str) -> Path:
        name = _SAFE_NAME_RE.sub("_", provider_profile).strip("._") or "provider"
        return self.root / f"{name}.json"

    def save(self, snapshot: ModelCatalogSnapshot) -> bool:
        """Atomically replace one cache record; cache failure is non-fatal."""

        target = self.path_for(snapshot.provider_profile)
        try:
            payload = json.dumps(snapshot.to_dict(), ensure_ascii=False, sort_keys=True)
            with file_lock(target):
                atomic_write_text(target, payload + "\n", mode=0o600)
            return True
        except OSError:
            return False

    def load(
        self,
        provider_profile: str,
        *,
        account_fingerprint: str | None = None,
        allow_stale: bool = True,
        fetch_error: CatalogError | None = None,
    ) -> ModelCatalogSnapshot | None:
        path = self.path_for(provider_profile)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return None
            snapshot = ModelCatalogSnapshot.from_dict(raw)
            expires = _epoch_from_iso(snapshot.expires_at)
        except (OSError, ValueError, TypeError, KeyError):
            return None
        if snapshot.provider_profile != provider_profile:
            return None
        if (
            account_fingerprint is not None
            and account_fingerprint != snapshot.account_fingerprint
        ):
            return None
        stale = self.clock() >= expires
        if stale and not allow_stale:
            return None
        retrieved = snapshot.retrieved_at
        origin = snapshot.origin_source or snapshot.source
        if origin is CatalogSource.BILLABLE_VALIDATION and stale:
            label = (
                f"Stale successful billable validation from {retrieved}; "
                "availability unverified"
            )
        elif origin is CatalogSource.BILLABLE_VALIDATION:
            label = (
                f"Cached successful billable validation from {retrieved} "
                "(exact model only)"
            )
        elif stale:
            label = f"Stale cached catalog from {retrieved}; availability unverified"
        else:
            label = f"Cached catalog from {retrieved}"
        return snapshot.as_cache(
            stale=stale,
            availability_verified=not stale,
            label=label,
            error=fetch_error,
        )


__all__ = ["ModelCatalogCache"]
