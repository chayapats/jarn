"""MCP client integration.

Translates J.A.R.N.'s :class:`jarn.config.schema.MCPServer` entries into the
connection dict understood by ``langchain-mcp-adapters`` and fetches the tools
they expose so they can be handed to the deep agent alongside built-in tools.

Tool fetching is async (MCP servers are spawned/connected on demand). Each
server is loaded in ISOLATION: a broken or unreachable server records an error
and is skipped rather than taking down the tools of every other server.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from jarn.config.schema import MCPServer
from jarn.config.secrets import SecretResolutionError, redact_secrets, resolve

if TYPE_CHECKING:
    from jarn.config.schema import NetworkPolicy

logger = logging.getLogger("jarn.mcp")


def run_blocking(coro: Any) -> Any:
    """Run an async coroutine to completion from synchronous code.

    The controller command registry (``/mcp …``) is invoked synchronously from
    the REPL's already-running event loop, where ``asyncio.run`` would raise
    "loop already running". When a loop is live we run the coroutine on a
    one-shot worker thread with its own loop; outside a loop (tests / headless
    callers) we ``asyncio.run`` it inline. Mirrors the pattern the ``/mcp
    refresh`` handler used before this was factored out for reuse by the prompt
    and resource fetch paths."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


def _network_block_reason(
    server: MCPServer, network_policy: NetworkPolicy | None
) -> str | None:
    """Egress-policy check for an http/sse endpoint host; ``None`` = permitted.

    ``stdio`` servers spawn a local subprocess with no egress host, so they are
    never blocked here. Mirrors the SSRF host semantics of web_fetch: ``deny``
    always wins, and a non-empty ``allow`` restricts to listed hosts.
    """
    if network_policy is None or not (network_policy.allow or network_policy.deny):
        return None
    if server.transport not in ("http", "sse", "streamable_http"):
        return None
    from jarn.permissions.guard import NetworkVerdict, classify_host

    host = urlparse(server.url or "").hostname or ""
    verdict = classify_host(host, network_policy)
    if verdict is NetworkVerdict.DENIED:
        return f"endpoint host {host!r} is denied by the permissions.network policy"
    if verdict is NetworkVerdict.NOT_ALLOWED:
        return f"endpoint host {host!r} is not on the permissions.network allowlist"
    return None


@dataclass(slots=True)
class MCPLoadResult:
    """Outcome of loading MCP tools, per-server.

    ``tools`` is the flat list of every successfully loaded tool (across all
    healthy servers). ``health`` maps each enabled server name to ``"ok"`` or
    ``"error"``; ``errors`` carries the failure message for the servers that
    errored (a subset of ``health``).
    """

    tools: list[Any] = field(default_factory=list)
    health: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def degraded(self) -> bool:
        """True when at least one enabled server failed to load."""
        return bool(self.errors)


def _strip_mcp_prefix(name: str) -> str:
    """Collapse any server-supplied ``mcp__<x>__`` prefix from a tool name.

    The ``mcp__<server>__<tool>`` scheme is RESERVED for provenance stamped by
    this loader (:func:`_namespace_tool`). A malicious server that names its own
    tool ``mcp__other__x`` would otherwise smuggle a forged provenance once we
    prepend the real prefix (yielding a name that parses as server ``other``), so
    every leading ``mcp__<seg>__`` is stripped first. Handles nested/double
    prefixes (``mcp__a__mcp__b__x`` → ``x``).
    """
    while name.startswith("mcp__"):
        rest = name[len("mcp__") :]
        sep = rest.find("__")
        if sep == -1:
            name = rest  # bare "mcp__foo": drop the reserved marker only
            break
        name = rest[sep + 2 :]
    return name


