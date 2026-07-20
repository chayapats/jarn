"""MCP prompts + resources + secret-resolved header/env tests.

Covers Wave B:
  * ``${ENV}`` / ``keychain:`` / ``file:`` references in ``MCPServer.headers`` and
    ``env`` are resolved at spawn (``to_connection``), the on-disk config keeps the
    reference, and a failed resolution is an isolated, secret-redacted error.
  * server prompts are discovered, namespaced ``mcp__<server>__<prompt>``, and
    invokable — ``render`` fetches + returns the prompt text injected into a turn.
  * server resources are listed and readable.

Reuses the fake-client monkeypatch pattern from ``test_extensibility`` (patch the
lazily-imported ``MultiServerMCPClient``).
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

import pytest

from jarn.config.schema import MCPServer
from jarn.config.secrets import redact_secrets
from jarn.extensibility.mcp import (
    build_client,
    list_mcp_resources,
    load_mcp_prompts,
    load_mcp_tools,
    read_mcp_resource,
    to_connection,
)


def _stdio(name: str) -> MCPServer:
    return MCPServer(name=name, transport="stdio", command="run-" + name)


# ── fakes ────────────────────────────────────────────────────────────────────


class _Msg:
    """Minimal stand-in for a LangChain message (only ``.content`` is read)."""

    def __init__(self, content):
        self.content = content


class _Blob:
    """Minimal stand-in for a resource Blob."""

    def __init__(self, text):
        self._text = text

    def as_string(self):
        return self._text


class _Session:
    def __init__(self, prompts, resources):
        self._prompts = prompts
        self._resources = resources

    async def list_prompts(self):
        return SimpleNamespace(prompts=self._prompts)

    async def list_resources(self):
        return SimpleNamespace(resources=self._resources)


class _FakeClient:
    """Fake MultiServerMCPClient exposing prompts + resources per server."""

    def __init__(
        self, connections, *, prompts=None, resources=None,
        prompt_fn=None, resource_fn=None, session_fail=None,
    ):
        self.connections = connections
        self._prompts = prompts or {}
        self._resources = resources or {}
        self._prompt_fn = prompt_fn
        self._resource_fn = resource_fn
        self._session_fail = session_fail or {}

    @contextlib.asynccontextmanager
    async def session(self, server_name, *, auto_initialize=True):
        if server_name in self._session_fail:
            raise self._session_fail[server_name]
        yield _Session(
            self._prompts.get(server_name, []),
            self._resources.get(server_name, []),
        )

    async def get_prompt(self, server_name, prompt_name, *, arguments=None):
        if self._prompt_fn is not None:
            return self._prompt_fn(server_name, prompt_name, arguments)
        return []

    async def get_resources(self, server_name=None, *, uris=None):
        if self._resource_fn is not None:
            return self._resource_fn(server_name, uris)
        return []


class _ToolsClient:
    def __init__(self, connections):
        self.connections = connections

    async def get_tools(self, *, server_name=None):
        return [SimpleNamespace(name=f"{server_name}_tool")]


def _patch_client(monkeypatch, **kwargs):
    import langchain_mcp_adapters.client as client_mod

    monkeypatch.setattr(
        client_mod,
        "MultiServerMCPClient",
        lambda connections: _FakeClient(connections, **kwargs),
    )


def _patch_tools_client(monkeypatch):
    import langchain_mcp_adapters.client as client_mod

    monkeypatch.setattr(
        client_mod, "MultiServerMCPClient", _ToolsClient
    )


# ── secret-resolved headers / env ────────────────────────────────────────────


def test_mcp_headers_secret_resolved_at_spawn(monkeypatch):
    """``${ENV}`` in headers resolves in to_connection; config keeps the reference."""
    secret = "sk-secret-abc123def456ghi789xyz"
    monkeypatch.setenv("MCP_AUTH_TOKEN", secret)
    server = MCPServer(
        name="remote",
        transport="http",
        url="https://mcp.example/v1",
        headers={"Authorization": "${MCP_AUTH_TOKEN}", "X-Static": "plain"},
    )
    conn = to_connection(server)

    # Resolved at spawn (the value handed to the transport is the real secret).
    assert conn["headers"]["Authorization"] == secret
    assert conn["headers"]["X-Static"] == "plain"  # literal passes through
    assert conn["transport"] == "streamable_http"
    # Config reference PRESERVED — /mcp status and config dumps show the ref, never
    # the secret (to_connection returns a fresh dict, never mutates the server).
    assert server.headers == {"Authorization": "${MCP_AUTH_TOKEN}", "X-Static": "plain"}
    # And the central redaction net scrubs the resolved value out of any output.
    assert secret not in redact_secrets(str(conn["headers"]))


def test_mcp_env_secret_resolved_at_spawn(monkeypatch):
    """``${ENV}`` in a stdio server's env resolves at spawn; ref preserved."""
    secret = "ghp_" + "A1B2C3D4E5F6G7H8I9J0"
    monkeypatch.setenv("MCP_DB_TOKEN", secret)
    server = MCPServer(
        name="db",
        transport="stdio",
        command="run-db",
        env={"DB_TOKEN": "${MCP_DB_TOKEN}", "PLAIN": "literal"},
    )
    conn = to_connection(server)

    assert conn["env"]["DB_TOKEN"] == secret
    assert conn["env"]["PLAIN"] == "literal"
    assert server.env == {"DB_TOKEN": "${MCP_DB_TOKEN}", "PLAIN": "literal"}


