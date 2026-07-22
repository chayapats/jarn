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
import time
from types import SimpleNamespace

import pytest

from jarn.config.schema import MCPServer, NetworkPolicy
from jarn.config.secrets import redact_secrets
from jarn.extensibility.mcp import (
    _build_prompt_command,
    _parse_prompt_args,
    build_client,
    list_mcp_resources,
    load_mcp_prompts,
    load_mcp_tools,
    read_mcp_resource,
    to_connection,
)


def _stdio(name: str) -> MCPServer:
    return MCPServer(name=name, transport="stdio", command="run-" + name)


def _http(name: str, url: str) -> MCPServer:
    return MCPServer(name=name, transport="http", url=url)


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
            r = self._prompt_fn(server_name, prompt_name, arguments)
            # A fn may return a coroutine to model a slow server (timeout tests).
            return await r if asyncio.iscoroutine(r) else r
        return []

    async def get_resources(self, server_name=None, *, uris=None):
        if self._resource_fn is not None:
            r = self._resource_fn(server_name, uris)
            return await r if asyncio.iscoroutine(r) else r
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
        config=SimpleNamespace(
            mcp_servers=[_stdio("srv")],
            permissions=SimpleNamespace(network=NetworkPolicy()),
        ),
        runtime=rt,
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
        config=SimpleNamespace(
            mcp_servers=[_stdio("srv")],
            permissions=SimpleNamespace(network=NetworkPolicy()),
        ),
        runtime=rt,
    )
    out = cmd_mcp(ctrl, "prompt srv greet name=Cy").text
    assert "Hello, Cy!" in out
    assert "mcp__srv__greet" in rt.commands  # also registered for direct invoke


# ── prompt-argument quoting (whitespace inside quoted values) ─────────────────


def _echo_meta():
    """A prompt declaring two named args, so key=value parsing is exercised."""
    return SimpleNamespace(
        name="echo",
        description="",
        arguments=[SimpleNamespace(name="topic"), SimpleNamespace(name="style")],
    )


def _capture(store):
    """A prompt_fn that records the resolved arguments dict for assertions."""

    def _fn(server, prompt_name, arguments):
        store.clear()
        store.update(arguments or {})
        return [_Msg("ok")]

    return _fn


def test_parse_prompt_args_preserves_quoted_whitespace():
    """Quoted multiword values keep their spaces; quotes are consumed (shlex),
    not left dangling on a truncated first word (the old str.split() bug)."""
    two = ("topic", "style")
    # double quotes
    assert _parse_prompt_args('topic="hello world" style=brief', two) == {
        "topic": "hello world",
        "style": "brief",
    }
    # single quotes
    assert _parse_prompt_args("topic='hello world' style=brief", two) == {
        "topic": "hello world",
        "style": "brief",
    }
    # mixed quoted / unquoted
    assert _parse_prompt_args('topic=plain style="two words"', two) == {
        "topic": "plain",
        "style": "two words",
    }
    # an ``=`` inside the (quoted) value is preserved, not re-split
    assert _parse_prompt_args('topic="a=b" style=x', two) == {
        "topic": "a=b",
        "style": "x",
    }
    # single-arg no-``=`` fallback: a bare multiword value still works
    assert _parse_prompt_args("hello world", ("name",)) == {"name": "hello world"}
    # malformed quoting must not raise (graceful best-effort fallback)
    assert isinstance(_parse_prompt_args('topic="unterminated', ("topic",)), dict)


def test_mcp_prompt_render_preserves_quoted_whitespace(monkeypatch):
    """Direct namespaced invocation: render() → _parse_prompt_args keeps spaces."""
    got: dict = {}
    _patch_client(
        monkeypatch, prompts={"srv": [_echo_meta()]}, prompt_fn=_capture(got)
    )
    result = asyncio.run(load_mcp_prompts([_stdio("srv")]))
    cmd = result.prompts["mcp__srv__echo"]

    cmd.render('topic="hello world" style=brief')
    assert got == {"topic": "hello world", "style": "brief"}

    cmd.render("topic='hello world' style=brief")
    assert got == {"topic": "hello world", "style": "brief"}


