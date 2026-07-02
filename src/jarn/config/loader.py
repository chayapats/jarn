"""Load and merge the two configuration tiers into a :class:`Config`.

Merge order (later wins): built-in defaults < global (~/.jarn) < project (.jarn).
Dicts merge recursively; lists and scalars are replaced wholesale, *except*
``permissions.allow`` / ``permissions.deny`` and ``hooks`` / ``mcp_servers``
which are concatenated so a project can extend (not just replace) global rules.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import ValidationError

from jarn.config import paths
from jarn.config.pydantic_schema import (
    ConfigValidationError,
    config_to_dataclass,
    parse_config_model,
)
from jarn.config.schema import Config, ProviderConfig

_LIST_EXTEND_KEYS = {"hooks", "mcp_servers", "async_subagents"}


class ConfigError(ValueError):
    """Raised on malformed configuration."""


def _parse_yaml_text(text: str, source: Path | None) -> dict[str, Any]:
    """Parse a YAML string into a dict, validating the top-level is a mapping.

    Shared by :func:`_read_yaml` (path-based) and the trust flow (bytes-based,
    so the fingerprint and the loaded config come from one read — no TOCTOU).
    """
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {source}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Top-level config in {source} must be a mapping.")
    return data


def _read_yaml(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    return _parse_yaml_text(path.read_text(encoding="utf-8"), path)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if key in _LIST_EXTEND_KEYS and isinstance(value, list):
            out[key] = [*out.get(key, []), *value]
        elif key == "permissions" and isinstance(value, dict):
            # Allow/deny rules concatenate so a project extends global rules.
            out[key] = _merge_permissions(out.get(key, {}), value)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _merge_permissions(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for bucket in ("allow", "deny"):
        if bucket in overlay:
            merged[bucket] = [*base.get(bucket, []), *overlay.get(bucket, [])]
    return merged


def _validation_error_to_config_error(exc: ValidationError | ConfigValidationError) -> ConfigError:
    if isinstance(exc, ConfigValidationError):
        return ConfigError(str(exc))
    errors = exc.errors()
    if not errors:
        return ConfigError(str(exc))
    first = errors[0]
    loc_parts = [str(part) for part in first.get("loc", ())]
    loc = ".".join(loc_parts)
    msg = first.get("msg", str(exc))
    if "extra_forbidden" in msg or "Extra inputs are not permitted" in msg:
        key = loc_parts[-1] if loc_parts else "unknown"
        if len(loc_parts) == 1:
            return ConfigError(
                f"Unknown top-level config key {key!r}; "
                "see docs/CONFIGURATION.md for recognised keys."
            )
        return ConfigError(f"Unknown config key {key!r} at {loc}")
    if loc:
        return ConfigError(f"{loc}: {msg}")
    return ConfigError(msg)


def _build_config(raw: dict[str, Any]) -> Config:
    try:
        model = parse_config_model(raw)
    except (ValidationError, ConfigValidationError) as exc:
        raise _validation_error_to_config_error(exc) from exc
    cfg = config_to_dataclass(model)
    _validate_inline_api_keys(cfg.providers, cfg.strict_secrets)
    for mcp in cfg.mcp_servers:
        if mcp.url and mcp.transport in ("http", "sse", "streamable_http"):
            _warn_mcp_url_ssrf(mcp.url, name=mcp.name)
    return cfg


def _warn_mcp_url_ssrf(url: str, *, name: str) -> None:
    """Defense-in-depth: warn when an MCP HTTP URL targets a private/loopback host."""
    from jarn.agent.web_tools import _check_host

    host = urlparse(url).hostname or ""
    _ips, reason = _check_host(host)
    if reason is not None:
        warnings.warn(
            f"MCP server {name!r} url {url!r} targets a private/loopback host "
            f"({reason}). MCP endpoints may reach internal services by design; "
            "ensure you trust this config.",
            stacklevel=3,
        )


class InlineSecretWarning(UserWarning):
    """Emitted when a provider defines an inline plaintext ``api_key``.

    Inline keys sit in config.yaml on disk and in memory, contradicting the
    "referenced, never inlined" guidance. Default behaviour is to warn; set
    ``strict_secrets: true`` to turn this into a hard :class:`ConfigError`.
    """


def _validate_inline_api_keys(
    providers: dict[str, ProviderConfig], strict: bool
) -> None:
    """Warn/error on providers whose ``api_key`` is an inline plaintext secret.

    A reference (``${ENV}``, ``keychain:``, ``file:``) is never flagged — only a
    literal that :func:`looks_like_secret` recognises. Empty keys and short
    local-provider tokens (e.g. ``lm-studio``) pass silently.
    """
    from jarn.config.secrets import is_reference, looks_like_secret

    offenders: list[str] = []
    for name, prov in providers.items():
        ref = prov.api_key
        if ref is None or is_reference(ref):
            continue
        if looks_like_secret(ref):
            offenders.append(name)
            if not strict:
                warnings.warn(
                    f"Provider {name!r} has an inline plaintext api_key in "
                    "config.yaml — move it to a reference (keychain:..., "
                    "file:..., or ${ENV_VAR}) so it isn't persisted to disk. "
                    "Set strict_secrets: true to reject this.",
                    InlineSecretWarning,
                    stacklevel=2,
                )
    if strict and offenders:
        raise ConfigError(
            f"Provider(s) {offenders!r} define inline plaintext api_keys but "
            "strict_secrets is on. Move each to a reference (keychain:..., "
            "file:..., or ${ENV_VAR})."
        )


def load_config(
    *,
    global_path: Path | None = None,
    project_path: Path | None = None,
    project_root: Path | None = None,
    project_trusted: bool = True,
    project_raw: dict[str, Any] | None = None,
) -> Config:
    """Load, merge, and validate configuration from both tiers.

    Paths default to the discovered global/project locations; they are injectable
    for testing.

    ``project_trusted`` is the trust boundary: when ``False`` the project tier's
    capability-granting keys (``hooks``, ``mcp_servers``, ``providers``, …) are
    stripped before merging, so opening an untrusted repo can't run code or leak
    secrets. The launcher decides trust (see :mod:`jarn.config.trust`); the
    default is ``True`` so the global tier and explicitly-trusted callers behave
    as before.

    ``project_raw`` lets a caller pass the already-read project tier dict so the
    fingerprinted content and the loaded content are guaranteed identical (no
    TOCTOU between the trust decision and the load). When ``None`` the project
    path is read here as before.
    """
    gpath = global_path if global_path is not None else paths.global_config_path()
    ppath = (
        project_path
        if project_path is not None
        else paths.project_config_path(project_root)
    )

    if project_raw is None:
        project_raw = _read_yaml(ppath)
    if not project_trusted:
        from jarn.config.trust import sanitize_project

        project_raw = sanitize_project(project_raw)

    merged: dict[str, Any] = {}
    merged = _deep_merge(merged, _read_yaml(gpath))
    merged = _deep_merge(merged, project_raw)
    return _build_config(merged)
