"""Caps Lock keyfix: drop kitty report-all-keys flags, keep disambiguation."""

from __future__ import annotations

import io

import textual.drivers.linux_driver as ld

from jarn.tui.keyfix import apply_kitty_keyfix, apply_repl_keyfix


def test_keyfix_drops_report_all_keys(monkeypatch):
    monkeypatch.setattr(ld, "KITTY_REPORT_ALL_KEYS", 0b1000, raising=False)
    monkeypatch.setattr(ld, "KITTY_REPORT_ASSOCIATED_TEXT", 0b10000, raising=False)
    assert apply_kitty_keyfix() is True
    assert ld.KITTY_REPORT_ALL_KEYS == 0
    assert ld.KITTY_REPORT_ASSOCIATED_TEXT == 0
    # Disambiguate stays on so Shift+Enter / Ctrl+I still work.
    assert ld.KITTY_DISAMBIGUATE_ESCAPE_CODES == 0b1
    # The flag the driver would now send is DISAMBIGUATE only.
    flag = (
        ld.KITTY_DISAMBIGUATE_ESCAPE_CODES
        | ld.KITTY_REPORT_ALL_KEYS
        | ld.KITTY_REPORT_ASSOCIATED_TEXT
    )
    assert flag == 0b1


def test_keyfix_opt_out(monkeypatch):
    monkeypatch.setenv("JARN_KEEP_KITTY_ALL_KEYS", "1")
    assert apply_kitty_keyfix() is False


def test_repl_keyfix_pops_kitty_stack(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr("sys.stderr", buf)
    assert apply_repl_keyfix() is True
    assert buf.getvalue() == "\x1b[<u"


def test_repl_keyfix_opt_out(monkeypatch):
    monkeypatch.setenv("JARN_KEEP_KITTY_ALL_KEYS", "1")
    assert apply_repl_keyfix() is False
