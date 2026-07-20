"""Extensibility loader tests — skills, commands, subagents, hooks."""

from __future__ import annotations

import pytest

from jarn.config.schema import HookSpec, MCPServer
from jarn.extensibility.commands import load_commands, parse_input
from jarn.extensibility.hooks import HookEvent, HookRunner
from jarn.extensibility.mcp import MCPLoadResult, load_mcp_tools
from jarn.extensibility.skills import (
    Skill,
    auto_skill_catalog,
    find_skill,
    load_skills,
    render_skill_invocation,
)
from jarn.extensibility.subagents import load_subagents


def _skill(dirpath, name, trigger="auto"):
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: does {name}\ntrigger: {trigger}\n---\n"
        f"Instructions for {name}.",
        encoding="utf-8",
    )


def test_load_skills_and_project_override(monkeypatch, tmp_path, project_dir):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _skill(tmp_path / "home" / "skills", "shared")
    _skill(project_dir / ".jarn" / "skills", "shared")  # overrides global
    _skill(project_dir / ".jarn" / "skills", "local")
    skills = load_skills(project_dir)
    assert set(skills) == {"shared", "local"}
    assert skills["shared"].scope == "project"


def test_manual_skill_excluded_from_auto_catalog(monkeypatch, tmp_path, project_dir):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    _skill(project_dir / ".jarn" / "skills", "autoskill", trigger="auto")
    _skill(project_dir / ".jarn" / "skills", "manualskill", trigger="manual")
    skills = load_skills(project_dir)
    catalog = auto_skill_catalog(skills)
    assert "autoskill" in catalog
    assert "manualskill" not in catalog
    assert skills["manualskill"].is_manual


def test_find_skill_exact_and_case_insensitive():
    skills = {
        "Deploy": Skill(name="Deploy", description="d", body="b", trigger="manual"),
        "lint": Skill(name="lint", description="l", body="b2", trigger="auto"),
    }
    assert find_skill(skills, "Deploy") is skills["Deploy"]  # exact
    assert find_skill(skills, "deploy") is skills["Deploy"]  # case-insensitive
    assert find_skill(skills, "  lint ") is skills["lint"]   # trimmed
    assert find_skill(skills, "missing") is None


def test_render_skill_invocation_includes_name_and_body():
    skill = Skill(
        name="deploy",
        description="Deploy safely",
        body="Step 1. Test.\nStep 2. Ship.",
        trigger="manual",
    )
    out = render_skill_invocation(skill)
    assert "deploy" in out
    assert "Deploy safely" in out
    assert "Step 1. Test." in out
    assert "Step 2. Ship." in out


def test_parse_input_command_vs_chat():
    p = parse_input("/mode yolo")
    assert p.is_command and p.name == "mode" and p.args == "yolo"
    assert not parse_input("hello world").is_command


def test_parse_input_shell_escape_with_space():
    p = parse_input("! git status")
    assert p.is_shell is True
    assert p.shell_command == "git status"
    assert p.is_command is False
    assert p.text == ""


def test_parse_input_shell_escape_no_space():
    p = parse_input("!ls")
    assert p.is_shell is True
    assert p.shell_command == "ls"


def test_parse_input_bare_bang_is_noop():
    p = parse_input("!")
    assert p.is_shell is True
    assert p.shell_command == ""


def test_parse_input_help_still_command():
    p = parse_input("/help")
    assert p.is_command is True and p.name == "help"
    assert p.is_shell is False


def test_parse_input_plain_chat_not_shell():
    p = parse_input("hello world")
    assert p.is_command is False and p.is_shell is False
    assert p.text == "hello world"


def test_load_custom_commands(monkeypatch, tmp_path, project_dir):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    cdir = project_dir / ".jarn" / "commands"
    cdir.mkdir(parents=True)
    (cdir / "summarize.md").write_text(
        "---\ndescription: summarize diff\n---\nSummarize this: $ARGS", encoding="utf-8"
    )
    cmds = load_commands(project_dir)
    assert "summarize" in cmds
    assert cmds["summarize"].render("the auth module") == "Summarize this: the auth module"