def test_cmd_mcp_prompt_path_preserves_quoted_whitespace(monkeypatch):
    """The /mcp prompt <server> <name> <args…> dispatcher preserves quoting all the
    way to the parser (no split()+join() collapse of token boundaries)."""
    from jarn.controller.commands.diagnostics import cmd_mcp

    got: dict = {}
    _patch_client(
        monkeypatch, prompts={"srv": [_echo_meta()]}, prompt_fn=_capture(got)
    )
    rt = SimpleNamespace(commands={})
    ctrl = SimpleNamespace(
        config=SimpleNamespace(
            mcp_servers=[_stdio("srv")],
            permissions=SimpleNamespace(network=NetworkPolicy()),
        ),
        runtime=rt,
    )

    cmd_mcp(ctrl, 'prompt srv echo topic="hello world" style=brief')
    assert got == {"topic": "hello world", "style": "brief"}

    cmd_mcp(ctrl, "prompt srv echo topic='hello world' style=brief")
    assert got == {"topic": "hello world", "style": "brief"}


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
        config=SimpleNamespace(
            mcp_servers=[_stdio("srv")],
            permissions=SimpleNamespace(network=NetworkPolicy()),
        ),
        runtime=None,
    )
    out = cmd_mcp(ctrl, "resources").text
    assert "file:///notes.txt" in out
    read_out = cmd_mcp(ctrl, "read srv file:///notes.txt").text
    assert "body" in read_out


# ── BUG 1: network egress policy on prompts + resources ──────────────────────
# Deny + non-empty-allow policy must block EACH http prompt/resource op (as it
# already does for tool-loading) while stdio stays unaffected.

_POLICY = NetworkPolicy(allow=["*.github.com"], deny=["evil.example"])


def test_mcp_prompts_discovery_blocked_by_policy(monkeypatch):
    """A denied http endpoint yields no prompt (error); stdio is unaffected."""
    _patch_client(
        monkeypatch, prompts={"local": [_greet_meta()]}, prompt_fn=_greet_fn
    )
    servers = [_http("bad", "https://evil.example/v1"), _stdio("local")]
    result = asyncio.run(load_mcp_prompts(servers, _POLICY))

    assert result.health["bad"] == "error"
    assert "denied" in result.errors["bad"]
    assert not any(k.startswith("mcp__bad__") for k in result.prompts)
    # stdio has no egress host — never blocked.
    assert "mcp__local__greet" in result.prompts
    assert result.health["local"] == "ok"


def test_mcp_prompt_fetch_reenforces_policy(monkeypatch):
    """The lazy fetch closure RETAINS the policy: a command built for a denied
    host still refuses to connect (defense in depth vs. a stale registration)."""
    _patch_client(monkeypatch, prompts={"bad": [_greet_meta()]}, prompt_fn=_greet_fn)
    server = _http("bad", "https://evil.example/v1")
    # Build the client WITHOUT the policy so the connection exists (as if the
    # command were registered before the policy tightened), then confirm the
    # closure still blocks because it carries the policy.
    client, _invalid = build_client([server])
    cmd = _build_prompt_command(client, server, _greet_meta(), _POLICY, 30)
    out = cmd.render("Ada")
    assert "denied" in out
    assert "Hello" not in out  # never reached the server


def test_mcp_resources_list_blocked_by_policy(monkeypatch):
    """A denied http endpoint contributes no resources (error); stdio unaffected."""
    _patch_client(
        monkeypatch,
        resources={"local": [_notes_meta()]},
        resource_fn=lambda server, uris: [_Blob("body")],
    )
    servers = [_http("bad", "https://evil.example/v1"), _stdio("local")]
    result = asyncio.run(list_mcp_resources(servers, _POLICY))

    assert result.health["bad"] == "error"
    assert "denied" in result.errors["bad"]
    assert not any(r.server == "bad" for r in result.resources)
    assert any(r.server == "local" for r in result.resources)
    assert result.health["local"] == "ok"


def test_mcp_resource_read_blocked_by_policy(monkeypatch):
    """Reading from a denied http host raises the block reason."""
    _patch_client(monkeypatch, resource_fn=lambda server, uris: [_Blob("body")])
    server = _http("bad", "https://evil.example/v1")
    with pytest.raises(ValueError, match="denied"):
        asyncio.run(read_mcp_resource([server], "bad", "file:///x", _POLICY))


