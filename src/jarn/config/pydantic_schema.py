"""Pydantic configuration schema, versioning, and migration.

Validated at load time; converted to the public dataclass :class:`Config` at the
boundary in :mod:`jarn.config.loader`.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    PlanConfig,
    PolicyConfig,
    PricingConfig,
    ProviderConfig,
    ProviderType,
    RoutingConfig,
    TracingConfig,
    UIConfig,
    VerifyConfig,
    WikiConfig,
)

CURRENT_CONFIG_VERSION = 2

_TRUE_STRINGS = {"true", "yes", "on", "1"}
_FALSE_STRINGS = {"false", "no", "off", "0", ""}

_PROVIDER_EXTRA_KEYS = frozenset({
    "max_retries",
    "timeout",
    "temperature",
    "top_p",
    "max_tokens",
    "streaming",
    "model_kwargs",
    "extra_body",
    "frequency_penalty",
    "presence_penalty",
    "stop",
    "n",
    "seed",
    "keep_alive",
})

_VALID_MCP_TRANSPORTS = frozenset({"stdio", "http", "sse", "streamable_http"})
_STDIO_CMD_META = re.compile(r"[;|&`$<>(){}]")


class ConfigValidationError(ValueError):
    """Raised by pydantic validators; converted to ConfigError at the boundary."""


def _normalize_bool(value: Any, path: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_STRINGS:
            return True
        if token in _FALSE_STRINGS:
            return False
        raise ConfigValidationError(f"{path} must be a boolean (got {value!r}).")
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)
        raise ConfigValidationError(f"{path} must be a boolean (got {value!r}).")
    raise ConfigValidationError(f"{path} must be a boolean (got {value!r}).")


def _coerce_int(value: Any, path: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigValidationError(f"{path} must be an integer (got {value!r}).") from exc


def _coerce_float(value: Any, path: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigValidationError(f"{path} must be a number (got {value!r}).") from exc


def _check_pct(value: int, path: str) -> int:
    if not 0 <= value <= 100:
        raise ConfigValidationError(f"{path} must be between 0 and 100 (got {value}).")
    return value


def _validate_absolute_http_url(url: str, *, context: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigValidationError(f"{context} must be an absolute http(s) URL (got {url!r}).")


def _validate_stdio_command(command: str, *, context: str) -> None:
    if _STDIO_CMD_META.search(command):
        raise ConfigValidationError(
            f"{context} must not contain shell metacharacters "
            f"(got {command!r}). MCP stdio servers are spawned without a shell."
        )


def _validate_string_headers(headers: object, *, context: str) -> dict[str, str]:
    if headers is None:
        return {}
    if not isinstance(headers, dict):
        raise ConfigValidationError(f"{context} must be a mapping (got {headers!r}).")
    out: dict[str, str] = {}
    for key, val in headers.items():
        if not isinstance(key, str) or not isinstance(val, str):
            raise ConfigValidationError(
                f"{context} keys and values must be strings (got {key!r}: {val!r})."
            )
        out[key] = val
    return out


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderConfigModel(_StrictModel):
    type: ProviderType
    api_key: str | None = None
    base_url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _split_extra(cls, data: Any, info: Any) -> Any:
        if not isinstance(data, dict):
            return data
        known = {"type", "api_key", "base_url", "headers", "extra"}
        extra = {k: v for k, v in data.items() if k not in known}
        bad_extra = set(extra) - _PROVIDER_EXTRA_KEYS
        if bad_extra:
            raise ConfigValidationError(
                f"Provider has unknown extra key(s) {sorted(bad_extra)}. "
                f"Allowed: {sorted(_PROVIDER_EXTRA_KEYS)}. "
                "Use the top-level 'headers' field for HTTP auth headers."
            )
        result = {k: v for k, v in data.items() if k in known}
        if extra:
            result["extra"] = {**result.get("extra", {}), **extra}
        return result

    @field_validator("headers", mode="before")
    @classmethod
    def _headers(cls, value: Any) -> dict[str, str]:
        return _validate_string_headers(value, context="Provider 'headers'")


class RoutingConfigModel(_StrictModel):
    main: str | None = None
    subagent: str | None = None
    summarizer: str | None = None
    fallback: list[str] = Field(default_factory=list)
    prompt_cache: str = "auto"
    keep_alive: int = 1800

    @field_validator("prompt_cache", mode="before")
    @classmethod
    def _prompt_cache(cls, value: Any) -> str:
        from jarn.config.schema import _VALID_PROMPT_CACHE

        if isinstance(value, bool):
            value = "off" if value is False else "auto"
        raw = str(value)
        if raw not in _VALID_PROMPT_CACHE:
            raise ConfigValidationError(
                f"routing.prompt_cache must be one of "
                f"{sorted(_VALID_PROMPT_CACHE)} (got {raw!r})."
            )
        return raw

    @field_validator("keep_alive", mode="before")
    @classmethod
    def _keep_alive(cls, value: Any) -> int:
        raw = _coerce_int(value, "routing.keep_alive")
        if raw < 0:
            raise ConfigValidationError(f"routing.keep_alive must be >= 0 (got {raw}).")
        return raw


class BudgetConfigModel(_StrictModel):
    per_session_usd: float | None = None
    hard_stop: bool = True
    warn_at_pct: int = 80

    @field_validator("per_session_usd", mode="before")
    @classmethod
    def _per_session(cls, value: Any) -> float | None:
        if value is None:
            return None
        raw = _coerce_float(value, "budget.per_session_usd")
        if raw < 0:
            raise ConfigValidationError(f"budget.per_session_usd must be >= 0 (got {raw}).")
        return raw

    @field_validator("hard_stop", mode="before")
    @classmethod
    def _hard_stop(cls, value: Any) -> bool:
        return _normalize_bool(value, "budget.hard_stop")

    @field_validator("warn_at_pct", mode="before")
    @classmethod
    def _warn_at_pct(cls, value: Any) -> int:
        return _check_pct(_coerce_int(value, "budget.warn_at_pct"), "budget.warn_at_pct")


class ContextConfigModel(_StrictModel):
    auto_compact: bool = True
    compact_at_pct: int = 85
    repo_map: str = "tool"
    repo_map_tokens: int = 1024
    memory_tokens: int = 4096
    wiki_index_tokens: int = 1024
    project_context_tokens: int = 8192

    @field_validator("auto_compact", mode="before")
    @classmethod
    def _auto_compact(cls, value: Any) -> bool:
        return _normalize_bool(value, "context.auto_compact")

    @field_validator("compact_at_pct", mode="before")
    @classmethod
    def _compact_at_pct(cls, value: Any) -> int:
        return _check_pct(
            _coerce_int(value, "context.compact_at_pct"), "context.compact_at_pct"
        )

    @field_validator("repo_map", mode="before")
    @classmethod
    def _repo_map(cls, value: Any) -> str:
        from jarn.config.schema import _VALID_REPO_MAP_MODES

        raw = str(value)
        if raw not in _VALID_REPO_MAP_MODES:
            raise ConfigValidationError(
                f"context.repo_map must be one of "
                f"{sorted(_VALID_REPO_MAP_MODES)} (got {raw!r})."
            )
        return raw

    @field_validator(
        "repo_map_tokens", "memory_tokens", "wiki_index_tokens", "project_context_tokens",
        mode="before",
    )
    @classmethod
    def _positive_tokens(cls, value: Any, info: Any) -> int:
        path = f"context.{info.field_name}"
        raw = _coerce_int(value, path)
        if raw <= 0:
            raise ConfigValidationError(f"{path} must be > 0 (got {raw}).")
        return raw


class ExecutionConfigModel(_StrictModel):
    backend: str = "local"
    background: bool = True
    background_max_concurrent: int | None = None
    background_max_lifetime_secs: float | None = None
    sandbox_provider: str = "langsmith"
    docker_image: str = "python:3.12"
    multimodal: bool = True
    allow_local_fallback: bool = False
    local_sandbox: str = "off"
    sandbox_allow_network: bool = True
    sandbox_writable: list[str] = Field(default_factory=list)
    docker_memory: str = ""
    docker_pids: int = 0
    docker_cpus: str = ""
    docker_user: str = ""

    @field_validator("backend", mode="before")
    @classmethod
    def _backend(cls, value: Any) -> str:
        raw = str(value)
        valid = {"local", "sandbox", "docker"}
        if raw not in valid:
            raise ConfigValidationError(
                f"execution.backend must be one of {sorted(valid)} (got {raw!r})."
            )
        return raw

    @field_validator("local_sandbox", mode="before")
    @classmethod
    def _local_sandbox(cls, value: Any) -> str:
        raw = str(value)
        valid = {"off", "auto", "require"}
        if raw not in valid:
            raise ConfigValidationError(
                f"execution.local_sandbox must be one of {sorted(valid)} (got {raw!r})."
            )
        return raw

    @field_validator(
        "background", "multimodal", "allow_local_fallback", "sandbox_allow_network",
        mode="before",
    )
    @classmethod
    def _bools(cls, value: Any, info: Any) -> bool:
        return _normalize_bool(value, f"execution.{info.field_name}")

    @field_validator("sandbox_writable", mode="before")
    @classmethod
    def _sandbox_writable(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ConfigValidationError(
                f"execution.sandbox_writable must be a list (got {value!r})."
            )
        return [str(p) for p in value]

    @field_validator("docker_memory", "docker_cpus", "docker_user", mode="before")
    @classmethod
    def _docker_strings(cls, value: Any, info: Any) -> str:
        if not isinstance(value, str):
            raise ConfigValidationError(
                f"execution.{info.field_name} must be a string (got {value!r})."
            )
        return value

    @field_validator("docker_pids", mode="before")
    @classmethod
    def _docker_pids(cls, value: Any) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ConfigValidationError(
                f"execution.docker_pids must be an integer (got {value!r})."
            )
        return value


class PolicyConfigModel(_StrictModel):
    web_tools: bool = True

    @field_validator("web_tools", mode="before")
    @classmethod
    def _web_tools(cls, value: Any) -> bool:
        return _normalize_bool(value, "policy.web_tools")


class PermissionRulesModel(_StrictModel):
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class HookSpecModel(_StrictModel):
    event: str
    command: str
    name: str | None = None
    matcher: str | None = None
    blocking: bool = False

    @field_validator("event", mode="before")
    @classmethod
    def _event(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ConfigValidationError(f"Hook 'event' must be a string (got {value!r}).")
        from jarn.extensibility.hooks import HookEvent

        valid_events = {e.value for e in HookEvent}
        if value not in valid_events:
            raise ConfigValidationError(
                f"Hook 'event' must be one of {sorted(valid_events)} (got {value!r})."
            )
        return value

    @field_validator("command", mode="before")
    @classmethod
    def _command(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ConfigValidationError(
                f"Hook 'command' must be a string (got {value!r})."
            )
        return value

    @field_validator("blocking", mode="before")
    @classmethod
    def _blocking(cls, value: Any) -> bool:
        return _normalize_bool(value, "hook.blocking")


class AsyncSubagentSpecModel(_StrictModel):
    name: str
    description: str
    graph_id: str
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("name", "description", "graph_id", mode="before")
    @classmethod
    def _required_strings(cls, value: Any, info: Any) -> str:
        if not isinstance(value, str):
            raise ConfigValidationError(
                f"async_subagent '{info.field_name}' must be a string (got {value!r})."
            )
        return value

    @field_validator("url", mode="before")
    @classmethod
    def _url(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ConfigValidationError(
                f"async_subagent 'url' must be a string (got {value!r})."
            )
        _validate_absolute_http_url(value, context="async_subagent 'url'")
        return value

    @field_validator("headers", mode="before")
    @classmethod
    def _headers(cls, value: Any) -> dict[str, str]:
        return _validate_string_headers(value, context="async_subagent 'headers'")


class MCPServerModel(_StrictModel):
    name: str
    transport: str = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    health: str | None = None
    timeout_secs: int = 30

    @field_validator("transport", mode="before")
    @classmethod
    def _transport(cls, value: Any) -> str:
        transport = str(value)
        if transport not in _VALID_MCP_TRANSPORTS:
            raise ConfigValidationError(
                f"MCP server transport must be one of "
                f"{sorted(_VALID_MCP_TRANSPORTS)} (got {transport!r})."
            )
        return transport

    @field_validator("args", mode="before")
    @classmethod
    def _args(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ConfigValidationError(f"MCP server 'args' must be a list (got {value!r}).")
        return list(value)

    @field_validator("env", mode="before")
    @classmethod
    def _env(cls, value: Any) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ConfigValidationError(f"MCP server 'env' must be a mapping (got {value!r}).")
        return dict(value)

    @field_validator("enabled", mode="before")
    @classmethod
    def _enabled(cls, value: Any) -> bool:
        return _normalize_bool(value, "mcp_server.enabled")

    @field_validator("timeout_secs", mode="before")
    @classmethod
    def _timeout_secs(cls, value: Any) -> int:
        raw = _coerce_int(value, "mcp_server.timeout_secs")
        if raw <= 0:
            raise ConfigValidationError(
                f"mcp_server.timeout_secs must be > 0 (got {raw})."
            )
        return raw

    @field_validator("headers", mode="before")
    @classmethod
    def _headers(cls, value: Any) -> dict[str, str]:
        return _validate_string_headers(value, context="MCP server 'headers'")

    @model_validator(mode="after")
    def _transport_requirements(self) -> MCPServerModel:
        if self.transport == "stdio":
            if not self.command or not isinstance(self.command, str):
                raise ConfigValidationError(
                    f"MCP server {self.name!r} with transport 'stdio' needs a 'command' string."
                )
            _validate_stdio_command(
                self.command, context=f"MCP server {self.name!r} command"
            )
        if self.transport in ("http", "sse", "streamable_http"):
            if not self.url or not isinstance(self.url, str):
                raise ConfigValidationError(
                    f"MCP server {self.name!r} with transport {self.transport!r} "
                    "needs a 'url' string."
                )
            _validate_absolute_http_url(self.url, context=f"MCP server {self.name!r} url")
        return self


class TracingConfigModel(_StrictModel):
    backend: str = "langsmith"

    @field_validator("backend", mode="before")
    @classmethod
    def _backend(cls, value: Any) -> str:
        raw = str(value)
        valid = {"langsmith", "otel"}
        if raw not in valid:
            raise ConfigValidationError(
                f"observability.tracing.backend must be one of {sorted(valid)} "
                f"(got {raw!r})."
            )
        return raw


class ObservabilityConfigModel(_StrictModel):
    langsmith: bool = False
    tracing: TracingConfigModel = Field(default_factory=TracingConfigModel)
    telemetry: bool = False
    log_level: str = "info"
    transcript: bool = True

    @field_validator("langsmith", "telemetry", "transcript", mode="before")
    @classmethod
    def _bools(cls, value: Any, info: Any) -> bool:
        return _normalize_bool(value, f"observability.{info.field_name}")

    @field_validator("log_level", mode="before")
    @classmethod
    def _log_level(cls, value: Any) -> str:
        raw = str(value)
        valid = {"debug", "info", "warning", "error"}
        if raw not in valid:
            raise ConfigValidationError(
                f"observability.log_level must be one of {sorted(valid)} (got {raw!r})."
            )
        return raw


class UIConfigModel(_StrictModel):
    theme: str = "dark"
    accent: str = "cyan"
    splash: str = "compact"
    approval_diff_lines: int = 40
    notify: str = "bell"
    notify_min_secs: int = 10
    terminal_title: bool = True

    @field_validator("splash", mode="before")
    @classmethod
    def _splash(cls, value: Any) -> str:
        from jarn.config.schema import _VALID_SPLASH_VALUES

        raw = str(value)
        if raw not in _VALID_SPLASH_VALUES:
            raise ConfigValidationError(
                f"ui.splash must be one of {sorted(_VALID_SPLASH_VALUES)} (got {raw!r})."
            )
        return raw

    @field_validator("notify", mode="before")
    @classmethod
    def _notify(cls, value: Any) -> str:
        from jarn.config.schema import _VALID_NOTIFY_VALUES

        raw = str(value)
        if raw not in _VALID_NOTIFY_VALUES:
            raise ConfigValidationError(
                f"ui.notify must be one of {sorted(_VALID_NOTIFY_VALUES)} (got {raw!r})."
            )
        return raw

    @field_validator("notify_min_secs", mode="before")
    @classmethod
    def _notify_min_secs(cls, value: Any) -> int:
        raw = _coerce_int(value, "ui.notify_min_secs")
        if raw < 0:
            raise ConfigValidationError(
                f"ui.notify_min_secs must be >= 0 (got {raw})."
            )
        return raw

    @field_validator("terminal_title", mode="before")
    @classmethod
    def _terminal_title(cls, value: Any) -> bool:
        return _normalize_bool(value, "ui.terminal_title")


class CompatConfigModel(_StrictModel):
    context_files: list[str] = Field(
        default_factory=lambda: ["JARN.md", "AGENTS.md", "CLAUDE.md"]
    )
    read_claude_dir: bool = True

    @field_validator("context_files", mode="before")
    @classmethod
    def _context_files(cls, value: Any) -> list[str]:
        if value is None:
            return ["JARN.md", "AGENTS.md", "CLAUDE.md"]
        if not isinstance(value, list):
            raise ConfigValidationError(
                f"compat.context_files must be a list (got {value!r})."
            )
        return [str(f) for f in value]

    @field_validator("read_claude_dir", mode="before")
    @classmethod
    def _read_claude_dir(cls, value: Any) -> bool:
        return _normalize_bool(value, "compat.read_claude_dir")


class GitConfigModel(_StrictModel):
    autocheckpoint: bool = False
    checkpoint_mode: str = "shadow"

    @field_validator("autocheckpoint", mode="before")
    @classmethod
    def _autocheckpoint(cls, value: Any) -> bool:
        return _normalize_bool(value, "git.autocheckpoint")

    @field_validator("checkpoint_mode", mode="before")
    @classmethod
    def _checkpoint_mode(cls, value: Any) -> str:
        mode = str(value)
        valid = {"shadow", "commit"}
        if mode not in valid:
            raise ConfigValidationError(
                f"git.checkpoint_mode must be one of {sorted(valid)} (got {mode!r})."
            )
        return mode


class WikiConfigModel(_StrictModel):
    enabled: bool = False

    @field_validator("enabled", mode="before")
    @classmethod
    def _enabled(cls, value: Any) -> bool:
        return _normalize_bool(value, "wiki.enabled")


class PlanConfigModel(_StrictModel):
    exit_mode: str = "auto-edit"

    @field_validator("exit_mode", mode="before")
    @classmethod
    def _exit_mode(cls, value: Any) -> str:
        from jarn.config.schema import _VALID_EXIT_MODES

        raw = str(value)
        if raw not in _VALID_EXIT_MODES:
            raise ConfigValidationError(
                f"plan.exit_mode must be one of {sorted(_VALID_EXIT_MODES)} (got {raw!r})."
            )
        return raw


class VerifyConfigModel(_StrictModel):
    gate: str = "suggest"

    @field_validator("gate", mode="before")
    @classmethod
    def _gate(cls, value: Any) -> str:
        from jarn.config.schema import _VALID_VERIFY_GATES

        raw = str(value)
        if raw not in _VALID_VERIFY_GATES:
            raise ConfigValidationError(
                f"verify.gate must be one of {sorted(_VALID_VERIFY_GATES)} (got {raw!r})."
            )
        return raw


class PricingConfigModel(_StrictModel):
    network: bool = True

    @field_validator("network", mode="before")
    @classmethod
    def _network(cls, value: Any) -> bool:
        return _normalize_bool(value, "pricing.network")


class ConfigModel(_StrictModel):
    config_version: int = CURRENT_CONFIG_VERSION
    default_profile: str = "openrouter"
    default_model: str | None = None
    permission_mode: PermissionMode = PermissionMode.ASK
    strict_secrets: bool = False
    hook_inherit_env: bool = False
    hook_global_require_trust: bool = False
    providers: dict[str, ProviderConfigModel] = Field(default_factory=dict)
    routing: RoutingConfigModel = Field(default_factory=RoutingConfigModel)
    budget: BudgetConfigModel = Field(default_factory=BudgetConfigModel)
    context: ContextConfigModel = Field(default_factory=ContextConfigModel)
    execution: ExecutionConfigModel = Field(default_factory=ExecutionConfigModel)
    policy: PolicyConfigModel = Field(default_factory=PolicyConfigModel)
    permissions: PermissionRulesModel = Field(default_factory=PermissionRulesModel)
    hooks: list[HookSpecModel] = Field(default_factory=list)
    mcp_servers: list[MCPServerModel] = Field(default_factory=list)
    async_subagents: list[AsyncSubagentSpecModel] = Field(default_factory=list)
    observability: ObservabilityConfigModel = Field(default_factory=ObservabilityConfigModel)
    ui: UIConfigModel = Field(default_factory=UIConfigModel)
    compat: CompatConfigModel = Field(default_factory=CompatConfigModel)
    git: GitConfigModel = Field(default_factory=GitConfigModel)
    wiki: WikiConfigModel = Field(default_factory=WikiConfigModel)
    plan: PlanConfigModel = Field(default_factory=PlanConfigModel)
    verify: VerifyConfigModel = Field(default_factory=VerifyConfigModel)
    pricing: PricingConfigModel = Field(default_factory=PricingConfigModel)

    @field_validator("permission_mode", mode="before")
    @classmethod
    def _permission_mode(cls, value: Any) -> PermissionMode:
        try:
            return PermissionMode(str(value))
        except ValueError as exc:
            raise ConfigValidationError(
                f"Unknown permission_mode {value!r}; "
                f"expected one of {[m.value for m in PermissionMode]}"
            ) from exc

    @field_validator(
        "strict_secrets", "hook_inherit_env", "hook_global_require_trust", mode="before"
    )
    @classmethod
    def _top_bools(cls, value: Any, info: Any) -> bool:
        return _normalize_bool(value, info.field_name)

    @model_validator(mode="before")
    @classmethod
    def _providers_type_default(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        providers = data.get("providers")
        if not isinstance(providers, dict):
            return data
        fixed: dict[str, Any] = {}
        for name, spec in providers.items():
            if isinstance(spec, dict) and "type" not in spec:
                fixed[name] = {**spec, "type": name}
            else:
                fixed[name] = spec
        return {**data, "providers": fixed}


def _migrate_v0_to_v1(raw: dict[str, Any]) -> dict[str, Any]:
    """Upgrade version-0 (no ``config_version``) configs to version 1."""
    out = dict(raw)
    # v0 placed log_level at the top level; v1 nests it under observability.
    if "log_level" in out and "observability" not in out:
        out["observability"] = {"log_level": out.pop("log_level")}
    elif "log_level" in out:
        obs = dict(out.get("observability") or {})
        obs.setdefault("log_level", out.pop("log_level"))
        out["observability"] = obs
    out["config_version"] = 1
    return out


def _migrate_v1_to_v2(raw: dict[str, Any]) -> dict[str, Any]:
    """Upgrade version-1 configs to version 2.

    v2 removes ``policy.profile`` — the ``--profile`` CLI flag and ``/profile``
    command were removed in v0.6.0.  If the key is present, drop it and emit a
    :class:`UserWarning` so users know to switch to ``--preset`` / ``/preset``.
    """
    import warnings

    out = dict(raw)
    policy = out.get("policy")
    if isinstance(policy, dict) and "profile" in policy:
        dropped = policy["profile"]
        new_policy = {k: v for k, v in policy.items() if k != "profile"}
        out["policy"] = new_policy
        preset_hint = (
            f" Use 'jarn --preset {dropped}' or '/preset {dropped}' instead."
            if dropped
            else ""
        )
        warnings.warn(
            f"policy.profile ('{dropped}') was removed in v0.6.0 and has been "
            f"ignored.{preset_hint} Remove it from your config to silence this warning.",
            UserWarning,
            stacklevel=2,
        )
    out["config_version"] = CURRENT_CONFIG_VERSION
    return out


_MIGRATORS: dict[int, Any] = {
    0: _migrate_v0_to_v1,
    1: _migrate_v1_to_v2,
}


def migrate_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Apply sequential migrations until ``config_version`` is current."""
    version = raw.get("config_version", 0)
    if not isinstance(version, int) or isinstance(version, bool):
        raise ConfigValidationError(
            f"config_version must be an integer (got {version!r})."
        )
    data = dict(raw)
    while version < CURRENT_CONFIG_VERSION:
        migrator = _MIGRATORS.get(version)
        if migrator is None:
            raise ConfigValidationError(
                f"Unsupported config_version {version}; "
                f"expected {CURRENT_CONFIG_VERSION} or a migratable prior version."
            )
        data = migrator(data)
        version = data.get("config_version", CURRENT_CONFIG_VERSION)
    return data