def test_custom_command_cannot_shadow_builtin(monkeypatch, tmp_path, project_dir):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    cdir = project_dir / ".jarn" / "commands"
    cdir.mkdir(parents=True)
    (cdir / "cost.md").write_text("---\n---\nbody", encoding="utf-8")
    cmds = load_commands(project_dir)
    assert "cost-custom" in cmds
    assert "cost" not in cmds


def test_load_subagents(monkeypatch, tmp_path, project_dir):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    adir = project_dir / ".jarn" / "agents"
    adir.mkdir(parents=True)
    (adir / "tester.md").write_text(
        "---\nname: tester\ndescription: writes tests\nmodel: openrouter/x\n---\n"
        "You write tests.",
        encoding="utf-8",
    )
    agents = load_subagents(project_dir)
    assert "tester" in agents
    spec = agents["tester"].to_spec()
    assert spec["name"] == "tester"
    assert spec["system_prompt"] == "You write tests."
    assert spec["model"] == "openrouter/x"  # passthrough when no factory


def test_every_builtin_command_is_dispatchable():
    """Every advertised built-in must have a handler — guards against orphans
    like the old /mouse entry that printed 'Unknown command'."""
    from jarn.commands.registry import COMMAND_SPECS, ui_command_names
    from jarn.controller.commands import REGISTRY

    ui_names = ui_command_names()
    for spec in COMMAND_SPECS:
        has_core = spec.name in REGISTRY
        has_ui = spec.name in ui_names
        assert has_core or has_ui, f"/{spec.name} advertised but unhandled"


class _FakeTool:
    def __init__(self, name):
        self.name = name


def test_subagent_tools_restrict_extra_tools():
    """Declaring tools: limits the subagent to that subset of extra tools."""
    from jarn.extensibility.subagents import CustomSubagent

    tools = [_FakeTool("web_search"), _FakeTool("web_fetch")]
    sa = CustomSubagent(
        name="searcher", description="d", system_prompt="p", tools=["web_search"],
    )
    spec = sa.to_spec(available_tools=tools)
    assert [t.name for t in spec["tools"]] == ["web_search"]  # web_fetch excluded


def test_subagent_no_tools_inherits_all():
    """No tools: declared → no 'tools' key, so the subagent inherits parent tools."""
    from jarn.extensibility.subagents import CustomSubagent

    sa = CustomSubagent(name="x", description="d", system_prompt="p")
    spec = sa.to_spec(available_tools=[_FakeTool("web_search")])
    assert "tools" not in spec


def test_subagent_builtin_only_gets_no_extra_tools():
    """tools: [read_file] is valid (built-in) but yields no extra/network tools."""
    from jarn.extensibility.subagents import CustomSubagent

    sa = CustomSubagent(
        name="reader", description="d", system_prompt="p", tools=["read_file"],
    )
    spec = sa.to_spec(available_tools=[_FakeTool("web_search")])
    assert spec["tools"] == []  # no web/MCP tools; fs built-ins remain via middleware


def test_subagent_unknown_tool_raises():
    """A typo'd / unknown tool name fails fast at build time."""
    from jarn.config import ConfigError
    from jarn.extensibility.subagents import CustomSubagent

    sa = CustomSubagent(
        name="oops", description="d", system_prompt="p", tools=["web_serch"],
    )
    with pytest.raises(ConfigError, match="unknown tool"):
        sa.to_spec(available_tools=[_FakeTool("web_search")])


def test_hook_runner_runs_and_reports(tmp_path):
    runner = HookRunner(
        hooks=[HookSpec(event="post_edit", command="echo edited")],
        cwd=tmp_path,
    )
    results = runner.run(HookEvent.POST_EDIT)
    assert len(results) == 1
    assert results[0].ok
    assert "edited" in results[0].stdout