def test_mcp_resource_read_stdio_unaffected_by_policy(monkeypatch):
    """A strict allowlist must not disable a stdio server's resource read."""
    _patch_client(
        monkeypatch,
        resources={"local": [_notes_meta()]},
        resource_fn=lambda server, uris: [_Blob("body")],
    )
    content = asyncio.run(
        read_mcp_resource(
            [_stdio("local")], "local", "file:///notes.txt",
            NetworkPolicy(allow=["nope.example"]),
        )
    )
    assert content == "body"


# ── BUG 2: prompt/resource lazy fetch honours per-server timeout_secs ─────────
# A server that stalls on the fetch (get_prompt / get_resources) must time out
# per timeout_secs instead of hanging the synchronous REPL command forever.


async def _stall_prompt():
    await asyncio.sleep(5)
    return [_Msg("too late")]


async def _stall_resources():
    await asyncio.sleep(5)
    return [_Blob("too late")]


def test_mcp_prompt_fetch_times_out(monkeypatch):
    _patch_client(
        monkeypatch,
        prompts={"srv": [_greet_meta()]},
        prompt_fn=lambda s, n, a: _stall_prompt(),
    )
    server = MCPServer(
        name="srv", transport="stdio", command="run-srv", timeout_secs=0.05
    )
    result = asyncio.run(load_mcp_prompts([server]))
    cmd = result.prompts["mcp__srv__greet"]

    started = time.monotonic()
    out = cmd.render("Ada")
    elapsed = time.monotonic() - started
    assert "timed out" in out
    assert elapsed < 2, f"fetch should time out ~0.05s, waited {elapsed:.2f}s"


def test_mcp_resource_read_times_out(monkeypatch):
    _patch_client(
        monkeypatch,
        resources={"srv": [_notes_meta()]},
        resource_fn=lambda s, uris: _stall_resources(),
    )
    server = MCPServer(
        name="srv", transport="stdio", command="run-srv", timeout_secs=0.05
    )
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="timed out"):
        asyncio.run(read_mcp_resource([server], "srv", "file:///notes.txt"))
    elapsed = time.monotonic() - started
    assert elapsed < 2, f"read should time out ~0.05s, waited {elapsed:.2f}s"


# ── BUG C: lazy fetch exceptions are redacted, never dumped raw ──────────────
# The redirect egress hook raises MCPEgressBlocked, and transport calls raise
# RuntimeError/httpx errors, from get_prompt / get_resources. These escape the
# TimeoutError-only catch, and the REPL's direct extension-command dispatch has
# no exception boundary, so a raw (secret-bearing) message would be shown to the
# user. The lazy fetch must catch every NON-cancellation exception, pass it
# through redact_secrets, and return (prompt) / raise (resource) a STABLE error.

#: A live-secret-shaped token that must NEVER survive into any surfaced error.
_LEAKY = "sk-live-secret-0123456789abcdefABCDEF"


def test_mcp_prompt_fetch_exception_is_redacted(monkeypatch):
    """A prompt fetch that raises a secret-bearing exception returns a redacted
    STRING (render never raises, the token never appears in the output)."""
    def _boom(server, name, arguments):
        raise RuntimeError(f"transport failed: Authorization=Bearer {_LEAKY}")

    _patch_client(monkeypatch, prompts={"srv": [_greet_meta()]}, prompt_fn=_boom)
    result = asyncio.run(load_mcp_prompts([_stdio("srv")]))
    cmd = result.prompts["mcp__srv__greet"]

    out = cmd.render("Ada")  # must NOT raise
    assert _LEAKY not in out
    assert "sk-live-secret" not in out
    assert "Bearer sk" not in out
    assert "greet" in out  # a stable, human-meaningful error is still returned


