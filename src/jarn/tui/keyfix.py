"""Terminal key-handling fix for the macOS Caps Lock language-switch bug.

Textual (>=8) enables the Kitty keyboard protocol with the ``REPORT_ALL_KEYS``
and ``REPORT_ASSOCIATED_TEXT`` flags. On macOS, when Caps Lock is configured to
switch input source, those flags make the terminal report the Caps Lock press as
a stray ``a`` keystroke — which then lands in the input. Other TUIs don't hit
this because they don't request "report all keys".

The fix drops just those two flags (keeping ``DISAMBIGUATE_ESCAPE_CODES`` so
Shift+Enter, Ctrl+I/Tab, etc. still disambiguate). We patch the driver module's
flag globals before the driver starts; the driver reads them at call time, so
setting them to 0 yields ``flag = DISAMBIGUATE`` only.

Opt out with ``JARN_KEEP_KITTY_ALL_KEYS=1`` if you rely on full key reporting.
"""

from __future__ import annotations

import os

_DROP_FLAGS = ("KITTY_REPORT_ALL_KEYS", "KITTY_REPORT_ASSOCIATED_TEXT")


def apply_kitty_keyfix() -> bool:
    """Disable the kitty flags that cause the Caps Lock stray-char bug.

    Returns True if the patch was applied. Safe to call multiple times and on
    platforms/Textual versions where the symbols don't exist (no-op).
    """
    if os.environ.get("JARN_KEEP_KITTY_ALL_KEYS"):
        return False
    patched = False
    try:
        import textual.drivers.linux_driver as linux_driver
    except Exception:  # noqa: BLE001 - non-posix or layout change: nothing to do
        return False
    for name in _DROP_FLAGS:
        if hasattr(linux_driver, name):
            setattr(linux_driver, name, 0)
            patched = True
    return patched
