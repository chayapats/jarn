"""Load and merge the two configuration tiers into a :class:`Config`.

Merge order (later wins): built-in defaults < global (~/.jarn) < project (.jarn).
Dicts merge recursively; lists and scalars are replaced wholesale, *except*
``permissions.allow`` / ``permissions.deny`` and ``hooks`` / ``mcp_servers``
which are concatenated so a project can extend (not just replace) global rules.
"""

from __future__ import annotations

import hashlib
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
    safe_config_validation_message,
)
from jarn.config.schema import Config

_LIST_EXTEND_KEYS = {"hooks", "mcp_servers", "async_subagents"}

#: Top-level keys that may only be set in the global tier (``~/.jarn/config.yaml``).
#: A project-tier value is stripped with a warning whether the project is trusted
#: or not — these bind machine-local secrets / daemons and must not be influenced
#: by a repo's config (#36/#34).
_GLOBAL_ONLY_KEYS: frozenset[str] = frozenset({"gateway"})


class ConfigError(ValueError):
    """Raised on malformed configuration."""


def _migrate_before_load(
    path: Path | None,
    *,
    expected_raw: dict[str, Any] | None = None,
) -> None:
    """Transactionally migrate an existing tier before it enters the merge.

    ``expected_raw`` is used by the project-trust flow, which deliberately read
    the project bytes once before calling the loader.  A migration is allowed
    only while the on-disk mapping still matches those trusted bytes; the plan's
    digest then closes the remaining read/apply race.
    """
    if path is None or not path.is_file():
        return
    from jarn.config.migrations import apply_config_migration, plan_config_migration
    from jarn.errors import JarnUserError

    try:
        plan = plan_config_migration(path)
        if not plan.changed:
            return
        if expected_raw is not None:
            current_text = path.read_text(encoding="utf-8")
            current_hash = hashlib.sha256(current_text.encode("utf-8")).hexdigest()
            current_raw = _parse_yaml_text(current_text, path)
            if current_hash != plan.source_sha256 or current_raw != expected_raw:
                raise ConfigError(
                    "Project config changed after its trust read; migration was not "
                    "applied. Re-run the command to review the current file."
                )
        apply_config_migration(plan)
    except JarnUserError as exc:
        # Keep the long-standing ConfigError boundary for callers while retaining
        # the stable code and complete actionable anatomy in the message.
        raise ConfigError(exc.detail.render()) from exc


def _strip_global_only_keys(raw: dict[str, Any]) -> dict[str, Any]:
    """Drop :data:`_GLOBAL_ONLY_KEYS` from a project-tier dict, warning per key."""
    present = sorted(k for k in _GLOBAL_ONLY_KEYS if k in raw)
    if not present:
        return raw
    out = {k: v for k, v in raw.items() if k not in _GLOBAL_ONLY_KEYS}
    for key in present:
        warnings.warn(
            f"Ignoring project-tier {key!r} config — this key is global-only "
            f"(set it in ~/.jarn/config.yaml). Remove it from the project "
            f"config to silence this warning.",
            UserWarning,
            stacklevel=3,
        )
    return out


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
            base_val = base.get(bucket, [])
            overlay_val = overlay.get(bucket, [])
            if isinstance(base_val, list) and overlay_val is None:
                # A null overlay means "inherit the base", NOT "replace with an
                # empty list". Preserve the base so an overlay (project) tier can't
                # ERASE a restrictive global allow/deny by setting it to null — a
                # privilege escalation (FINDING E). Without this the None replaced
                # the list and pydantic normalized it to [], wiping the base.
                merged[bucket] = base_val
            elif isinstance(base_val, list) and isinstance(overlay_val, list):
                merged[bucket] = [*base_val, *overlay_val]
            else:
                # A scalar on either side can't be spliced: splatting a str yields
                # per-character host globs. Preserve the raw overlay value (replace)
                # so downstream pydantic validation raises the intended, clear
                # validation error (see pydantic_schema NetworkPolicyModel).
                merged[bucket] = overlay_val
    # ``sensitive_read_globs`` is a full-list REPLACE (last-writer wins), matching
    # the existing tier precedence: a tier that EXPLICITLY supplies it — including
    # the documented empty-list opt-out ``[]`` — replaces the prior list. Without
    # this it was dropped by the ``dict(base)`` copy for whichever tier set it
    # (base's carries through, but the overlay's never did), so a user's custom
    # list or the opt-out was silently discarded and the built-in defaults always
    # won (BUG D). ``[]`` is honoured via the ``in`` test (not truthiness).
    if "sensitive_read_globs" in overlay:
        merged["sensitive_read_globs"] = overlay["sensitive_read_globs"]
    # The nested network egress policy also concatenates its allow/deny across
    # tiers (project extends global). Without this, ``permissions.network`` in an
    # overlay would be dropped by the ``dict(base)`` copy above, silently
    # disabling the egress policy — the worst failure mode for a security feature.
    base_net, overlay_net = base.get("network"), overlay.get("network")
    if isinstance(base_net, dict) and isinstance(overlay_net, dict):
        merged["network"] = _merge_permissions(base_net, overlay_net)
    elif overlay_net is not None:
        merged["network"] = overlay_net  # only overlay has it (or malformed → pydantic)
    return merged


