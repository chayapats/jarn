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


class SearchProviderType(str, Enum):
    """Which search provider ``web_search`` should use.

    ``auto``       — try tavily → brave → exa in order; first with a resolved
                     key wins; fall back to the keyless DuckDuckGo scraper.
    ``duckduckgo`` — always use the keyless DDG HTML scraper.
    ``tavily``     — use Tavily (requires ``TAVILY_API_KEY`` or ``search.api_key``).
    ``brave``      — use Brave Search (requires ``BRAVE_API_KEY`` or ``search.api_key``).
    ``exa``        — use Exa (requires ``EXA_API_KEY`` or ``search.api_key``).
    """

    AUTO = "auto"
    DUCKDUCKGO = "duckduckgo"
    TAVILY = "tavily"
    BRAVE = "brave"
    EXA = "exa"


@dataclass(slots=True)
class SearchConfig:
    """Pluggable web-search provider settings.

    ``provider``
        Which search backend to use (default ``auto``).

    ``api_key``
        Secret reference (``${ENV_VAR}`` / ``keychain:jarn/<provider>`` / ``file:…``)
        for the explicitly-named provider.  Ignored in ``auto`` mode — per-provider
        env vars and keychain entries are discovered automatically.  Never an inline
        literal.
    """

    provider: SearchProviderType = SearchProviderType.AUTO
    api_key: str = ""   # reference-only; resolved via jarn.config.secrets.resolve


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
    #: Optional HTTP headers forwarded to the provider client (e.g. custom auth).
    #: Prefer this over stuffing headers into ``extra`` so they can be redacted.
    headers: dict[str, str] = field(default_factory=dict)
    # Extra kwargs forwarded verbatim to the underlying chat model constructor.
    extra: dict[str, Any] = field(default_factory=dict)


_VALID_PROMPT_CACHE: frozenset[str] = frozenset({"auto", "off"})


@dataclass(slots=True)
class RoutingConfig:
    """Per-task model routing. Values are fully-qualified model refs of the form
    ``<profile>/<model>`` (e.g. ``openrouter/anthropic/claude-opus-4-8``)."""

    main: str | None = None
    subagent: str | None = None
    summarizer: str | None = None
    #: Ordered fallback chain tried when the primary model errors.
    fallback: list[str] = field(default_factory=list)
    #: Prompt caching. Cloud caching is automatic — the agent engine adds
    #: Anthropic cache-control breakpoints for Anthropic models, and the other
    #: cloud providers cache by prefix server-side. The lever JARN adds is the
    #: *local* keep-warm (``keep_alive``) so Ollama / LM Studio don't drop their
    #: KV cache between turns. ``"auto"`` (default) applies the keep-warm; ``"off"``
    #: skips it (cloud caching is the engine/provider default and stays on).
    prompt_cache: str = "auto"
    #: Seconds to keep a *local* model + its KV/prefix cache resident between
    #: turns. Wired to Ollama's ``keep_alive`` and LM Studio's request ``ttl``.
    #: Without it those servers unload the model on idle and drop the prefix
    #: cache, so the next turn recomputes from scratch. ``0`` leaves it to the
    #: provider's own default. Ignored when ``prompt_cache`` is ``"off"``.
    keep_alive: int = 1800


@dataclass(slots=True)
class BudgetConfig:
    per_session_usd: float | None = None
    hard_stop: bool = True       # stop the run when exceeded vs warn only
    warn_at_pct: int = 80


_VALID_REPO_MAP_MODES: frozenset[str] = frozenset({"off", "tool", "auto"})


@dataclass(slots=True)
class ContextConfig:
    auto_compact: bool = True
    compact_at_pct: int = 85     # summarize when context window this % full
    #: How the repo map is exposed to the agent.
    #: ``"off"``  — disabled entirely (no tool, no system-prompt injection).
    #: ``"tool"`` — (default) a ``repo_map`` tool is registered; the model
    #:              calls it on demand.
    #: ``"auto"`` — the map is also injected into the system prompt at build
    #:              time (budget-capped) in addition to the tool.
    repo_map: str = "tool"
    #: Token budget for the repo map (both system-prompt injection and tool
    #: responses).  Must be > 0.
    repo_map_tokens: int = 1024
    #: Token budget for MEMORY.md index injection (global + project tiers).
    memory_tokens: int = 4096
    #: Token budget for the wiki index block injected into the system prompt.
    wiki_index_tokens: int = 1024
    #: Token budget for the project context file (JARN.md / AGENTS.md / …).
    project_context_tokens: int = 8192


_VALID_INLINE_IMAGES: frozenset[str] = frozenset({"auto", "off"})


