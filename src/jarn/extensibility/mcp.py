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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from jarn.config.schema import MCPServer

if TYPE_CHECKING:
    from jarn.config.schema import NetworkPolicy

logger = logging.getLogger("jarn.mcp")


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


def to_connection(server: MCPServer) -> dict[str, Any]:
    """Build the per-server connection dict for MultiServerMCPClient."""
    if server.transport == "stdio":
        if not server.command:
            raise ValueError(f"MCP server {server.name!r} (stdio) needs a 'command'.")
        return {
            "transport": "stdio",
            "command": server.command,
            "args": list(server.args),
            "env": dict(server.env) or None,
        }
    if server.transport in ("http", "streamable_http", "sse"):
        if not server.url:
            raise ValueError(f"MCP server {server.name!r} (http) needs a 'url'.")
        transport = "sse" if server.transport == "sse" else "streamable_http"
        conn: dict[str, Any] = {"transport": transport, "url": server.url}
        if server.headers:
            conn["headers"] = dict(server.headers)
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
