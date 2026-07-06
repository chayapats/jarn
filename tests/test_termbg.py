"""Tests for termbg — OSC 11 terminal background detection."""

from __future__ import annotations

import io


def test_parse_osc11_white():
    from jarn.tui.termbg import parse_osc11

    rgb = parse_osc11("rgb:ffff/ffff/ffff")
    assert rgb == (0xFFFF, 0xFFFF, 0xFFFF)


def test_parse_osc11_black():
    from jarn.tui.termbg import parse_osc11

    rgb = parse_osc11("rgb:0000/0000/0000")
    assert rgb == (0, 0, 0)


def test_parse_osc11_8bit():
    from jarn.tui.termbg import parse_osc11

    # 8-bit form: ff/ff/ff
    rgb = parse_osc11("rgb:ff/ff/ff")
    assert rgb == (0xFF, 0xFF, 0xFF)


def test_parse_osc11_mixed_component_lengths():
    from jarn.tui.termbg import parse_osc11

    # some terminals report partial-hex e.g. rgb:1c1c/1e1e/2020
    rgb = parse_osc11("rgb:1c1c/1e1e/2020")
    assert rgb is not None
    assert len(rgb) == 3


def test_parse_osc11_malformed_returns_none():
    from jarn.tui.termbg import parse_osc11

    assert parse_osc11("") is None
    assert parse_osc11("bogus") is None
    assert parse_osc11("rgb:gg/ff/ff") is None
    assert parse_osc11("rgb:ffff/ffff") is None  # only 2 components


def test_parse_and_luminance_white_is_light():
    """White background → luminance ≥ threshold → light."""
    from jarn.tui.termbg import luminance, parse_osc11

    rgb = parse_osc11("rgb:ffff/ffff/ffff")
    assert rgb is not None
    lum = luminance(rgb)
    # white has relative luminance ~1.0
    assert lum > 0.5, f"expected light (lum={lum:.4f})"


def test_parse_and_luminance_black_is_dark():
    """Black background → luminance < threshold → dark."""
    from jarn.tui.termbg import luminance, parse_osc11

    rgb = parse_osc11("rgb:0000/0000/0000")
    assert rgb is not None
    lum = luminance(rgb)
    assert lum < 0.5, f"expected dark (lum={lum:.4f})"


def test_luminance_boundary_split():
    """Mid-grey sits near the boundary; function should handle without crashing."""
    from jarn.tui.termbg import luminance

    # 50% grey in 16-bit form
    mid = (0x8080, 0x8080, 0x8080)
    lum = luminance(mid)
    assert 0.0 <= lum <= 1.0


def test_detect_non_tty_returns_none_without_writing(monkeypatch):
    """detect() must return None (and write NO bytes) when stdin/stdout are not ttys."""
    from jarn.tui import termbg

    written: list[bytes] = []

    class _FakeStream(io.RawIOBase):
        def isatty(self) -> bool:
            return False

        def write(self, b: bytes) -> int:
            written.append(b)
            return len(b)

    fake = _FakeStream()
    monkeypatch.setattr(termbg, "_stdin_stream", lambda: fake)
    monkeypatch.setattr(termbg, "_stdout_stream", lambda: fake)

    result = termbg.detect(timeout=0.01)
    assert result is None, "detect() must return None on non-tty"
    assert written == [], "detect() must not write any bytes to a non-tty stream"
