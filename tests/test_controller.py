"""Controller-level tests: provider validation & status line."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

from jarn.config.schema import GitConfig, PermissionMode
from jarn.tui.controller import Controller


def _controller(tmp_path, monkeypatch, base_config):
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    return Controller(base_config, root)


def test_validate_ok_when_model_builds(tmp_path, monkeypatch, base_config):
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    fake = GenericFakeChatModel(messages=iter([]))
    with patch("jarn.providers.models.ModelFactory.build", return_value=fake):
        ok, msg = ctrl.validate()
    assert ok and ctrl.health == "ok"
    assert "●" in ctrl.status_line
    ctrl.close()


def test_validate_error_on_missing_key(tmp_path, monkeypatch, base_config):
    base_config.providers["openrouter"].api_key = "${DEFINITELY_UNSET_XYZ}"
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    ok, msg = ctrl.validate()
    assert not ok and ctrl.health == "error"
    assert "✗" in ctrl.status_line
    assert ctrl.last_error
    ctrl.close()


def test_error_status_line_shows_doctor_hint(tmp_path, monkeypatch, base_config):
    """When health is error the status line must show /doctor as an actionable pointer."""
    base_config.providers["openrouter"].api_key = "${DEFINITELY_UNSET_XYZ}"
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    ctrl.validate()
    assert ctrl.health == "error"
    line = ctrl.status_line
    assert "/doctor" in line, f"expected /doctor in status_line, got: {line!r}"
    ctrl.close()


def test_sandbox_command_mentions_fail_closed(tmp_path, monkeypatch, base_config):
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    result = ctrl.handle_command("sandbox", "on")
    assert "allow_local_fallback" in result.text
    assert "fails closed" in result.text.lower()
    ctrl.close()


def test_help_text_has_no_stale_features(tmp_path, monkeypatch, base_config):
    """/help must not advertise removed features (full-screen TUI leftovers)."""
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    text = ctrl.handle_command("help", "").text
    for stale in ("Ctrl+T", "/mouse", "PageUp", "PageDown", "side panel"):
        assert stale not in text, f"/help still mentions removed feature: {stale}"
    ctrl.close()


def test_help_renders_through_rich(tmp_path, monkeypatch, base_config):
    """Usage hints like [/ref] must not break Rich markup in /help."""
    from io import StringIO

    from rich.console import Console

    from jarn.extensibility.commands import format_help

    ctrl = _controller(tmp_path, monkeypatch, base_config)
    buf = StringIO()
    # highlight=False: we're checking that our explicit Rich markup (and the
    # escaped [/ref] usage hint) renders without breaking — not the repr
    # highlighter, which cosmetically splits tokens like "/model" on the slash.
    console = Console(file=buf, force_terminal=True, width=100, highlight=False)
    console.print(ctrl.handle_command("help", "").text)
    console.print(format_help())
    assert "/model" in buf.getvalue()
    assert "[/ref]" in buf.getvalue() or "/ref" in buf.getvalue()
    ctrl.close()


def test_all_builtin_command_outputs_render_through_rich(
    tmp_path, monkeypatch, base_config
):
    """Every built-in slash command output must be valid Rich markup."""
    from io import StringIO

    from rich.console import Console

    from jarn.extensibility.commands import BUILTINS

    ctrl = _controller(tmp_path, monkeypatch, base_config)
    buf = StringIO()
    console = Console(file=buf, force_terminal=True, width=100)

    # Seed data that often contains brackets — common markup foot-guns.
    ctrl.tracker.record("openrouter/test", input_tokens=10, output_tokens=5)
    (tmp_path / "proj" / ".jarn" / "skills").mkdir(parents=True, exist_ok=True)
    (tmp_path / "proj" / ".jarn" / "skills" / "x.md").write_text(
        "---\nname: bracket-skill\ndescription: uses [tags]\ntrigger: manual\n---\nbody",
        encoding="utf-8",
    )
    ctrl.sessions.touch("thread-1", title="Session [draft]", when=1.0)

    args_by_name = {
        "memory": "search [brackets]",
        "sandbox": "off",
        "mode": "ask",
        "model": "openrouter/anthropic/claude-opus-4-8",
    }
    for cmd in BUILTINS:
        if cmd.name == "quit":
            continue
        args = args_by_name.get(cmd.name, "")
        result = ctrl.handle_command(cmd.name, args)
        console.print(result.text)

    ctrl.close()


def test_cmd_cost_shows_top_burners(tmp_path, monkeypatch, base_config):
    """/cost gains a per-tool 'top burners' section ranked by cost."""
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    ctrl.tracker.record("claude-opus-4-8", 2_000_000, 0, tool="execute")    # $10
    ctrl.tracker.record("claude-opus-4-8", 1_000_000, 0, tool="web_fetch")  # $5
    ctrl.tracker.record("claude-opus-4-8", 10, 5)  # plain reply

    text = ctrl.handle_command("cost", "").text
    assert "top burners" in text
    # Ranked by cost: the costliest tool appears before the cheaper one.
    assert text.index("execute") < text.index("web_fetch")
    assert "$10.0000" in text
    ctrl.close()


def test_record_turn_emits_numeric_event(tmp_path, monkeypatch, base_config):
    """record_turn writes a 'turn' event with numeric props (telemetry on)."""
    import json

    base_config.observability.telemetry = True
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    assert ctrl.telemetry.enabled
    ctrl.tracker.context_tokens = 1234
    ctrl.tracker.total.input_tokens = 800
    ctrl.tracker.total.output_tokens = 200
    ctrl.tracker.total.cost_usd = 0.05
    ctrl.tracker.total.calls = 3

    ctrl.record_turn(when=42.0)
    ctrl.telemetry.flush()

    row = json.loads(ctrl.telemetry.sink_path.read_text().splitlines()[0])
    assert row["event"] == "turn"
    assert row["context_tokens"] == 1234
    assert row["total_tokens"] == 1000
    assert row["cost_cents"] == 5.0
    assert row["calls"] == 3
    ctrl.close()


def test_record_turn_noop_when_disabled(tmp_path, monkeypatch, base_config):
    ctrl = _controller(tmp_path, monkeypatch, base_config)  # telemetry off by default
    assert not ctrl.telemetry.enabled
    ctrl.record_turn(when=1.0)
    ctrl.telemetry.flush()
    assert not ctrl.telemetry.sink_path.exists()
    ctrl.close()


@pytest.mark.asyncio
async def test_compact_records_summarizer_usage(tmp_path, monkeypatch, base_config):
    """compact() bills the summarizer call to the summarizer's model ref."""
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage, HumanMessage

    ctrl = _controller(tmp_path, monkeypatch, base_config)
    base_config.routing.summarizer = "openrouter/anthropic/claude-haiku-4-5"

    class _Summarizer:
        async def ainvoke(self, prompt):
            msg = AIMessage(content="SUMMARY: did X.")
            msg.usage_metadata = {"input_tokens": 500, "output_tokens": 120}
            return msg

    class _Agent:
        def __init__(self):
            self.updated = None

        async def aget_state(self, config):
            return SimpleNamespace(values={
                "messages": [HumanMessage(content="hi"), AIMessage(content="hello")]
            })

        async def aupdate_state(self, config, values):
            self.updated = values

    ctrl.runtime = SimpleNamespace(
        agent=_Agent(),
        factory=SimpleNamespace(
            build_summarizer=lambda: _Summarizer(),
            build_main=lambda: _Summarizer(),
        ),
        main_model_ref="openrouter/anthropic/claude-opus-4-8",
    )

    summary = await ctrl.compact()
    assert "SUMMARY" in summary
    per = ctrl.tracker.per_model
    assert "openrouter/anthropic/claude-haiku-4-5" in per
    bucket = per["openrouter/anthropic/claude-haiku-4-5"]
    assert bucket.input_tokens == 500 and bucket.output_tokens == 120
    assert bucket.calls == 1
    ctrl.close()


