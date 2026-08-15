"""Display SSOT: grammar, layout helpers, and command usage pages."""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from jarn.commands.help import format_help, format_help_detail, usage_error
from jarn.tui import grammar, layout, palette


def test_context_level_ramps() -> None:
    assert grammar.context_level(0.0) == "ok"
    assert grammar.context_level(0.49) == "ok"
    assert grammar.context_level(0.50) == "warn"
    assert grammar.context_level(0.79) == "warn"
    assert grammar.context_level(0.80) == "hot"
    assert grammar.context_level(0.94) == "hot"
    assert grammar.context_level(0.95) == "exceeded"
    assert grammar.context_level(1.2) == "exceeded"


def test_context_bar_width() -> None:
    bar = grammar.context_bar(0.6, width=10)
    assert len(bar) == 10
    assert grammar.GLYPH_BAR_FILL in bar
    assert grammar.GLYPH_BAR_EMPTY in bar


def test_format_tokens_and_duration() -> None:
    assert grammar.format_tokens(12) == "12"
    assert grammar.format_tokens(12_400) == "12.4K"
    assert grammar.format_tokens(200_000) == "200K"
    assert grammar.format_duration(45) == "45s"
    assert grammar.format_duration(12 * 60) == "12m"
    assert "h" in grammar.format_duration(3723)


def test_next_tool_progress_cycles() -> None:
    assert grammar.next_tool_progress("off") == "new"
    assert grammar.next_tool_progress("new") == "all"
    assert grammar.next_tool_progress("all") == "verbose"
    assert grammar.next_tool_progress("verbose") == "off"
    assert grammar.next_tool_progress("nope") == grammar.TOOL_PROGRESS_DEFAULT


def test_layout_kv_and_row_padding() -> None:
    line = layout.kv("Model", "claude")
    assert "Model" in line
    assert "claude" in line
    assert palette.C_DIM in line
    row = layout.row("/cost", "Show session tokens")
    assert palette.ACCENT in row
    assert "/cost" in row
    assert "Show session tokens" in row


def test_layout_semantic_colors_use_palette() -> None:
    assert palette.C_SUCCESS in layout.ok("ok")
    assert palette.C_WARN in layout.warn("warn")
    assert palette.C_ERROR in layout.err("err")
    assert palette.ACCENT in layout.accent("name")
    assert grammar.GLYPH_KEY_OK in layout.key_mark(True)
    assert grammar.GLYPH_KEY_OFF in layout.key_mark(False)


def test_layout_markup_is_valid_rich() -> None:
    buf = StringIO()
    Console(file=buf, force_terminal=True, width=80, highlight=False).print(
        "\n".join(
            [
                layout.title("Status"),
                layout.kv("Directory", "/tmp"),
                layout.context_gauge(0.06, used=12_400, window=200_000),
                layout.banner_ok("All good."),
            ]
        )
    )
    out = buf.getvalue()
    assert "Status" in out
    assert "Directory" in out
    assert "All good." in out


def test_usage_error_comes_from_registry() -> None:
    text = usage_error("config")
    assert "Usage:" in text
    assert "/config" in text
    assert "/help config" in text


def test_help_detail_page() -> None:
    text = format_help_detail("compact")
    assert "/compact" in text
    assert "Usage" in text
    assert "/clear" in text or "Related" in text


def test_help_index_uses_work_session_setup() -> None:
    body = format_help()
    assert "[b]Work[/b]" in body
    assert "[b]Session[/b]" in body
    assert "[b]Setup[/b]" in body
    assert body.index("[b]Work[/b]") < body.index("[b]Session[/b]") < body.index("[b]Setup[/b]")
    assert "[b]Glyphs[/b]" in body
    assert "[b]Daily[/b]" not in body
    assert "[b]Toolbar glyphs[/b]" not in body


def test_layout_html_dialect_is_telegram_safe() -> None:
    html = format_help(dialect="html")
    assert "<b>Work</b>" in html
    assert "<code>/model" in html or "<code>/mode" in html
    assert "<span" not in html
    assert "[b]" not in html
    assert palette.C_SUCCESS not in html
    detail = format_help_detail("compact", dialect="html")
    assert "<b>/compact" in detail
    assert "<span" not in detail


def test_error_detail_tty_adds_color_and_blank_lines(monkeypatch) -> None:
    from jarn.errors import ErrorCode, error_detail

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    class _Tty:
        def isatty(self) -> bool:
            return True

    detail = error_detail(
        ErrorCode.CLI_USAGE,
        "Command usage is invalid.",
        cause="missing argument",
        component="command line parser",
        retryable=False,
        action="Run jarn --help",
    )
    colored = detail.render(stream=_Tty())
    assert "\x1b[" in colored
    assert "\n\nCause:" in colored
    assert "Next:" in colored
    plain = detail.render(stream=object())
    assert "\x1b[" not in plain
    assert plain.splitlines()[1].startswith("Cause:")


def test_heading_and_more_helpers() -> None:
    line = layout.heading("Settings", "hint")
    assert "Settings" in line
    assert "hint" in line
    assert palette.C_DIM in line
    assert "3" in layout.more(3)


def test_layout_plain_dialect_has_no_markup() -> None:
    assert "[" not in layout.title("Commands", dialect="plain")
    row = layout.row("setup", "Run the onboarding wizard", dialect="plain")
    assert "setup" in row
    assert "Run the onboarding wizard" in row
    assert palette.ACCENT not in row
    assert layout.field("Purpose", dialect="plain") == "Purpose:"