def _namespace_tool(tool: Any, server_name: str) -> Any:
    """Stamp ``tool`` with the reserved ``mcp__<server>__<tool>`` provenance name.

    Invariant: the ``mcp__`` prefix is PROVENANCE. Permission classification
    (``permissions_bridge.tool_to_action``) treats ONLY non-prefixed names as
    jarn builtins, so a namespaced MCP tool can never be misclassified as a
    read-only builtin and auto-allowed — it falls through to ``ActionKind.NETWORK``
    and is gated. Any pre-existing ``mcp__`` prefix in the server-provided name is
    collapsed first (:func:`_strip_mcp_prefix`) so a server cannot forge a double
    prefix that parses as a different server. Tools without a string ``name`` are
    returned untouched. Pydantic ``StructuredTool`` normally accepts the direct
    assignment; if it is rejected we rebuild via ``model_copy``.
    """
    original = getattr(tool, "name", None)
    if not isinstance(original, str):
        return tool
    new_name = f"mcp__{server_name}__{_strip_mcp_prefix(original)}"
    if new_name == original:
        return tool  # already namespaced under this server; idempotent
    try:
        tool.name = new_name
    except Exception:  # noqa: BLE001 - pydantic may reject assignment; rebuild instead
        tool = tool.model_copy(update={"name": new_name})
    return tool


def _resolve_secret_map(
    mapping: dict[str, str], *, kind: str, server: str
) -> dict[str, str]:
    """Resolve ``${ENV}`` / ``keychain:`` / ``file:`` references in a header/env map.

    Reuses :func:`jarn.config.secrets.resolve` — the SINGLE secret-resolution
    helper — per value, so a header or env value that is a reference is turned
    into its concrete secret ONLY here, at connection-build (spawn) time. The
    on-disk config keeps the reference (this returns a fresh dict and never
    mutates the server), so ``/mcp status`` and any config dump still show the
    reference, never the secret. A literal value (no recognised prefix) passes
    through unchanged. A resolution failure is raised as ``ValueError`` (so
    :func:`build_client` records the server as ``error`` in isolation) with a
    secret-redacted message so no key value leaks into logs or health output."""
    resolved: dict[str, str] = {}
    for key, value in mapping.items():
        try:
            resolved[key] = resolve(value) or ""
        except SecretResolutionError as exc:
            raise ValueError(
                redact_secrets(f"MCP server {server!r} {kind} {key!r}: {exc}")
            ) from exc
    return resolved


def to_connection(server: MCPServer) -> dict[str, Any]:
    """Build the per-server connection dict for MultiServerMCPClient.

    Header and env values are secret-resolved here (at spawn), not at config
    load, so a ``${ENV}`` / ``keychain:`` / ``file:`` reference in ``headers`` or
    ``env`` becomes its concrete secret only in the connection dict handed to the
    transport — the persisted config keeps the reference. See
    :func:`_resolve_secret_map`."""
    if server.transport == "stdio":
        if not server.command:
            raise ValueError(f"MCP server {server.name!r} (stdio) needs a 'command'.")
        return {
            "transport": "stdio",
            "command": server.command,
            "args": list(server.args),
            "env": _resolve_secret_map(dict(server.env), kind="env", server=server.name)
            or None,
        }
    if server.transport in ("http", "streamable_http", "sse"):
        if not server.url:
            raise ValueError(f"MCP server {server.name!r} (http) needs a 'url'.")
        transport = "sse" if server.transport == "sse" else "streamable_http"
        conn: dict[str, Any] = {"transport": transport, "url": server.url}
        if server.headers:
            conn["headers"] = _resolve_secret_map(
                dict(server.headers), kind="header", server=server.name
            )
        return conn
    raise ValueError(f"MCP server {server.name!r}: unknown transport {server.transport!r}")


def build_client(
    servers: list[MCPServer], network_policy: NetworkPolicy | None = None
):
    """Construct a MultiServerMCPClient from enabled servers (or ``None``).

    Returns the client paired with the list of server names whose connection
    dict was built successfully. Servers with an invalid connection (e.g. a
    stdio server missing ``command``) — or whose http/sse endpoint host is
    refused by *network_policy* — are omitted here and surfaced separately by
    :func:`load_mcp_tools` so they still show up as ``error`` in health.
    """
    enabled = [s for s in servers if s.enabled]
    if not enabled:
        return None, []
    from langchain_mcp_adapters.client import MultiServerMCPClient

    connections: dict[str, Any] = {}
    bad: dict[str, str] = {}
    for server in enabled:
        try:
            conn = to_connection(server)
        except ValueError as exc:
            logger.warning("Skipping MCP server: %s", exc)
            bad[server.name] = str(exc)
            continue
        reason = _network_block_reason(server, network_policy)
        if reason is not None:
            logger.warning("MCP server %r blocked by network policy: %s",
                           server.name, reason)
            bad[server.name] = reason
            continue
        connections[server.name] = conn
    if not connections:
        return None, list(bad.items())
    return MultiServerMCPClient(connections), list(bad.items())