@pytest.mark.asyncio
async def test_compact_no_usage_metadata_records_nothing(tmp_path, monkeypatch, base_config):
    """A summarizer that returns no usage_metadata must not be recorded."""
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage, HumanMessage

    ctrl = _controller(tmp_path, monkeypatch, base_config)

    class _Summarizer:
        async def ainvoke(self, prompt):
            return AIMessage(content="SUMMARY: did X.")  # no usage_metadata

    class _Agent:
        async def aget_state(self, config):
            return SimpleNamespace(values={"messages": [HumanMessage(content="hi")]})

        async def aupdate_state(self, config, values):
            pass

    ctrl.runtime = SimpleNamespace(
        agent=_Agent(),
        factory=SimpleNamespace(
            build_summarizer=lambda: _Summarizer(),
            build_main=lambda: _Summarizer(),
        ),
        main_model_ref="openrouter/anthropic/claude-opus-4-8",
    )
    await ctrl.compact()
    assert ctrl.tracker.per_model == {}
    ctrl.close()


def test_should_auto_compact_threshold(tmp_path, monkeypatch, base_config):
    """Triggers only when enabled AND the context gauge crosses the threshold."""
    base_config.context.auto_compact = True
    base_config.context.compact_at_pct = 85
    ctrl = _controller(tmp_path, monkeypatch, base_config)

    monkeypatch.setattr(ctrl, "context_status", lambda: (80, 100, 0.80))
    assert ctrl.should_auto_compact() is False          # below threshold

    monkeypatch.setattr(ctrl, "context_status", lambda: (86, 100, 0.86))
    assert ctrl.should_auto_compact() is True           # at/over threshold

    monkeypatch.setattr(ctrl, "context_status", lambda: None)
    assert ctrl.should_auto_compact() is False          # unknown context

    monkeypatch.setattr(ctrl, "context_status", lambda: (90, 100, 0.90))
    ctrl.config.context.auto_compact = False
    assert ctrl.should_auto_compact() is False          # disabled
    ctrl.close()


