"""T-4-2 — Update-available notice (5 test cases).

Tests cover the core _do_update_check logic and the maybe_start_update_check
skip conditions. All HTTP is mocked — zero live network.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_pypi_response(version: str) -> dict[str, Any]:
    return {"info": {"version": version}}


def _make_config(check: bool = True) -> Any:
    """Return a minimal config-like object with updates.check wired."""
    return SimpleNamespace(updates=SimpleNamespace(check=check))


# ---------------------------------------------------------------------------
# Test case 1 — newer version produces notice + stamp
# ---------------------------------------------------------------------------

def test_newer_version_produces_notice_and_stamp(tmp_path: Path) -> None:
    """TC1: fake PyPI newer version → notice string produced + stamp file written."""
    from jarn.update_check import _do_update_check

    current = "0.5.0"
    newer = "0.8.1"
    call_count = [0]

    def fake_fetch() -> dict[str, Any]:
        call_count[0] += 1
        return _make_pypi_response(newer)

    notice = _do_update_check(
        home_dir=tmp_path,
        current_version=current,
        _fetch=fake_fetch,
    )

    assert notice is not None, "expected a notice for newer version"
    assert newer in notice, f"notice should mention {newer!r}: {notice!r}"
    assert call_count[0] == 1, "fetch should be called exactly once"

    stamp_path = tmp_path / "update-check.json"
    assert stamp_path.exists(), "stamp file must be written"
    stamp = json.loads(stamp_path.read_text())
    assert stamp["latest"] == newer
    assert "ts" in stamp
    assert isinstance(stamp["ts"], (int, float))


# ---------------------------------------------------------------------------
# Test case 2 — cache hit: no HTTP within 24 h
# ---------------------------------------------------------------------------

def test_second_call_within_24h_no_http(tmp_path: Path) -> None:
    """TC2: stamp < 24 h old → NO HTTP call at all."""
    from jarn.update_check import _do_update_check

    current = "0.5.0"
    newer = "0.8.1"

    # Seed a fresh stamp (< 1 second old)
    stamp_path = tmp_path / "update-check.json"
    stamp_path.write_text(json.dumps({"ts": time.time(), "latest": newer}))

    def must_not_be_called() -> dict[str, Any]:
        raise AssertionError("HTTP was called within the 24 h cache window!")

    notice = _do_update_check(
        home_dir=tmp_path,
        current_version=current,
        _fetch=must_not_be_called,
    )

    # Still returns the notice from cache
    assert notice is not None
    assert newer in notice


# ---------------------------------------------------------------------------
# Test case 3 — skip conditions: updates.check=false, offline preset, headless
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "cfg_check,preset_name,headless,description",
    [
        (False, None, False, "updates.check: false"),
        (True, "offline", False, "offline preset"),
        (True, None, True, "headless -p mode"),
    ],
)
def test_skip_conditions_no_http(
    cfg_check: bool,
    preset_name: str | None,
    headless: bool,
    description: str,
    tmp_path: Path,
) -> None:
    """TC3: each skip condition → maybe_start_update_check returns None (no thread, no HTTP)."""
    from jarn.update_check import maybe_start_update_check

    cfg = _make_config(check=cfg_check)
    console = MagicMock()

    # Patch _do_update_check to raise if called — proves no HTTP path reached
    with patch(
        "jarn.update_check._do_update_check",
        side_effect=AssertionError("update check must not run"),
    ):
        thread = maybe_start_update_check(
            cfg,
            console,
            preset_name=preset_name,
            headless=headless,
        )

    assert thread is None, f"{description}: expected None (skipped), got {thread!r}"


def test_demo_mode_skips_update_check(tmp_path: Path, monkeypatch) -> None:
    """F6: JARN_DEMO=1 → maybe_start_update_check returns None (no thread, no HTTP).

    A '⬆ v… available' line printed during a JARN_DEMO=1 session would contaminate
    the recorded demo GIF, so the check must be skipped when demo mode is active."""
    from jarn.update_check import maybe_start_update_check

    monkeypatch.setenv("JARN_DEMO", "1")
    cfg = _make_config(check=True)
    console = MagicMock()

    with patch(
        "jarn.update_check._do_update_check",
        side_effect=AssertionError("update check must not run in demo mode"),
    ):
        thread = maybe_start_update_check(cfg, console, preset_name=None, headless=False)

    assert thread is None, f"demo mode must skip the update check, got {thread!r}"


# ---------------------------------------------------------------------------
# Test case 4 — network failure is silent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("connection timed out"),
        OSError("network unreachable"),
        ValueError("bad JSON response"),
        KeyError("missing 'info' key"),
        RuntimeError("unexpected error"),
    ],
    ids=["timeout", "os-error", "bad-json", "bad-schema", "runtime"],
)
def test_network_failure_is_silent(exc: Exception, tmp_path: Path) -> None:
    """TC4: any network/parse failure → None returned, no exception propagates."""
    from jarn.update_check import _do_update_check

    def bad_fetch() -> dict[str, Any]:
        raise exc

    # Must not raise
    notice = _do_update_check(
        home_dir=tmp_path,
        current_version="0.5.0",
        _fetch=bad_fetch,
    )

    assert notice is None, f"expected None on error, got {notice!r}"


# ---------------------------------------------------------------------------
# Test case 5 — install command depends on frozen flag
# ---------------------------------------------------------------------------

def test_frozen_binary_uses_npm_command(tmp_path: Path) -> None:
    """TC5: sys.frozen set → notice says 'npm i -g jarn-cli'."""
    from jarn.update_check import _do_update_check

    notice = _do_update_check(
        home_dir=tmp_path,
        current_version="0.5.0",
        frozen=True,
        _fetch=lambda: _make_pypi_response("0.8.1"),
    )

    assert notice is not None
    assert "npm i -g jarn-cli" in notice, f"expected npm command in: {notice!r}"
    assert "pip install" not in notice


def test_non_frozen_uses_pip_command(tmp_path: Path) -> None:
    """TC5b: not frozen → notice says 'pip install -U jarn'."""
    from jarn.update_check import _do_update_check

    notice = _do_update_check(
        home_dir=tmp_path,
        current_version="0.5.0",
        frozen=False,
        _fetch=lambda: _make_pypi_response("0.8.1"),
    )

    assert notice is not None
    assert "pip install -U jarn" in notice, f"expected pip command in: {notice!r}"
    assert "npm" not in notice


# ---------------------------------------------------------------------------
# No-notice contract — same or older installed version → None (strict `>`)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "current,latest",
    [
        ("0.8.1", "0.5.0"),   # installed is NEWER than PyPI → no notice
        ("0.8.1", "0.8.1"),   # equal → no notice (strict greater-than)
    ],
    ids=["older-on-pypi", "equal"],
)
def test_same_or_older_version_no_notice(
    current: str, latest: str, tmp_path: Path
) -> None:
    """Same or older PyPI version → notice is None (pins the strict `>` compare)."""
    from jarn.update_check import _do_update_check

    notice = _do_update_check(
        home_dir=tmp_path,
        current_version=current,
        _fetch=lambda: _make_pypi_response(latest),
    )

    assert notice is None, f"expected no notice for {latest} vs {current}, got {notice!r}"