def test_mcp_prompt_fetch_egress_block_is_redacted(monkeypatch):
    """An MCPEgressBlocked raised during transport (a redirect hop) is caught and
    surfaced as a string — it never escapes render() as a raw exception."""
    from jarn.extensibility.mcp import MCPEgressBlocked

    def _blocked(server, name, arguments):
        raise MCPEgressBlocked("request host 'evil.example' is denied by policy")

    _patch_client(monkeypatch, prompts={"srv": [_greet_meta()]}, prompt_fn=_blocked)
    result = asyncio.run(load_mcp_prompts([_stdio("srv")]))
    out = result.prompts["mcp__srv__greet"].render("Ada")
    assert "denied" in out
    assert "Hello" not in out  # never reached the server


def test_mcp_prompt_fetch_cancellation_propagates(monkeypatch):
    """asyncio.CancelledError from the fetch MUST propagate (control flow), never
    be swallowed or redacted into a returned string."""
    from jarn.extensibility.mcp import _get_prompt_text

    class _CancelClient:
        async def get_prompt(self, *a, **k):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_get_prompt_text(_CancelClient(), "srv", "greet", {}, 30))


def test_mcp_resource_read_exception_is_redacted(monkeypatch):
    """A resource read whose transport raises a secret-bearing error re-raises a
    STABLE redacted error (never the raw token, no leaky __cause__ chain)."""
    def _boom(server, uris):
        raise RuntimeError(f"transport failed: Authorization=Bearer {_LEAKY}")

    _patch_client(monkeypatch, resource_fn=_boom)
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - assert on message
        asyncio.run(read_mcp_resource([_stdio("srv")], "srv", "file:///x"))
    msg = str(excinfo.value)
    assert _LEAKY not in msg
    assert "sk-live-secret" not in msg
    assert "Bearer sk" not in msg
    # The raw exception must not ride along as the cause (its str would leak too).
    assert excinfo.value.__cause__ is None


# ── Opaque (non-pattern) configured credential is scrubbed from errors ────────
# redact_secrets' pattern net only recognises secret-SHAPED strings (Bearer …,
# sk-…, vendor prefixes). A malicious/compromised server can echo the credential
# we configured FOR it back inside an error with NO Authorization/Bearer label,
# where it survives the pattern net. The configured header/env VALUES must be
# passed as redact_secrets(known=…) so exact-value scrubbing removes them
# regardless of shape — for prompts the raw error else reaches the model turn,
# for resources the user-shown error.

#: An arbitrary opaque credential: not sk-/Bearer/base64-shaped and too short for
#: the high-entropy blob rule, so ONLY exact-value scrubbing (known=…) removes it.
_OPAQUE_CRED = "odd!credential#42"


def test_mcp_prompt_opaque_credential_is_redacted(monkeypatch):
    """A prompt error echoing the configured (opaque, unlabelled) credential must
    be scrubbed via the server's known header/env values, not just the pattern
    net — otherwise the raw secret is injected into the model turn."""
    monkeypatch.setenv("MCP_OPAQUE_CRED", _OPAQUE_CRED)

    def _echo(server, name, arguments):
        raise RuntimeError(f"MCP rejected request; credential was {_OPAQUE_CRED}")

    _patch_client(monkeypatch, prompts={"srv": [_greet_meta()]}, prompt_fn=_echo)
    server = MCPServer(
        name="srv",
        transport="http",
        url="https://mcp.example/v1",
        headers={"X-Api-Key": "${MCP_OPAQUE_CRED}"},
    )
    result = asyncio.run(load_mcp_prompts([server]))

    out = result.prompts["mcp__srv__greet"].render("Ada")  # must NOT raise
    assert _OPAQUE_CRED not in out
    assert "credential was [REDACTED]" in out  # stable, scrubbed error still shown


def test_mcp_resource_opaque_credential_is_redacted(monkeypatch):
    """A resource read error echoing the configured (opaque, unlabelled) credential
    is scrubbed via the server's known header/env values before it is raised."""
    monkeypatch.setenv("MCP_OPAQUE_CRED", _OPAQUE_CRED)

    def _echo(server, uris):
        raise RuntimeError(f"MCP rejected request; credential was {_OPAQUE_CRED}")

    _patch_client(monkeypatch, resource_fn=_echo)
    server = MCPServer(
        name="srv",
        transport="http",
        url="https://mcp.example/v1",
        headers={"X-Api-Key": "${MCP_OPAQUE_CRED}"},
    )
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - assert on message
        asyncio.run(read_mcp_resource([server], "srv", "res://x"))
    msg = str(excinfo.value)
    assert _OPAQUE_CRED not in msg
    assert "credential was [REDACTED]" in msg
    assert excinfo.value.__cause__ is None  # raw cause dropped so it can't leak


