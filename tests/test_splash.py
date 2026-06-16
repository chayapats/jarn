"""Tests for P1.A — suppressible / compact splash.

Covers:
- ui.splash: off  → SHORTCUT_HINT printed, no banner
- ui.splash: compact (default) → single-line banner (JARN + version + hint)
- ui.splash: full  → full ASCII wordmark splash
- First-ever run shows full splash regardless of configured value
- UIConfig.splash field defaults to 'compact'
- loader rejects unknown splash values
- config template includes splash key
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
import yaml
from rich.console import Console

from jarn.config.loader import ConfigError, load_config
from jarn.config.schema import UIConfig
from jarn.tui.logo import SHORTCUT_HINT, splash, splash_compact

# ---------------------------------------------------------------------------
# logo.py unit tests
# ---------------------------------------------------------------------------

def test_shortcut_hint_contains_help():
    assert "/help" in SHORTCUT_HINT


def test_splash_full_contains_wordmark():
    out = splash("1.2.3", "openrouter/m", "ask")
    assert "██" in out          # box-drawing block in ASCII art
    assert "1.2.3" in out


def test_splash_compact_contains_version_and_hint():
    out = splash_compact("2.0.0", "openrouter/m", "ask")
    assert "2.0.0" in out
    assert "/help" in out
    assert "JARN" in out
    # compact variant must NOT contain the multi-line wordmark block characters
    assert "██" not in out


def test_splash_compact_is_shorter_than_full():
    full = splash("1.0", None, "ask")
    compact = splash_compact("1.0", None, "ask")
    assert len(compact) < len(full)


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
# Splash-rendering helper (mirrors the repl.py branching logic)
# ---------------------------------------------------------------------------

def _render_splash(
    splash_value: str,
    version: str,
    first_run_marker: Path,
) -> str:
    """Reproduce the repl.py splash branching in isolation, return Rich plain text."""
    out = StringIO()
    console = Console(file=out, highlight=False, markup=False, width=120)

    is_first_run = not first_run_marker.exists()
    if is_first_run:
        first_run_marker.parent.mkdir(parents=True, exist_ok=True)
        first_run_marker.touch()
        console.print(splash(version, None, "ask"))
    elif splash_value == "full":
        console.print(splash(version, None, "ask"))
    elif splash_value == "compact":
        console.print(splash_compact(version, None, "ask"))
    else:  # off
        console.print(SHORTCUT_HINT)

    return out.getvalue()


# ---------------------------------------------------------------------------
# Splash branching: all three values + first-run path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("splash_value,first_run,expect_wordmark,expect_tagline,expect_hint", [
    # full: big wordmark + tagline + hint
    ("full", False, True, True, True),
    # compact: no big wordmark, but 'JARN' text label + hint (no tagline on separate line)
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

    if expect_hint:
        assert "/help" in output, \
            f"Expected shortcut hint in output for splash={splash_value!r}, first_run={first_run}"


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
    assert "JARN" in output          # compact 'JARN' label
    assert "/help" in output         # hint still present


def test_off_splash_still_shows_hint(tmp_path):
    """splash=off must still emit the shortcut hint, never nothing."""
    marker = tmp_path / "state" / "first_run_done"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()

    output = _render_splash("off", "1.0", marker)
    assert "/help" in output
    assert "Shift+Tab" in output     # hints include mode-cycle shortcut
    assert "██" not in output        # no wordmark block


# ---------------------------------------------------------------------------
# config template includes splash key
# ---------------------------------------------------------------------------

def test_defaults_template_includes_splash():
    from jarn.config.defaults import global_config_template
    template = global_config_template()
    assert "splash:" in template
    assert "compact" in template