@dataclass(slots=True)
class ExecutionConfig:
    """Where tools run. ``local`` is the default; ``sandbox`` isolates execution
    (requires an available sandbox runtime — see docs)."""

    backend: str = "local"            # local | sandbox | docker
    #: Register the background-process tools (run/check/kill/list_background) so the
    #: agent can run a dev server / watcher / long build without blocking the turn.
    #: Local backend only — under docker/sandbox the tools are not registered (a
    #: host process would escape the container). Default on.
    background: bool = True
    #: Max concurrent background processes (enforced — N+1th start is refused; ``None`` = unlimited).
    background_max_concurrent: int | None = None
    #: Kill a background process that exceeds this lifetime in seconds (``None`` = unlimited).
    background_max_lifetime_secs: float | None = None
    sandbox_provider: str = "langsmith"  # langsmith (remote); docker is its own backend
    # Container image for ``backend: docker``. Must ship python3 + /bin/sh
    # (BaseSandbox derives glob/edit/read via inline python3 scripts). Non-slim
    # so ``procps``/``pkill`` is present for in-container turn cancellation.
    docker_image: str = "python:3.12"
    multimodal: bool = True           # read_file auto-detects images/PDF/audio/video
    # Inline @-mentioned images as native multimodal content blocks in the user
    # message (base64), so weak vision models see the image without having to call
    # read_file. ``auto`` (default) inlines images ≤ 5 MB; ``off`` keeps the old
    # text-only @path behaviour. On a provider that rejects images, the front-end
    # falls back to text-only for the rest of the session (auto behaves like off).
    inline_images: str = "auto"       # auto | off
    # When ``backend: sandbox`` but the sandbox can't start, fall back to running
    # on the host. OFF by default: silently downgrading isolation is a footgun, so
    # we fail closed unless the user explicitly opts in.
    allow_local_fallback: bool = False
    # When True (default), output from ``! <cmd>`` shell-escape commands is
    # captured and fed into the next agent turn's context so the agent sees what
    # the user ran.  Set to False to disable the injection.
    shell_escape_context: bool = True

    # OS-level kernel-enforced sandbox for the local shell backend.
    # ``off``     — no OS sandbox (default; current behaviour preserved exactly).
    # ``auto``    — use the OS sandbox when available, degrade with a one-time
    #               warning when not; never blocks startup.
    # ``require`` — OS sandbox or fail closed: execute() returns an error if the
    #               sandbox backend is unavailable on this host.
    local_sandbox: str = "off"        # off | auto | require
    sandbox_allow_network: bool = True
    sandbox_writable: list[str] = field(default_factory=list)  # extra writable paths

    # -- Docker resource limits (items 2 & 3) ---------------------------------
    # Memory cap passed to ``--memory``; empty string = unset (no cap).
    # Example: "2g", "512m".
    docker_memory: str = ""
    # Process-ID limit passed to ``--pids-limit``; 0 = unset (no cap).
    # Default of 0 (unset) means the daemon default applies. Consider setting
    # 512 for untrusted code — it prevents fork bombs without breaking most
    # legit workloads.
    docker_pids: int = 0
    # CPU cap passed to ``--cpus``; empty string = unset (no cap).
    # Example: "2" for at most two CPU cores.
    docker_cpus: str = ""
    # User/group for ``--user``; empty string = image default (often root).
    # FOOTGUN: when left empty, container processes run as root. On Linux,
    # files written to the bind-mounted project root land as uid 0 (host root),
    # which can produce root-owned files in your working tree. Set this to a
    # uid:gid (e.g. "1000:1000") that matches your host uid to avoid that.
    # Do NOT force a non-root default here: many images need root for apt/pip.
    docker_user: str = ""


@dataclass(slots=True)
class PolicyConfig:
    """Policy settings.

    ``web_tools`` gates the in-process web_search/web_fetch tools; presets such
    as ``offline`` set it ``False`` so those tools (which bypass the OS sandbox)
    are not registered.

    ``profile`` was removed in v0.6.0 — use ``jarn --preset`` / ``/preset``
    instead.
    """

    web_tools: bool = True


@dataclass(slots=True)
class NetworkPolicy:
    """Unified per-host network egress allow/deny policy (host globs).

    Applied uniformly to ``web_fetch``, MCP http/sse endpoints, and — best
    effort — shell ``curl``/``wget``. Semantics: ``deny`` always wins; an empty
    ``allow`` means allow-all (back-compat); a non-empty ``allow`` restricts
    egress to hosts matching one of its globs. Globs are shell-style and matched
    case-insensitively against the resolved target host (e.g. ``*.github.com``
    matches ``api.github.com`` but not the bare ``github.com``).

    Composition with the legacy ``JARN_WEB_FETCH_ALLOW_HOSTS`` env var: that var
    still governs the web_fetch SSRF private-IP bypass, and for web_fetch its
    hosts are additionally treated as permitted egress (union) so existing
    setups keep working. A config ``deny`` overrides the env var — deny always
    wins. When both ``allow`` and ``deny`` are empty the policy is inert and
    default behaviour is unchanged.
    """

    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PermissionRules:
    """Persisted fine-grained allow/deny rules layered under the coarse mode.

    Patterns are shell-glob-style and matched against the normalized command
    string for shell, or the path for filesystem writes.
    """

    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    #: Per-host network egress allow/deny policy (see :class:`NetworkPolicy`).
    #: Enforced across web_fetch, MCP endpoints, and best-effort shell egress.
    network: NetworkPolicy = field(default_factory=NetworkPolicy)


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
    #: Per-server timeout for ``get_tools`` (seconds). Default 30.
    timeout_secs: int = 30