async def load_mcp_tools(
    servers: list[MCPServer], network_policy: NetworkPolicy | None = None
) -> MCPLoadResult:
    """Load tools from every enabled MCP server, each in isolation.

    The installed ``langchain-mcp-adapters`` ``MultiServerMCPClient`` exposes
    ``get_tools(server_name=...)``, which opens and tears down a fresh session
    for just that one server. We call it once per server inside a try/except so
    one bad server's failure is recorded and skipped, never losing the tools of
    the healthy ones. Servers are loaded CONCURRENTLY (via ``asyncio.gather``)
    so startup latency is the slowest server's handshake, not the sum of all;
    per-server isolation and per-server ``timeout_secs`` are preserved because
    each ``_load_one`` owns its own ``wait_for`` and try/except. The client
    itself holds no persistent connection (it is not an async context manager),
    so there is no client to close.
    """
    result = MCPLoadResult()
    client, invalid = build_client(servers, network_policy)
    # Servers whose connection dict was malformed: mark error up front.
    for name, message in invalid:
        result.health[name] = "error"
        result.errors[name] = message
    if client is None:
        return result

    timeout_by_name = {s.name: s.timeout_secs for s in servers if s.enabled}

    async def _load_one(name: str):
        """Load one server's tools in isolation; never raises."""
        secs = timeout_by_name.get(name, 30)
        try:
            tools = await asyncio.wait_for(
                client.get_tools(server_name=name), timeout=secs
            )
            return name, tools, None
        except TimeoutError:
            logger.warning("Timed out loading MCP tools from %s after %ss", name, secs)
            return name, None, f"timed out after {secs}s"
        except Exception as exc:  # noqa: BLE001 - one bad server must not kill startup
            logger.warning("Failed to load MCP tools from %s: %s", name, exc)
            return name, None, str(exc)

    # gather preserves argument order, so tools are extended in client.connections
    # iteration order regardless of which handshake completes first.
    for name, tools, err in await asyncio.gather(
        *(_load_one(n) for n in client.connections)
    ):
        if err is not None:
            result.health[name] = "error"
            result.errors[name] = err
        else:
            # Namespace every tool to mcp__<server>__<tool> before it leaves the
            # loader: the prefix is provenance so permission classification can
            # never mistake an MCP tool for a jarn builtin (see _namespace_tool).
            result.tools.extend(_namespace_tool(t, name) for t in tools)
            result.health[name] = "ok"
    return result


# ── MCP prompts → invokable slash commands ──────────────────────────────────
# A server-published prompt is exposed as a runtime-invokable command named
# ``mcp__<server>__<prompt>`` (same provenance scheme as tools). Registering these
# into the runtime's command table (see the /mcp handler) means the REPL's EXISTING
# dispatch — ``rt.commands[name].render(args)`` fed to a turn — injects the prompt
# text with no change to the turn path. Discovery (list) is cheap; the prompt body
# is fetched lazily on invoke so per-prompt ``arguments`` can be supplied.


def _message_text(message: Any) -> str:
    """Flatten a LangChain message's content to plain text."""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content
        )
    return str(content)


def _join_prompt_messages(messages: Any) -> str:
    """Join the text of every prompt message into one injectable block."""
    parts = [t for m in (messages or []) if (t := _message_text(m))]
    return "\n\n".join(parts)