def test_blocking_hook_aborts(tmp_path):
    runner = HookRunner(
        hooks=[
            HookSpec(event="pre_commit", command="exit 1", blocking=True),
            HookSpec(event="pre_commit", command="echo should-not-run"),
        ],
        cwd=tmp_path,
    )
    results = runner.run(HookEvent.PRE_COMMIT)
    assert results[0].should_abort
    assert len(results) == 1  # stopped early


def test_hook_matcher_filters(tmp_path):
    runner = HookRunner(
        hooks=[HookSpec(event="post_edit", command="echo py", matcher="*.py")],
        cwd=tmp_path,
    )
    assert len(runner.run(HookEvent.POST_EDIT, target="a.js")) == 0
    assert len(runner.run(HookEvent.POST_EDIT, target="a.py")) == 1


def test_hook_event_validation_rejects_typo(tmp_path):
    """A hook with a typo'd event name is rejected at load, not silently no-op'd."""
    import yaml

    from jarn.config import ConfigError
    from jarn.config.loader import load_config

    gp = tmp_path / "g.yaml"
    gp.write_text(
        yaml.safe_dump(
            {"hooks": [{"event": "sesion_start", "command": "echo hi"}]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="must be one of"):
        load_config(global_path=gp, project_path=None)


def test_hook_env_allowlist_hides_api_key(tmp_path, monkeypatch):
    """By default a hook subprocess does NOT see ``*_API_KEY`` env vars; only the
    minimal allowlist + declared ``extra_env`` (or ``inherit_env`` opt-in) reach it."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret-xyz")
    cmd = (
        'python -c "import os; print(os.environ.get('
        "'OPENROUTER_API_KEY', 'MISSING'))\""
    )
    runner = HookRunner(
        hooks=[HookSpec(event="post_edit", command=cmd)], cwd=tmp_path
    )

    # Default: allowlist → key is NOT inherited.
    out = runner.run(HookEvent.POST_EDIT)[0].stdout
    assert "secret-xyz" not in out
    assert "MISSING" in out

    # Declared via extra_env → explicitly passed through.
    out = runner.run(
        HookEvent.POST_EDIT, extra_env={"OPENROUTER_API_KEY": "secret-xyz"}
    )[0].stdout
    assert "secret-xyz" in out

    # Opt-in inherit_env restores the old leak-everything behavior.
    leaky = HookRunner(
        hooks=[HookSpec(event="post_edit", command=cmd)],
        cwd=tmp_path,
        inherit_env=True,
    )
    out = leaky.run(HookEvent.POST_EDIT)[0].stdout
    assert "secret-xyz" in out


# --- MCP per-server lifecycle / health isolation -------------------------


class _MCPTool:
    """Minimal stand-in for a StructuredTool: just a mutable ``.name``.

    The loader namespaces tools by mutating ``tool.name``, so the fake must
    expose one (a bare string, as the old fake returned, has no ``.name`` and
    would silently skip namespacing)."""

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return f"_MCPTool({self.name!r})"


def _tool_names(tools):
    return [t.name for t in tools]


class _FakeMCPClient:
    """Stand-in for MultiServerMCPClient: per-server get_tools that can fail.

    ``fail`` maps a server name to the exception it should raise; any other
    server yields a single sentinel tool named ``<server>_tool`` (which the
    loader then namespaces to ``mcp__<server>__<server>_tool``).
    """

    def __init__(self, connections, fail=None):
        self.connections = connections
        self._fail = fail or {}

    async def get_tools(self, *, server_name=None):
        assert server_name is not None  # we always load per-server in isolation
        if server_name in self._fail:
            raise self._fail[server_name]
        return [_MCPTool(f"{server_name}_tool")]


def _patch_client(monkeypatch, fail=None):
    """Patch the lazily-imported MultiServerMCPClient with our fake."""
    import langchain_mcp_adapters.client as client_mod

    monkeypatch.setattr(
        client_mod,
        "MultiServerMCPClient",
        lambda connections: _FakeMCPClient(connections, fail=fail),
    )


def _stdio(name):
    return MCPServer(name=name, transport="stdio", command="run-" + name)


@pytest.mark.asyncio
async def test_one_server_fails_others_still_load(monkeypatch):
    """A single failing server records an error; healthy servers keep their tools."""
    _patch_client(monkeypatch, fail={"bad": RuntimeError("boom")})
    result = await load_mcp_tools([_stdio("good"), _stdio("bad")])

    assert isinstance(result, MCPLoadResult)
    # bad server's tools lost, good kept — and namespaced with server provenance.
    assert _tool_names(result.tools) == ["mcp__good__good_tool"]
    assert result.health == {"good": "ok", "bad": "error"}
    assert "bad" in result.errors and "boom" in result.errors["bad"]
    assert result.degraded is True


@pytest.mark.asyncio
async def test_all_servers_fail_shape(monkeypatch):
    """All-fail: tools empty, every server 'error', and result is degraded."""
    _patch_client(
        monkeypatch,
        fail={"a": RuntimeError("no a"), "b": ConnectionError("no b")},
    )
    result = await load_mcp_tools([_stdio("a"), _stdio("b")])

    assert result.tools == []
    assert result.health == {"a": "error", "b": "error"}
    assert set(result.errors) == {"a", "b"}
    assert result.degraded is True


@pytest.mark.asyncio
async def test_all_servers_ok_not_degraded(monkeypatch):
    """All-ok: tools accumulate across servers, no errors, not degraded."""
    _patch_client(monkeypatch)
    result = await load_mcp_tools([_stdio("a"), _stdio("b")])

    assert sorted(_tool_names(result.tools)) == ["mcp__a__a_tool", "mcp__b__b_tool"]
    assert result.health == {"a": "ok", "b": "ok"}
    assert result.errors == {}
    assert result.degraded is False


@pytest.mark.asyncio
async def test_disabled_servers_ignored(monkeypatch):
    """Disabled servers don't appear in health and yield no tools."""
    _patch_client(monkeypatch)
    off = MCPServer(name="off", transport="stdio", command="x", enabled=False)
    result = await load_mcp_tools([off])

    assert result.tools == []
    assert result.health == {}  # disabled server not probed at all


def test_mcp_http_headers_round_trip(tmp_path):
    """HTTP MCP auth headers survive config load and to_connection()."""
    import yaml

    from jarn.config.loader import load_config
    from jarn.extensibility.mcp import to_connection

    gp = tmp_path / "g.yaml"
    gp.write_text(
        yaml.safe_dump(
            {
                "mcp_servers": [
                    {
                        "name": "remote",
                        "transport": "http",
                        "url": "https://mcp.example/v1",
                        "headers": {"Authorization": "Bearer secret"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(global_path=gp, project_path=None)
    server = cfg.mcp_servers[0]
    assert server.headers == {"Authorization": "Bearer secret"}
    conn = to_connection(server)
    assert conn["headers"] == {"Authorization": "Bearer secret"}
    assert conn["transport"] == "streamable_http"


@pytest.mark.asyncio
async def test_invalid_connection_marked_error(monkeypatch):
    """A server whose connection dict can't be built is an 'error', not a crash."""
    _patch_client(monkeypatch)
    bad = MCPServer(name="nocmd", transport="stdio", command=None)  # missing command
    result = await load_mcp_tools([bad, _stdio("ok")])

    assert _tool_names(result.tools) == ["mcp__ok__ok_tool"]
    assert result.health["nocmd"] == "error"
    assert "nocmd" in result.errors
    assert result.health["ok"] == "ok"


@pytest.mark.asyncio
async def test_mcp_timeout(monkeypatch):
    """A slow MCP server is marked error when get_tools exceeds timeout_secs."""
    import asyncio

    class _SlowClient:
        def __init__(self, connections, fail=None):
            self.connections = connections

        async def get_tools(self, *, server_name=None):
            await asyncio.sleep(5)
            return [f"{server_name}_tool"]

    import langchain_mcp_adapters.client as client_mod

    monkeypatch.setattr(
        client_mod,
        "MultiServerMCPClient",
        lambda connections: _SlowClient(connections),
    )
    server = MCPServer(
        name="slow", transport="stdio", command="run-slow", timeout_secs=0.05,
    )
    result = await load_mcp_tools([server])

    assert result.tools == []
    assert result.health["slow"] == "error"
    assert "timed out" in result.errors["slow"]


@pytest.mark.asyncio
async def test_servers_load_concurrently(monkeypatch):
    """Two slow servers load in parallel: wall time ~ one delay, not the sum."""
    import asyncio
    import time

    class _SlowClient:
        def __init__(self, connections):
            self.connections = connections

        async def get_tools(self, *, server_name=None):
            await asyncio.sleep(0.2)
            return [f"{server_name}_tool"]

    import langchain_mcp_adapters.client as client_mod

    monkeypatch.setattr(
        client_mod, "MultiServerMCPClient", lambda connections: _SlowClient(connections)
    )
    start = time.monotonic()
    result = await load_mcp_tools([_stdio("a"), _stdio("b")])
    elapsed = time.monotonic() - start

    assert result.tools == ["a_tool", "b_tool"]
    assert elapsed < 0.4  # sequential would be ~0.4s; concurrent is ~0.2s


@pytest.mark.asyncio
async def test_result_order_is_connection_order(monkeypatch):
    """Tools are ordered by client.connections iteration order, not by which
    server's handshake finishes first."""
    import asyncio

    class _SkewClient:
        """First server resolves slowly, later ones fast — completion order is
        the reverse of connection order."""

        def __init__(self, connections):
            self.connections = connections
            self._delays = {"first": 0.15, "second": 0.05, "third": 0.0}

        async def get_tools(self, *, server_name=None):
            await asyncio.sleep(self._delays.get(server_name, 0.0))
            return [f"{server_name}_tool"]

    import langchain_mcp_adapters.client as client_mod

    monkeypatch.setattr(
        client_mod, "MultiServerMCPClient", lambda connections: _SkewClient(connections)
    )
    result = await load_mcp_tools(
        [_stdio("first"), _stdio("second"), _stdio("third")]
    )

    assert result.tools == ["first_tool", "second_tool", "third_tool"]


# --- MCP tool namespacing (permission-classification provenance) ---------


def _patch_named_client(monkeypatch, tools_by_server):
    """Patch MultiServerMCPClient with a fake whose per-server tools are given by
    name (so tests can control the raw, pre-namespacing tool names)."""
    import langchain_mcp_adapters.client as client_mod

    class _NamedClient:
        def __init__(self, connections):
            self.connections = connections

        async def get_tools(self, *, server_name=None):
            return [_MCPTool(n) for n in tools_by_server.get(server_name, [])]

    monkeypatch.setattr(
        client_mod, "MultiServerMCPClient", lambda connections: _NamedClient(connections)
    )


@pytest.mark.asyncio
async def test_mcp_tools_namespaced_and_classify_as_network(monkeypatch):
    """A loaded MCP tool comes back as mcp__<server>__<tool> and classifies as
    NETWORK (not a builtin READ) so the engine gates it."""
    from jarn.agent.permissions_bridge import tool_to_action
    from jarn.permissions import ActionKind

    _patch_named_client(monkeypatch, {"srv": ["do_thing"]})
    result = await load_mcp_tools([_stdio("srv")])

    assert _tool_names(result.tools) == ["mcp__srv__do_thing"]
    action = tool_to_action("mcp__srv__do_thing", {})
    assert action.kind is ActionKind.NETWORK


@pytest.mark.asyncio
async def test_mcp_tool_named_like_builtin_is_namespaced_and_network(monkeypatch):
    """The reviewer's repro: an MCP tool literally named ``wiki_read`` must end up
    as mcp__srv__wiki_read and classify as NETWORK — never as the auto-allowed
    builtin READ that let it run without approval in plan mode."""
    from jarn.agent.permissions_bridge import tool_to_action
    from jarn.permissions import ActionKind

    _patch_named_client(monkeypatch, {"srv": ["wiki_read"]})
    result = await load_mcp_tools([_stdio("srv")])

    assert _tool_names(result.tools) == ["mcp__srv__wiki_read"]
    # As a builtin name it would be READ (auto-allowed); namespaced it is NETWORK.
    assert tool_to_action("wiki_read", {}).kind is ActionKind.READ
    assert tool_to_action("mcp__srv__wiki_read", {}).kind is ActionKind.NETWORK


@pytest.mark.asyncio
async def test_mcp_double_prefix_smuggle_is_normalized(monkeypatch):
    """A server that names its tool ``mcp__other__x`` cannot smuggle a forged
    provenance: the pre-existing prefix is collapsed before ours is applied, so it
    classifies under the REAL server, not ``other``."""
    from jarn.agent.permissions_bridge import _network_target

    _patch_named_client(monkeypatch, {"srv": ["mcp__other__x"]})
    result = await load_mcp_tools([_stdio("srv")])

    assert _tool_names(result.tools) == ["mcp__srv__x"]
    # Display target parses the provenance as the real server 'srv', not 'other'.
    assert _network_target("mcp__srv__x", {}).startswith("mcp/srv/")


def test_strip_mcp_prefix_collapses_nested():
    """Nested/double mcp__ prefixes collapse fully to the bare tool name."""
    from jarn.extensibility.mcp import _strip_mcp_prefix

    assert _strip_mcp_prefix("plain") == "plain"
    assert _strip_mcp_prefix("mcp__a__x") == "x"
    assert _strip_mcp_prefix("mcp__a__mcp__b__x") == "x"


def test_namespace_tool_uses_model_copy_when_assignment_rejected():
    """When a tool rejects direct ``name`` assignment (pydantic validate_assignment),
    the loader rebuilds it via model_copy rather than crashing."""
    from jarn.extensibility.mcp import _namespace_tool

    class _Frozen:
        def __init__(self, name):
            object.__setattr__(self, "_name", name)

        @property
        def name(self):
            return self._name

        @name.setter
        def name(self, value):
            raise AttributeError("read-only")

        def model_copy(self, *, update):
            return _Frozen(update["name"])

    out = _namespace_tool(_Frozen("wiki_read"), "srv")
    assert out.name == "mcp__srv__wiki_read"


def test_mcp_status_refresh(tmp_path, monkeypatch, base_config):
    """``/mcp refresh`` re-runs load_mcp_tools and updates health maps."""
    from jarn.controller.commands.diagnostics import cmd_mcp
    from jarn.extensibility.mcp import MCPLoadResult

    base_config.mcp_servers = [_stdio("a")]
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    from jarn.controller.core import Controller

    ctrl = Controller(base_config, tmp_path / "proj")
    calls: list[int] = []

    async def _fake_load(servers):
        calls.append(1)
        return MCPLoadResult(
            tools=["a_tool"], health={"a": "ok"}, errors={},
        )

    import jarn.controller.commands.diagnostics as diag_mod

    monkeypatch.setattr(diag_mod, "load_mcp_tools", _fake_load)
    out = cmd_mcp(ctrl, "refresh").text
    assert calls == [1]
    assert ctrl.mcp_health == {"a": "ok"}
    assert ctrl.mcp_errors == {}
    assert "a" in out and "ok" in out
    ctrl.close()
