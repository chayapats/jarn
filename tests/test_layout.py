"""Display SSOT: grammar, layout helpers, and command usage pages."""

from __future__ import annotations

import sys
from io import StringIO

from rich.console import Console

from jarn.commands.help import format_help, format_help_detail, usage_error
from jarn.tui import grammar, layout, palette
from jarn.tui.i18n import t


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
    text = usage_error("config", locale="en")
    assert "Usage:" in text
    assert "/config" in text
    assert "/help config" in text


def test_help_detail_page() -> None:
    text = format_help_detail("compact", locale="en")
    assert "/compact" in text
    assert "Usage" in text
    assert "/clear" in text or "Related" in text


def test_help_index_uses_work_session_setup() -> None:
    body = format_help(locale="en")
    assert "[b]Work[/b]" in body
    assert "[b]Session[/b]" in body
    assert "[b]Setup[/b]" in body
    assert body.index("[b]Work[/b]") < body.index("[b]Session[/b]") < body.index("[b]Setup[/b]")
    assert "[b]Glyphs[/b]" not in body
    assert "[b]Daily[/b]" not in body
    assert "[b]Toolbar glyphs[/b]" not in body


def test_help_index_shows_bare_model_name() -> None:
    """Index lists ``/model``; syntax stays on the detail page."""
    body = format_help(locale="en")
    index = body.split("[b]Shortcuts[/b]", 1)[0]
    model_line = next(
        line for line in index.splitlines() if "Show or switch the active model" in line
    )
    assert "/model" in model_line
    assert "[name]" not in model_line
    assert "refresh" not in model_line
    assert "[/ref" not in index
    detail = format_help_detail("model", locale="en")
    assert "/model" in detail
    assert "name" in detail and "refresh" in detail


def test_format_help_wide_keeps_name_and_description_on_one_line() -> None:
    for body in (format_help(locale="en"), format_help(columns=80, locale="en")):
        status_line = next(line for line in body.splitlines() if "/status" in line)
        assert "Show directory" in status_line
        cost_line = next(line for line in body.splitlines() if "/cost" in line)
        assert "Show session tokens" in cost_line


def test_format_help_narrow_puts_description_on_next_line() -> None:
    body = format_help(columns=50, locale="en")
    idx = body.index("/status")
    name_line_end = body.index("\n", idx)
    name_line = body[body.rfind("\n", 0, idx) + 1 : name_line_end]
    desc_line = body[name_line_end + 1 : body.index("\n", name_line_end + 1)]
    assert "Show directory" not in name_line
    assert desc_line.startswith("      ")
    assert "Show directory" in desc_line


def test_layout_row_wraps_only_when_narrow() -> None:
    wrapped = layout.row("/cost", "Show session tokens", columns=50)
    wide = layout.row("/cost", "Show session tokens", columns=80)
    assert "\n" in wrapped
    assert "\n" not in wide
    html = layout.row("/x", "y", columns=40, dialect="html")
    assert "<span" not in html
    assert "\n" in html
    plain = layout.row("/x", "y", columns=40, dialect="plain")
    assert "[" not in plain
    assert "\n" in plain


def test_layout_html_dialect_is_telegram_safe() -> None:
    html = format_help(dialect="html", locale="en")
    assert "<b>Work</b>" in html
    assert "<code>/model" in html or "<code>/mode" in html
    assert "<span" not in html
    assert "[b]" not in html
    assert palette.C_SUCCESS not in html
    detail = format_help_detail("compact", dialect="html", locale="en")
    assert "<b>/compact" in detail
    assert "<span" not in detail


def test_format_help_html_thai_keeps_english_names() -> None:
    html = format_help(dialect="html", locale="th")
    assert f"<b>{t('help.group.Work', 'th')}</b>" in html
    assert "/model" in html
    assert "<span" not in html
    assert "[b]" not in html
    assert f"<b>{t('help.group.Work', 'en')}</b>" not in html


def test_format_help_thai_body_keeps_english_names() -> None:
    en = format_help(locale="en")
    th = format_help(locale="th")
    assert "Show or switch the active model." in en
    assert "แสดงหรือเปลี่ยนโมเดลที่ใช้อยู่" in th
    assert "Show or switch the active model." not in th
    assert "/model" in th and "/mode" in th and "/help" in th
    assert "[b]Work[/b]" in en
    assert "[b]งาน[/b]" in th
    assert "maps to" not in en.lower()
    assert "maps to" not in th.lower()