def _parse_prompt_args(args: str, names: tuple[str, ...]) -> dict[str, Any]:
    """Parse a raw ``/mcp__srv__p`` argument string into MCP prompt arguments.

    ``key=value key2=value2`` tokens map to named arguments. When the prompt
    declares exactly one argument and no ``=`` is present, the whole string is
    that argument's value (so ``/mcp__srv__greet Ada`` works). Otherwise an
    argument-less prompt just gets ``{}``."""
    args = args.strip()
    if not args:
        return {}
    if "=" in args:
        out: dict[str, Any] = {}
        for token in args.split():
            if "=" in token:
                key, _, value = token.partition("=")
                out[key.strip()] = value.strip()
        if out:
            return out
    if len(names) == 1:
        return {names[0]: args}
    return {}


@dataclass(slots=True)
class MCPPromptCommand:
    """An MCP server prompt exposed as a runtime-invokable slash command.

    Duck-types :class:`jarn.extensibility.commands.CustomCommand` (``name``,
    ``description``, ``render(args) -> str``) so it can be dropped straight into
    the runtime's ``commands`` table: the REPL then injects ``render(args)`` into
    a turn exactly as it does for user-defined ``.jarn/commands`` files. ``render``
    fetches the prompt body from the server on demand (``fetch``), so invoking
    ``/mcp__<server>__<prompt> key=value`` resolves the live prompt text."""

    name: str  # namespaced: mcp__<server>__<prompt>
    server: str
    prompt_name: str  # original server-side name (used for get_prompt)
    description: str = ""
    argument_names: tuple[str, ...] = ()
    fetch: Callable[[dict[str, Any]], str] | None = field(default=None, repr=False)

    def render(self, args: str) -> str:
        """Fetch and return the prompt text to inject into the turn."""
        if self.fetch is None:  # pragma: no cover - always bound by the loader
            return ""
        return self.fetch(_parse_prompt_args(args, self.argument_names))


@dataclass(slots=True)
class MCPPromptLoadResult:
    """Outcome of discovering MCP prompts across enabled servers."""

    prompts: dict[str, MCPPromptCommand] = field(default_factory=dict)
    health: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


async def _list_prompts(client: Any, server_name: str) -> list[Any]:
    async with client.session(server_name) as session:
        listed = await session.list_prompts()
    return list(getattr(listed, "prompts", []) or [])


async def _get_prompt_text(
    client: Any, server_name: str, prompt_name: str, arguments: dict[str, Any]
) -> str:
    messages = await client.get_prompt(
        server_name, prompt_name, arguments=arguments or None
    )
    return _join_prompt_messages(messages)


def _build_prompt_command(client: Any, server: str, prompt: Any) -> MCPPromptCommand:
    raw_name = str(getattr(prompt, "name", "") or "")
    # Collapse any server-supplied mcp__ prefix (provenance is ours to stamp) but
    # keep the ORIGINAL name for the get_prompt call.
    display = _strip_mcp_prefix(raw_name) or raw_name
    arg_names = tuple(
        str(getattr(a, "name", "") or "")
        for a in (getattr(prompt, "arguments", None) or [])
    )

    def _fetch(arguments: dict[str, Any]) -> str:
        return run_blocking(_get_prompt_text(client, server, raw_name, arguments))

    return MCPPromptCommand(
        name=f"mcp__{server}__{display}",
        server=server,
        prompt_name=raw_name,
        description=str(getattr(prompt, "description", "") or ""),
        argument_names=arg_names,
        fetch=_fetch,
    )