def test_enrich_turn_input_injects_recall_and_skips_untrusted_project(
    tmp_path, monkeypatch, base_config
):
    from jarn.memory import Memory, MemoryStore

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    project = MemoryStore.project_store(root)
    assert project is not None
    project.save(
        Memory(
            name="project-db",
            description="project uses neon postgres",
            body="Neon Postgres is the database.",
            type="project",
        )
    )

    trusted = Controller(base_config, root, project_trusted=True)
    enriched = trusted.enrich_turn_input("which postgres database?")
    assert "# Relevant memories" in enriched
    assert "project-db" in enriched
    trusted.close()

    untrusted = Controller(base_config, root, project_trusted=False)
    assert untrusted.enrich_turn_input("which postgres database?") == "which postgres database?"
    assert "trusted" in untrusted.handle_command("memory", "add project project x y").text
    untrusted.close()


def test_memory_commands_crud_project_scope(tmp_path, monkeypatch, base_config):
    ctrl = _controller(tmp_path, monkeypatch, base_config)

    add = ctrl.handle_command(
        "memory",
        'add project project "test-style" "Use pytest" "Prefer parametrized tests."',
    )
    assert "Saved project memory" in add.text

    listed = ctrl.handle_command("memory", "").text
    assert "test-style" in listed

    search = ctrl.handle_command("memory", "search parametrized").text
    assert "test-style" in search
    assert "Prefer parametrized tests." in search

    show = ctrl.handle_command("memory", "show project test-style").text
    assert "Use pytest" in show
    assert "Prefer parametrized tests." in show

    update = ctrl.handle_command(
        "memory",
        'update project test-style "Use pytest fixtures" "Prefer fixtures."',
    )
    assert "Updated project memory" in update.text
    assert "Use pytest fixtures" in ctrl.handle_command("memory", "show test-style").text

    delete = ctrl.handle_command("memory", "delete project test-style")
    assert "Deleted project memory" in delete.text
    assert "test-style" not in ctrl.handle_command("memory", "").text
    ctrl.close()


def test_memory_search_dedupes_global_and_project(tmp_path, monkeypatch, base_config):
    from jarn.memory import Memory, MemoryStore

    ctrl = _controller(tmp_path, monkeypatch, base_config)
    MemoryStore.global_store().save(
        Memory(
            name="prefers-pytest",
            description="global pytest",
            body="Use pytest globally.",
            type="user",
        )
    )
    project = MemoryStore.project_store(ctrl.project_root)
    assert project is not None
    project.save(
        Memory(
            name="prefers-pytest",
            description="project pytest",
            body="Use pytest in this project.",
            type="project",
        )
    )

    result = ctrl.handle_command("memory", "search pytest").text

    assert result.count("prefers-pytest") == 1
    ctrl.close()


