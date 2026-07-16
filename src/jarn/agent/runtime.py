"""Assemble a deep agent from J.A.R.N. configuration.

This is the seam between J.A.R.N.'s subsystems (config, providers, permissions,
memory, extensibility) and the deepagents library. It produces a
:class:`JarnRuntime` holding the compiled agent graph plus everything the TUI
needs to drive a session.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jarn.agent import prompts
from jarn.agent.backends_factory import _make_backend
from jarn.agent.builtin_tools import _wire_builtin_tools
from jarn.agent.permissions_bridge import (
    ASYNC_SUBAGENT_TOOLS,
    BACKGROUND_CONTROL_TOOLS,
    BACKGROUND_START_TOOL,
    INTERNAL_TOOLS,
    MUTATING_TOOLS,
    READONLY_TOOLS,
    WIKI_MUTATING_TOOLS,
    WIKI_READONLY_TOOLS,
    interrupt_map,
)
from jarn.agent.verify import ProjectCapabilities, detect_capabilities
from jarn.config import paths
from jarn.config.schema import Config
from jarn.extensibility.commands import CustomCommand, load_commands
from jarn.extensibility.skills import Skill, auto_skill_catalog, load_skills
from jarn.extensibility.subagents import CustomSubagent, load_subagents
from jarn.memory.context import assemble_system_context
from jarn.providers import ModelFactory, ModelResolutionError

logger = logging.getLogger("jarn.agent")

#: Ambient env vars the langgraph_sdk auto-loads as the ``x-api-key`` header on
#: *every* request when no explicit api_key is passed. deepagents builds the
#: async-subagent client without forwarding any api_key (it only forwards
#: ``headers``), so a non-local async-subagent url silently receives whichever
#: of these is set in the operator's environment. We cannot suppress the
#: auto-load from here (no api_key seam in the deepagents call), so the best we
#: can do at build time is refuse to start when an ambient key would be sent to
#: a third-party url. Order mirrors langgraph_sdk's own precedence.
_AMBIENT_LANGGRAPH_KEY_VARS = (
    "LANGGRAPH_API_KEY",
    "LANGSMITH_API_KEY",
    "LANGCHAIN_API_KEY",
)

#: Hosts treated as the operator's own machine — sending an ambient key there is
#: not an exfiltration concern, so no warning is emitted.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})

#: Names jarn reserves for its own builtin tools. An EXTRA tool (arriving via
#: web/MCP ``extra_tools``) that is NOT namespaced (``mcp__…``) yet carries one of
#: these names is a collision attack: name-keyed permission classification
#: (``permissions_bridge.tool_to_action``) would treat it as a read-only builtin and
#: auto-ALLOW it in every mode — including plan. Such tools are dropped at assembly.
#: ``web_search``/``web_fetch`` are deliberately absent (they are jarn's own web
#: tools and map to NETWORK, so they are kept and gated).
_RESERVED_BUILTIN_NAMES = frozenset({
    *MUTATING_TOOLS,
    *READONLY_TOOLS,
    *INTERNAL_TOOLS,
    *WIKI_MUTATING_TOOLS,
    *WIKI_READONLY_TOOLS,
    BACKGROUND_START_TOOL,
    *BACKGROUND_CONTROL_TOOLS,
    *ASYNC_SUBAGENT_TOOLS,
    "repo_map",
    "exit_plan_mode",
    "suggest_memory",
})


def _url_is_local(url: str) -> bool:
    """True if ``url`` points at the local machine (so an ambient key is safe).

    A url with no scheme/host (e.g. a bare path) or an unparseable host is
    treated as non-local — we'd rather warn spuriously than stay silent on a
    url we can't reason about.
    """
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    return (host or "") in _LOCAL_HOSTS


class AmbientKeyLeakError(RuntimeError):
    """Raised when an ambient LangGraph/LangSmith/LangChain key would leak to a
    non-local async-subagent URL at runtime."""

    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        super().__init__(messages[0] if messages else "ambient key leak risk")


def _ambient_key_leak_messages(config: Config) -> list[str]:
    """Detect non-local async-subagent urls that would receive an ambient key.

    Threat: a trusted-but-misconfigured (or compromised) project may point an
    ``async_subagents[].url`` at a third party. The langgraph_sdk then attaches
    the operator's ambient ``*_API_KEY`` as ``x-api-key`` to requests bound for
    that url, leaking it. deepagents exposes no api_key control on the spec (and
    the SDK reserves the ``x-api-key`` header, so it can't be overridden via
    ``headers``), so we fail closed at build time rather than start a session
    that would exfiltrate the operator's key.
    """
    present = [var for var in _AMBIENT_LANGGRAPH_KEY_VARS if os.environ.get(var)]
    if not present:
        return []
    messages: list[str] = []
    for a in config.async_subagents:
        if a.url and not _url_is_local(a.url):
            msg = (
                f"async subagent {a.name!r} targets non-local url {a.url} while "
                f"ambient {'/'.join(present)} is set: the langgraph_sdk would send "
                f"that key as the x-api-key header to this url. Unset the ambient "
                f"key or scope auth explicitly via the subagent's 'headers'."
            )
            logger.error("%s", msg)
            messages.append(msg)
    return messages


@dataclass(slots=True)
class JarnRuntime:
    """Everything a session needs, produced by :func:`build_runtime`."""

    agent: Any                       # compiled LangGraph deep agent
    config: Config
    factory: ModelFactory
    project_root: Path | None
    system_prompt: str
    capabilities: ProjectCapabilities
    skills: dict[str, Skill] = field(default_factory=dict)
    commands: dict[str, CustomCommand] = field(default_factory=dict)
    subagents: dict[str, CustomSubagent] = field(default_factory=dict)
    main_model_ref: str | None = None
    #: Model refs the session driver may attribute streamed usage to (main +
    #: per-model subagents + summarizer). It canonicalizes the model each provider
    #: reports (response_metadata) against this set; see :class:`SessionDriver`.
    known_model_refs: tuple[str, ...] = ()
    backend: Any = None              # execution backend (for cancel/terminate)


def _async_subagent_specs(config: Config) -> list[Any]:
    """Build DeepAgents ``AsyncSubAgent`` dicts from config (Agent Protocol)."""
    specs: list[Any] = []
    for a in config.async_subagents:
        spec: dict[str, Any] = {
            "name": a.name,
            "description": a.description,
            "graph_id": a.graph_id,
        }
        if a.url:
            spec["url"] = a.url
        if a.headers:
            spec["headers"] = a.headers
        specs.append(spec)
    return specs


# ---------------------------------------------------------------------------
# Auto-compaction: a single in-graph summarization pass.
#
# deepagents adds its own ``SummarizationMiddleware`` to every agent it builds,
# unconditionally, on the **main** model at a fixed ``("fraction", 0.85)``
# trigger. JARN instead wants ONE summarization pass on the configured
# *summarizer* model, triggered at ``context.compact_at_pct`` of the **main**
# model's context window. So build_runtime registers a ``HarnessProfile`` for the
# main model's key that (a) excludes the built-in ``"SummarizationMiddleware"``
# and (b) supplies jarn's own instance via the profile's ``extra_middleware``.
#
# ``extra_middleware`` (not ``create_deep_agent(middleware=)``) is the seam
# because it is applied to *every* stack the profile touches — the main agent,
# the auto-added ``general-purpose`` subagent, and same-model declarative
# subagents — whereas ``middleware=`` reaches only the main agent. Since the
# exclusion is model-keyed, it also strips the built-in from the GP subagent
# (which shares the main model's profile); routing our replacement through
# ``extra_middleware`` re-covers that stack instead of leaving it with no
# summarization (and no ContextOverflowError recovery). The exclusion matches by
# ``AgentMiddleware.name``: the built-in reports the public alias
# ``"SummarizationMiddleware"`` while our subclass reports its own class name, so
# the filter drops only the built-in and keeps ours (and ``create_agent``, which
# rejects two middleware sharing a ``.name``, then sees exactly one per stack).
#
# The profile registration is process-global and sticky (register-once, additive
# merge), but jarn's instance depends on per-build config (summarizer model,
# resolved trigger, auto_compact on/off). To keep the sticky registration honest
# without re-registering, ``extra_middleware`` is a *stable* zero-arg factory that
# reads a per-key builder slot in ``_SUMMARIZATION_BUILDERS`` — which each
# build_runtime updates (or clears when auto_compact is off). The factory also
# returns a FRESH instance per stack so no middleware instance is shared across
# graphs. deepagents imports stay lazy so importing this module doesn't drag the
# whole library into CLI startup.

#: Resolved model keys we've already registered the exclusion for. deepagents'
#: registration is process-global and additive; this guard keeps it idempotent
#: (register once per key per process).
_SUMMARIZATION_EXCLUDED_KEYS: set[str] = set()

#: Per-key builder for the summarization middleware, updated each build. The
#: profile's ``extra_middleware`` factory reads this so the sticky registration
#: always reflects the latest config; absent/``None`` means "no auto-summarization
#: for this key" (auto_compact off), so the factory yields an empty stack.
_SUMMARIZATION_BUILDERS: dict[str, Any] = {}

#: Cached ``SummarizationMiddleware`` subclass (built on first use). Caching keeps
#: a single stable type across builds; subclassing gives it a distinct ``.name``.
_JARN_SUMMARIZATION_CLS: Any = None


def _jarn_summarization_cls() -> Any:
    """The JARN ``SummarizationMiddleware`` subclass, built + cached on first use.

    Subclassing deepagents' middleware flips ``.name`` from the built-in's public
    ``"SummarizationMiddleware"`` alias to ``"JarnSummarizationMiddleware"``, so a
    profile that excludes the built-in by that alias removes only deepagents'
    instance while ours survives the filter and ``create_agent``'s name-uniqueness
    check."""
    global _JARN_SUMMARIZATION_CLS
    if _JARN_SUMMARIZATION_CLS is None:
        from deepagents.middleware.summarization import SummarizationMiddleware

        class JarnSummarizationMiddleware(SummarizationMiddleware):  # type: ignore[valid-type,misc]
            """deepagents ``SummarizationMiddleware`` under a distinct ``.name``."""

        _JARN_SUMMARIZATION_CLS = JarnSummarizationMiddleware
    return _JARN_SUMMARIZATION_CLS


def _summarization_profile_key(model: Any) -> str | None:
    """The harness-profile key deepagents will resolve for a pre-built ``model``.

    Mirrors deepagents' own lookup: it tries ``provider:identifier`` first, then
    the bare ``provider``. We register under the exact ``provider:identifier`` key
    when both are known (narrowest — spares subagents on the same provider but a
    different model) and fall back to the provider key. Returns ``None`` when
    neither can be derived, in which case the caller keeps the built-in rather
    than risk stripping or doubling summarization."""
    from deepagents._models import get_model_identifier, get_model_provider

    provider = get_model_provider(model)
    identifier = get_model_identifier(model)
    if provider and identifier and ":" not in identifier:
        return f"{provider}:{identifier}"
    if provider:
        return provider
    return None


def _summarization_extra_middleware(key: str) -> Any:
    """A *stable* zero-arg ``extra_middleware`` factory for ``key``.

    Registered once (the closure never changes) but consulted on every stack
    assembly, it reads the per-key builder in ``_SUMMARIZATION_BUILDERS`` so the
    sticky profile registration always reflects the latest ``build_runtime`` config
    — and returns a FRESH middleware instance per stack (main agent, general-purpose
    subagent, same-model declarative subagents) so nothing is shared across graphs.
    Yields an empty stack when no builder is set (auto_compact off)."""

    def _factory() -> list[Any]:
        builder = _SUMMARIZATION_BUILDERS.get(key)
        return list(builder()) if builder is not None else []

    return _factory


def _ensure_summarization_excluded(model: Any) -> str | None:
    """Register (once per process) a ``HarnessProfile`` for ``model``'s key that
    excludes deepagents' built-in ``SummarizationMiddleware`` and supplies jarn's
    replacement via a stable ``extra_middleware`` factory. Returns the key, or
    ``None`` when it can't be derived (then the caller leaves the built-in in
    place)."""
    key = _summarization_profile_key(model)
    if key is None:
        return None
    if key not in _SUMMARIZATION_EXCLUDED_KEYS:
        from deepagents import HarnessProfile, register_harness_profile

        register_harness_profile(
            key,
            HarnessProfile(
                excluded_middleware=frozenset({"SummarizationMiddleware"}),
                extra_middleware=_summarization_extra_middleware(key),
            ),
        )
        _SUMMARIZATION_EXCLUDED_KEYS.add(key)
    return key


def _summarization_trigger(model: Any, *, main_window: int, pct: int) -> Any:
    """The auto-summarization trigger.

    When jarn knows the **main** model's context window, return an explicit
    ``("tokens", N)`` trigger at ``pct``% of THAT window — the same window the ctx%
    gauge resolves (:func:`jarn.cost.pricing.context_window`). We deliberately do
    NOT use deepagents' ``("fraction", …)`` trigger: it resolves the fraction
    against the *summarizer* model's window (overflow risk belongs to the main
    model), and — worse — a fraction trigger silently degrades to
    ``("tokens", 170000)`` for models without a langchain profile (jarn's
    OpenRouter defaults), so ``compact_at_pct`` would be inert. When the main
    window is unknown, fall back to deepagents' computed default for ``model``."""
    if main_window > 0:
        return ("tokens", max(1, int(main_window * pct / 100)))
    from deepagents.middleware.summarization import compute_summarization_defaults

    return compute_summarization_defaults(model)["trigger"]