def test_layout_to_html_transcodes_rich_tags() -> None:
    rich = f"{layout.title('Status')}\n{layout.kv('Model', 'claude')}"
    html = layout.to_html(rich)
    assert "<b>Status</b>" in html
    assert "claude" in html
    assert "<span" not in html
    assert "[b]" not in html
    assert layout.looks_like_layout_markup(rich)
    assert not layout.looks_like_layout_markup("hello /status")


def test_parse_slash_line_and_gateway_local() -> None:
    from jarn.commands.registry import is_gateway_local_command, parse_slash_line

    assert parse_slash_line("/status") == ("status", "")
    assert parse_slash_line("/HELP compact") == ("help", "compact")
    assert parse_slash_line("/cost@MyBot") == ("cost", "")
    assert parse_slash_line("not a command") is None
    assert is_gateway_local_command("status")
    assert is_gateway_local_command("usage")
    assert is_gateway_local_command("verbose")
    assert not is_gateway_local_command("quit")
    assert not is_gateway_local_command("nope")
    assert not is_gateway_local_command("config")
    assert not is_gateway_local_command("preset")
    assert not is_gateway_local_command("memory")
    assert not is_gateway_local_command("sandbox")
    from jarn.commands.registry import (
        GATEWAY_LOCAL_COMMANDS,
        GATEWAY_READONLY_COMMANDS,
        GATEWAY_SESSION_COMMANDS,
    )

    assert "verbose" in GATEWAY_SESSION_COMMANDS
    assert "focus" in GATEWAY_SESSION_COMMANDS
    assert "title" in GATEWAY_SESSION_COMMANDS
    assert "verbose" not in GATEWAY_READONLY_COMMANDS
    assert "config" not in GATEWAY_LOCAL_COMMANDS
    assert GATEWAY_LOCAL_COMMANDS == GATEWAY_READONLY_COMMANDS | GATEWAY_SESSION_COMMANDS


def test_live_stream_helpers_use_grammar_and_palette() -> None:
    assert grammar.GLYPH_PROMPT in layout.prompt("hello")
    assert palette.C_USER in layout.prompt("hello")
    assert "hello" in layout.prompt("hello")
    queued = layout.steer("next", queued=True)
    assert grammar.GLYPH_STEER in queued
    assert "queued:" in queued
    assert palette.C_DIM in queued
    shell = layout.host_shell("ls -la")
    assert palette.C_ERROR in shell
    assert "ls -la" in shell
    assert "(host shell)" in shell
    assert palette.C_USER not in shell
    assert grammar.GLYPH_THINKING in layout.thinking()
    assert grammar.GLYPH_SUBAGENT in layout.subagent_prefix("reviewer")
    done = layout.subagent_done("reviewer", 3, hint="ctrl+o")
    assert "3 tool calls" in done
    assert "ctrl+o" in done
    opened = layout.tool_open("bash", "ls")
    assert palette.C_TOOL in opened
    assert grammar.GLYPH_TOOL in opened
    assert "bash" in opened
    closed = layout.tool_result("ok", duration=" · 1.2s", hint="· ctrl+o")
    assert grammar.GLYPH_RESULT in closed
    assert "1.2s" in closed
    assert layout.cancelled() == layout.muted("cancelled")
    assert "·" in layout.sep()
    assert palette.C_DIM in layout.sep()
    rich = layout.host_shell("rm -rf /")
    html = layout.to_html(rich)
    assert "<span" not in html
    assert "rm -rf /" in html
    assert layout.host_shell("x", dialect="plain").startswith("! x")


def test_layout_helpers_escape_user_text() -> None:
    assert layout.escape("[bold]x") == "\\[bold]x"
    assert "\\[bold]x" in layout.prompt("[bold]x")
    assert "\\[rm]" in layout.host_shell("[rm]")
    assert "\\[x]" in layout.todo_item("[x]", "pending")
    assert "\\[s]" in layout.steer("[s]", queued=True)


def test_todo_spinner_banner_and_link_helpers() -> None:
    done = layout.todo_glyph("completed")
    assert grammar.GLYPH_TODO_DONE in done
    assert palette.C_SUCCESS in done
    running = layout.todo_item("ship it", "in_progress")
    assert grammar.GLYPH_TODO_RUN in running
    assert palette.ACCENT in running
    assert "ship it" in running
    finished = layout.todo_item("old", "completed")
    assert palette.C_DIM in finished
    truncated = layout.todo_item("abcdefghijklmnop", "pending", truncate=12)
    assert "…" in truncated
    spin = layout.spinner("⠋", "Working…")
    assert palette.C_TOOL in spin
    assert "Working…" in spin
    banner = layout.host_shell_banner()
    assert "⚡ host shell" in banner
    assert palette.C_ERROR in banner
    assert "danger-guard skipped" in banner
    steered = layout.steer("next", queued=True, hint="  ·  [s] steer now")
    assert "queued:" in steered
    assert "steer now" in steered
    rich_link = layout.link("https://example.test/a")
    assert "[link=" in rich_link
    assert "https://example.test/a" in rich_link
    assert layout.link("https://example.test/a", dialect="plain") == "https://example.test/a"
    html_link = layout.link("https://example.test/a", dialect="html")
    assert "<a href=" in html_link
    assert "<span" not in html_link
