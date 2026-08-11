"""Transactional, comment-preserving setup configuration commits."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from jarn.config.defaults import CLOUD_PROVIDERS, PROVIDER_BASE_URLS
from jarn.config.pydantic_schema import (
    CURRENT_CONFIG_VERSION,
    ConfigValidationError,
    migrate_config,
    parse_config_model,
)
from jarn.util.atomic import atomic_write_text, file_lock


class SetupConfigError(RuntimeError):
    """A staged setup configuration could not be safely prepared or committed."""


@dataclass(frozen=True, slots=True)
class StagedSetupConfig:
    path: Path
    candidate: dict[str, Any] = field(repr=False)
    candidate_text: str = field(repr=False)
    source_text: str = field(repr=False)
    source_sha256: str
    source_exists: bool
    source_mode: int | None
    provider: str
    model: str
    permission_mode: str


@dataclass(frozen=True, slots=True)
class SetupCommitResult:
    path: Path
    backup_path: Path | None
    staged: StagedSetupConfig = field(repr=False)


def _yaml() -> YAML:
    value = YAML()
    value.preserve_quotes = True
    return value


def _load_roundtrip(text: str, path: Path) -> dict[str, Any]:
    try:
        loaded = _yaml().load(text)
    except YAMLError as exc:
        raise SetupConfigError(
            f"Existing configuration is invalid YAML and was not changed: {path}: {exc}"
        ) from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise SetupConfigError(
            f"Existing configuration root must be a mapping and was not changed: {path}"
        )
    return loaded


def _render(candidate: dict[str, Any], *, new_file: bool) -> str:
    from jarn.onboarding.wizard import _CONFIG_HEADER

    output = io.StringIO()
    _yaml().dump(candidate, output)
    text = output.getvalue()
    return _CONFIG_HEADER + text if new_file else text


def _mapping(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if value is None:
        value = {}
        parent[key] = value
    if not isinstance(value, dict):
        raise SetupConfigError(f"Existing {key} setting must be a mapping; no changes were made.")
    return value


def _merge_selection(
    existing: dict[str, Any],
    *,
    provider: str,
    api_key_ref: str | None,
    model: str,
    theme: str,
    mode: str | None,
    base_url: str | None,
    reasoning_effort: str | None,
    routing_subagent: str | None,
    routing_summarizer: str | None,
    routing_fallback: list[str] | None,
    budget_per_session_usd: float | None,
    budget_hard_stop: bool | None,
    budget_warn_at_pct: int | None,
) -> dict[str, Any]:
    """Update setup-owned values while preserving every advanced customization."""

    from jarn.onboarding.wizard import _build_config_dict, derive_routing_models

    if not existing:
        candidate = _build_config_dict(
            provider,
            api_key_ref,
            model,
            theme,
            mode=mode or "ask",
            base_url_override=base_url,
            reasoning_effort=reasoning_effort,
        )
    else:
        candidate = deepcopy(existing)
    candidate["config_version"] = CURRENT_CONFIG_VERSION
    candidate["default_profile"] = provider
    candidate["default_model"] = model

    providers = _mapping(candidate, "providers")
    selected_raw = providers.get(provider)
    if selected_raw is None:
        selected_raw = {}
        providers[provider] = selected_raw
    if not isinstance(selected_raw, dict):
        raise SetupConfigError(
            f"Existing providers.{provider} must be a mapping; no changes were made."
        )
    selected_raw["type"] = provider
    if provider in CLOUD_PROVIDERS:
        if api_key_ref is not None:
            selected_raw["api_key"] = api_key_ref
    else:
        # Subscription/local providers must never inherit an old billable key.
        selected_raw.pop("api_key", None)
    if base_url is not None:
        selected_raw["base_url"] = base_url
    elif "base_url" not in selected_raw and provider in PROVIDER_BASE_URLS:
        selected_raw["base_url"] = PROVIDER_BASE_URLS[provider]
    if reasoning_effort:
        selected_raw["reasoning_effort"] = reasoning_effort

    routing = candidate.get("routing")
    if routing is None:
        candidate["routing"] = derive_routing_models(provider, model)
    elif isinstance(routing, dict):
        # Main follows the explicit setup selection.  Advanced task routes,
        # fallbacks, prompt-cache policy, and keep-alive remain untouched.
        routing["main"] = model
    else:
        raise SetupConfigError("Existing routing setting must be a mapping; no changes were made.")

    routing = _mapping(candidate, "routing")
    if routing_subagent is not None:
        routing["subagent"] = routing_subagent
    if routing_summarizer is not None:
        routing["summarizer"] = routing_summarizer
    if routing_fallback is not None:
        routing["fallback"] = list(routing_fallback)

    budget = _mapping(candidate, "budget")
    if budget_per_session_usd is not None:
        budget["per_session_usd"] = budget_per_session_usd
    if budget_hard_stop is not None:
        budget["hard_stop"] = budget_hard_stop
    if budget_warn_at_pct is not None:
        budget["warn_at_pct"] = budget_warn_at_pct

    if mode is not None:
        candidate["permission_mode"] = mode

    ui = _mapping(candidate, "ui")
    ui["theme"] = theme
    # Existing permission_mode, permissions, headers, provider extras, hooks,
    # MCPs, budgets, and all unrelated top-level settings are intentionally kept.
    return candidate


def stage_setup_config(
    path: Path,
    *,
    provider: str,
    api_key_ref: str | None,
    model: str,
    theme: str,
    mode: str | None = None,
    base_url: str | None = None,
    reasoning_effort: str | None = None,
    routing_subagent: str | None = None,
    routing_summarizer: str | None = None,
    routing_fallback: list[str] | None = None,
    budget_per_session_usd: float | None = None,
    budget_hard_stop: bool | None = None,
    budget_warn_at_pct: int | None = None,
) -> StagedSetupConfig:
    """Build and validate a complete candidate without touching disk."""

    target = Path(path)
    if target.is_symlink():
        raise SetupConfigError(
            f"Refusing to update configuration through a symbolic link: {target}"
        )
    source_exists = target.exists()
    if source_exists and not target.is_file():
        raise SetupConfigError(f"Configuration path is not a regular file: {target}")
    try:
        source_text = target.read_text(encoding="utf-8") if source_exists else ""
    except OSError as exc:
        raise SetupConfigError(f"Could not read existing configuration {target}: {exc}") from exc
    source_mode: int | None = None
    if source_exists and os.name != "nt":
        try:
            source_mode = stat.S_IMODE(target.stat().st_mode)
        except OSError:
            source_mode = None
    raw = _load_roundtrip(source_text, target) if source_exists else {}
    try:
        migrated = migrate_config(raw) if source_exists else {}
        if source_exists:
            parse_config_model(migrated)
        candidate = _merge_selection(
            migrated,
            provider=provider,
            api_key_ref=api_key_ref,
            model=model,
            theme=theme,
            mode=mode,
            base_url=base_url,
            reasoning_effort=reasoning_effort,
            routing_subagent=routing_subagent,
            routing_summarizer=routing_summarizer,
            routing_fallback=routing_fallback,
            budget_per_session_usd=budget_per_session_usd,
            budget_hard_stop=budget_hard_stop,
            budget_warn_at_pct=budget_warn_at_pct,
        )
        parsed = parse_config_model(candidate)
    except (ConfigValidationError, ValueError) as exc:
        raise SetupConfigError(
            f"Configuration candidate is invalid; the existing file was not changed: {exc}"
        ) from exc
    text = _render(candidate, new_file=not source_exists)
    permission = str(parsed.permission_mode.value)
    return StagedSetupConfig(
        path=target,
        candidate=candidate,
        candidate_text=text,
        source_text=source_text,
        source_sha256=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        source_exists=source_exists,
        source_mode=source_mode,
        provider=provider,
        model=model,
        permission_mode=permission,
    )


def _timestamped_backup(path: Path, *, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    base = path.with_name(f"{path.name}.bak.{stamp}")
    candidate = base
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{base.name}.{counter}")
        counter += 1
    return candidate


def _verify_installed(path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8")
        raw = _load_roundtrip(text, path)
        parsed = parse_config_model(raw)
    except Exception as exc:
        raise SetupConfigError(f"Published configuration verification failed: {exc}") from exc
    if raw.get("config_version") != CURRENT_CONFIG_VERSION:
        raise SetupConfigError("Published configuration does not use the current config version.")
    if parsed.default_profile != str(raw.get("default_profile") or ""):
        raise SetupConfigError("Published configuration profile verification failed.")


def commit_staged_config(
    staged: StagedSetupConfig,
    *,
    now: datetime | None = None,
) -> SetupCommitResult:
    """Back up and atomically publish a still-current staged candidate."""

    path = staged.path
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with file_lock(path) as locked:
        if not locked:
            raise SetupConfigError("Could not acquire the configuration write lock.")
        current_exists = path.exists()
        try:
            current_text = path.read_text(encoding="utf-8") if current_exists else ""
        except OSError as exc:
            raise SetupConfigError(f"Could not re-read configuration before commit: {exc}") from exc
        if (
            current_exists != staged.source_exists
            or hashlib.sha256(current_text.encode("utf-8")).hexdigest() != staged.source_sha256
        ):
            raise SetupConfigError(
                "Configuration changed while setup was open; no setup answers were committed."
            )
        backup: Path | None = None
        try:
            if staged.source_exists:
                backup = _timestamped_backup(path, now=now)
                shutil.copy2(path, backup)
                with backup.open("rb") as handle:
                    os.fsync(handle.fileno())
            atomic_write_text(
                path,
                staged.candidate_text,
                mode=staged.source_mode if staged.source_mode is not None else 0o600,
            )
            _verify_installed(path)
        except Exception as exc:
            try:
                if staged.source_exists:
                    atomic_write_text(
                        path,
                        staged.source_text,
                        mode=staged.source_mode if staged.source_mode is not None else 0o600,
                    )
                else:
                    path.unlink(missing_ok=True)
            except OSError as rollback_exc:
                raise SetupConfigError(
                    f"Configuration commit failed ({exc}) and rollback failed ({rollback_exc}). "
                    f"Inspect {backup or path}."
                ) from rollback_exc
            raise SetupConfigError(
                f"Configuration commit failed; the previous file was restored: {exc}"
            ) from exc
    return SetupCommitResult(path=path, backup_path=backup, staged=staged)


def rollback_setup_commit(result: SetupCommitResult) -> None:
    """Restore the pre-setup source if a later completion gate fails."""

    staged = result.staged
    with file_lock(staged.path) as locked:
        if not locked:
            raise SetupConfigError("Could not lock configuration for setup rollback.")
        try:
            active_text = staged.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SetupConfigError(
                f"Could not read configuration for setup rollback: {exc}"
            ) from exc
        if active_text != staged.candidate_text:
            raise SetupConfigError(
                "Configuration changed after setup commit; refusing to overwrite the newer edit."
            )
        if staged.source_exists:
            atomic_write_text(
                staged.path,
                staged.source_text,
                mode=staged.source_mode if staged.source_mode is not None else 0o600,
            )
        else:
            staged.path.unlink(missing_ok=True)
        restored = staged.path.read_text(encoding="utf-8") if staged.source_exists else ""
        if restored != staged.source_text:
            raise SetupConfigError("Setup rollback verification failed.")


__all__ = [
    "SetupCommitResult",
    "SetupConfigError",
    "StagedSetupConfig",
    "commit_staged_config",
    "rollback_setup_commit",
    "stage_setup_config",
]