def test_help_mode_glosses_plan_ask_auto_edit_yolo() -> None:
    en = format_help_detail("mode", locale="en")
    th = format_help_detail("mode", locale="th")
    for mode_id in ("plan", "ask", "auto-edit", "yolo"):
        assert mode_id in en
        assert mode_id in th
    assert "confirm each change" in en
    assert "ถามก่อนทุกการเปลี่ยน" in th
    assert "danger-guard" in en
    assert "อันตราย" in th


def test_help_login_cli_is_detail_extra_not_index() -> None:
    index = format_help(locale="en")
    login_line = next(line for line in index.splitlines() if "/login" in line)
    assert "Sign in to ChatGPT." in login_line
    assert "jarn auth login" not in login_line
    assert "maps to" not in index
    detail = format_help_detail("login", locale="en")
    assert "maps to" not in detail
    assert "jarn auth login" in detail
    assert "In a terminal:" in detail
    th = format_help_detail("login", locale="th")
    assert "jarn auth login" in th
    assert "ในเทอร์มินัล:" in th
    assert "maps to" not in th


def test_error_detail_tty_adds_color_and_blank_lines(monkeypatch) -> None:
    from jarn.errors import ErrorCode, error_detail
    from jarn.tui.i18n import t

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(sys, "argv", ["pytest"])

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
    colored = detail.render(stream=_Tty(), locale="en")
    assert "\x1b[" in colored
    assert "\n\n" in colored
    assert f"{t('error.next', 'en')}:" in colored
    assert "component:" not in colored.lower()
    assert "Cause:" not in colored
    assert "Log:" not in colored
    verbose = detail.render(stream=_Tty(), locale="en", verbose=True)
    assert "Component:" in verbose
    assert "Cause:" in verbose
    thai = detail.render(stream=_Tty(), locale="th")
    assert f"{t('error.next', 'th')}:" in thai
    assert "component:" not in thai.lower()
    plain = detail.render(stream=object(), locale="en")
    assert "\x1b[" not in plain
    assert plain.splitlines()[1].startswith("Cause:")
    assert "Component:" in plain


def test_error_detail_jarn_verbose_argv_shows_component_on_tty(monkeypatch) -> None:
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
    monkeypatch.setattr(sys, "argv", ["jarn", "--verbose", "nope"])
    shown = detail.render(stream=_Tty(), locale="en")
    assert "Component:" in shown
    monkeypatch.setattr(sys, "argv", ["python", "-m", "jarn", "--verbose"])
    module = detail.render(stream=_Tty(), locale="en")
    assert "Component:" in module
    monkeypatch.setattr(sys, "argv", ["/usr/bin/python3.14", "-m", "jarn", "--verbose"])
    versioned = detail.render(stream=_Tty(), locale="en")
    assert "Component:" in versioned
    monkeypatch.setattr(sys, "argv", ["/opt/jarn/src/jarn/__main__.py", "--verbose"])
    rewritten = detail.render(stream=_Tty(), locale="en")
    assert "Component:" in rewritten
    monkeypatch.setattr(sys, "argv", ["pytest", "--verbose"])
    quiet = detail.render(stream=_Tty(), locale="en")
    assert "component:" not in quiet.lower()
    monkeypatch.setattr(sys, "argv", ["pytest", "-m", "jarn", "--verbose"])
    marker = detail.render(stream=_Tty(), locale="en")
    assert "component:" not in marker.lower()


