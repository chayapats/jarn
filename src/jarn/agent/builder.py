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
    #: Reserved for non-fatal build-time warnings surfaced to the TUI.
    warnings: tuple[str, ...] = ()


class SandboxUnavailable(RuntimeError):
    """Raised when a sandbox backend is requested but cannot be constructed."""


def _make_local_backend(project_root: Path | None, config: Config | None = None):
    """Local-first backend: real filesystem + shell, scoped to the project root.

    ``virtual_mode=True`` adds path guardrails (blocks ``..``/absolute escapes)
    for filesystem ops. Shell execution is still on the host — that is gated by
    the permission engine and danger-guard at the TUI layer.

    When ``config.execution.local_sandbox`` is ``"auto"`` or ``"require"``, each
    shell command is additionally wrapped by :mod:`jarn.agent.os_sandbox` so the
    kernel enforces write isolation and optional network denial.  The default is
    ``"off"`` which preserves the original behaviour exactly.
    """
    from jarn.agent.local_backend import CancellableLocalShellBackend

    root = str(project_root) if project_root else str(Path.cwd())
    root_path = Path(root)

    sandbox_mode = "off"
    sandbox_allow_network = True
    sandbox_extra_writable: list[Path] = []

    if config is not None:
        ex = config.execution
        sandbox_mode = ex.local_sandbox
        sandbox_allow_network = ex.sandbox_allow_network
        sandbox_extra_writable = [Path(p).expanduser() for p in ex.sandbox_writable]

    return CancellableLocalShellBackend(
        root_dir=root,
        virtual_mode=True,
        sandbox_mode=sandbox_mode,
        project_root=root_path,
        sandbox_allow_network=sandbox_allow_network,
        sandbox_extra_writable=sandbox_extra_writable,
    )


def _make_sandbox_backend(config: Config):
    """Construct an isolated sandbox backend.

    Sandbox execution requires an external runtime (e.g. a LangSmith Sandbox)
    and credentials; if unavailable we raise :class:`SandboxUnavailable` so the
    caller can fall back to local with a clear message.
    """
    provider = config.execution.sandbox_provider
    if provider == "langsmith":
        try:
            from langgraph_sandbox import Sandbox  # type: ignore
        except ImportError as exc:
            raise SandboxUnavailable(
                "LangSmith sandbox runtime not installed. Install the sandbox "
                "extra and set credentials, or use execution.backend: local."
            ) from exc
        try:
            from jarn.agent.sandbox_backend import CancellableLangSmithSandbox

            return CancellableLangSmithSandbox(Sandbox())
        except Exception as exc:  # noqa: BLE001
            raise SandboxUnavailable(f"Could not start sandbox: {exc}") from exc
    raise SandboxUnavailable(f"Unknown sandbox provider: {provider!r}")


def _make_docker_backend(config: Config, project_root: Path | None):
    """Construct a Docker container backend (real OS-level isolation).

    Requires the ``docker`` CLI on PATH and a reachable daemon; otherwise raises
    :class:`SandboxUnavailable` so the caller can fail closed (or fall back to
    the host only when ``allow_local_fallback`` is set). The project root is
    bind-mounted read-write at its own absolute path; everything else the agent
    sees is the container image's filesystem.
    """
    from jarn.agent.docker_backend import (
        CancellableDockerSandbox,
        DockerStartError,
        docker_available,
    )

    if not docker_available():
        raise SandboxUnavailable(
            "Docker is not available (the `docker` CLI is missing or the daemon "
            "is not running). Start Docker, or use execution.backend: local."
        )
    root = Path(project_root) if project_root else Path.cwd()
    ex = config.execution
    try:
        return CancellableDockerSandbox(
            project_root=root,
            image=ex.docker_image,
            allow_network=ex.sandbox_allow_network,
            extra_writable=[Path(p).expanduser() for p in ex.sandbox_writable],
            memory=ex.docker_memory,
            pids=ex.docker_pids,
            cpus=ex.docker_cpus,
            user=ex.docker_user,
        )
    except DockerStartError as exc:
        raise SandboxUnavailable(
            f"Could not start docker sandbox with image {ex.docker_image!r}: {exc}\n"
            f"The image may not be present locally — try: docker pull {ex.docker_image}"
        ) from exc


def _make_backend(config: Config, project_root: Path | None):
    if config.execution.backend == "docker":
        return _make_docker_backend(config, project_root)  # may raise SandboxUnavailable
    if config.execution.backend == "sandbox":
        # The "sandbox" backend historically meant the remote LangSmith runtime;
        # `sandbox_provider: docker` redirects it to the local container backend.
        if config.execution.sandbox_provider == "docker":
            return _make_docker_backend(config, project_root)
        return _make_sandbox_backend(config)  # may raise SandboxUnavailable
    return _make_local_backend(project_root, config)


