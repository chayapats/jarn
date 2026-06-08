"""Phase 3 UX: registry, queue, toolbar, tool correlation."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from jarn.cost import BudgetStatus
from jarn.extensibility.commands import (
    BUILTINS,
    builtin_names,
    format_help,
    readme_command_rows,
    route_for,
)
from jarn.repl_renderer import TurnRenderer
from jarn.tui import palette
from jarn.tui.input_queue import InputQueue
from jarn.tui.toolbar import render_toolbar


def test_builtin_registry_routes_are_handled():
    from jarn.tui.controller import Controller

    for cmd in BUILTINS:
        if cmd.route == "controller":
            assert hasattr(Controller, f"_cmd_{cmd.name.replace('-', '_')}")
        elif cmd.route == "repl":
            assert cmd.name in {
                "compact", "expand", "resume", "model", "mode", "queue",
            }


def test_help_and_completion_use_registry():
    body = format_help()
    for name in builtin_names():
        assert f"/{name}" in body


def test_format_help_is_valid_rich_markup():
    from io import StringIO

    from rich.console import Console

    buf = StringIO()
    Console(file=buf, force_terminal=True, width=100).print(format_help())
    assert "Built-in commands" in buf.getvalue()


def test_readme_rows_cover_all_builtins():
    rows = readme_command_rows()
    assert len(rows) == len(BUILTINS)
    names = {row[0].split("`")[1].lstrip("/").split()[0] for row in rows}
    assert names == set(builtin_names())


def test_readme_commands_match_registry():
    """README built-in command table stays aligned with BUILTINS."""
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    for cmd in BUILTINS:
        assert f"/{cmd.name}" in readme
        assert cmd.description in readme, f"README missing description for /{cmd.name}"


def test_route_for_unknown():
    assert route_for("not-a-command") == "unknown"


def test_input_queue_fifo_and_ops():
    q = InputQueue()
    q.append("a", "payload-a")
    q.append("b", "payload-b")
    assert len(q) == 2
    assert q.pop_next().payload == "payload-a"
    q.append("c", "payload-c")
    assert q.move(1, 2) is True
    assert [i.display for i in q.list()] == ["c", "b"]
    removed = q.cancel(2)
    assert removed.display == "b"
    assert q.clear() == 1


def test_toolbar_shows_queue_and_collapses_narrow():
    wide = render_toolbar(
        model="openrouter/claude",
        mode="ask",
        cost_line="$0.01 · 100 tok · 1 calls",
        cost_status=BudgetStatus.OK,
        queue_count=2,
        context_frac=0.42,
        width=120,
    )
    assert "queue 2" in wide.value
    narrow = render_toolbar(
        model="openrouter/claude",
        mode="ask",
        cost_line="$0.01 · 100 tok · 1 calls",
        cost_status=BudgetStatus.OK,
        queue_count=2,
        context_frac=0.42,
        width=30,
    )
    # Model + mode survive; context or cost may drop on very narrow width.
    assert "ask" in narrow.value


def test_no_color_plain_toolbar(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    palette.configure_ui(theme="dark", accent="cyan")
    style = palette.toolbar_style_dict()
    assert "bg:" not in style.get("bottom-toolbar", "")


@pytest.mark.asyncio
async def test_parallel_tool_durations_by_call_id():
    console = Console(file=StringIO(), width=80)
    renderer = TurnRenderer(console)
    renderer.on_tool("search", {"q": "a"}, tool_call_id="call-1")
    renderer.on_tool("search", {"q": "b"}, tool_call_id="call-2")
    renderer.on_tool_end("search", "3 lines", tool_call_id="call-1")
    renderer.on_tool_end("search", "5 lines", tool_call_id="call-2")
    out = console.file.getvalue()
    assert out.count("⎿") == 2
    assert "3 lines" in out and "5 lines" in out


@pytest.mark.asyncio
async def test_queue_command_list(tmp_path, monkeypatch):
    from jarn import repl
    from jarn.config.schema import Config, ProviderConfig, ProviderType, RoutingConfig

    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    root = tmp_path / "proj"
    (root / ".jarn").mkdir(parents=True)
    cfg = Config(
        default_profile="openrouter",
        providers={"openrouter": ProviderConfig(type=ProviderType.OPENROUTER, api_key="x")},
        routing=RoutingConfig(main="openrouter/m"),
    )
    app = repl.InlineApp(cfg, root)
    app._input_queue.append("first", "first")
    app._input_queue.append("second", "second")
    buf = StringIO()
    app.console = Console(file=buf, width=80)
    await app._cmd_queue("")
    out = buf.getvalue()
    assert "1. first" in out and "2. second" in out