_VALID_VERIFY_GATES: frozenset[str] = frozenset({"off", "suggest", "auto"})

_VALID_DIAGNOSTICS_MODES: frozenset[str] = frozenset({"off", "suggest", "auto"})


@dataclass(slots=True)
class VerifyConfig:
    """Post-edit verification gate + diagnostics feedback loop.

    ``gate``
        ``off``     — no post-edit verify prompts or runs.
        ``suggest`` — (default) emit a NOTICE with the detected test command.
        ``auto``    — run the detected test command via the execution backend
                      when permissions allow (explicit opt-in).

    ``diagnostics``
        ``off``     — no diagnostics feedback.
        ``suggest`` — (default) emit a NOTICE listing ruff/pyright findings on
                      edited files so the user can see them.
        ``auto``    — if edited files have new errors, queue ONE internal
                      follow-up turn asking the agent to fix them.  Bounded by
                      ``diagnostics_max_rounds`` (default 1) to prevent loops.

    ``diagnostics_max_rounds``
        Maximum consecutive auto-fix rounds before the loop stops.  Default 1.

    ``diagnostics_ts``
        Run ``npx tsc --noEmit --pretty false`` as a diagnostics tool.  OFF by
        default: tsc runs project-wide and is slow — opt in explicitly.

    ``max_repair_rounds``
        Number of bounded same-turn repair attempts after an automatic verification
        failure.  The failed command/output is fed back to the agent before each
        attempt.  Default 1; set 0 to fail immediately without a repair attempt.
    """

    gate: str = "suggest"
    max_repair_rounds: int = 1
    diagnostics: str = "suggest"
    diagnostics_max_rounds: int = 1
    diagnostics_ts: bool = False


@dataclass(slots=True)
class PricingConfig:
    """OpenRouter catalog fetch controls."""

    #: When ``False``, :func:`jarn.cost.pricing.warm_catalog` skips network fetch
    #: (bundled anchors + user overrides still apply). Also disabled by env
    #: ``JARN_NO_NETWORK_PRICING=1``.
    network: bool = True


@dataclass(slots=True)
class TracingConfig:
    backend: str = "langsmith"    # langsmith | otel


@dataclass(slots=True)
class ObservabilityConfig:
    langsmith: bool = False       # opt-in LangSmith tracing (when backend is langsmith)
    tracing: TracingConfig = field(default_factory=TracingConfig)
    telemetry: bool = False       # opt-in usage analytics, default OFF
    log_level: str = "info"
    transcript: bool = True       # append-only JSONL session transcript under .jarn/sessions/


_VALID_SPLASH_VALUES: frozenset[str] = frozenset({"full", "compact", "off"})

_VALID_NOTIFY_VALUES: frozenset[str] = frozenset({"off", "bell", "desktop", "both"})


@dataclass(slots=True)
class UIConfig:
    theme: str = "dark"           # dark | light | high-contrast | auto
    accent: str = "cyan"          # brand accent
    splash: str = "compact"       # full | compact | off
    #: Max diff lines shown inline in a write/edit approval prompt before the
    #: rest collapses to a "… (+N more lines)" footer. Over-cap diffs offer a
    #: [v] view-full-diff option that opens the complete diff in the pager.
    approval_diff_lines: int = 40
    #: Notification mode when a long turn finishes or an approval is needed.
    #: ``off``     — silent (no bell, no desktop notification).
    #: ``bell``    — (default) emit a terminal BEL character (\a).
    #: ``desktop`` — fire a native OS notification (macOS osascript / Linux notify-send).
    #: ``both``    — BEL + desktop notification.
    notify: str = "bell"
    #: Minimum elapsed seconds before a turn-end notification fires.
    #: Approval notifications always fire regardless of elapsed time.
    notify_min_secs: int = 10
    #: Update the terminal-tab title via OSC 2 to show idle / working / approval
    #: states. Disabled when False or when stdout is not a tty.
    terminal_title: bool = True
    #: Mid-turn steering (T-4-6): while a turn is running, promote a queued line into
    #: the live turn with the ``[s]`` fastkey or ``/queue steer <n>`` — the agent
    #: sees it as a new user message before its next tool call (injected only at a
    #: settled tool boundary). When False, the ``[s] steer now`` affordance is hidden
    #: and ``/queue steer`` errors politely.
    steering: bool = True


