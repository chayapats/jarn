"""S04 — human activity lines (tool verbs + plain thinking)."""

from __future__ import annotations

import re
from io import StringIO
from pathlib import Path

import pytest
import yaml
from rich.console import Console

from jarn.config import settings
from jarn.config.defaults import global_config_template
from jarn.config.loader import ConfigError, load_config
from jarn.config.schema import UIConfig
from jarn.repl_renderer import TurnRenderer
from jarn.tui import layout, palette
from jarn.tui.i18n import t
from jarn.tui.tool_labels import (
    activity_open,
    activity_result,
    display_verb,
    is_checklist_tool,
    primary_object,
)


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _renderer(*, locale: str = "en", tool_progress: str = "new", **kwargs) -> tuple[TurnRenderer, StringIO]:
    buf = StringIO()
    console = Console(file=buf, width=80, highlight=False)
    renderer = TurnRenderer(
        console,
        live_sink=lambda _s: None,
        spinner=False,
        locale=locale,
        tool_progress=tool_progress,
        **kwargs,
    )
    return renderer, buf


# ---------------------------------------------------------------------------
# tool_labels
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "args", "locale", "verb", "obj"),
    [
        ("read_file", {"path": "src/auth/session.py"}, "en", "Read", "src/auth/session.py"),
        ("read_file", {"path": "src/auth/session.py"}, "th", "อ่าน", "src/auth/session.py"),
        ("edit_file", {"path": "src/auth/session.py"}, "en", "Edit", "src/auth/session.py"),
        ("edit_file", {"path": "src/auth/session.py"}, "th", "แก้", "src/auth/session.py"),
        ("write_file", {"path": "src/auth/session.py"}, "en", "Write", "src/auth/session.py"),
        ("write_file", {"path": "src/auth/session.py"}, "th", "เขียน", "src/auth/session.py"),
        ("bash", {"command": "git grep -n login"}, "en", "Run", "git grep -n login"),
        ("bash", {"command": "git grep -n login"}, "th", "รัน", "git grep -n login"),
        ("execute", {"command": "git grep -n login"}, "en", "Run", "git grep -n login"),
        ("execute", {"command": "git grep -n login"}, "th", "รัน", "git grep -n login"),
    ],
)
def test_activity_open_catalog_verbs(name, args, locale, verb, obj):
    assert activity_open(name, args, locale=locale) == (verb, obj)
    line = layout.tool_open(verb, obj, dialect="plain")
    assert f"{verb}  {obj}" in line
    assert name not in line
    assert "=" not in line


def test_read_file_prefers_file_path():
    assert primary_object("read_file", {"file_path": "a.py"}) == "a.py"


def test_unknown_tool_title_case_plus_path():
    verb, obj = activity_open("web_search", {"query": "gold", "url": "https://x"}, locale="en")
    assert verb == "Web Search"
    assert obj == "https://x"


def test_unknown_tool_without_path_has_no_kwargs():
    verb, obj = activity_open("web_search", {"query": "gold price"}, locale="th")
    assert verb == "Web Search"
    assert obj == ""


def test_mcp_id_is_not_translated():
    name = "mcp__github__create_issue"
    assert display_verb(name, "th") == name
    verb, obj = activity_open(name, {"path": "src/x.py"}, locale="th")
    assert verb == name
    assert obj == "src/x.py"


def test_checklist_tool_is_write_todos():
    assert is_checklist_tool("write_todos")
    assert not is_checklist_tool("read_file")


@pytest.mark.parametrize(
    ("summary", "locale", "expected"),
    [
        ("42 lines", "en", "42 lines"),
        ("42 lines", "th", "42 บรรทัด"),
        ("1 line", "en", "1 lines"),
        ("1 line", "th", "1 บรรทัด"),
        ("done", "en", "done"),
        ("done", "th", "เสร็จ"),
        ("updated render_toolbar", "th", "updated render_toolbar"),
    ],
)
def test_activity_result_localizes_lines_and_done(summary, locale, expected):
    assert activity_result(summary, locale=locale) == expected


