"""Typed configuration model for J.A.R.N.

The on-disk format is YAML; these dataclasses are the in-memory representation
after the two tiers (global + project) have been merged and secrets resolved.

Every field has a sensible default so a completely empty config still yields a
usable agent. See :mod:`jarn.config.defaults` for the shipped defaults and
:mod:`jarn.config.loader` for how YAML is parsed into these structures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PermissionMode(str, Enum):
    """Coarse trust level applied at the top of a session.

    Ordering matters: each mode is strictly more permissive than the previous.
    """

    PLAN = "plan"          # read-only: may read/plan, never write or run shell
    ASK = "ask"            # default: prompt before write / impactful shell / network
    AUTO_EDIT = "auto-edit"  # edit + web_search/fetch freely; still prompt for shell/MCP
    YOLO = "yolo"          # full-auto: never prompt (danger-guard still applies)

    @property
    def rank(self) -> int:
        return {"plan": 0, "ask": 1, "auto-edit": 2, "yolo": 3}[self.value]


class ProviderType(str, Enum):
    # OpenAI-compatible (served through ChatOpenAI + base_url — no extra deps)
    OPENROUTER = "openrouter"
    OPENAI = "openai"
    LMSTUDIO = "lmstudio"
    GROQ = "groq"
    DEEPSEEK = "deepseek"
    TOGETHER = "together"
    FIREWORKS = "fireworks"
    XAI = "xai"
    OPENAI_COMPATIBLE = "openai_compatible"  # any custom base_url endpoint
    # Dedicated integrations
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"
    GOOGLE = "google"
    MISTRAL = "mistral"


@dataclass(slots=True)
class ProviderConfig:
    """A single model provider. ``api_key`` may be an unresolved reference
    (``${ENV_VAR}`` or ``keychain:service/user``) until secrets are resolved."""

    type: ProviderType
    api_key: str | None = None
    base_url: str | None = None
    # Extra kwargs forwarded verbatim to the underlying chat model constructor.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RoutingConfig:
    """Per-task model routing. Values are fully-qualified model refs of the form
    ``<profile>/<model>`` (e.g. ``openrouter/anthropic/claude-opus-4-8``)."""

    main: str | None = None
    subagent: str | None = None
    summarizer: str | None = None
    #: Ordered fallback chain tried when the primary model errors.
    fallback: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BudgetConfig:
    per_session_usd: float | None = None
    hard_stop: bool = True       # stop the run when exceeded vs warn only
    warn_at_pct: int = 80


@dataclass(slots=True)
class ContextConfig:
    auto_compact: bool = True
    compact_at_pct: int = 85     # summarize when context window this % full


@dataclass(slots=True)
class ExecutionConfig:
    """Where tools run. ``local`` is the default; ``sandbox`` isolates execution
    (requires an available sandbox runtime — see docs)."""

    backend: str = "local"            # local | sandbox
    sandbox_provider: str = "langsmith"  # future: docker, e2b, ...
    multimodal: bool = True           # read_file auto-detects images/PDF/audio/video
    # When ``backend: sandbox`` but the sandbox can't start, fall back to running
    # on the host. OFF by default: silently downgrading isolation is a footgun, so
    # we fail closed unless the user explicitly opts in.
    allow_local_fallback: bool = False


@dataclass(slots=True)
class PermissionRules:
    """Persisted fine-grained allow/deny rules layered under the coarse mode.

    Patterns are shell-glob-style and matched against the normalized command
    string for shell, or the path for filesystem writes.
    """

    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)


@dataclass(slots=True)
class HookSpec:
    """A single hook: a shell command run on a lifecycle event."""

    event: str                   # see jarn.extensibility.hooks.HookEvent
    command: str
    name: str | None = None
    matcher: str | None = None   # optional glob to scope which tool/file triggers
    blocking: bool = False       # if True, non-zero exit aborts the action


@dataclass(slots=True)
class AsyncSubagentSpec:
    """A remote/background subagent reached via the DeepAgents Agent Protocol."""

    name: str
    description: str
    graph_id: str
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class MCPServer:
    """An MCP server the agent connects to for extra tools."""

    name: str
    transport: str = "stdio"     # "stdio" | "http"
    command: str | None = None   # stdio
    args: list[str] = field(default_factory=list)
    url: str | None = None       # http
    headers: dict[str, str] = field(default_factory=dict)  # http/sse auth
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    health: str | None = None    # last-known health: "ok" | "error" | None (unknown)


@dataclass(slots=True)
class ObservabilityConfig:
    langsmith: bool = False       # opt-in tracing
    telemetry: bool = False       # opt-in usage analytics, default OFF
    log_level: str = "info"
    transcript: bool = True       # append-only JSONL session transcript under .jarn/sessions/


@dataclass(slots=True)
class UIConfig:
    theme: str = "dark"           # dark | light | high-contrast
    accent: str = "cyan"          # brand accent


@dataclass(slots=True)
class CompatConfig:
    """Cross-vendor interop: which context files to check and whether to read
    ``.claude/`` extension directories alongside ``.jarn/``.

    ``context_files`` is an ordered list — the first file present in the project
    root wins. Add or reorder entries to control which vendor's context file
    J.A.R.N. picks up when ``JARN.md`` is absent.

    ``read_claude_dir`` enables discovery of skills and commands from
    ``~/.claude/skills``, ``~/.claude/commands``, ``<project>/.claude/skills``,
    and ``<project>/.claude/commands`` in addition to the canonical ``.jarn``
    directories. ``.jarn`` always takes precedence on name conflicts.
    """

    context_files: list[str] = field(
        default_factory=lambda: ["JARN.md", "AGENTS.md", "CLAUDE.md"]
    )
    read_claude_dir: bool = True


@dataclass(slots=True)
class Config:
    """The fully-merged configuration handed to the rest of the application."""

    default_profile: str = "openrouter"
    default_model: str | None = None
    permission_mode: PermissionMode = PermissionMode.ASK

    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    permissions: PermissionRules = field(default_factory=PermissionRules)
    hooks: list[HookSpec] = field(default_factory=list)
    mcp_servers: list[MCPServer] = field(default_factory=list)
    async_subagents: list[AsyncSubagentSpec] = field(default_factory=list)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    compat: CompatConfig = field(default_factory=CompatConfig)

    def resolved_main_model(self) -> str | None:
        """The model used for the top-level agent loop."""
        return self.routing.main or self.default_model

    def resolved_subagent_model(self) -> str | None:
        return self.routing.subagent or self.resolved_main_model()

    def resolved_summarizer_model(self) -> str | None:
        return self.routing.summarizer or self.resolved_subagent_model()