@dataclass(slots=True)
class GitConfig:
    """Git working-tree safety features.

    ``autocheckpoint`` causes J.A.R.N. to snapshot the working tree into a
    private git ref (``refs/jarn/checkpoints/``) at the start of every agent
    turn.  This enables ``/undo`` and ``/redo``: the user can revert or
    re-apply the last agent turn's changes without affecting HEAD, the branch,
    or the staged index.

    ``checkpoint_mode`` is reserved for future expansion:
      ``"shadow"`` (default) — snapshots live in private refs only.
      ``"commit"`` — reserved; behaves like ``"shadow"`` today.
    """

    autocheckpoint: bool = False
    checkpoint_mode: str = "shadow"   # "shadow" | "commit"


_VALID_EXIT_MODES: frozenset[str] = frozenset({"ask", "auto-edit"})


@dataclass(slots=True)
class PlanConfig:
    """Plan-mode handoff.

    When the agent calls ``exit_plan_mode`` from read-only ``plan`` mode and the
    user approves, the session escalates to ``exit_mode`` so the plan can be
    carried out in the same turn. The approval picker still offers the other
    editing mode; this is just the highlighted default.
    """

    exit_mode: str = "auto-edit"   # ask | auto-edit


@dataclass(slots=True)
class WikiConfig:
    """Per-project (and global) markdown knowledge base.

    When ``enabled`` is ``True`` four wiki tools are registered on the agent
    (``wiki_search``, ``wiki_read``, ``wiki_write``, ``wiki_append``) and the
    wiki index is injected into the system prompt at build time.
    Disabled by default so the feature is opt-in.
    """

    enabled: bool = False


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
class UpdatesConfig:
    """Background update-available check.

    ``check`` (default ``True``) — at interactive launch, a daemon thread
    checks ``https://pypi.org/pypi/jarn/json`` (2 s timeout) and prints one
    dim line under the splash when a newer release exists.  The result is
    cached in ``~/.jarn/update-check.json`` for 24 h so the check never runs
    twice in a day.  Set to ``False`` to disable entirely; the check is also
    skipped automatically when the ``offline`` preset is active or when
    running headless (``jarn -p``).
    """

    check: bool = True


@dataclass(slots=True)
class Config:
    """The fully-merged configuration handed to the rest of the application."""

    default_profile: str = "openrouter"
    default_model: str | None = None
    permission_mode: PermissionMode = PermissionMode.ASK
    #: When True, inline plaintext ``api_key`` literals in config.yaml are
    #: rejected at load; when False (default) they emit a warning. Back-compat
    #: default is False so existing setups keep working, just noisier.
    strict_secrets: bool = False
    #: When True, lifecycle-hook subprocesses inherit the *full* ``os.environ``
    #: (pre-T-1-8 behavior, leaks secrets to hook scripts). Default False → hooks
    #: get only a minimal allowlist (``PATH``/``HOME``/``JARN_*`` + declared
    #: ``extra_env``), so a compromised hook can't exfiltrate ``*_API_KEY``.
    hook_inherit_env: bool = False
    #: When True, lifecycle hooks do not run until the user has recorded a
    #: one-time accept for global hooks (``jarn trust-hooks``). Default False for
    #: back-compat. Stripped from untrusted project configs (not in the
    #: :data:`jarn.config.trust.SAFE_PROJECT_KEYS` allowlist), so only the global
    #: tier or a trusted project can enable it.
    hook_global_require_trust: bool = False

    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    permissions: PermissionRules = field(default_factory=PermissionRules)
    hooks: list[HookSpec] = field(default_factory=list)
    mcp_servers: list[MCPServer] = field(default_factory=list)
    async_subagents: list[AsyncSubagentSpec] = field(default_factory=list)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    compat: CompatConfig = field(default_factory=CompatConfig)
    git: GitConfig = field(default_factory=GitConfig)
    wiki: WikiConfig = field(default_factory=WikiConfig)
    plan: PlanConfig = field(default_factory=PlanConfig)
    verify: VerifyConfig = field(default_factory=VerifyConfig)
    pricing: PricingConfig = field(default_factory=PricingConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    updates: UpdatesConfig = field(default_factory=UpdatesConfig)

    def resolved_main_model(self) -> str | None:
        """The model used for the top-level agent loop."""
        return self.routing.main or self.default_model

    def resolved_subagent_model(self) -> str | None:
        return self.routing.subagent or self.resolved_main_model()

    def resolved_summarizer_model(self) -> str | None:
        return self.routing.summarizer or self.resolved_subagent_model()