def _validation_error_to_config_error(
    exc: ValidationError | ConfigValidationError,
    *,
    raw: dict[str, Any] | None = None,
) -> ConfigError:
    if isinstance(exc, ConfigValidationError):
        return ConfigError(safe_config_validation_message(exc, raw=raw))
    errors = exc.errors()
    if not errors:
        return ConfigError(safe_config_validation_message(exc, raw=raw))
    first = errors[0]
    loc_parts = [str(part) for part in first.get("loc", ())]
    loc = ".".join(loc_parts)
    msg = first.get("msg", "Invalid configuration value.")
    if "extra_forbidden" in msg or "Extra inputs are not permitted" in msg:
        key = loc_parts[-1] if loc_parts else "unknown"
        if len(loc_parts) == 1:
            return ConfigError(
                f"Unknown top-level config key {key!r}; "
                "see docs/CONFIGURATION.md for recognised keys."
            )
        return ConfigError(f"Unknown config key {key!r} at {loc}")
    return ConfigError(safe_config_validation_message(exc, raw=raw))


def _build_config(raw: dict[str, Any]) -> Config:
    try:
        model = parse_config_model(raw)
    except (ValidationError, ConfigValidationError) as exc:
        raise _validation_error_to_config_error(exc, raw=raw) from exc
    cfg = config_to_dataclass(model)
    _validate_secret_references(cfg)
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

    Deprecated compatibility warning retained for import stability. GA config
    rejects credential-shaped inline keys regardless of ``strict_secrets``.
    """


def _validate_secret_references(cfg: Config) -> None:
    """Reject plaintext credentials in every supported config secret field.

    Explicit credential fields require a reference for every non-empty value.
    Header/environment maps may also contain ordinary values, so only a
    credential-shaped field name or value requires a reference there.
    """
    from jarn.config.secrets import (
        is_reference,
        is_sensitive_field_name,
        looks_like_secret,
    )

    offenders: list[str] = []
    for name, prov in cfg.providers.items():
        ref = prov.api_key
        if ref and not is_reference(ref):
            offenders.append(f"providers.{name}.api_key")

        for header, value in prov.headers.items():
            if value and not is_reference(value) and (
                is_sensitive_field_name(header) or looks_like_secret(value)
            ):
                offenders.append(f"providers.{name}.headers.{header}")

    if cfg.search.api_key and not is_reference(cfg.search.api_key):
        offenders.append("search.api_key")
    if cfg.gateway.telegram.token and not is_reference(cfg.gateway.telegram.token):
        offenders.append("gateway.telegram.token")

    for server in cfg.mcp_servers:
        for kind, mapping in (("headers", server.headers), ("env", server.env)):
            for key, value in mapping.items():
                if value and not is_reference(value) and (
                    is_sensitive_field_name(key) or looks_like_secret(value)
                ):
                    offenders.append(f"mcp_servers.{server.name}.{kind}.{key}")

    for agent in cfg.async_subagents:
        for header, value in agent.headers.items():
            if value and not is_reference(value) and (
                is_sensitive_field_name(header) or looks_like_secret(value)
            ):
                offenders.append(f"async_subagents.{agent.name}.headers.{header}")

    if offenders:
        raise ConfigError(
            "Inline plaintext credentials are not allowed at: "
            f"{', '.join(sorted(offenders))}. Store each as a reference "
            "(${ENV_VAR}, keychain:service/account, or file:service/account)."
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

    # The global tier is always trusted operator state.  Migrate it first so the
    # bytes users see on disk match the schema actually used by the process.
    _migrate_before_load(gpath)

    if project_raw is None:
        if project_trusted:
            _migrate_before_load(ppath)
        project_raw = _read_yaml(ppath)
    elif project_trusted:
        # The trust flow supplied an already-read mapping.  Preserve its TOCTOU
        # guarantee while still upgrading the on-disk project tier.
        _migrate_before_load(ppath, expected_raw=project_raw)
    # Global-only keys are stripped from every project tier (trusted == untrusted).
    project_raw = _strip_global_only_keys(project_raw)
    if not project_trusted:
        from jarn.config.trust import sanitize_project

        project_raw = sanitize_project(project_raw)

    merged: dict[str, Any] = {}
    merged = _deep_merge(merged, _read_yaml(gpath))
    merged = _deep_merge(merged, project_raw)
    return _build_config(merged)