def test_error_detail_follows_destination_stream_not_stderr(monkeypatch) -> None:
    """Rich Console writes to stdout; quiet/full must follow that file, not stderr."""
    from io import StringIO

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
    monkeypatch.setattr(sys, "stderr", _Tty())
    piped = StringIO()
    rendered = detail.render(stream=piped, locale="en")
    assert "Component:" in rendered
    assert rendered.splitlines()[1].startswith("Cause:")


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
    from jarn.commands.registry import (
        COMMAND_SPECS,
        GATEWAY_LOCAL_COMMANDS,
        GATEWAY_MUTATING_COMMANDS,
        GATEWAY_ONLY_COMMANDS,
        GATEWAY_READONLY_COMMANDS,
        GATEWAY_SESSION_COMMANDS,
        gateway_botfather_commands,
        is_gateway_local_command,
        is_gateway_mutating_command,
        parse_slash_line,
    )

    assert parse_slash_line("/status") == ("status", "")
    assert parse_slash_line("/HELP compact") == ("help", "compact")
    assert parse_slash_line("/cost@MyBot") == ("cost", "")
    assert parse_slash_line("not a command") is None
    assert is_gateway_local_command("status")
    assert is_gateway_local_command("usage")
    assert is_gateway_local_command("verbose")
    assert is_gateway_local_command("map")
    assert is_gateway_local_command("wiki")
    assert is_gateway_local_command("model")
    assert is_gateway_local_command("mode")
    assert is_gateway_local_command("compact")
    assert is_gateway_local_command("undo")
    assert is_gateway_local_command("resume")
    assert is_gateway_local_command("skill")
    assert not is_gateway_local_command("quit")
    assert not is_gateway_local_command("nope")
    assert not is_gateway_local_command("config")
    assert not is_gateway_local_command("preset")
    assert not is_gateway_local_command("memory")
    assert not is_gateway_local_command("sandbox")

    assert "verbose" in GATEWAY_SESSION_COMMANDS
    assert "focus" in GATEWAY_SESSION_COMMANDS
    assert "title" in GATEWAY_SESSION_COMMANDS
    assert "model" in GATEWAY_SESSION_COMMANDS
    assert "mode" in GATEWAY_SESSION_COMMANDS
    assert "compact" in GATEWAY_SESSION_COMMANDS
    assert "undo" in GATEWAY_SESSION_COMMANDS
    assert "redo" in GATEWAY_SESSION_COMMANDS
    assert "resume" in GATEWAY_SESSION_COMMANDS
    assert "skill" in GATEWAY_SESSION_COMMANDS
    assert "verbose" not in GATEWAY_READONLY_COMMANDS
    assert "sessions" in GATEWAY_READONLY_COMMANDS
    assert "checkpoints" in GATEWAY_READONLY_COMMANDS
    assert "ps" in GATEWAY_READONLY_COMMANDS
    assert "config" not in GATEWAY_LOCAL_COMMANDS
    assert "config" in GATEWAY_MUTATING_COMMANDS
    assert GATEWAY_LOCAL_COMMANDS == GATEWAY_READONLY_COMMANDS | GATEWAY_SESSION_COMMANDS
    mutating = (
        "config",
        "preset",
        "memory",
        "sandbox",
        "trust",
        "key",
        "login",
        "logout",
        "add-dir",
        "init",
        "module",
        "theme",
        "rewind",
        "queue",
        "abort",
        "commit",
        "review",
        "clear",
        "quit",
        "exit",
        "expand",
        "diff",
        "busy",
    )
    for name in mutating:
        assert name in GATEWAY_MUTATING_COMMANDS
        assert name not in GATEWAY_LOCAL_COMMANDS
        assert is_gateway_mutating_command(name)
        assert not is_gateway_local_command(name)
    assert is_gateway_mutating_command("new")  # alias of clear
    assert not is_gateway_local_command("new")
    for spec in COMMAND_SPECS:
        canonical = spec.alias_of or spec.name
        assert canonical in GATEWAY_LOCAL_COMMANDS or canonical in GATEWAY_MUTATING_COMMANDS, (
            canonical
        )
    menu = gateway_botfather_commands()
    menu_names = {name for name, _ in menu}
    assert menu_names <= (GATEWAY_LOCAL_COMMANDS | GATEWAY_ONLY_COMMANDS)
    assert "config" not in menu_names
    assert "clear" not in menu_names
    assert {"stop", "new", "repo", "help", "reset"} <= menu_names


def test_gateway_mutating_notice_localizes_terminal_hint() -> None:
    from jarn.commands.registry import GATEWAY_MUTATING_NOTICE, gateway_mutating_notice
    from jarn.tui.i18n import t

    assert gateway_mutating_notice("config") == t("telegram.mutating.named", "en", name="config")
    assert gateway_mutating_notice("config", locale="th") == t(
        "telegram.mutating.named", "th", name="config"
    )
    assert gateway_mutating_notice() == GATEWAY_MUTATING_NOTICE
    assert gateway_mutating_notice() == t("telegram.mutating", "en")
    assert "/config" in gateway_mutating_notice("config", locale="th")
    assert gateway_mutating_notice("config", locale="en") != gateway_mutating_notice(
        "config", locale="th"
    )


