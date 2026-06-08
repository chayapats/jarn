"""Extensibility loader tests — skills, commands, subagents, hooks."""

from __future__ import annotations

import pytest

from jarn.config.schema import HookSpec, MCPServer
from jarn.extensibility.commands import load_commands, parse_input
from jarn.extensibility.hooks import HookEvent, HookRunner
from jarn.extensibility.mcp import MCPLoadResult, load_mcp_tools
from jarn.extensibility.skills import auto_skill_catalog, load_skills
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
    (cdir / "review.md").write_text(
        "---\ndescription: review diff\n---\nReview this: $ARGS", encoding="utf-8"
    )
    cmds = load_commands(project_dir)
    assert "review" in cmds
    assert cmds["review"].render("the auth module") == "Review this: the auth module"


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
    from jarn.extensibility.commands import BUILTINS
    from jarn.tui.controller import Controller

    repl_handled = {"compact", "expand", "resume", "model", "mode", "queue"}
    for cmd in BUILTINS:
        name = cmd.name
        has_handler = hasattr(Controller, f"_cmd_{name.replace('-', '_')}")
        assert (
            has_handler or name in repl_handled or cmd.route == "repl"
        ), f"/{name} advertised but unhandled"


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


# --- MCP per-server lifecycle / health isolation -------------------------


class _FakeMCPClient:
    """Stand-in for MultiServerMCPClient: per-server get_tools that can fail.

    ``fail`` maps a server name to the exception it should raise; any other
    server yields a single sentinel tool named ``<server>_tool``.
    """

    def __init__(self, connections, fail=None):
        self.connections = connections
        self._fail = fail or {}

    async def get_tools(self, *, server_name=None):
        assert server_name is not None  # we always load per-server in isolation
        if server_name in self._fail:
            raise self._fail[server_name]
        return [f"{server_name}_tool"]


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
    assert result.tools == ["good_tool"]  # bad server's tools lost, good kept
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

    assert sorted(result.tools) == ["a_tool", "b_tool"]
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

    assert result.tools == ["ok_tool"]
    assert result.health["nocmd"] == "error"
    assert "nocmd" in result.errors
    assert result.health["ok"] == "ok"
