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
    rendered = buf.getvalue()
    # Grouped sections replace the old flat "Built-in commands" header.
    assert "Daily" in rendered
    assert "Setup" in rendered
    assert "Session" in rendered


def test_format_help_groups_contain_expected_commands():
    """Commands appear under the correct group section."""
    body = format_help()
    # Verify section ordering: Daily appears before Setup, Setup before Session.
    assert body.index("[b]Daily[/b]") < body.index("[b]Setup[/b]")
    assert body.index("[b]Setup[/b]") < body.index("[b]Session[/b]")
    # Spot-check a few commands per group.
    setup_pos = body.index("[b]Setup[/b]")
    session_pos = body.index("[b]Session[/b]")
    # Daily commands appear before Setup header.
    assert body.index("/model") < setup_pos
    assert body.index("/cost") < setup_pos
    assert body.index("/clear") < setup_pos
    # Setup commands appear between Setup and Session headers.
    assert setup_pos < body.index("/config") < session_pos
    assert setup_pos < body.index("/trust") < session_pos
    # Session commands appear after Session header.
    assert body.index("/resume") > session_pos
    assert body.index("/queue") > session_pos


def test_format_help_has_shortcuts_block():
    """Shortcuts block is present in the rendered help."""
    body = format_help()
    assert "[b]Shortcuts[/b]" in body
    assert "Shift+Tab" in body or "Tab complete" in body


def test_format_help_has_toolbar_glyphs_legend():
    """Toolbar glyphs legend block is rendered with the expected symbols."""
    body = format_help()
    assert "[b]Toolbar glyphs[/b]" in body
    assert "◇" in body   # plan
    assert "◆" in body   # ask
    assert "⚡" in body  # auto-edit
    assert "⚠" in body   # yolo
    assert "●" in body   # key ok
    assert "✗" in body   # key fail
    assert "queue N" in body


def test_builtin_command_group_field():
    """Every builtin has a non-empty group assigned."""
    from jarn.extensibility.commands import BUILTINS

    for cmd in BUILTINS:
        assert cmd.group in ("Daily", "Setup", "Session"), (
            f"/{cmd.name} has unexpected group {cmd.group!r}"
        )


def test_profile_command_entry_unchanged():
    """P3.A is deferred — the 'profile' command entry must not be renamed."""
    from jarn.extensibility.commands import BUILTINS

    profile_cmd = next((c for c in BUILTINS if c.name == "profile"), None)
    assert profile_cmd is not None, "/profile must remain in BUILTINS (P3.A deferred)"
    assert profile_cmd.group == "Setup"


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


def test_toolbar_trusted_shows_lock():
    result = render_toolbar(
        model="openrouter/claude",
        mode="ask",
        cost_line="$0.00 · 0 tok · 0 calls",
        cost_status=BudgetStatus.OK,
        trusted=True,
        width=120,
    )
    assert "trusted" in result.value
    assert "untrusted" not in result.value


def test_toolbar_untrusted_shows_warning_and_pointer():
    result = render_toolbar(
        model="openrouter/claude",
        mode="ask",
        cost_line="$0.00 · 0 tok · 0 calls",
        cost_status=BudgetStatus.OK,
        trusted=False,
        width=120,
    )
    assert "untrusted" in result.value
    assert "jarn trust" in result.value


def test_toolbar_trust_segment_survives_narrow():
    """Trust segment has priority 2; cost (priority 5) drops before trust does."""
    # At width=60 the cost segment drops but model, mode, and trust all fit.
    narrow = render_toolbar(
        model="m",
        mode="ask",
        cost_line="$0.00 · 1000 tok · 99 calls · very long cost line",
        cost_status=BudgetStatus.OK,
        trusted=False,
        width=60,
    )
    assert "untrusted" in narrow.value


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
