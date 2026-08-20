"""Tests for splash variants — compact orientation vs full wordmark.

Covers:
- ui.splash: off  → SHORTCUT_HINT printed, no banner, no kv strip
- ui.splash: compact (default) → ≤6-line orientation (locale-aware, no Skills)
- ui.splash: full  → full ASCII wordmark + Model/Folder/Mode kv extra
- First-ever run shows full splash regardless of configured value
- UIConfig.splash field defaults to 'compact'
- loader rejects unknown splash values
- config template includes splash key
"""

from __future__ import annotations

import re
from io import StringIO
from pathlib import Path

import pytest
import yaml
from rich.console import Console

from jarn.config.loader import ConfigError, load_config
from jarn.config.schema import UIConfig
from jarn.tui.i18n import t
from jarn.tui.logo import (
    SHORTCUT_HINT,
    render_launch_banner,
    splash,
    splash_compact,
    splash_info_strip,
)

_MARKUP = re.compile(r"\[/?[^\]]+\]")


def _plain(text: str) -> str:
    return _MARKUP.sub("", text)


# ---------------------------------------------------------------------------
# logo.py unit tests
# ---------------------------------------------------------------------------

def test_shortcut_hint_contains_help():
    assert "/help" in SHORTCUT_HINT


def test_splash_full_contains_wordmark():
    out = splash("1.2.3")
    assert "██" in out          # box-drawing block in ASCII art
    assert "1.2.3" in out


def test_splash_compact_matches_orientation_picture():
    out = splash_compact(
        "1.0.9",
        model="sonnet-4",
        folder="~/harness",
        mode="ask",
        locale="th",
    )
    assert _plain(out).splitlines() == [
        "jarn  v1.0.9",
        "  sonnet-4  ·  ~/harness  ·  ask",
        "  พิมพ์ข้อความได้เลย  ·  /help สำหรับคำสั่ง",
    ]
    assert "██" not in out
    assert "Skills" not in out
    assert "type /skills" not in out
    assert "just a reliable nerd" not in out


def test_splash_compact_en_orientation_differs_from_th():
    kwargs = dict(model="sonnet-4", folder="~/harness", mode="ask")
    th = _plain(splash_compact("1.0.9", locale="th", **kwargs))
    en = _plain(splash_compact("1.0.9", locale="en", **kwargs))
    assert th != en
    assert t("splash.orientation", "th") in th
    assert t("splash.orientation", "en") in en
    assert "Type a message. /help for commands." in en.splitlines()[2]
    assert th.splitlines()[:2] == en.splitlines()[:2]


def test_splash_compact_is_at_most_six_lines():
    out = splash_compact(
        "2.0.0",
        model="claude-sonnet-4",
        folder="~/Projects/harness",
        mode="ask",
        locale="en",
    )
    lines = _plain(out).splitlines()
    assert 1 <= len(lines) <= 6
    assert "Skills" not in out
    assert "type /skills" not in out
    assert "JARN" not in out
    assert "jarn" in lines[0]


def test_splash_compact_is_shorter_than_full():
    full = splash("1.0")
    compact = splash_compact("1.0", model="m", folder="~", mode="ask", locale="en")
    assert len(compact) < len(full)


def test_splash_info_strip_has_kv_without_skills():
    strip = splash_info_strip(model="sonnet-4", folder="~/harness", mode="ask")
    plain = _plain(strip)
    assert "Model" in plain
    assert "Folder" in plain
    assert "Mode" in plain
    assert "Skills" not in plain
    assert "type /skills" not in plain


def test_compact_launch_banner_has_no_kv_strip():
    body = render_launch_banner(
        "1.0.9",
        variant="compact",
        first_run=False,
        model="sonnet-4",
        folder="~/harness",
        mode="ask",
        locale="en",
    )
    plain = _plain(body)
    assert len(plain.splitlines()) <= 6
    assert "Skills" not in plain
    assert "type /skills" not in plain
    assert "Model" not in plain
    assert "Folder" not in plain
    assert t("splash.orientation", "en") in plain


def test_full_launch_banner_keeps_wordmark_and_kv():
    body = render_launch_banner(
        "1.0.9",
        variant="full",
        first_run=False,
        model="sonnet-4",
        folder="~/harness",
        mode="ask",
        locale="en",
    )
    plain = _plain(body)
    assert "██" in body
    assert "Model" in plain
    assert "Folder" in plain
    assert "Mode" in plain
    assert "Skills" not in plain
    assert "type /skills" not in plain


# ---------------------------------------------------------------------------
# UIConfig default value
# ---------------------------------------------------------------------------

def test_ui_config_splash_defaults_to_compact():
    cfg = UIConfig()
    assert cfg.splash == "compact"


# ---------------------------------------------------------------------------
# Config loader: ui.splash parsing
# ---------------------------------------------------------------------------

def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_loader_splash_default_is_compact(tmp_path):
    cfg = load_config(
        global_path=tmp_path / "missing.yaml",
        project_path=None,
    )
    assert cfg.ui.splash == "compact"


def test_loader_splash_full(tmp_path):
    gp = tmp_path / "g.yaml"
    _write(gp, {"ui": {"splash": "full"}})
    cfg = load_config(global_path=gp, project_path=None)
    assert cfg.ui.splash == "full"