def parse_config_model(raw: dict[str, Any]) -> ConfigModel:
    """Validate merged raw config via Pydantic."""
    migrated = migrate_config(raw)
    return ConfigModel.model_validate(migrated)


def config_to_dataclass(model: ConfigModel) -> Config:
    """Convert a validated Pydantic model to the public dataclass."""
    providers = {
        name: ProviderConfig(
            type=p.type,
            api_key=p.api_key,
            base_url=p.base_url,
            headers=dict(p.headers),
            extra=dict(p.extra),
        )
        for name, p in model.providers.items()
    }
    return Config(
        default_profile=model.default_profile,
        default_model=model.default_model,
        permission_mode=model.permission_mode,
        strict_secrets=model.strict_secrets,
        hook_inherit_env=model.hook_inherit_env,
        hook_global_require_trust=model.hook_global_require_trust,
        providers=providers,
        routing=RoutingConfig(
            main=model.routing.main,
            subagent=model.routing.subagent,
            summarizer=model.routing.summarizer,
            fallback=list(model.routing.fallback),
            prompt_cache=model.routing.prompt_cache,
            keep_alive=model.routing.keep_alive,
        ),
        budget=BudgetConfig(
            per_session_usd=model.budget.per_session_usd,
            hard_stop=model.budget.hard_stop,
            warn_at_pct=model.budget.warn_at_pct,
        ),
        context=ContextConfig(
            auto_compact=model.context.auto_compact,
            compact_at_pct=model.context.compact_at_pct,
            repo_map=model.context.repo_map,
            repo_map_tokens=model.context.repo_map_tokens,
            memory_tokens=model.context.memory_tokens,
            wiki_index_tokens=model.context.wiki_index_tokens,
            project_context_tokens=model.context.project_context_tokens,
        ),
        execution=ExecutionConfig(
            backend=model.execution.backend,
            background=model.execution.background,
            background_max_concurrent=model.execution.background_max_concurrent,
            background_max_lifetime_secs=model.execution.background_max_lifetime_secs,
            sandbox_provider=model.execution.sandbox_provider,
            docker_image=model.execution.docker_image,
            multimodal=model.execution.multimodal,
            allow_local_fallback=model.execution.allow_local_fallback,
            local_sandbox=model.execution.local_sandbox,
            sandbox_allow_network=model.execution.sandbox_allow_network,
            sandbox_writable=list(model.execution.sandbox_writable),
            docker_memory=model.execution.docker_memory,
            docker_pids=model.execution.docker_pids,
            docker_cpus=model.execution.docker_cpus,
            docker_user=model.execution.docker_user,
        ),
        policy=PolicyConfig(
            web_tools=model.policy.web_tools,
        ),
        permissions=PermissionRules(
            allow=list(model.permissions.allow),
            deny=list(model.permissions.deny),
        ),
        hooks=[
            HookSpec(
                event=h.event,
                command=h.command,
                name=h.name,
                matcher=h.matcher,
                blocking=h.blocking,
            )
            for h in model.hooks
        ],
        mcp_servers=[
            MCPServer(
                name=m.name,
                transport=m.transport,
                command=m.command,
                args=list(m.args),
                url=m.url,
                headers=dict(m.headers),
                env=dict(m.env),
                enabled=m.enabled,
                health=m.health,
                timeout_secs=m.timeout_secs,
            )
            for m in model.mcp_servers
        ],
        async_subagents=[
            AsyncSubagentSpec(
                name=a.name,
                description=a.description,
                graph_id=a.graph_id,
                url=a.url,
                headers=dict(a.headers),
            )
            for a in model.async_subagents
        ],
        observability=ObservabilityConfig(
            langsmith=model.observability.langsmith,
            tracing=TracingConfig(backend=model.observability.tracing.backend),
            telemetry=model.observability.telemetry,
            log_level=model.observability.log_level,
            transcript=model.observability.transcript,
        ),
        ui=UIConfig(
            theme=model.ui.theme,
            accent=model.ui.accent,
            splash=model.ui.splash,
            approval_diff_lines=model.ui.approval_diff_lines,
            notify=model.ui.notify,
            notify_min_secs=model.ui.notify_min_secs,
            terminal_title=model.ui.terminal_title,
        ),
        compat=CompatConfig(
            context_files=list(model.compat.context_files),
            read_claude_dir=model.compat.read_claude_dir,
        ),
        git=GitConfig(
            autocheckpoint=model.git.autocheckpoint,
            checkpoint_mode=model.git.checkpoint_mode,
        ),
        wiki=WikiConfig(enabled=model.wiki.enabled),
        plan=PlanConfig(exit_mode=model.plan.exit_mode),
        verify=VerifyConfig(gate=model.verify.gate),
        pricing=PricingConfig(network=model.pricing.network),
    )