def _exit_plan_mode_tool():
    """The ``exit_plan_mode`` tool — present a plan and request approval to act.

    The tool body runs only *after* the user approves (the session driver gates
    it behind a plan-approval interrupt), so its return value is simply the
    "go" signal the model reads to start executing.
    """
    from langchain_core.tools import tool

    @tool
    def exit_plan_mode(plan: str) -> str:
        """Present your implementation plan and ask to leave read-only plan mode.

        Call this ONLY when the session is in plan mode and you have finished
        researching and have a concrete, step-by-step plan. The user reviews the
        plan and, on approval, the session switches to an editing mode — then
        carry the plan out, verifying as you go. Do not call this in other modes
        or just to display text.

        Args:
            plan: The proposed plan as concise markdown (numbered steps).
        """
        return (
            "Plan approved by the user. You are now in an editing mode — "
            "implement the plan step by step and verify as you go."
        )

    return exit_plan_mode


def _suggest_memory_tool():
    """The ``suggest_memory`` tool — propose a long-term memory for the user to keep.

    The tool body runs only *after* the user approves (the session driver gates it
    behind a memory-approval interrupt and the approver does the actual write via
    the memory store, respecting the global/project tier and trust gating), so its
    return value is simply the confirmation the model reads.
    """
    from langchain_core.tools import tool

    @tool
    def suggest_memory(
        name: str,
        description: str,
        body: str,
        type: str = "project",
        scope: str = "project",
    ) -> str:
        """Suggest a durable memory for the user to approve, edit, or decline.

        Call this when you learn something worth remembering across sessions — a
        stable user preference, a project convention, or a hard-won fact — and want
        it persisted. The user reviews your suggestion and chooses to save it (as is
        or edited) or decline; nothing is written unless they approve. Do not use
        this for transient, turn-local details.

        Args:
            name: Short title for the memory (used as its filename/slug).
            description: One-line summary shown in the memory index.
            body: The memory content in concise markdown.
            type: Memory category — one of user, feedback, project, reference.
            scope: Where to store it — "global" (all projects) or "project".
        """
        return (
            "Memory saved with the user's approval. Continue with the task; you can "
            "rely on this being remembered in future sessions."
        )

    return suggest_memory


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

    # Wiki tools — registered only when wiki.enabled is True.
    if config.wiki.enabled:
        wiki_tools, system_prompt = _add_wiki_tools(
            tools,
            system_prompt,
            root,
            project_trusted=project_trusted,
            wiki_index_tokens=config.context.wiki_index_tokens,
        )
        tools = wiki_tools

    # Repo map tool and/or system-prompt injection.
    repo_map_mode = config.context.repo_map
    if repo_map_mode in ("tool", "auto"):
        repo_map_tool = _build_repo_map_tool(
            root, token_budget=config.context.repo_map_tokens
        )
        tools = [*tools, repo_map_tool]

    if repo_map_mode == "auto" and root is not None:
        system_prompt = _inject_repo_map(
            system_prompt, root, token_budget=config.context.repo_map_tokens
        )

    # Background-process tools (run/check/kill/list_background). Local backend
    # only: under docker/sandbox a host process would escape the container, so we
    # don't register them there. Gated like shell via the permission bridge.
    if config.execution.backend == "local" and config.execution.background:
        from jarn.agent.background import build_background_tools

        tools = [*tools, *build_background_tools(
            root or Path.cwd(),
            max_concurrent=config.execution.background_max_concurrent,
            max_lifetime_secs=config.execution.background_max_lifetime_secs,
        )]

    # Plan-mode handoff tool: lets the agent present a plan and request approval
    # to leave read-only plan mode. Gated as an interrupt (below) and special-cased
    # by the session driver so it is callable *in* plan mode (the engine would
    # otherwise deny it like any other non-read action).
    tools = [*tools, _exit_plan_mode_tool()]

    # Memory-suggestion tool: lets the agent propose a durable memory for the user
    # to approve. Like exit_plan_mode it is gated as an interrupt (below) and
    # special-cased by the driver — the approver does the write on approval, so the
    # tool itself never touches the store and is safe in any mode.
    tools = [*tools, _suggest_memory_tool()]

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
        warnings=(),
    )