def test_mcp_unresolvable_header_is_redacted_isolated_error(monkeypatch):
    """A header ref that can't resolve marks THAT server error (no crash, no leak),
    while other servers still load."""
    _patch_tools_client(monkeypatch)
    bad = MCPServer(
        name="bad",
        transport="http",
        url="https://mcp.example/v1",
        headers={"Authorization": "${MCP_DEFINITELY_UNSET_TOKEN}"},
    )
    # build_client alone marks the unresolvable server without spawning it.
    client, invalid = build_client([bad])
    names = dict(invalid)
    assert client is None
    assert "bad" in names
    # The error names the env var (so the user can fix it) but leaks no secret.
    assert "MCP_DEFINITELY_UNSET_TOKEN" in names["bad"]

    result = asyncio.run(load_mcp_tools([bad, _stdio("good")]))
    assert result.health["bad"] == "error"
    assert "MCP_DEFINITELY_UNSET_TOKEN" in result.errors["bad"]
    assert result.health["good"] == "ok"
    assert [t.name for t in result.tools] == ["mcp__good__good_tool"]


def test_mcp_http_headers_literal_still_round_trips(monkeypatch):
    """Regression: a literal (non-reference) header is unchanged by resolution."""
    server = MCPServer(
        name="remote",
        transport="http",
        url="https://mcp.example/v1",
        headers={"Authorization": "Bearer static-value"},
    )
    conn = to_connection(server)
    assert conn["headers"] == {"Authorization": "Bearer static-value"}


# ── prompts ──────────────────────────────────────────────────────────────────


def _greet_meta():
    return SimpleNamespace(
        name="greet",
        description="Greet someone",
        arguments=[SimpleNamespace(name="name")],
    )


def _greet_fn(server, prompt_name, arguments):
    who = (arguments or {}).get("name", "world")
    return [_Msg(f"Hello, {who}!")]


def test_mcp_prompts_discovered_and_invokable(monkeypatch):
    """Prompts are discovered, namespaced, and render() fetches the injected text."""
    _patch_client(
        monkeypatch, prompts={"srv": [_greet_meta()]}, prompt_fn=_greet_fn
    )
    result = asyncio.run(load_mcp_prompts([_stdio("srv")]))

    assert set(result.prompts) == {"mcp__srv__greet"}
    cmd = result.prompts["mcp__srv__greet"]
    assert cmd.description == "Greet someone"
    assert cmd.argument_names == ("name",)
    # Invoking renders (fetches) the prompt text that will be injected into a turn.
    assert cmd.render("Ada") == "Hello, Ada!"          # single positional arg
    assert cmd.render("name=Bo") == "Hello, Bo!"       # key=value arg
    assert cmd.render("") == "Hello, world!"           # no arg → server default
    assert result.health == {"srv": "ok"}


def test_mcp_prompts_multiline_messages_joined(monkeypatch):
    """Multiple prompt messages are concatenated into one injectable block."""
    def _multi(server, name, arguments):
        return [_Msg("first"), _Msg([{"type": "text", "text": "second"}])]

    _patch_client(
        monkeypatch,
        prompts={"srv": [SimpleNamespace(name="multi", description="", arguments=[])]},
        prompt_fn=_multi,
    )
    result = asyncio.run(load_mcp_prompts([_stdio("srv")]))
    assert result.prompts["mcp__srv__multi"].render("") == "first\n\nsecond"


