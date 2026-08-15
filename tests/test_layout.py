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
    assert not is_gateway_local_command("quit")
    assert not is_gateway_local_command("nope")
