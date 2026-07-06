"""Terminal background auto-detection via OSC 11.

Queries the terminal for its background colour (``\\x1b]11;?\\x07``), parses
the ``rgb:R/G/B`` reply, computes relative luminance, and classifies the
background as "light" or "dark".

Safe guards:
- Only attempts detection when BOTH stdin and stdout are ttys.
- Hard-deadline 100 ms timeout — the terminal is assumed unresponsive after
  that, and ``None`` is returned (caller falls back to dark).
- Only runs BEFORE prompt_toolkit's Application owns the tty (the caller must
  invoke it at the right point in the bootstrap sequence).
- Any failure (import error, OS error, parse error) returns ``None``.

Public API
----------
``parse_osc11(reply)``   — parse raw ``rgb:…`` component string → ``(R, G, B)``
                           in the terminal's native bit-depth, or ``None``.
``luminance(rgb)``       — relative luminance (0..1) from a raw RGB tuple.
``detect(timeout=0.1)``  — full probe → ``"light"`` | ``"dark"`` | ``None``.
"""

from __future__ import annotations

import re
import sys
from typing import Literal

# Threshold: relative luminance above this → light background.
# W3C contrast formula uses 0.179 as the mid-point between black (0) and
# white (1).  We use a slightly more generous 0.3 so moderately light grey
# terminals are classified light rather than borderline-dark.
_LUMINANCE_THRESHOLD = 0.3

# OSC 11 probe/reply patterns
_OSC_QUERY = "\x1b]11;?\x07"
# Accept both 4-digit (16-bit) and 2-digit (8-bit) hex components, with an
# optional ESC-backslash terminator that some terminals emit instead of BEL.
_OSC11_RE = re.compile(
    r"rgb:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})"
)


# ── injectable stream accessors (monkeypatched in tests) ─────────────────────

def _stdin_stream():  # pragma: no cover
    return sys.stdin


def _stdout_stream():  # pragma: no cover
    return sys.stdout


# ── public API ────────────────────────────────────────────────────────────────

def parse_osc11(reply: str) -> tuple[int, int, int] | None:
    """Parse a raw ``rgb:RRRR/GGGG/BBBB`` reply string.

    Returns a ``(R, G, B)`` tuple in the terminal's native bit-depth
    (typically 16-bit components in range 0..65535) or ``None`` on parse
    failure.  Both 4-digit (16-bit) and 2-digit (8-bit) hex forms are
    accepted; components must all have the same length (the regex allows
    mixed lengths, but terminals always send uniform widths).
    """
    m = _OSC11_RE.search(reply)
    if m is None:
        return None
    try:
        r = int(m.group(1), 16)
        g = int(m.group(2), 16)
        b = int(m.group(3), 16)
    except ValueError:
        return None
    return r, g, b


def luminance(rgb: tuple[int, int, int]) -> float:
    """Compute the relative luminance (0.0 .. 1.0) of an RGB triple.

    The triple is assumed to be in the terminal's native bit-depth
    (16-bit: range 0..65535, or 8-bit: range 0..255).  The function
    normalises by the maximum value seen across the three components so it
    handles both depths without the caller having to specify which.

    Uses the IEC 61966-2-1 sRGB linearisation + the W3C relative-luminance
    formula (WCAG 2.x).
    """
    if max(rgb) == 0:
        return 0.0
    # Determine bit-depth by the maximum component value.
    scale = 65535.0 if max(rgb) > 255 else 255.0
    r_lin, g_lin, b_lin = (_linearise(c / scale) for c in rgb)
    return 0.2126 * r_lin + 0.7152 * g_lin + 0.0722 * b_lin


def _linearise(c: float) -> float:
    """sRGB → linear light."""
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def detect(timeout: float = 0.1) -> Literal["light", "dark"] | None:
    """Probe the terminal background and return ``"light"`` or ``"dark"``.

    Returns ``None`` (without writing any bytes) when:
    - stdin or stdout is not a tty (pipes, headless, CI).
    - the terminal does not reply within ``timeout`` seconds.
    - any OS or parse error occurs.

    This function MUST be called before prompt_toolkit's Application
    starts owning the tty.
    """
    stdin = _stdin_stream()
    stdout = _stdout_stream()

    try:
        if not stdin.isatty() or not stdout.isatty():
            return None
    except Exception:  # noqa: BLE001
        return None

    try:
        return _probe(stdin, stdout, timeout)
    except Exception:  # noqa: BLE001
        return None


def _probe(stdin, stdout, timeout: float) -> Literal["light", "dark"] | None:
    """Internal: write the OSC 11 query and read the reply in raw mode."""
    import os
    import select
    import termios
    import tty

    # We need the raw file descriptor for termios/select.
    try:
        fd = stdin.fileno()
    except Exception:  # noqa: BLE001
        return None

    # Save terminal state so we can restore it even on exception.
    try:
        old_attrs = termios.tcgetattr(fd)
    except termios.error:
        return None

    reply_buf = ""
    try:
        tty.setraw(fd)
        # Write the OSC 11 query to stdout.
        try:
            stdout.write(_OSC_QUERY)
            stdout.flush()
        except Exception:  # noqa: BLE001
            return None

        # Read bytes until we see a terminator or the deadline expires.
        deadline = timeout
        while deadline > 0:
            ready, _, _ = select.select([fd], [], [], min(deadline, 0.05))
            if not ready:
                deadline -= 0.05
                # If we've accumulated something that looks like a partial
                # reply, keep waiting briefly; otherwise give up.
                if not reply_buf:
                    break
                continue
            try:
                chunk = os.read(fd, 256).decode("latin-1", errors="replace")
            except OSError:
                break
            reply_buf += chunk
            deadline -= 0.05
            # OSC 11 reply ends with BEL (\x07) or ST (ESC \)
            if "\x07" in reply_buf or "\x1b\\" in reply_buf:
                break
    finally:
        # Unconditionally restore the terminal.
        import contextlib
        with contextlib.suppress(Exception):
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)

    if not reply_buf:
        return None

    rgb = parse_osc11(reply_buf)
    if rgb is None:
        return None

    lum = luminance(rgb)
    return "light" if lum >= _LUMINANCE_THRESHOLD else "dark"
