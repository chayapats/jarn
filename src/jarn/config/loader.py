"""Load and merge the two configuration tiers into a :class:`Config`.

Merge order (later wins): built-in defaults < global (~/.jarn) < project (.jarn).
Dicts merge recursively; lists and scalars are replaced wholesale, *except*
``permissions.allow`` / ``permissions.deny`` and ``hooks`` / ``mcp_servers``
which are concatenated so a project can extend (not just replace) global rules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from jarn.config import paths
from jarn.config.schema import (
    AsyncSubagentSpec,
    BudgetConfig,
    CompatConfig,
    Config,
    ContextConfig,
    ExecutionConfig,
    GitConfig,
    HookSpec,
    MCPServer,
    ObservabilityConfig,
    PermissionMode,
    PermissionRules,
    PolicyConfig,
    ProviderConfig,
    ProviderType,
    RoutingConfig,
    UIConfig,
    WikiConfig,
)

_LIST_EXTEND_KEYS = {"hooks", "mcp_servers", "async_subagents"}

#: The authoritative set of recognised top-level config keys. Anything else is a
#: typo or an unsupported feature and is rejected loudly rather than ignored.
_KNOWN_TOP_LEVEL_KEYS = {
    "default_profile",
    "default_model",
    "permission_mode",
    "providers",
    "routing",
    "budget",
    "context",
    "execution",
    "policy",
    "permissions",
    "hooks",
    "mcp_servers",
    "async_subagents",
    "observability",
    "ui",
    "compat",
    "git",
    "wiki",
}

_TRUE_STRINGS = {"true", "yes", "on", "1"}
_FALSE_STRINGS = {"false", "no", "off", "0", ""}


class ConfigError(ValueError):
    """Raised on malformed configuration."""


def _normalize_bool(value: Any, path: str) -> bool:
    """Coerce a YAML scalar to a strict bool, rejecting ambiguous values.

    Real ``bool`` passes through. Strings (stripped/lowercased) map via the
    well-known truthy/falsey words; ints ``0``/``1`` map to ``False``/``True``.
    Anything else raises :class:`ConfigError` naming ``path`` so the user sees
    which key was wrong instead of a silent surprising coercion.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_STRINGS:
            return True
        if token in _FALSE_STRINGS:
            return False
        raise ConfigError(
            f"{path} must be a boolean (got {value!r})."
        )
    if isinstance(value, int):  # note: bool already handled above
        if value in (0, 1):
            return bool(value)
        raise ConfigError(f"{path} must be a boolean (got {value!r}).")
    raise ConfigError(f"{path} must be a boolean (got {value!r}).")


def _coerce_int(value: Any, path: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path} must be an integer (got {value!r}).") from exc


def _coerce_float(value: Any, path: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path} must be a number (got {value!r}).") from exc


def _check_pct(value: int, path: str) -> int:
    if not 0 <= value <= 100:
        raise ConfigError(f"{path} must be between 0 and 100 (got {value}).")
    return value