# ── Rotated credential: the OLD (build-time) value is redacted, not a re-resolve ─
# The credential VALUES scrubbed from an error must be captured at the moment the
# connection is built (what the LIVE client actually sends), never re-resolved at
# error time. If a credential rotates (env/keychain/file changes) AFTER the client
# was built, the live client still holds the OLD value — so a server can echo the
# OLD credential in an error. Re-resolving at error time yields the NEW value, which
# fails to scrub the OLD one, leaking it (prompt errors reach the model turn).

#: The credential the connection is built with (opaque: only exact-value scrubbing
#: removes it) and the value it rotates to afterwards.
_OLD_CRED = "odd!credential#42"
_NEW_CRED = "rotated!credential#77"


def _patch_rotating_resolve(monkeypatch, ref):
    """Model a credential rotation for *ref*: ``resolve(ref)`` yields ``_OLD_CRED``
    the FIRST time (the connection build) and ``_NEW_CRED`` on every later call (a
    re-resolution at error time). Any other value passes through unchanged."""
    import jarn.extensibility.mcp as mcp_mod

    calls = {"n": 0}

    def _resolve(value, *args, **kwargs):
        if value == ref:
            calls["n"] += 1
            return _OLD_CRED if calls["n"] == 1 else _NEW_CRED
        return value

    monkeypatch.setattr(mcp_mod, "resolve", _resolve)


def test_mcp_prompt_rotated_credential_old_value_is_redacted(monkeypatch):
    """A prompt error echoing the credential the LIVE client holds (the OLD value)
    must be scrubbed even after the underlying credential rotates: the value is
    captured at connection-build time, not re-resolved at fetch time."""
    _patch_rotating_resolve(monkeypatch, "${MCP_ROT_CRED}")

    def _echo_old(server, name, arguments):
        # The live client still sends the OLD credential; the server echoes it back.
        raise RuntimeError(f"MCP rejected request; credential was {_OLD_CRED}")

    _patch_client(monkeypatch, prompts={"srv": [_greet_meta()]}, prompt_fn=_echo_old)
    server = MCPServer(
        name="srv",
        transport="http",
        url="https://mcp.example/v1",
        headers={"X-Api-Key": "${MCP_ROT_CRED}"},
    )
    # Connection built here with the OLD credential; the command is registered and
    # invoked later (below), after the credential has rotated.
    result = asyncio.run(load_mcp_prompts([server]))

    out = result.prompts["mcp__srv__greet"].render("Ada")  # must NOT raise
    assert _OLD_CRED not in out  # the value the live client actually sent
    assert _NEW_CRED not in out  # the re-resolved value must not appear either
    assert "credential was [REDACTED]" in out


def test_mcp_resource_rotated_credential_old_value_is_redacted(monkeypatch):
    """A resource read error echoing the OLD (build-time) credential is scrubbed
    from the raised error even after the credential rotates — the captured value,
    not a re-resolution, drives redaction."""
    _patch_rotating_resolve(monkeypatch, "${MCP_ROT_CRED}")

    def _echo_old(server, uris):
        raise RuntimeError(f"MCP rejected request; credential was {_OLD_CRED}")

    _patch_client(monkeypatch, resource_fn=_echo_old)
    server = MCPServer(
        name="srv",
        transport="http",
        url="https://mcp.example/v1",
        headers={"X-Api-Key": "${MCP_ROT_CRED}"},
    )
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - assert on message
        asyncio.run(read_mcp_resource([server], "srv", "res://x"))
    msg = str(excinfo.value)
    assert _OLD_CRED not in msg
    assert _NEW_CRED not in msg
    assert "credential was [REDACTED]" in msg
    assert excinfo.value.__cause__ is None  # raw cause dropped so it can't leak