def test_loader_splash_off(tmp_path):
    gp = tmp_path / "g.yaml"
    _write(gp, {"ui": {"splash": "off"}})
    cfg = load_config(global_path=gp, project_path=None)
    assert cfg.ui.splash == "off"


def test_loader_splash_compact_explicit(tmp_path):
    gp = tmp_path / "g.yaml"
    _write(gp, {"ui": {"splash": "compact"}})
    cfg = load_config(global_path=gp, project_path=None)
    assert cfg.ui.splash == "compact"


def test_loader_splash_invalid_raises(tmp_path):
    gp = tmp_path / "g.yaml"
    _write(gp, {"ui": {"splash": "huge"}})
    with pytest.raises(ConfigError, match="ui.splash"):
        load_config(global_path=gp, project_path=None)


# ---------------------------------------------------------------------------
# Splash-rendering helper (mirrors REPL boot via render_launch_banner)
# ---------------------------------------------------------------------------

def _render_splash(
    splash_value: str,
    version: str,
    first_run_marker: Path,
    *,
    locale: str = "en",
) -> str:
    """Reproduce splash branching in isolation, return Rich plain text."""
    out = StringIO()
    console = Console(file=out, highlight=False, markup=False, width=120)

    is_first_run = not first_run_marker.exists()
    if is_first_run:
        first_run_marker.parent.mkdir(parents=True, exist_ok=True)
        first_run_marker.touch()
    console.print(
        render_launch_banner(
            version,
            variant=splash_value,
            first_run=is_first_run,
            model="sonnet-4",
            folder="~/harness",
            mode="ask",
            locale=locale,
        )
    )

    return out.getvalue()


# ---------------------------------------------------------------------------
# Splash branching: all three values + first-run path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("splash_value,first_run,expect_wordmark,expect_tagline,expect_hint", [
    # full: big wordmark + tagline + hint
    ("full", False, True, True, True),
    # compact: no big wordmark, orientation sentence (no tagline)
    ("compact", False, False, False, True),
    # off: no banner at all, but hint still present
    ("off", False, False, False, True),
    # first run with compact config → still shows full splash
    ("compact", True, True, True, True),
    # first run with off config → still shows full splash
    ("off", True, True, True, True),
])
def test_splash_branching(
    tmp_path,
    splash_value, first_run, expect_wordmark, expect_tagline, expect_hint,
):
    marker = tmp_path / "state" / "first_run_done"
    if not first_run:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()

    output = _render_splash(splash_value, "9.9.9", marker)

    if expect_wordmark:
        assert "██" in output, \
            f"Expected ASCII wordmark (██) in output for splash={splash_value!r}, first_run={first_run}"
    else:
        assert "██" not in output, \
            f"Did not expect ASCII wordmark for splash={splash_value!r}, first_run={first_run}"

    if expect_tagline:
        # The tagline "just a reliable nerd" appears in the full splash
        assert "just a reliable nerd" in output, \
            f"Expected tagline in output for splash={splash_value!r}, first_run={first_run}"
    elif splash_value == "compact" and not first_run:
        assert "just a reliable nerd" not in output

    if expect_hint:
        assert "/help" in output, \
            f"Expected shortcut hint in output for splash={splash_value!r}, first_run={first_run}"

    if splash_value == "compact" and not first_run:
        body_lines = [ln for ln in _plain(output).splitlines() if ln.strip()]
        assert len(body_lines) <= 6
        assert "Skills" not in output
        assert "type /skills" not in output


def test_first_run_marker_created(tmp_path):
    """First run must write the state marker so subsequent runs aren't first-run."""
    marker = tmp_path / "state" / "first_run_done"
    assert not marker.exists()
    _render_splash("compact", "1.0", marker)
    assert marker.exists(), "first_run_done marker must be created on first run"


def test_second_run_is_not_first_run(tmp_path):
    """After marker exists, compact config renders compact (not full) splash."""
    marker = tmp_path / "state" / "first_run_done"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()

    output = _render_splash("compact", "1.0", marker)
    assert "██" not in output        # no full wordmark block
    assert "jarn" in output          # compact name
    assert "/help" in output         # orientation still present
    assert "Skills" not in output
    assert "type /skills" not in output


def test_off_splash_still_shows_hint(tmp_path):
    """splash=off must still emit the shortcut hint, never nothing."""
    marker = tmp_path / "state" / "first_run_done"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()

    output = _render_splash("off", "1.0", marker)
    assert "/help" in output
    assert "Shift+Tab" in output     # hints include mode-cycle shortcut
    assert "██" not in output        # no wordmark block
    assert "Skills" not in output
    assert "type /skills" not in output


def test_off_splash_has_no_kv_strip(tmp_path):
    marker = tmp_path / "state" / "first_run_done"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    output = _plain(_render_splash("off", "1.0", marker))
    assert "Model" not in output
    assert "Folder" not in output


# ---------------------------------------------------------------------------
# config template includes splash key
# ---------------------------------------------------------------------------

def test_defaults_template_includes_splash():
    from jarn.config.defaults import global_config_template
    template = global_config_template()
    assert "splash:" in template
    assert "compact" in template
