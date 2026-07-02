"""Read an image from the system clipboard and save it to a file.

A pasted screenshot can't travel through the terminal's text paste, so instead we
let the user press a key, grab the clipboard image here, write it under
``<project>/.jarn/pastes/``, and hand the path back so the REPL can insert it as
an ``@path`` reference — which the agent's multimodal ``read_file`` then loads.

Platform support (best-effort; never raises):

* **macOS** — ``pngpaste`` when installed, else AppleScript (PNG, then TIFF/JPEG).
* **Linux** — ``wl-paste`` (Wayland) or ``xclip`` (X11), PNG from the clipboard.
* **Windows** — PowerShell ``System.Windows.Forms.Clipboard.GetImage()`` saved as PNG.

Images larger than :data:`_MAX_IMAGE_BYTES` are rejected (returns ``False``; the
caller shows a message). Unsupported platforms or empty clipboards also return
``False``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

_MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB
_MAX_IMAGE_MB = _MAX_IMAGE_BYTES // (1024 * 1024)

#: Set on the last failed :func:`grab_clipboard_image` / :func:`save_clipboard_image`
#: attempt when a user-facing reason is known (e.g. size cap).
_last_grab_error: str | None = None


def grab_error_message() -> str | None:
    """User-facing reason for the last clipboard grab failure, if any."""
    return _last_grab_error

#: AppleScript templates — ``{path}`` is substituted with the destination path.
_OSASCRIPT_PNG = (
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
_OSASCRIPT_TIFF = (
    'set theFile to (POSIX file "{path}")\n'
    "try\n"
    "    set imgData to (the clipboard as «class TIFF»)\n"
    "on error\n"
    '    return "no-image"\n'
    "end try\n"
    "set fh to open for access theFile with write permission\n"
    "set eof fh to 0\n"
    "write imgData to fh\n"
    "close access fh\n"
    'return "ok"\n'
)
_OSASCRIPT_JPEG = (
    'set theFile to (POSIX file "{path}")\n'
    "try\n"
    "    set imgData to (the clipboard as JPEG)\n"
    "on error\n"
    '    return "no-image"\n'
    "end try\n"
    "set fh to open for access theFile with write permission\n"
    "set eof fh to 0\n"
    "write imgData to fh\n"
    "close access fh\n"
    'return "ok"\n'
)


def _ok(dest: Path) -> bool:
    return dest.exists() and dest.stat().st_size > 0


def _within_size_limit(dest: Path) -> bool:
    """Return True when ``dest`` exists, is non-empty, and within the size cap."""
    global _last_grab_error
    if not _ok(dest):
        return False
    if dest.stat().st_size > _MAX_IMAGE_BYTES:
        dest.unlink(missing_ok=True)
        _last_grab_error = (
            f"clipboard image exceeds {_MAX_IMAGE_MB} MB — save the file and use @path"
        )
        return False
    return True


def _grab_via_pngpaste(dest: Path) -> bool:
    try:
        proc = subprocess.run(
            ["pngpaste", str(dest)], capture_output=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and _within_size_limit(dest)


def _grab_via_osascript(dest: Path, script: str) -> bool:
    try:
        proc = subprocess.run(
            ["osascript", "-e", script.format(path=dest)],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and "ok" in (proc.stdout or "") and _within_size_limit(dest)


def _grab_via_wl_paste(dest: Path) -> bool:
    try:
        proc = subprocess.run(
            ["wl-paste", "-t", "image/png"],
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0 or not proc.stdout:
        return False
    dest.write_bytes(proc.stdout)
    return _within_size_limit(dest)


def _grab_via_xclip(dest: Path) -> bool:
    try:
        proc = subprocess.run(
            ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0 or not proc.stdout:
        return False
    dest.write_bytes(proc.stdout)
    return _within_size_limit(dest)


def _grab_windows(dest: Path) -> bool:
    path_str = str(dest).replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "Add-Type -AssemblyName System.Drawing; "
        f"$img = [System.Windows.Forms.Clipboard]::GetImage(); "
        "if ($null -eq $img) { exit 1 }; "
        f"$img.Save('{path_str}', [System.Drawing.Imaging.ImageFormat]::Png); "
        "exit 0"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and _within_size_limit(dest)


def _grab_darwin(dest: Path) -> bool:
    if shutil.which("pngpaste") and _grab_via_pngpaste(dest):
        return True
    if dest.exists():
        dest.unlink(missing_ok=True)
    for script in (_OSASCRIPT_PNG, _OSASCRIPT_TIFF, _OSASCRIPT_JPEG):
        if _grab_via_osascript(dest, script):
            return True
        if dest.exists():
            dest.unlink(missing_ok=True)
    return False


def _grab_linux(dest: Path) -> bool:
    if shutil.which("wl-paste") and _grab_via_wl_paste(dest):
        return True
    if dest.exists():
        dest.unlink(missing_ok=True)
    if shutil.which("xclip"):
        return _grab_via_xclip(dest)
    return False


def grab_clipboard_image(dest: Path) -> bool:
    """Write the clipboard image to ``dest`` (PNG). Return True on success.

    Returns ``False`` when the platform is unsupported, no image is on the
    clipboard, no helper is available, or the image exceeds the size cap.
    On failure, :func:`grab_error_message` may carry a user-facing reason.
    """
    global _last_grab_error
    _last_grab_error = None
    if sys.platform == "darwin":
        return _grab_darwin(dest)
    if sys.platform == "win32":
        return _grab_windows(dest)
    if sys.platform.startswith("linux"):
        return _grab_linux(dest)
    return False


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