def _add_wiki_tools(
    tools: list[Any],
    system_prompt: str,
    root: Path | None,
    *,
    project_trusted: bool,
    wiki_index_tokens: int | None = None,
) -> tuple[list[Any], str]:
    """Register the four wiki tools and optionally inject the wiki index.

    Returns ``(updated_tools, updated_system_prompt)``.

    Trust gate mirrors project JARN.md / skills: project-tier wiki content is
    only injected into the system prompt when ``project_trusted`` is ``True``
    (an untrusted repo's wiki could carry prompt-injection payloads).  Global
    wiki is always available.
    """
    from langchain_core.tools import tool

    from jarn.memory.wiki import WikiStore

    # When the project is untrusted, the tool-facing store is global-only so
    # that wiki_read / wiki_search cannot expose project wiki pages (which could
    # carry prompt-injection payloads).  Mirrors the index-injection gate below.
    full_store = WikiStore.build(root)
    store = full_store if project_trusted else WikiStore(global_wiki_dir=full_store.global_wiki_dir)

    @tool
    def wiki_search(query: str) -> str:  # type: ignore[misc]
        """Search the project wiki knowledge base for pages matching a query.

        Performs a case-insensitive substring search over all wiki pages and
        returns the matching lines from each page.  Use this to find relevant
        notes, decisions, or documentation before writing new ones.

        Args:
            query: Substring to search for (case-insensitive).
        """
        results = store.search(query)
        if not results:
            return f"No wiki pages matched {query!r}."
        lines = [f"wiki_search results for {query!r}:\n"]
        for slug, matched in results:
            lines.append(f"## {slug}")
            for line in matched[:10]:
                lines.append(f"  {line}")
            if len(matched) > 10:
                lines.append(f"  … ({len(matched) - 10} more lines)")
        return "\n".join(lines)

    @tool
    def wiki_read(page: str) -> str:  # type: ignore[misc]
        """Read the full contents of a wiki page by its name/slug.

        Args:
            page: The slug or name of the wiki page to read.
        """
        try:
            return store.read(page)
        except (FileNotFoundError, ValueError) as exc:
            return f"wiki_read error: {exc}"

    @tool
    def wiki_write(page: str, content: str) -> str:  # type: ignore[misc]
        """Create or overwrite a wiki page with the given content.

        Writes to the project tier if available, otherwise to the global tier.
        The page name is sanitized to a safe slug — no path traversal allowed.
        Requires approval in ask mode (same policy as file writes).

        Args:
            page: Name for the wiki page (becomes the slug/filename).
            content: Full markdown content to write.
        """
        try:
            ref = store.write(page, content)
            return f"wiki page written: {ref}"
        except ValueError as exc:
            return f"wiki_write error: {exc}"

    @tool
    def wiki_append(page: str, text: str) -> str:  # type: ignore[misc]
        """Append text to an existing wiki page (creates the page if absent).

        Requires approval in ask mode (same policy as file writes).

        Args:
            page: Name/slug of the wiki page to append to.
            text: Markdown text to append.
        """
        try:
            ref = store.append(page, text)
            return f"wiki page updated: {ref}"
        except ValueError as exc:
            return f"wiki_append error: {exc}"

    new_tools = [*tools, wiki_search, wiki_read, wiki_write, wiki_append]

    # Inject wiki index into the system prompt when pages exist.
    # Global tier always loads; project tier only when project is trusted.
    index_parts: list[str] = []

    if project_trusted:
        # Full combined index (project + global).
        index_text = store.index_text(token_budget=wiki_index_tokens)
    else:
        # Untrusted project: build a global-only store so project pages are
        # excluded from injection.  The tools still have access to both tiers
        # at call time because the store holds both dirs, but the *passive*
        # injection into the system prompt respects the trust gate.
        from jarn.memory.wiki import WikiStore as _WS

        global_only = _WS(global_wiki_dir=store.global_wiki_dir)
        index_text = global_only.index_text(token_budget=wiki_index_tokens)

    if index_text.strip():
        index_parts.append(index_text.strip())

    if index_parts:
        block = "\n\n<wiki_index>\n" + "\n\n".join(index_parts) + "\n</wiki_index>\n\n"
        system_prompt = block + system_prompt

    return new_tools, system_prompt


def _build_repo_map_tool(root: Path | None, *, token_budget: int):
    """Return a LangChain ``@tool``-decorated function for the agent to call.

    The tool is read-only (no side effects) and delegates to
    :func:`jarn.agent.repomap.build_repo_map`.  ``root`` is captured in a
    closure so the model just passes an optional ``focus`` string.
    """
    from langchain_core.tools import tool

    from jarn.agent.repomap import build_repo_map

    # Capture root at build time so the tool closure works even if root is None
    # (falls back to cwd) — same pattern as other built-in tools.
    _root = root or Path.cwd()
    _budget = token_budget

    @tool
    def repo_map(focus: str = "") -> str:
        """Return a ranked, token-budgeted map of the current repository.

        Provides file paths and their top-level symbols (classes, functions,
        types) ordered by importance so you can orient in a large codebase
        without reading every file.

        Args:
            focus: Optional substring to bias ranking toward matching paths.
        """
        try:
            return build_repo_map(_root, token_budget=_budget, focus=focus)
        except Exception as exc:  # noqa: BLE001
            return f"repo_map failed: {exc}"

    return repo_map


def _inject_repo_map(system_prompt: str, root: Path, *, token_budget: int) -> str:
    """Prepend a repo map block to *system_prompt* (for ``context.repo_map: auto``).

    Failures are silently swallowed — a missing map should never prevent the
    agent from starting.
    """
    from jarn.agent.repomap import build_repo_map

    try:
        map_text = build_repo_map(root, token_budget=token_budget)
        if not map_text.strip():
            return system_prompt
        block = (
            "<repo_map>\n"
            + map_text
            + "\n</repo_map>\n\n"
        )
        return block + system_prompt
    except Exception:  # noqa: BLE001
        return system_prompt