def _read_yaml(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Top-level config in {path} must be a mapping.")
    return data


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


def _build_config(raw: dict[str, Any]) -> Config:
    cfg = Config()

    unknown = set(raw) - _KNOWN_TOP_LEVEL_KEYS
    if unknown:
        key = sorted(unknown)[0]
        raise ConfigError(
            f"Unknown top-level config key {key!r}; "
            f"expected one of {sorted(_KNOWN_TOP_LEVEL_KEYS)}"
        )

    if "default_profile" in raw:
        cfg.default_profile = str(raw["default_profile"])
    if "default_model" in raw:
        cfg.default_model = raw["default_model"]
    if "permission_mode" in raw:
        try:
            cfg.permission_mode = PermissionMode(str(raw["permission_mode"]))
        except ValueError as exc:
            raise ConfigError(
                f"Unknown permission_mode {raw['permission_mode']!r}; "
                f"expected one of {[m.value for m in PermissionMode]}"
            ) from exc

    cfg.providers = _build_providers(raw.get("providers", {}))

    routing = raw.get("routing", {}) or {}
    cfg.routing = RoutingConfig(
        main=routing.get("main"),
        subagent=routing.get("subagent"),
        summarizer=routing.get("summarizer"),
        fallback=list(routing.get("fallback", []) or []),
    )

    budget = raw.get("budget", {}) or {}
    per_session = budget.get("per_session_usd")
    if per_session is not None:
        per_session = _coerce_float(per_session, "budget.per_session_usd")
        if per_session < 0:
            raise ConfigError(
                f"budget.per_session_usd must be >= 0 (got {per_session})."
            )
    cfg.budget = BudgetConfig(
        per_session_usd=per_session,
        hard_stop=_normalize_bool(budget.get("hard_stop", True), "budget.hard_stop"),
        warn_at_pct=_check_pct(
            _coerce_int(budget.get("warn_at_pct", 80), "budget.warn_at_pct"),
            "budget.warn_at_pct",
        ),
    )

    ctx = raw.get("context", {}) or {}
    repo_map_raw = str(ctx.get("repo_map", "tool"))
    from jarn.config.schema import _VALID_REPO_MAP_MODES

    if repo_map_raw not in _VALID_REPO_MAP_MODES:
        raise ConfigError(
            f"context.repo_map must be one of "
            f"{sorted(_VALID_REPO_MAP_MODES)} (got {repo_map_raw!r})."
        )
    repo_map_tokens_raw = _coerce_int(ctx.get("repo_map_tokens", 1024), "context.repo_map_tokens")
    if repo_map_tokens_raw <= 0:
        raise ConfigError(
            f"context.repo_map_tokens must be > 0 (got {repo_map_tokens_raw})."
        )
    cfg.context = ContextConfig(
        auto_compact=_normalize_bool(
            ctx.get("auto_compact", True), "context.auto_compact"
        ),
        compact_at_pct=_check_pct(
            _coerce_int(ctx.get("compact_at_pct", 85), "context.compact_at_pct"),
            "context.compact_at_pct",
        ),
        repo_map=repo_map_raw,
        repo_map_tokens=repo_map_tokens_raw,
    )

    ex = raw.get("execution", {}) or {}
    _valid_local_sandbox = {"off", "auto", "require"}
    local_sandbox_raw = str(ex.get("local_sandbox", "off"))
    if local_sandbox_raw not in _valid_local_sandbox:
        raise ConfigError(
            f"execution.local_sandbox must be one of "
            f"{sorted(_valid_local_sandbox)} (got {local_sandbox_raw!r})."
        )
    _valid_backends = {"local", "sandbox", "docker"}
    backend_raw = str(ex.get("backend", "local"))
    if backend_raw not in _valid_backends:
        raise ConfigError(
            f"execution.backend must be one of "
            f"{sorted(_valid_backends)} (got {backend_raw!r})."
        )
    sandbox_writable_raw = ex.get("sandbox_writable", []) or []
    if not isinstance(sandbox_writable_raw, list):
        raise ConfigError(
            f"execution.sandbox_writable must be a list "
            f"(got {sandbox_writable_raw!r})."
        )
    docker_memory_raw = ex.get("docker_memory", "")
    if not isinstance(docker_memory_raw, str):
        raise ConfigError(
            f"execution.docker_memory must be a string (got {docker_memory_raw!r})."
        )
    docker_pids_raw = ex.get("docker_pids", 0)
    if not isinstance(docker_pids_raw, int) or isinstance(docker_pids_raw, bool):
        raise ConfigError(
            f"execution.docker_pids must be an integer (got {docker_pids_raw!r})."
        )
    docker_cpus_raw = ex.get("docker_cpus", "")
    if not isinstance(docker_cpus_raw, str):
        raise ConfigError(
            f"execution.docker_cpus must be a string (got {docker_cpus_raw!r})."
        )
    docker_user_raw = ex.get("docker_user", "")
    if not isinstance(docker_user_raw, str):
        raise ConfigError(
            f"execution.docker_user must be a string (got {docker_user_raw!r})."
        )
    cfg.execution = ExecutionConfig(
        backend=backend_raw,
        sandbox_provider=str(ex.get("sandbox_provider", "langsmith")),
        docker_image=str(ex.get("docker_image", "python:3.12")),
        multimodal=_normalize_bool(ex.get("multimodal", True), "execution.multimodal"),
        allow_local_fallback=_normalize_bool(
            ex.get("allow_local_fallback", False), "execution.allow_local_fallback"
        ),
        local_sandbox=local_sandbox_raw,
        sandbox_allow_network=_normalize_bool(
            ex.get("sandbox_allow_network", True), "execution.sandbox_allow_network"
        ),
        sandbox_writable=[str(p) for p in sandbox_writable_raw],
        docker_memory=docker_memory_raw,
        docker_pids=docker_pids_raw,
        docker_cpus=docker_cpus_raw,
        docker_user=docker_user_raw,
    )

    cfg.policy = _build_policy_config(raw.get("policy", {}) or {})

    perms = raw.get("permissions", {}) or {}
    cfg.permissions = PermissionRules(
        allow=list(perms.get("allow", []) or []),
        deny=list(perms.get("deny", []) or []),
    )

    cfg.hooks = [_build_hook(h) for h in raw.get("hooks", []) or []]
    cfg.mcp_servers = [_build_mcp(m) for m in raw.get("mcp_servers", []) or []]
    cfg.async_subagents = [
        _build_async_subagent(a) for a in raw.get("async_subagents", []) or []
    ]

    obs = raw.get("observability", {}) or {}
    _valid_log_levels = {"debug", "info", "warning", "error"}
    log_level_raw = str(obs.get("log_level", "info"))
    if log_level_raw not in _valid_log_levels:
        raise ConfigError(
            f"observability.log_level must be one of "
            f"{sorted(_valid_log_levels)} (got {log_level_raw!r})."
        )
    cfg.observability = ObservabilityConfig(
        langsmith=_normalize_bool(
            obs.get("langsmith", False), "observability.langsmith"
        ),
        telemetry=_normalize_bool(
            obs.get("telemetry", False), "observability.telemetry"
        ),
        log_level=log_level_raw,
        transcript=_normalize_bool(
            obs.get("transcript", True), "observability.transcript"
        ),
    )

    ui = raw.get("ui", {}) or {}
    cfg.ui = UIConfig(
        theme=str(ui.get("theme", "dark")),
        accent=str(ui.get("accent", "cyan")),
    )

    compat = raw.get("compat", {}) or {}
    cfg.compat = _build_compat(compat)

    git = raw.get("git", {}) or {}
    cfg.git = _build_git_config(git)

    wiki = raw.get("wiki", {}) or {}
    cfg.wiki = _build_wiki_config(wiki)

    return cfg


def _build_providers(raw: dict[str, Any]) -> dict[str, ProviderConfig]:
    providers: dict[str, ProviderConfig] = {}
    for name, spec in (raw or {}).items():
        spec = spec or {}
        if not isinstance(spec, dict):
            raise ConfigError(
                f"Provider {name!r} must be a mapping (got {spec!r})."
            )
        type_str = spec.get("type", name)
        try:
            ptype = ProviderType(str(type_str))
        except ValueError as exc:
            raise ConfigError(
                f"Provider {name!r} has unknown type {type_str!r}; "
                f"expected one of {[p.value for p in ProviderType]}"
            ) from exc
        known = {"type", "api_key", "base_url"}
        providers[name] = ProviderConfig(
            type=ptype,
            api_key=spec.get("api_key"),
            base_url=spec.get("base_url"),
            extra={k: v for k, v in spec.items() if k not in known},
        )
    return providers


def _build_hook(raw: dict[str, Any]) -> HookSpec:
    if "event" not in raw or "command" not in raw:
        raise ConfigError(f"Hook entry must have 'event' and 'command': {raw!r}")
    if not isinstance(raw["event"], str):
        raise ConfigError(f"Hook 'event' must be a string (got {raw['event']!r}).")
    if not isinstance(raw["command"], str):
        raise ConfigError(
            f"Hook 'command' must be a string (got {raw['command']!r})."
        )
    return HookSpec(
        event=raw["event"],
        command=raw["command"],
        name=raw.get("name"),
        matcher=raw.get("matcher"),
        blocking=_normalize_bool(raw.get("blocking", False), "hook.blocking"),
    )


def _build_async_subagent(raw: dict[str, Any]) -> AsyncSubagentSpec:
    for key in ("name", "description", "graph_id"):
        if key not in raw:
            raise ConfigError(f"async_subagent entry needs '{key}': {raw!r}")
        if not isinstance(raw[key], str):
            raise ConfigError(
                f"async_subagent '{key}' must be a string (got {raw[key]!r})."
            )
    headers = raw.get("headers")
    if headers is not None and not isinstance(headers, dict):
        raise ConfigError(
            f"async_subagent 'headers' must be a mapping (got {headers!r})."
        )
    return AsyncSubagentSpec(
        name=raw["name"],
        description=raw["description"],
        graph_id=raw["graph_id"],
        url=raw.get("url"),
        headers=dict(headers or {}),
    )


def _build_mcp(raw: dict[str, Any]) -> MCPServer:
    if "name" not in raw:
        raise ConfigError(f"MCP server entry must have a 'name': {raw!r}")
    args = raw.get("args", []) or []
    if not isinstance(args, list):
        raise ConfigError(f"MCP server 'args' must be a list (got {args!r}).")
    env = raw.get("env", {}) or {}
    if not isinstance(env, dict):
        raise ConfigError(f"MCP server 'env' must be a mapping (got {env!r}).")
    headers = raw.get("headers")
    if headers is not None and not isinstance(headers, dict):
        raise ConfigError(
            f"MCP server 'headers' must be a mapping (got {headers!r})."
        )
    return MCPServer(
        name=str(raw["name"]),
        transport=str(raw.get("transport", "stdio")),
        command=raw.get("command"),
        args=list(args),
        url=raw.get("url"),
        headers=dict(headers or {}),
        env=dict(env),
        enabled=_normalize_bool(raw.get("enabled", True), "mcp_server.enabled"),
    )


def _build_git_config(raw: dict[str, Any]) -> GitConfig:
    _valid_modes = {"shadow", "commit"}
    mode = str(raw.get("checkpoint_mode", "shadow"))
    if mode not in _valid_modes:
        raise ConfigError(
            f"git.checkpoint_mode must be one of "
            f"{sorted(_valid_modes)} (got {mode!r})."
        )
    return GitConfig(
        autocheckpoint=_normalize_bool(
            raw.get("autocheckpoint", False), "git.autocheckpoint"
        ),
        checkpoint_mode=mode,
    )


def _build_policy_config(raw: dict[str, Any]) -> PolicyConfig:
    from jarn.config.profiles import PROFILE_NAMES

    profile = str(raw.get("profile", ""))
    if profile and profile not in PROFILE_NAMES:
        raise ConfigError(
            f"policy.profile must be one of "
            f"{sorted(PROFILE_NAMES)} or empty (got {profile!r})."
        )
    return PolicyConfig(
        profile=profile,
        web_tools=_normalize_bool(raw.get("web_tools", True), "policy.web_tools"),
    )


def _build_wiki_config(raw: dict[str, Any]) -> WikiConfig:
    return WikiConfig(
        enabled=_normalize_bool(raw.get("enabled", False), "wiki.enabled"),
    )


def _build_compat(raw: dict[str, Any]) -> CompatConfig:
    context_files_raw = raw.get("context_files")
    if context_files_raw is not None:
        if not isinstance(context_files_raw, list):
            raise ConfigError(
                f"compat.context_files must be a list (got {context_files_raw!r})."
            )
        context_files = [str(f) for f in context_files_raw]
    else:
        context_files = ["JARN.md", "AGENTS.md", "CLAUDE.md"]
    read_claude_dir = _normalize_bool(
        raw.get("read_claude_dir", True), "compat.read_claude_dir"
    )
    return CompatConfig(context_files=context_files, read_claude_dir=read_claude_dir)


def load_config(
    *,
    global_path: Path | None = None,
    project_path: Path | None = None,
    project_root: Path | None = None,
    project_trusted: bool = True,
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
    """
    gpath = global_path if global_path is not None else paths.global_config_path()
    ppath = (
        project_path
        if project_path is not None
        else paths.project_config_path(project_root)
    )

    project_raw = _read_yaml(ppath)
    if not project_trusted:
        from jarn.config.trust import sanitize_project

        project_raw = sanitize_project(project_raw)

    merged: dict[str, Any] = {}
    merged = _deep_merge(merged, _read_yaml(gpath))
    merged = _deep_merge(merged, project_raw)
    return _build_config(merged)
