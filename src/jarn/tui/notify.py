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