# --- MCP health wiring through ensure_runtime ----------------------------


def _stub_runtime_build(monkeypatch, mcp_result):
    """Patch ensure_runtime's heavy deps so it only exercises MCP wiring.

    Returns the list of extra_tools build_runtime was handed (mutated in place).
    """
    import jarn.tui.controller as controller_mod
    from jarn.extensibility.mcp import MCPLoadResult

    seen = {}

    async def _fake_loader(servers):
        assert isinstance(mcp_result, MCPLoadResult)
        return mcp_result

    async def _fake_checkpointer(db_path):
        return object(), None

    def _fake_build_runtime(
        config, *, project_root, project_trusted=True, checkpointer, extra_tools,
        system_prompt_override=None,
    ):
        seen["extra_tools"] = extra_tools
        seen["project_trusted"] = project_trusted
        from types import SimpleNamespace

        return SimpleNamespace(agent=object(), main_model_ref="m", warnings=())

    monkeypatch.setattr(controller_mod, "load_mcp_tools", _fake_loader)
    monkeypatch.setattr(controller_mod, "create_async_checkpointer", _fake_checkpointer)
    monkeypatch.setattr(controller_mod, "build_runtime", _fake_build_runtime)
    return seen


@pytest.mark.asyncio
async def test_ensure_runtime_degraded_on_partial_mcp_failure(
    tmp_path, monkeypatch, base_config
):
    """A failing MCP server degrades the session and names it in last_error,
    while the healthy server's tools still reach build_runtime."""
    from jarn.config.schema import MCPServer
    from jarn.extensibility.mcp import MCPLoadResult

    base_config.mcp_servers = [
        MCPServer(name="good", command="x"),
        MCPServer(name="bad", command="y"),
    ]
    result = MCPLoadResult(
        tools=["good_tool"],
        health={"good": "ok", "bad": "error"},
        errors={"bad": "boom"},
    )
    seen = _stub_runtime_build(monkeypatch, result)
    ctrl = _controller(tmp_path, monkeypatch, base_config)

    await ctrl.ensure_runtime()

    assert ctrl.health == "degraded"
    assert ctrl.last_error is not None and "bad" in ctrl.last_error
    assert ctrl.mcp_health == {"good": "ok", "bad": "error"}
    assert ctrl.mcp_errors == {"bad": "boom"}
    assert seen["extra_tools"] == ["good_tool"]  # flat tool list, healthy only
    # Per-server health mirrored onto the config entries.
    by_name = {s.name: s.health for s in ctrl.config.mcp_servers}
    assert by_name == {"good": "ok", "bad": "error"}
    ctrl.close()


@pytest.mark.asyncio
async def test_ensure_runtime_stays_healthy_when_all_mcp_ok(
    tmp_path, monkeypatch, base_config
):
    """All-ok MCP load leaves health unchanged (not degraded) and no last_error."""
    from jarn.config.schema import MCPServer
    from jarn.extensibility.mcp import MCPLoadResult

    base_config.mcp_servers = [MCPServer(name="a", command="x")]
    result = MCPLoadResult(tools=["a_tool"], health={"a": "ok"}, errors={})
    seen = _stub_runtime_build(monkeypatch, result)
    ctrl = _controller(tmp_path, monkeypatch, base_config)

    await ctrl.ensure_runtime()

    assert ctrl.health != "degraded"
    assert ctrl.last_error is None
    assert ctrl.mcp_health == {"a": "ok"}
    assert ctrl.mcp_errors == {}
    assert seen["extra_tools"] == ["a_tool"]
    ctrl.close()


