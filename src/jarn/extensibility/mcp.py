"""MCP client integration.

Translates J.A.R.N.'s :class:`jarn.config.schema.MCPServer` entries into the
connection dict understood by ``langchain-mcp-adapters`` and fetches the tools
they expose so they can be handed to the deep agent alongside built-in tools.

Tool fetching is async (MCP servers are spawned/connected on demand). Each
server is loaded in ISOLATION: a broken or unreachable server records an error
and is skipped rather than taking down the tools of every other server.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from jarn.config.schema import MCPServer

logger = logging.getLogger("jarn.mcp")


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


def build_client(servers: list[MCPServer]):
    """Construct a MultiServerMCPClient from enabled servers (or ``None``).

    Returns the client paired with the list of server names whose connection
    dict was built successfully. Servers with an invalid connection (e.g. a
    stdio server missing ``command``) are omitted here and surfaced separately
    by :func:`load_mcp_tools` so they still show up as ``error`` in health.
    """
    enabled = [s for s in servers if s.enabled]
    if not enabled:
        return None, []
    from langchain_mcp_adapters.client import MultiServerMCPClient

    connections: dict[str, Any] = {}
    bad: dict[str, str] = {}
    for server in enabled:
        try:
            connections[server.name] = to_connection(server)
        except ValueError as exc:
            logger.warning("Skipping MCP server: %s", exc)
            bad[server.name] = str(exc)
    if not connections:
        return None, list(bad.items())
    return MultiServerMCPClient(connections), list(bad.items())


async def load_mcp_tools(servers: list[MCPServer]) -> MCPLoadResult:
    """Load tools from every enabled MCP server, each in isolation.

    The installed ``langchain-mcp-adapters`` ``MultiServerMCPClient`` exposes
    ``get_tools(server_name=...)``, which opens and tears down a fresh session
    for just that one server. We call it once per server inside a try/except so
    one bad server's failure is recorded and skipped, never losing the tools of
    the healthy ones. The client itself holds no persistent connection (it is
    not an async context manager), so there is no client to close.
    """
    result = MCPLoadResult()
    client, invalid = build_client(servers)
    # Servers whose connection dict was malformed: mark error up front.
    for name, message in invalid:
        result.health[name] = "error"
        result.errors[name] = message
    if client is None:
        return result

    for name in client.connections:
        try:
            tools = await client.get_tools(server_name=name)
        except Exception as exc:  # noqa: BLE001 - one bad server shouldn't kill startup
            logger.warning("Failed to load MCP tools from %s: %s", name, exc)
            result.health[name] = "error"
            result.errors[name] = str(exc)
            continue
        result.tools.extend(tools)
        result.health[name] = "ok"
    return result