# ---------------------------------------------------------------------------
# TurnRenderer density new / verbose
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("locale", "verb"), [("en", "Read"), ("th", "อ่าน")])
def test_new_density_prints_verb_and_path(locale, verb):
    r, buf = _renderer(locale=locale, tool_progress="new")
    path = "src/auth/session.py"
    r.on_tool("read_file", {"path": path})
    r.on_tool_end("read_file", "42 lines")
    out = buf.getvalue()
    assert verb in out
    assert path in out
    assert "read_file" not in out
    assert "path=" not in out
    assert "ctrl+o" not in out
    assert not re.search(r"\d+\.\d+s", out)
    if locale == "th":
        assert "42 บรรทัด" in out
    else:
        assert "42 lines" in out


def test_new_density_bash_prints_run_and_command():
    r, buf = _renderer(locale="en", tool_progress="new")
    r.on_tool("execute", {"command": "git grep -n login"})
    out = buf.getvalue()
    assert "Run" in out
    assert "git grep -n login" in out
    assert "execute" not in out
    assert "command=" not in out


def test_verbose_still_shows_kwargs_and_duration_and_ctrl_o():
    r, buf = _renderer(locale="en", tool_progress="verbose")
    r.on_tool("read_file", {"path": "src/auth/session.py"})
    r.on_tool_end("read_file", "42 lines", "full body\n")
    out = buf.getvalue()
    assert "read_file" in out
    assert "path=" in out
    assert "ctrl+o" in out
    assert re.search(r"\d+\.\d+s", out)


def test_write_todos_has_no_extra_line_at_new():
    r, buf = _renderer(tool_progress="new")
    r.on_tool("write_todos", {"todos": [{"content": "x", "status": "pending"}]})
    r.on_tool_end("write_todos", "updated")
    out = buf.getvalue()
    assert "write_todos" not in out
    assert "updated" not in out


def test_write_todos_still_prints_when_verbose():
    r, buf = _renderer(tool_progress="verbose")
    r.on_tool("write_todos", {"todos": []})
    r.on_tool_end("write_todos", "updated")
    out = buf.getvalue()
    assert "write_todos" in out
    assert "updated" in out


def test_plain_thinking_label_is_catalog():
    r, buf = _renderer(locale="th", thinking_style="plain", show_reasoning="collapsed")
    r.on_reasoning("weighing options")
    r.on_text("done.")
    out = buf.getvalue()
    assert t("thinking.plain", "th") in out
    assert "weighing options" in out
    assert "Cogitating" not in out


def test_quirky_thinking_uses_session_word():
    word = palette.session_thinking_word(style="quirky")
    assert word in palette.THINKING_WORDS
    r, buf = _renderer(thinking_style="quirky")
    r.on_reasoning("pondering")
    r.on_text("ok")
    out = buf.getvalue()
    assert word in out
    assert "pondering" in out


# ---------------------------------------------------------------------------
# ui.thinking_style config
# ---------------------------------------------------------------------------


def test_ui_thinking_style_defaults_to_plain():
    assert UIConfig().thinking_style == "plain"


def test_ui_thinking_style_is_settable():
    assert settings.is_settable("ui.thinking_style")
    assert settings.coerce("ui.thinking_style", "quirky") == "quirky"
    with pytest.raises(settings.SettingError):
        settings.coerce("ui.thinking_style", "kawaii")


def test_loader_thinking_style_accepted(tmp_path):
    gp = tmp_path / "g.yaml"
    _write(gp, {"ui": {"thinking_style": "quirky"}})
    cfg = load_config(global_path=gp, project_path=None)
    assert cfg.ui.thinking_style == "quirky"


def test_loader_thinking_style_invalid_raises(tmp_path):
    gp = tmp_path / "g.yaml"
    _write(gp, {"ui": {"thinking_style": "kawaii"}})
    with pytest.raises(ConfigError, match="ui.thinking_style"):
        load_config(global_path=gp, project_path=None)


def test_defaults_template_includes_thinking_style():
    template = global_config_template()
    assert "thinking_style:" in template
    assert "plain | quirky" in template


def test_session_thinking_word_plain_is_stable():
    first = palette.session_thinking_word(style="plain", locale="en")
    assert first == t("thinking.plain", "en")
    assert all(
        palette.session_thinking_word(style="plain", locale="en") == first
        for _ in range(10)
    )


def test_session_thinking_word_quirky_is_stable():
    word = palette.session_thinking_word(style="quirky")
    assert word in palette.THINKING_WORDS
    assert all(palette.session_thinking_word(style="quirky") == word for _ in range(20))