def test_live_stream_helpers_use_grammar_and_palette() -> None:
    assert grammar.GLYPH_PROMPT in layout.prompt("hello")
    assert palette.C_USER in layout.prompt("hello")
    assert "hello" in layout.prompt("hello")
    token = "[Pasted text #1 +12 lines]"
    preview = layout.paste_preview(token)
    assert "\n" not in preview
    assert palette.C_DIM in preview
    assert layout.is_paste_token(token)
    assert not layout.is_paste_token("hello")
    echo = layout.submitted_echo(token, "a\nb\nc")
    assert echo == preview
    assert "\n" not in echo
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
    assert grammar.GLYPH_HOST_SHELL in banner
    assert f"{grammar.GLYPH_HOST_SHELL} host shell" in banner
    assert palette.C_ERROR in banner
    assert "danger-guard skipped" in banner
    assert grammar.MODE_GLYPH["auto-edit"] == grammar.GLYPH_HOST_SHELL
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


def test_truncate_bullet_rule_item_helpers() -> None:
    assert layout.truncate("hello", 10) == "hello"
    assert layout.truncate("abcdefghij", 8) == "abcdefg…"
    assert layout.truncate("x", 1) == "x"
    assert layout.truncate("xy", 1) == "…"
    dotted = layout.bullet("install deps")
    assert "·" in dotted
    assert "install deps" in dotted
    assert "\\[x]" in layout.bullet("[x]")
    ruled = layout.rule("Status")
    assert "──" in ruled
    assert "Status" in ruled
    assert palette.C_DIM in ruled
    bare = layout.rule()
    assert bare == layout.muted("──")
    assert layout.bar(4, dialect="plain") == "────"
    assert palette.C_DIM in layout.bar(3)
    boxed = layout.composer_box("hello", width=8, dialect="plain")
    assert boxed.splitlines()[0] == "─" * 8
    assert boxed.splitlines()[1].startswith(f"{grammar.GLYPH_PROMPT} hello")
    typed = layout.composer_box("hello", width=8, typed="draft", dialect="plain")
    assert "draft" in typed
    assert "hello" not in typed.splitlines()[1]
    row = layout.item("bash", "run a command", meta="tool")
    assert palette.ACCENT in row
    assert "bash" in row
    assert "run a command" in row
    assert "tool" in row
    assert palette.C_DIM in row
    assert "\\[n]" in layout.item("[n]")
    named = layout.item("only")
    assert "only" in named
    assert palette.ACCENT in named


def test_code_and_pre_html_has_no_span() -> None:
    html_code = layout.code("rm -rf", dialect="html")
    assert "<code>" in html_code
    assert "rm -rf" in html_code
    assert "<span" not in html_code
    html_pre = layout.pre("a < b", dialect="html")
    assert "<pre>" in html_pre
    assert "&lt;" in html_pre
    assert "<span" not in html_pre
    rich_code = layout.code("x")
    assert palette.ACCENT in rich_code
    assert "x" in rich_code
    assert layout.code("x", dialect="plain") == "x"
    assert layout.pre("x", dialect="plain") == "x"
    assert "\\[b]" in layout.code("[b]")
    assert "\\[b]" in layout.pre("[b]")


def test_format_todos_and_host_shell_banner_glyphs() -> None:
    todos = [
        {"content": "done", "status": "completed"},
        {"content": "active", "status": "in_progress"},
        {"content": "wait", "status": "pending"},
    ]
    lines = layout.format_todos(todos, 80)
    joined = "\n".join(lines)
    assert "Todos" in joined
    assert "active" in joined
    assert grammar.GLYPH_TODO_RUN in joined
    overflow = (
        [{"content": f"d{i}", "status": "completed"} for i in range(3)]
        + [{"content": "go", "status": "in_progress"}]
        + [{"content": f"p{i}", "status": "pending"} for i in range(16)]
    )
    capped = layout.format_todos(overflow, 80, cap=8)
    body = capped[1:]
    assert len(body) <= 8
    assert any("more" in line for line in body)
    assert any("… +" in line and "more" in line for line in body)
    banner = layout.host_shell_banner()
    assert grammar.GLYPH_HOST_SHELL in banner
    assert grammar.GLYPH_PLAY == "▶"
    assert grammar.GLYPH_PROMPT == "›"
