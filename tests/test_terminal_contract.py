"""GA terminal, plain-output, and Unicode regression contract."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from jarn.repl.app import InlineApp
from jarn.tui import palette


def test_non_utf8_locale_help_is_controlled_ascii_fallback(tmp_path) -> None:
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(tmp_path),
            "LC_ALL": "C",
            "LANG": "C",
            "PYTHONUTF8": "0",
            "PYTHONCOERCECLOCALE": "0",
        }
    )

    completed = subprocess.run(  # noqa: S603 - fixed interpreter/module argv
        [sys.executable, "-m", "jarn", "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=10,
    )

    assert completed.returncode == 0
    assert b"usage: jarn" in completed.stdout
    assert b"Traceback" not in completed.stdout + completed.stderr
    assert b"UnicodeEncodeError" not in completed.stdout + completed.stderr


@pytest.mark.parametrize(
    "arguments",
    [
        ["definitely-nope"],
        ["--definitely-not-a-real-option"],
    ],
    ids=("unknown-command", "unknown-option"),
)
def test_cold_process_usage_error_keeps_stable_taxonomy(tmp_path, arguments) -> None:
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "JARN_HOME": str(tmp_path / "jarn-home"),
            "TERM": "dumb",
            "NO_COLOR": "1",
        }
    )

    completed = subprocess.run(  # noqa: S603 - fixed interpreter/module argv
        [sys.executable, "-m", "jarn", *arguments],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "JARN-CLI-001: Command usage is invalid." in completed.stderr
    for label in ("Cause:", "Component:", "retryable:", "Next:", "Log:"):
        assert label in completed.stderr
    assert "JARN-INTERNAL-001" not in completed.stderr
    assert "partially initialized module" not in completed.stderr
    assert "Traceback" not in completed.stderr


def test_term_dumb_disables_rich_and_prompt_toolkit_color(
    tmp_path, monkeypatch, base_config
) -> None:
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.delenv("NO_COLOR", raising=False)

    app = InlineApp(base_config, tmp_path)
    assert palette.no_color() is True
    assert app.console.is_terminal is False
    assert "<style" not in palette.styled_fg("red", "error")
    assert "fg:" not in " ".join(palette.toolbar_style_dict().values())

    rendered = app._render_stream_md("**plain** `output`")
    dimmed = app._render_dim_ansi("plain status")
    assert "\x1b[" not in rendered
    assert "\x1b[" not in dimmed
    assert "plain" in rendered
    assert "**" not in rendered

    fenced = app._render_stream_md("say **bold**\n\n```\nkeep **stars**\n```\n- list item")
    assert "**bold**" not in fenced
    assert "keep **stars**" in fenced
    # Rich still renders lists (dash becomes a bullet); the item text must remain.
    assert "list item" in fenced


@pytest.mark.asyncio
async def test_thai_combining_text_and_unicode_path_survive_80x24(
    tmp_path, monkeypatch, base_config
) -> None:
    monkeypatch.setenv("JARN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(
        "jarn.repl.app.shutil.get_terminal_size",
        lambda fallback=(80, 24): os.terminal_size((80, 24)),
    )
    project = tmp_path / "โปรเจกต์ทดสอบ"
    project.mkdir()

    app = InlineApp(base_config, project)
    thai = "กำลังแก้ไขไฟล์ ชื่อเรื่องภาษาไทย.md"
    app.input.insert_text(thai)

    assert app.input.document.text == thai
    assert app.controller.project_root == project
    assert app.console.width == 80
    assert thai in app._render_stream_md(thai)