async def load_mcp_prompts(servers: list[MCPServer]) -> MCPPromptLoadResult:
    """Discover prompts from every enabled MCP server, each in isolation.

    Mirrors :func:`load_mcp_tools`: one bad/unreachable server records an error
    and is skipped rather than losing every other server's prompts. Only prompt
    metadata is fetched here (a ``list_prompts`` round-trip); each returned
    :class:`MCPPromptCommand` fetches its body lazily when invoked."""
    result = MCPPromptLoadResult()
    client, invalid = build_client(servers)
    for name, message in invalid:
        result.health[name] = "error"
        result.errors[name] = message
    if client is None:
        return result

    timeout_by_name = {s.name: s.timeout_secs for s in servers if s.enabled}

    async def _one(name: str) -> tuple[str, list[Any] | None, str | None]:
        secs = timeout_by_name.get(name, 30)
        try:
            prompts = await asyncio.wait_for(_list_prompts(client, name), timeout=secs)
            return name, prompts, None
        except TimeoutError:
            return name, None, f"timed out after {secs}s"
        except Exception as exc:  # noqa: BLE001 - one bad server must not kill discovery
            logger.warning("Failed to list MCP prompts from %s: %s", name, exc)
            return name, None, str(exc)

    for name, prompts, err in await asyncio.gather(
        *(_one(n) for n in client.connections)
    ):
        if err is not None:
            result.health[name] = "error"
            result.errors[name] = redact_secrets(err)
            continue
        result.health[name] = "ok"
        for prompt in prompts or []:
            command = _build_prompt_command(client, name, prompt)
            result.prompts[command.name] = command
    return result


# ── MCP resources → listing + read ──────────────────────────────────────────


@dataclass(slots=True)
class MCPResource:
    """A resource published by an MCP server (metadata only)."""

    server: str
    uri: str
    name: str = ""
    description: str = ""
    mime_type: str = ""


@dataclass(slots=True)
class MCPResourceListResult:
    """Outcome of listing MCP resources across enabled servers."""

    resources: list[MCPResource] = field(default_factory=list)
    health: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


async def _list_resources(client: Any, server_name: str) -> list[Any]:
    async with client.session(server_name) as session:
        listed = await session.list_resources()
    return list(getattr(listed, "resources", []) or [])


async def list_mcp_resources(servers: list[MCPServer]) -> MCPResourceListResult:
    """List resources from every enabled MCP server, each in isolation."""
    result = MCPResourceListResult()
    client, invalid = build_client(servers)
    for name, message in invalid:
        result.health[name] = "error"
        result.errors[name] = message
    if client is None:
        return result

    timeout_by_name = {s.name: s.timeout_secs for s in servers if s.enabled}

    async def _one(name: str) -> tuple[str, list[Any] | None, str | None]:
        secs = timeout_by_name.get(name, 30)
        try:
            resources = await asyncio.wait_for(
                _list_resources(client, name), timeout=secs
            )
            return name, resources, None
        except TimeoutError:
            return name, None, f"timed out after {secs}s"
        except Exception as exc:  # noqa: BLE001 - one bad server must not kill discovery
            logger.warning("Failed to list MCP resources from %s: %s", name, exc)
            return name, None, str(exc)

    for name, resources, err in await asyncio.gather(
        *(_one(n) for n in client.connections)
    ):
        if err is not None:
            result.health[name] = "error"
            result.errors[name] = redact_secrets(err)
            continue
        result.health[name] = "ok"
        for res in resources or []:
            result.resources.append(
                MCPResource(
                    server=name,
                    uri=str(getattr(res, "uri", "") or ""),
                    name=str(getattr(res, "name", "") or ""),
                    description=str(getattr(res, "description", "") or ""),
                    mime_type=str(getattr(res, "mimeType", "") or ""),
                )
            )
    return result


def _blob_text(blob: Any) -> str:
    """Best-effort text of one resource Blob (falls back to its raw data)."""
    try:
        return blob.as_string()
    except Exception:  # noqa: BLE001 - binary blob or missing encoding
        data = getattr(blob, "data", "")
        return data if isinstance(data, str) else str(data)


async def read_mcp_resource(
    servers: list[MCPServer], server: str, uri: str
) -> str:
    """Read one resource's content into text.

    Raises ``ValueError`` when ``server`` is not a configured/enabled MCP server
    (so the /mcp handler can surface a clear message)."""
    client, _invalid = build_client(servers)
    if client is None or server not in getattr(client, "connections", {}):
        raise ValueError(f"MCP server {server!r} is not configured or not enabled.")
    blobs = await client.get_resources(server, uris=uri)
    return "\n\n".join(t for b in (blobs or []) if (t := _blob_text(b)))