@pytest.mark.asyncio
async def test_ensure_runtime_errors_on_ambient_key_leak(
    tmp_path, monkeypatch, base_config
):
    """Ambient key leak to a non-local async subagent fails closed at build time."""
    import jarn.tui.controller as controller_mod
    from jarn.agent.builder import AmbientKeyLeakError
    from jarn.extensibility.mcp import MCPLoadResult

    _stub_runtime_build(monkeypatch, MCPLoadResult(tools=[], health={}, errors={}))

    def _leak_build(
        config, *, project_root, project_trusted, checkpointer, extra_tools,
        system_prompt_override=None,
    ):
        raise AmbientKeyLeakError(
            ["ambient LANGGRAPH_API_KEY would leak to https://evil.example.com/x"]
        )

    monkeypatch.setattr(controller_mod, "build_runtime", _leak_build)
    ctrl = _controller(tmp_path, monkeypatch, base_config)

    with pytest.raises(AmbientKeyLeakError):
        await ctrl.ensure_runtime()

    assert ctrl.health == "error"
    assert ctrl.last_error is not None and "evil.example.com" in ctrl.last_error
    ctrl.close()


# ---------------------------------------------------------------------------
# M4: /mcp status
# ---------------------------------------------------------------------------


def test_cmd_mcp_no_servers(tmp_path, monkeypatch, base_config):
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    out = ctrl.handle_command("mcp", "").text
    assert "No MCP servers configured." in out
    ctrl.close()


def test_cmd_mcp_lists_servers_with_health_and_error(tmp_path, monkeypatch, base_config):
    from jarn.config.schema import MCPServer

    base_config.mcp_servers = [
        MCPServer(name="docs", transport="stdio"),
        MCPServer(name="search", transport="http"),
    ]
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    # Simulate post-ensure_runtime health population.
    ctrl.mcp_health = {"docs": "ok", "search": "error"}
    ctrl.mcp_errors = {"search": "connection refused"}

    out = ctrl.handle_command("mcp", "status").text
    assert "docs" in out and "search" in out
    assert "ok" in out
    assert "error" in out
    assert "connection refused" in out
    ctrl.close()


def test_cmd_mcp_falls_back_to_server_health_field(tmp_path, monkeypatch, base_config):
    from jarn.config.schema import MCPServer

    base_config.mcp_servers = [MCPServer(name="docs", transport="stdio", health="ok")]
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    out = ctrl.handle_command("mcp", "").text
    assert "docs" in out and "ok" in out
    ctrl.close()


# ---------------------------------------------------------------------------
# M4: /trust + untrusted floor
# ---------------------------------------------------------------------------


def test_untrusted_controller_starts_clamped(tmp_path, monkeypatch, base_config):
    """An untrusted controller cannot be loosened past the plan floor."""
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    ctrl = Controller(base_config, root, project_trusted=False)
    # Any attempt to escalate clamps to plan.
    assert ctrl.apply_mode("yolo") == "plan"
    assert ctrl.config.permission_mode.value == "plan"
    out = ctrl.handle_command("mode", "auto-edit")
    assert "untrusted" in out.text.lower()
    assert ctrl.config.permission_mode.value == "plan"
    ctrl.close()


def test_cmd_trust_lifts_floor_and_rebuilds(tmp_path, monkeypatch, base_config):
    """/trust trusts the project, reloads config, and restores the CONFIGURED
    mode from disk (not just lifts the flag) — the launch floor overwrote it."""
    import yaml

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("JARN_HOME", str(home))
    # The on-disk configured mode is ask. (The launch-time untrusted floor would
    # have overwritten the in-memory mode with plan — we simulate that below.)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"permission_mode": "ask"}), encoding="utf-8"
    )
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    # Simulate the launch clamp: the in-memory config is already floored to plan.
    base_config.permission_mode = PermissionMode.PLAN
    ctrl = Controller(base_config, root, project_trusted=False)
    assert ctrl.config.permission_mode == PermissionMode.PLAN
    ctrl.runtime = object()  # pretend a runtime exists so we can see it cleared

    result = ctrl.handle_command("trust", "")
    assert result.rebuilt is True
    assert ctrl.project_trusted is True
    assert ctrl.runtime is None  # rebuild forced
    # /trust ITSELF restored the configured mode from disk (no extra apply_mode).
    assert ctrl.config.permission_mode == PermissionMode.ASK
    assert ctrl.engine.mode == PermissionMode.ASK
    # And the floor is gone — escalation now sticks.
    assert ctrl.apply_mode("yolo") == "yolo"

    # Trust is persisted: a fresh TrustStore sees the root.
    from jarn.config.trust import TrustStore

    assert root.resolve() in {Path(r) for r in TrustStore.load().entries()}
    ctrl.close()


