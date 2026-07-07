"""Turn-end and approval-needed notifications (bell / desktop).

Emits a terminal BEL and/or a native desktop notification when a long agent
turn finishes or an approval prompt is about to render.

Privacy: desktop notification bodies use fixed strings only — no user prompt
content is ever included.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
from collections.abc import Callable
from subprocess import DEVNULL
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from jarn.config.schema import UIConfig


def _flush(write: Callable[[str], Any]) -> None:
    """Flush the stream backing *write* so a newline-free control sequence isn't
    left buffered.

    Under prompt_toolkit's ``patch_stdout(raw=True)`` the ``StdoutProxy`` holds
    writes that contain no ``\\n`` until a later flush — so a turn-end BEL or an
    OSC-2 title (both newline-free) would otherwise sit in the buffer and never
    reach the terminal.  The backing stream is the ``write`` callable's bound
    ``__self__`` (``console.file`` in production); a bare callable with no
    ``__self__`` degrades to a no-op.
    """
    stream = getattr(write, "__self__", None)
    getattr(stream, "flush", lambda: None)()


def set_title(
    text: str,
    *,
    settings: UIConfig,
    write: Callable[[str], Any],
    isatty: Callable[[], bool],
) -> None:
    """Emit an OSC 2 terminal-title sequence.

    Writes ``\\x1b]2;{text}\\x07`` (XTerm title-set, BEL-terminated) when
    ``settings.terminal_title`` is True **and** ``isatty()`` returns True.
    Both guards make the call a no-op in headless / piped contexts.

    Parameters
    ----------
    text:
        The title string to show in the terminal tab.
    settings:
        The ``UIConfig`` object carrying the ``terminal_title`` toggle.
    write:
        Callable that writes a string to the active output stream (typically
        ``console.file.write``).
    isatty:
        Callable returning whether the output stream is a real TTY.
    """
    if not settings.terminal_title:
        return
    if not isatty():
        return
    write(f"\x1b]2;{text}\x07")
    _flush(write)


def notify(
    event: Literal["turn_done", "needs_approval"],
    settings: UIConfig,
    *,
    elapsed: float,
    write: Callable[[str], Any],
) -> None:
    """Emit a bell and/or desktop notification for *event*.

    Parameters
    ----------
    event:
        ``"turn_done"`` — fires only when *elapsed* >= ``settings.notify_min_secs``.
        ``"needs_approval"`` — fires regardless of elapsed time.
    settings:
        The ``UIConfig`` object carrying ``notify`` (mode) and ``notify_min_secs``.
    elapsed:
        Seconds since the turn started (only meaningful for ``"turn_done"``).
    write:
        Callable that writes a string to the active output (typically
        ``console.file.write``).  Receives the raw ``"\a"`` BEL character so it
        reaches the same stream captured by the test harness.
    """
    mode = settings.notify  # "off" | "bell" | "desktop" | "both"

    if mode == "off":
        return

    # Turn-done notifications are throttled by the minimum-seconds threshold.
    if event == "turn_done" and elapsed < settings.notify_min_secs:
        return

    if mode in ("bell", "both"):
        write("\a")
        _flush(write)

    if mode in ("desktop", "both"):
        _desktop_notify(event, elapsed=elapsed)


def _desktop_notify(
    event: Literal["turn_done", "needs_approval"],
    *,
    elapsed: float,
) -> None:
    """Fire a fire-and-forget OS notification (non-blocking).

    macOS: ``osascript -e 'display notification …'``
    Linux: ``notify-send jarn "…"``

    Both are ``shutil.which``-guarded so a missing binary is silently skipped.
    The Popen is never waited on — the event loop stays responsive.
    Popen calls are wrapped in try/except to ensure failures never propagate.
    """
    import platform

    if event == "turn_done":
        elapsed_s = int(elapsed)
        body = f"turn finished ({elapsed_s}s)"
    else:
        body = "approval needed"

    title = "jarn"
    system = platform.system()

    if system == "Darwin":
        if shutil.which("osascript"):
            _safe_escaped = body.replace('"', '\\"')
            with contextlib.suppress(Exception):  # noqa: BLE001
                subprocess.Popen(  # noqa: S603
                    [
                        "osascript",
                        "-e",
                        f'display notification "{_safe_escaped}" with title "{title}"',
                    ],
                    stdout=DEVNULL,
                    stderr=DEVNULL,
                )
    else:
        if shutil.which("notify-send"):
            with contextlib.suppress(Exception):  # noqa: BLE001
                subprocess.Popen(  # noqa: S603
                    ["notify-send", "--expire-time", "2000", title, body],
                    stdout=DEVNULL,
                    stderr=DEVNULL,
                )