def _build_summarization_middleware(
    model: Any, backend: Any, *, main_window: int, pct: int
) -> Any:
    """Mirror deepagents' ``create_summarization_middleware`` (same keep window,
    tool-arg truncation, and backend offload) but on the summarizer ``model`` with
    the trigger resolved by :func:`_summarization_trigger` against the **main**
    model's window (``main_window``; 0 = unknown → deepagents default)."""
    from deepagents.middleware.summarization import compute_summarization_defaults

    defaults = compute_summarization_defaults(model)
    return _jarn_summarization_cls()(
        model=model,
        backend=backend,
        trigger=_summarization_trigger(model, main_window=main_window, pct=pct),
        keep=defaults["keep"],
        trim_tokens_to_summarize=None,
        truncate_args_settings=defaults["truncate_args_settings"],
    )


def _install_summarization_builder(
    key: str, summarizer_model: Any, backend: Any, *, main_window: int, pct: int
) -> None:
    """Point ``key``'s ``extra_middleware`` factory at the current build's config by
    storing a per-key builder that mints a fresh summarization middleware on demand.
    Snapshots the build's model/backend/window so a later build's changes don't leak
    into an already-registered (sticky) profile."""

    def _builder() -> list[Any]:
        return [
            _build_summarization_middleware(
                summarizer_model, backend, main_window=main_window, pct=pct
            )
        ]

    _SUMMARIZATION_BUILDERS[key] = _builder