def test_cmd_trust_already_trusted(tmp_path, monkeypatch, base_config):
    ctrl = _controller(tmp_path, monkeypatch, base_config)  # trusted by default
    out = ctrl.handle_command("trust", "")
    assert "already trusted" in out.text.lower()
    assert out.rebuilt is False
    ctrl.close()


def test_cmd_doctor_shows_provider_and_mode(tmp_path, monkeypatch, base_config):
    """/doctor returns the same checks as jarn doctor: key state, mode, profile."""
    from io import StringIO

    from rich.console import Console

    ctrl = _controller(tmp_path, monkeypatch, base_config)
    result = ctrl.handle_command("doctor", "")

    # Must not flag rebuilt/quit/clear_screen — it's a read-only diagnostic.
    assert result.rebuilt is False
    assert result.quit is False
    assert result.clear_screen is False

    # The output must mention the permission mode the config has.
    assert "ask" in result.text.lower()

    # Provider key state must appear.
    assert "key ok" in result.text.lower() or "openrouter" in result.text.lower()

    # The output must be valid Rich markup (no markup exceptions).
    buf = StringIO()
    console = Console(file=buf, force_terminal=True, width=120)
    console.print(result.text)
    rendered = buf.getvalue()
    assert "jarn doctor" in rendered
    ctrl.close()


def test_cmd_doctor_bad_key_shows_warning(tmp_path, monkeypatch, base_config):
    """/doctor surfaces key errors for providers with invalid keys."""
    from io import StringIO

    from rich.console import Console

    base_config.providers["openrouter"].api_key = "${DEFINITELY_UNSET_XYZ}"
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    result = ctrl.handle_command("doctor", "")
    rendered = StringIO()
    console = Console(file=rendered, force_terminal=True, width=120)
    console.print(result.text)
    output = rendered.getvalue()
    # Should surface a key resolution failure (not "key ok")
    assert "key ok" not in output.lower() or "DEFINITELY_UNSET_XYZ" in output
    ctrl.close()


def test_cmd_doctor_renders_same_data_as_cli(tmp_path, monkeypatch, base_config):
    """/doctor renders the same diagnostic blocks as `jarn doctor`, incl. Extensions."""
    from io import StringIO

    from rich.console import Console

    ctrl = _controller(tmp_path, monkeypatch, base_config)
    result = ctrl.handle_command("doctor", "")

    rendered = StringIO()
    console = Console(file=rendered, force_terminal=True, width=120)
    console.print(result.text)
    output = rendered.getvalue()

    # The same section headers the CLI _cmd_doctor renders must be present so
    # both surfaces show the same data (criterion 1 + the Extensions fidelity gap).
    for header in ("Providers", "Main model build", "Extensions"):
        assert header in output, f"/doctor output missing {header!r} block"
    # Extensions summary counts line is rendered.
    assert "skills" in output and "mcp" in output
    ctrl.close()


# ---------------------------------------------------------------------------
# P3.B — No-checkpoint UX: /undo and /redo with autocheckpoint disabled
# ---------------------------------------------------------------------------

def test_undo_disabled_gives_actionable_message(tmp_path, monkeypatch, base_config):
    """/undo while autocheckpoint is off must name how to enable it."""
    # base_config has git.autocheckpoint = False (the default)
    assert not base_config.git.autocheckpoint
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    result = ctrl.handle_command("undo", "")
    text = result.text
    # Must explain checkpoints are unavailable and name the fix.
    assert "autocheckpoint" in text.lower()
    assert "/config" in text
    ctrl.close()


def test_redo_disabled_gives_actionable_message(tmp_path, monkeypatch, base_config):
    """/redo while autocheckpoint is off must name how to enable it."""
    assert not base_config.git.autocheckpoint
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    result = ctrl.handle_command("redo", "")
    text = result.text
    assert "autocheckpoint" in text.lower()
    assert "/config" in text
    ctrl.close()