def test_mcp_prompt_discovery_isolated_on_failure(monkeypatch):
    """One server whose session fails is an error; others still yield prompts."""
    _patch_client(
        monkeypatch,
        prompts={"good": [_greet_meta()]},
        prompt_fn=_greet_fn,
        session_fail={"bad": RuntimeError("handshake boom")},
    )
    result = asyncio.run(load_mcp_prompts([_stdio("good"), _stdio("bad")]))
    assert set(result.prompts) == {"mcp__good__greet"}
    assert result.health == {"good": "ok", "bad": "error"}
    assert "boom" in result.errors["bad"]


def test_cmd_mcp_prompts_registers_runtime_command(monkeypatch):
    """/mcp prompts registers each prompt into rt.commands so the REPL's existing
    dispatch injects it into a turn."""
    from jarn.controller.commands.diagnostics import cmd_mcp

    _patch_client(
        monkeypatch, prompts={"srv": [_greet_meta()]}, prompt_fn=_greet_fn
    )
    rt = SimpleNamespace(commands={})
    ctrl = SimpleNamespace(
        config=SimpleNamespace(mcp_servers=[_stdio("srv")]), runtime=rt
    )
    out = cmd_mcp(ctrl, "prompts").text

    assert "mcp__srv__greet" in out
    assert "mcp__srv__greet" in rt.commands
    # The registered command is what the REPL calls on /mcp__srv__greet.
    assert rt.commands["mcp__srv__greet"].render("Ada") == "Hello, Ada!"


def test_cmd_mcp_prompt_single_fetch(monkeypatch):
    """/mcp prompt <server> <name> fetches and returns the prompt text."""
    from jarn.controller.commands.diagnostics import cmd_mcp

    _patch_client(
        monkeypatch, prompts={"srv": [_greet_meta()]}, prompt_fn=_greet_fn
    )
    rt = SimpleNamespace(commands={})
    ctrl = SimpleNamespace(
        config=SimpleNamespace(mcp_servers=[_stdio("srv")]), runtime=rt
    )
    out = cmd_mcp(ctrl, "prompt srv greet name=Cy").text
    assert "Hello, Cy!" in out
    assert "mcp__srv__greet" in rt.commands  # also registered for direct invoke


# ── resources ─────────────────────────────────────────────────────────────────


def _notes_meta():
    return SimpleNamespace(
        uri="file:///notes.txt",
        name="Notes",
        description="Scratch notes",
        mimeType="text/plain",
    )


def test_mcp_resources_listed_and_read(monkeypatch):
    """Resources are listed with metadata and read into text content."""
    _patch_client(
        monkeypatch,
        resources={"srv": [_notes_meta()]},
        resource_fn=lambda server, uris: [_Blob("remember the milk")],
    )
    listed = asyncio.run(list_mcp_resources([_stdio("srv")]))
    assert [r.uri for r in listed.resources] == ["file:///notes.txt"]
    assert listed.resources[0].mime_type == "text/plain"
    assert listed.resources[0].server == "srv"
    assert listed.health == {"srv": "ok"}

    content = asyncio.run(
        read_mcp_resource([_stdio("srv")], "srv", "file:///notes.txt")
    )
    assert content == "remember the milk"


def test_mcp_read_unknown_server_raises(monkeypatch):
    _patch_client(monkeypatch)
    with pytest.raises(ValueError, match="not configured"):
        asyncio.run(read_mcp_resource([_stdio("srv")], "nope", "file:///x"))


def test_cmd_mcp_resources_lists(monkeypatch):
    from jarn.controller.commands.diagnostics import cmd_mcp

    _patch_client(
        monkeypatch,
        resources={"srv": [_notes_meta()]},
        resource_fn=lambda server, uris: [_Blob("body")],
    )
    ctrl = SimpleNamespace(
        config=SimpleNamespace(mcp_servers=[_stdio("srv")]), runtime=None
    )
    out = cmd_mcp(ctrl, "resources").text
    assert "file:///notes.txt" in out
    read_out = cmd_mcp(ctrl, "read srv file:///notes.txt").text
    assert "body" in read_out