def resolved_auto_summarize_tokens(config: Config) -> int | None:
    """Token count at which auto-summarization fires for ``config``'s main model, or
    ``None`` when jarn can't resolve the main window.

    ``None`` means deepagents' ``("tokens", 170000)`` default applies and
    ``context.compact_at_pct`` has no effect. Mirrors the build-time trigger so
    ``/compact status`` and CONFIGURATION.md report the *resolved* value rather than
    the raw percentage."""
    from jarn.cost.pricing import context_window

    window = context_window(config.resolved_main_model() or "")
    if window <= 0:
        return None
    return max(1, int(window * config.context.compact_at_pct / 100))


def build_runtime(
    config: Config,
    *,
    project_root: Path | None = None,
    project_trusted: bool = True,
    checkpointer: Any | None = None,
    extra_tools: list[Any] | None = None,
    system_prompt_override: str | None = None,
    response_format: Any | None = None,
    extra_roots: list[Path] | None = None,
) -> JarnRuntime:
    """Build a ready-to-run :class:`JarnRuntime` from config.

    ``checkpointer`` (a LangGraph saver) enables resumable sessions; pass one
    obtained from :func:`jarn.memory.open_checkpointer`. ``extra_tools`` is for
    MCP-loaded tools (see :func:`jarn.extensibility.mcp.load_mcp_tools`).

    ``system_prompt_override`` replaces J.A.R.N.'s assembled system prompt
    wholesale (the "reliable nerd" persona + project context) with the given
    string — used by the eval harness to A/B the harness prompt against a bare
    tool-using agent while holding tools/model/loop constant. ``None`` (default)
    builds the normal prompt; ``""`` yields an empty prompt (DeepAgents' own
    default agent instructions still apply).

    Summarization-profile registration (:data:`_SUMMARIZATION_BUILDERS`,
    :data:`_SUMMARIZATION_EXCLUDED_KEYS`) is process-global: one process supports
    one active summarization config per model key at a time. Two runtimes built
    concurrently in the same process for the same key share the last-registered
    builder, so their auto-compaction settings are not independent.
    """
    from deepagents import create_deep_agent

    root = project_root or paths.find_project_root()

    factory = ModelFactory(config)
    model = factory.build_main()

    # Context: project JARN.md + memory + skill catalog + detected verify cmds.
    # Forward compat settings so that read_claude_dir and context_files are
    # honoured here — without these, compat config would be silently ignored.
    skills = load_skills(
        root,
        project_trusted=project_trusted,
        read_claude_dir=config.compat.read_claude_dir,
    )
    commands = load_commands(
        root,
        project_trusted=project_trusted,
        read_claude_dir=config.compat.read_claude_dir,
    )
    subagents = load_subagents(root, project_trusted=project_trusted)
    capabilities = detect_capabilities(root or Path.cwd())

    if system_prompt_override is not None:
        # A/B baseline: skip the JARN persona + project/skill/capability context
        # entirely. Config-gated injections below (wiki, repo map) still apply so
        # the *only* controlled difference is the base prompt — keep them off in
        # the config to isolate the prompt cleanly.
        system_prompt = system_prompt_override
    else:
        system_prompt = prompts.build_system_prompt(
            prompts.date_context(),
            assemble_system_context(
                root,
                project_trusted=project_trusted,
                context_files=config.compat.context_files,
                memory_tokens=config.context.memory_tokens,
                project_context_tokens=config.context.project_context_tokens,
            ),
            auto_skill_catalog(skills),
            capabilities.as_prompt_block(),
        )

    # Built-in web tools + any MCP-loaded tools. Web tools run in-process and
    # bypass the OS sandbox, so the policy layer can disable them (e.g. the
    # 'offline' profile sets policy.web_tools False).
    from jarn.agent.web_tools import build_web_tools

    web_tools = build_web_tools(config) if config.policy.web_tools else []
    tools = [*web_tools, *(extra_tools or [])]
    # Identities of the EXTRA tools (web + MCP) as they arrive, before builtins are
    # wired in. The collision guard below uses these to tell an impersonating extra
    # tool apart from a genuine jarn builtin (which _wire_builtin_tools constructs
    # fresh, so its id is never in this set and it is never dropped).
    _extra_ids = {id(t) for t in tools}

    tools, system_prompt, ungated_tools = _wire_builtin_tools(
        tools,
        system_prompt,
        config,
        root,
        project_trusted=project_trusted,
    )

    # Defense in depth. The MCP loader already namespaces its tools to
    # mcp__<server>__<tool> (extensibility/mcp._namespace_tool), but an extra tool
    # that somehow reaches here un-namespaced must not be allowed to impersonate a
    # builtin: an un-prefixed name matching a reserved builtin would make
    # tool_to_action classify it as a READ, which the engine auto-ALLOWs in EVERY
    # mode (including plan) — a networked/mutating tool could then run without
    # approval. Drop such tools (warn, never raise: one bad tool must not abort
    # startup). Genuine builtins (id not in _extra_ids) and correctly-namespaced MCP
    # tools are left untouched; identity-based ungating (below) is unaffected.
    kept: list[Any] = []
    for t in tools:
        name = getattr(t, "name", "")
        if (
            id(t) in _extra_ids
            and name
            and not name.startswith("mcp__")
            and name in _RESERVED_BUILTIN_NAMES
        ):
            logger.warning(
                "Dropping extra tool %r: an un-namespaced tool may not use a "
                "reserved jarn builtin name (it would misclassify as an "
                "auto-allowed builtin and bypass the permission engine).",
                name,
            )
            continue
        kept.append(t)
    tools = kept

    # Subagents may restrict themselves to a subset of the extra (web/MCP) tools;
    # pass the available set so to_spec can resolve names and reject typos.
    subagent_specs: list[Any] = [
        s.to_spec(factory, available_tools=tools) for s in subagents.values()
    ]
    subagent_specs += _async_subagent_specs(config)
    leak_msgs = _ambient_key_leak_messages(config)
    if leak_msgs:
        raise AmbientKeyLeakError(leak_msgs)

    # Models we may need to attribute streamed usage to: the main model, any
    # subagent that runs on its own model, and the summarizer. The session driver
    # canonicalizes the model each provider reports (response_metadata) against
    # this set, so a delegated subagent on a different model is billed correctly.
    main_ref = config.resolved_main_model()
    known_refs: set[str] = {main_ref} if main_ref else set()
    known_refs.update(s.model for s in subagents.values() if s.model)
    summarizer_ref = config.resolved_summarizer_model()
    if summarizer_ref:
        known_refs.add(summarizer_ref)

    # Gate every networked / MCP / mutating extra tool through the permission
    # engine too, so they cannot bypass policy (they map to ActionKind.NETWORK →
    # ASK by default). Async-subagent tools are middleware-injected (fixed names)
    # only when async subagents are configured; gate them too so their remote HTTP
    # calls route through the engine instead of bypassing it. Read-only/local
    # extras (wiki_search/wiki_read, repo_map, background controls) are excluded —
    # the engine always auto-ALLOWs them, so an interrupt would only cost a graph
    # pause/checkpoint/resume round-trip per call for no policy gain.
    #
    # The exclusion is by OBJECT IDENTITY of the instances WE constructed in
    # builtin_tools.py (returned as ungated_tools), never by name or metadata:
    # langchain-mcp-adapters copies server-controlled ToolAnnotations straight
    # into BaseTool.metadata, so a malicious MCP server can forge BOTH a colliding
    # name (wiki_read/repo_map/check_background) AND a metadata jarn_ungated flag.
    # Only our own objects are in ungated_ids, so any forged MCP/web tool — however
    # named or tagged — stays gated behind its required interrupt.
    ungated_ids = {id(t) for t in ungated_tools}
    extra_gated = [
        name
        for t in tools
        if (name := getattr(t, "name", "")) and id(t) not in ungated_ids
    ]
    interrupts = interrupt_map(
        extra_gated, include_async=bool(config.async_subagents)
    )

    backend = _make_backend(config, root, extra_roots=extra_roots)

    # Unify auto-compaction into a single in-graph summarization pass. Always
    # exclude deepagents' built-in SummarizationMiddleware (main model, fixed 85%
    # trigger) via the profile; when auto-compaction is enabled, supply one on the
    # configured summarizer model — triggered at context.compact_at_pct of the MAIN
    # model's window — through the profile's extra_middleware so it also covers the
    # general-purpose subagent (which shares the main model's profile). The builder
    # is per-key + per-build; auto_compact off clears it (built-in excluded, nothing
    # re-added). The controller's old thread-forking auto-compact trigger is gone
    # (see repl/turn.py), so this is the only automatic path now. When the model's
    # key can't be derived we leave the built-in untouched rather than risk zero- or
    # double-summarization.
    #
    # Prompt caching stays deepagents' job — it adds AnthropicPromptCachingMiddleware
    # unconditionally (a no-op off Anthropic); passing our own would duplicate it.
    # JARN's caching contribution is the local keep-warm in the model factory
    # (ModelFactory._inject_keep_warm); cloud providers cache server-side.
    excluded_key = _ensure_summarization_excluded(model)
    if excluded_key is not None:
        if config.context.auto_compact:
            try:
                summarizer_model = factory.build_summarizer() or model
            except ModelResolutionError:
                # A misconfigured summarizer must not break startup: auto-summarize
                # on the main model (matching the built-in's old behavior). Manual
                # /compact still surfaces the config error at compact time.
                logger.warning(
                    "summarizer model unbuildable; auto-summarization will use the main model"
                )
                summarizer_model = model
            from jarn.cost.pricing import context_window

            _install_summarization_builder(
                excluded_key,
                summarizer_model,
                backend,
                main_window=context_window(main_ref or ""),
                pct=config.context.compact_at_pct,
            )
        else:
            # auto_compact off: drop any prior builder so the extra_middleware
            # factory yields nothing (built-in stays excluded → zero
            # auto-summarization on every stack for this key).
            _SUMMARIZATION_BUILDERS.pop(excluded_key, None)

    agent = create_deep_agent(
        model=model,
        backend=backend,
        system_prompt=system_prompt,
        subagents=subagent_specs or None,
        interrupt_on=interrupts or None,
        checkpointer=checkpointer,
        tools=tools or None,
        response_format=response_format,
    )

    return JarnRuntime(
        agent=agent,
        config=config,
        factory=factory,
        project_root=root,
        system_prompt=system_prompt,
        capabilities=capabilities,
        skills=skills,
        commands=commands,
        subagents=subagents,
        main_model_ref=main_ref,
        known_model_refs=tuple(sorted(known_refs)),
        backend=backend,
    )