def _git(args: list[str], cwd: Path) -> None:
    """Run git in cwd; raises on non-zero exit."""
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def _repo_with_commit(tmp_path: Path) -> Path:
    """Return a fresh git repo with one commit."""
    root = tmp_path / "gitrepo"
    root.mkdir()
    _git(["init", "-b", "main"], cwd=root)
    _git(["config", "user.email", "test@jarn.test"], cwd=root)
    _git(["config", "user.name", "Jarn Test"], cwd=root)
    (root / "README.txt").write_text("init\n", encoding="utf-8")
    _git(["add", "README.txt"], cwd=root)
    _git(["commit", "-m", "init"], cwd=root)
    return root


def test_undo_enabled_normal_behavior(tmp_path, monkeypatch, base_config):
    """/undo with autocheckpoint ON and a snapshot present must succeed."""
    base_config.git = GitConfig(autocheckpoint=True)
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))

    root = _repo_with_commit(tmp_path)
    (root / ".jarn").mkdir(parents=True, exist_ok=True)
    ctrl = Controller(base_config, root)

    # Seed a snapshot so the undo stack is non-empty.
    (root / "file.txt").write_text("before\n", encoding="utf-8")
    ctrl.checkpoint_manager.snapshot("test-turn")

    # Modify the file — the undo should restore it.
    (root / "file.txt").write_text("after\n", encoding="utf-8")

    result = ctrl.handle_command("undo", "")
    assert result.text.lower().startswith("undone"), (
        f"Expected 'Undone. ...' but got: {result.text!r}"
    )
    assert (root / "file.txt").read_text(encoding="utf-8") == "before\n"
    ctrl.close()


def test_redo_enabled_normal_behavior(tmp_path, monkeypatch, base_config):
    """/redo with autocheckpoint ON and a redo point present must succeed."""
    base_config.git = GitConfig(autocheckpoint=True)
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))

    root = _repo_with_commit(tmp_path)
    (root / ".jarn").mkdir(parents=True, exist_ok=True)
    ctrl = Controller(base_config, root)

    # Snapshot, modify, then undo to create a redo point.
    (root / "file.txt").write_text("before\n", encoding="utf-8")
    ctrl.checkpoint_manager.snapshot("test-turn")
    (root / "file.txt").write_text("after\n", encoding="utf-8")
    ctrl.checkpoint_manager.undo()

    result = ctrl.handle_command("redo", "")
    assert result.text.lower().startswith("redone"), (
        f"Expected 'Redone. ...' but got: {result.text!r}"
    )
    assert (root / "file.txt").read_text(encoding="utf-8") == "after\n"
    ctrl.close()


# ---------------------------------------------------------------------------
# P4.C — /abort = cancel + roll back the turn (rollback half, controller side)
# ---------------------------------------------------------------------------


def test_abort_rollback_disabled_explains_autocheckpoint(tmp_path, monkeypatch, base_config):
    """abort_rollback() with autocheckpoint off cancels-only and names the fix."""
    # base_config has git.autocheckpoint = False (the default).
    assert not base_config.git.autocheckpoint
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    msg = ctrl.abort_rollback()
    # Turn is still reported cancelled, but rollback is unavailable + actionable.
    assert "cancel" in msg.lower()
    assert "autocheckpoint" in msg.lower()
    assert "/config" in msg
    ctrl.close()


def test_abort_rollback_enabled_reverts_turn_edits(tmp_path, monkeypatch, base_config):
    """abort_rollback() with a turn-start checkpoint reverts that turn's edits."""
    base_config.git = GitConfig(autocheckpoint=True)
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))

    root = _repo_with_commit(tmp_path)
    (root / ".jarn").mkdir(parents=True, exist_ok=True)
    ctrl = Controller(base_config, root)

    # Snapshot at the turn's start (what session.py does before the agent edits).
    (root / "file.txt").write_text("before\n", encoding="utf-8")
    ctrl.checkpoint_manager.snapshot("test-turn")

    # The agent edits a file mid-turn; /abort must revert it.
    (root / "file.txt").write_text("after\n", encoding="utf-8")

    msg = ctrl.abort_rollback()
    assert "rolled back" in msg.lower()
    assert (root / "file.txt").read_text(encoding="utf-8") == "before\n"
    ctrl.close()


