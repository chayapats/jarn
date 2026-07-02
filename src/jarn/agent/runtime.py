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
from jarn.agent.permissions_bridge import interrupt_map
from jarn.agent.verify import ProjectCapabilities, detect_capabilities
from jarn.config import paths
from jarn.config.schema import Config
from jarn.extensibility.commands import CustomCommand, load_commands
from jarn.extensibility.skills import Skill, auto_skill_catalog, load_skills
from jarn.extensibility.subagents import CustomSubagent, load_subagents
from jarn.memory.context import assemble_system_context
from jarn.providers import ModelFactory

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


def build_runtime(
    config: Config,
    *,
    project_root: Path | None = None,
    project_trusted: bool = True,
    checkpointer: Any | None = None,
    extra_tools: list[Any] | None = None,
    system_prompt_override: str | None = None,
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

    web_tools = build_web_tools() if config.policy.web_tools else []
    tools = [*web_tools, *(extra_tools or [])]

    tools, system_prompt = _wire_builtin_tools(
        tools,
        system_prompt,
        config,
        root,
        project_trusted=project_trusted,
    )

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

    # Gate every networked / MCP tool through the permission engine too, so they
    # cannot bypass policy (they map to ActionKind.NETWORK → ASK by default).
    # Async-subagent tools are middleware-injected (fixed names) only when async
    # subagents are configured; gate them too so their remote HTTP calls route
    # through the engine instead of bypassing it.
    extra_gated = [name for t in tools if (name := getattr(t, "name", ""))]
    interrupts = interrupt_map(
        extra_gated, include_async=bool(config.async_subagents)
    )

    backend = _make_backend(config, root)
    # Prompt caching for Anthropic is handled by deepagents itself — it adds an
    # AnthropicPromptCachingMiddleware unconditionally (a no-op for non-Anthropic
    # models). Passing our own would be a *duplicate* and create_agent rejects
    # that. JARN's caching contribution is the local keep-warm wired in the model
    # factory (see ModelFactory._inject_keep_warm); cloud providers cache
    # server-side. So no extra middleware is passed here.
    agent = create_deep_agent(
        model=model,
        backend=backend,
        system_prompt=system_prompt,
        subagents=subagent_specs or None,
        interrupt_on=interrupts or None,
        checkpointer=checkpointer,
        tools=tools or None,
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
