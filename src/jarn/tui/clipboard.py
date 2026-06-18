"""Read an image from the system clipboard and save it to a file.

A pasted screenshot can't travel through the terminal's text paste, so instead we
let the user press a key, grab the clipboard image here, write it under
``<project>/.jarn/pastes/``, and hand the path back so the REPL can insert it as
an ``@path`` reference — which the agent's multimodal ``read_file`` then loads.

macOS only for now (``pngpaste`` if installed, else an AppleScript fallback);
everywhere else this returns ``None`` and the caller tells the user to save the
file and ``@``-reference it. Both helpers are best-effort and never raise.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

#: AppleScript that writes the clipboard's PNG representation to a file, or prints
#: ``no-image`` if the clipboard holds no image. ``{path}`` is substituted.
_OSASCRIPT = (
    'set theFile to (POSIX file "{path}")\n'
    "try\n"
    "    set pngData to (the clipboard as «class PNGf»)\n"
    "on error\n"
    '    return "no-image"\n'
    "end try\n"
    "set fh to open for access theFile with write permission\n"
    "set eof fh to 0\n"
    "write pngData to fh\n"
    "close access fh\n"
    'return "ok"\n'
)


def _ok(dest: Path) -> bool:
    return dest.exists() and dest.stat().st_size > 0


def _grab_via_pngpaste(dest: Path) -> bool:
    try:
        proc = subprocess.run(
            ["pngpaste", str(dest)], capture_output=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and _ok(dest)


def _grab_via_osascript(dest: Path) -> bool:
    try:
        proc = subprocess.run(
            ["osascript", "-e", _OSASCRIPT.format(path=dest)],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and "ok" in (proc.stdout or "") and _ok(dest)


def grab_clipboard_image(dest: Path) -> bool:
    """Write the clipboard image to ``dest`` (PNG). Return True on success.

    macOS only; returns False when not on macOS, no image is on the clipboard,
    or no helper is available.
    """
    if sys.platform != "darwin":
        return False
    if shutil.which("pngpaste"):
        return _grab_via_pngpaste(dest)
    return _grab_via_osascript(dest)


def _next_index(dest_dir: Path) -> int:
    n = 0
    for p in dest_dir.glob("paste-*.png"):
        try:
            n = max(n, int(p.stem.split("-", 1)[1]))
        except (ValueError, IndexError):
            continue
    return n + 1


def save_clipboard_image(project_root: Path) -> Path | None:
    """Grab a clipboard image into ``<root>/.jarn/pastes/paste-N.png``.

    Returns the saved path, or ``None`` when there's no image to grab (or not on
    a supported platform). Leaves no empty file behind on failure.
    """
    dest_dir = project_root / ".jarn" / "pastes"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"paste-{_next_index(dest_dir)}.png"
    if grab_clipboard_image(dest):
        return dest
    if dest.exists() and dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
    return None