def test_autocheckpoint_off_hint_shown_once(tmp_path, monkeypatch, base_config):
    """autocheckpoint_off_hint() emits the hint the first time and None thereafter."""
    assert not base_config.git.autocheckpoint
    ctrl = _controller(tmp_path, monkeypatch, base_config)

    first = ctrl.autocheckpoint_off_hint()
    assert first is not None
    assert "autocheckpoint" in first.lower()
    assert "/config" in first

    # Subsequent calls must be silent.
    assert ctrl.autocheckpoint_off_hint() is None
    assert ctrl.autocheckpoint_off_hint() is None
    ctrl.close()


def test_autocheckpoint_off_hint_silent_when_enabled(tmp_path, monkeypatch, base_config):
    """autocheckpoint_off_hint() returns None when autocheckpoint is on."""
    base_config.git = GitConfig(autocheckpoint=True)
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj2"
    (root / ".jarn").mkdir(parents=True)
    ctrl = Controller(base_config, root)
    assert ctrl.autocheckpoint_off_hint() is None
    ctrl.close()


# ---------------------------------------------------------------------------
# P3.C — peek_next_mode (yolo transition guard helper)
# ---------------------------------------------------------------------------


def test_peek_next_mode_returns_next_without_applying(tmp_path, monkeypatch, base_config):
    """peek_next_mode() returns the would-be next mode without changing the current mode."""
    base_config.permission_mode = PermissionMode.AUTO_EDIT
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    assert ctrl.config.permission_mode.value == "auto-edit"
    nxt = ctrl.peek_next_mode()
    # auto-edit → yolo is the next in the cycle
    assert nxt == "yolo"
    # mode must NOT have changed
    assert ctrl.config.permission_mode.value == "auto-edit"
    ctrl.close()


def test_peek_next_mode_wraps_from_yolo_to_plan(tmp_path, monkeypatch, base_config):
    """After yolo, peek_next_mode() returns plan (wrap-around)."""
    base_config.permission_mode = PermissionMode.YOLO
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    assert ctrl.peek_next_mode() == "plan"
    # still yolo
    assert ctrl.config.permission_mode.value == "yolo"
    ctrl.close()


def test_discover_models_queries_local_ollama_provider(tmp_path, monkeypatch, base_config):
    """discover_models() probes the configured Ollama endpoint and qualifies refs."""
    ctrl = _controller(tmp_path, monkeypatch, base_config)
    urls = []

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"models": [{"name": "qwen3-coder:30b"}, {"name": "llama3:8b"}]}

    def _get(url, *a, **k):
        urls.append(url)
        return _Resp()

    with patch("httpx.get", _get):
        out = ctrl.discover_models()
    # Returned refs are qualified under the provider profile name ("ollama").
    assert ("ollama/qwen3-coder:30b", "ollama") in out
    assert ("ollama/llama3:8b", "ollama") in out
    # Only the local (ollama) provider was probed, not the cloud openrouter one.
    assert urls == ["http://localhost:11434/api/tags"]
    ctrl.close()


def test_discover_models_empty_when_endpoint_unreachable(tmp_path, monkeypatch, base_config):
    """Unreachable endpoint -> [] so the caller falls back to manual entry."""
    import httpx

    ctrl = _controller(tmp_path, monkeypatch, base_config)

    def _boom(*a, **k):
        raise httpx.ConnectError("no endpoint")

    with patch("httpx.get", _boom):
        assert ctrl.discover_models() == []
    ctrl.close()


def test_main_context_window_queries_local_once_and_caches(tmp_path, monkeypatch):
    """A local model's context window (not in the curated table) is fetched from
    its endpoint once and cached, so the toolbar gauge can show a real %."""
    from jarn.config.schema import Config, ProviderConfig, ProviderType, RoutingConfig

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="lmstudio",
        providers={"lmstudio": ProviderConfig(
            type=ProviderType.LMSTUDIO, base_url="http://localhost:1234/v1")},
        routing=RoutingConfig(main="lmstudio/mystery-local-7b"),
    )
    ctrl = Controller(cfg, root)
    calls: list[str] = []

    def _fake(provider, model_id):
        calls.append(model_id)
        return 8192

    monkeypatch.setattr("jarn.providers.remote_context_window", _fake)
    assert ctrl._main_context_window() == 8192
    assert ctrl._main_context_window() == 8192       # served from cache
    assert calls == ["mystery-local-7b"]             # endpoint queried exactly once
    ctrl.close()
