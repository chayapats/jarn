"""F3: clipboard image paste (macOS) — save orchestration + grab helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from jarn.tui import clipboard
from jarn.tui.clipboard import grab_clipboard_image, save_clipboard_image

# -- save orchestration ------------------------------------------------------

def test_save_returns_path_on_success(tmp_path):
    def fake_grab(dest):
        dest.write_bytes(b"\x89PNG\r\n")
        return True

    with patch("jarn.tui.clipboard.grab_clipboard_image", side_effect=fake_grab):
        p = save_clipboard_image(tmp_path)
    assert p is not None and p.exists()
    assert p.parent == tmp_path / ".jarn" / "pastes"
    assert p.suffix == ".png"


def test_save_returns_none_and_leaves_no_file_on_failure(tmp_path):
    with patch("jarn.tui.clipboard.grab_clipboard_image", return_value=False):
        p = save_clipboard_image(tmp_path)
    assert p is None
    assert list((tmp_path / ".jarn" / "pastes").glob("paste-*.png")) == []


def test_index_increments(tmp_path):
    def fake_grab(dest):
        dest.write_bytes(b"x")
        return True

    with patch("jarn.tui.clipboard.grab_clipboard_image", side_effect=fake_grab):
        a = save_clipboard_image(tmp_path)
        b = save_clipboard_image(tmp_path)
    assert a.name == "paste-1.png"
    assert b.name == "paste-2.png"


# -- platform guard ----------------------------------------------------------

def test_non_darwin_returns_false(monkeypatch, tmp_path):
    """Linux without clipboard helpers still returns False."""
    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    monkeypatch.setattr(clipboard.shutil, "which", lambda _n: None)
    assert grab_clipboard_image(tmp_path / "x.png") is False


# -- Linux wl-paste ----------------------------------------------------------

def test_linux_wl_paste(monkeypatch, tmp_path):
    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    monkeypatch.setattr(
        clipboard.shutil,
        "which",
        lambda name: "/usr/bin/wl-paste" if name == "wl-paste" else None,
    )
    dest = tmp_path / "p.png"

    def fake_run(cmd, **kw):
        assert cmd[:2] == ["wl-paste", "-t"]
        return type("R", (), {"returncode": 0, "stdout": b"\x89PNG\r\n"})()

    monkeypatch.setattr(clipboard.subprocess, "run", fake_run)
    assert grab_clipboard_image(dest) is True
    assert dest.read_bytes() == b"\x89PNG\r\n"


# -- Windows PowerShell ------------------------------------------------------

def test_windows_powershell(monkeypatch, tmp_path):
    monkeypatch.setattr(clipboard.sys, "platform", "win32")
    dest = tmp_path / "p.png"

    def fake_run(cmd, **kw):
        assert cmd[0] == "powershell"
        dest.write_bytes(b"\x89PNG\r\n")
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(clipboard.subprocess, "run", fake_run)
    assert grab_clipboard_image(dest) is True
    assert dest.read_bytes() == b"\x89PNG\r\n"


# -- macOS JPEG fallback -----------------------------------------------------

def test_macos_jpeg_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(clipboard.sys, "platform", "darwin")
    monkeypatch.setattr(clipboard.shutil, "which", lambda _n: None)
    dest = tmp_path / "p.png"
    calls: list[str] = []

    def fake_run(cmd, **kw):
        script = cmd[-1]
        calls.append(script)
        if "JPEG" in script:
            dest.write_bytes(b"JPEGDATA")
            return type("R", (), {"returncode": 0, "stdout": "ok\n"})()
        return type("R", (), {"returncode": 0, "stdout": "no-image\n"})()

    monkeypatch.setattr(clipboard.subprocess, "run", fake_run)
    assert grab_clipboard_image(dest) is True
    assert dest.read_bytes() == b"JPEGDATA"
    assert len(calls) == 3  # PNG, TIFF, then JPEG


# -- size cap ----------------------------------------------------------------

def test_size_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    monkeypatch.setattr(
        clipboard.shutil,
        "which",
        lambda name: "/usr/bin/wl-paste" if name == "wl-paste" else None,
    )
    dest = tmp_path / "p.png"
    oversized = b"x" * (clipboard._MAX_IMAGE_BYTES + 1)

    def fake_run(cmd, **kw):
        return type("R", (), {"returncode": 0, "stdout": oversized})()

    monkeypatch.setattr(clipboard.subprocess, "run", fake_run)
    assert grab_clipboard_image(dest) is False
    assert not dest.exists()
    assert "10 MB" in (clipboard.grab_error_message() or "")


# -- pngpaste helper ---------------------------------------------------------

def test_pngpaste_success(monkeypatch, tmp_path):
    monkeypatch.setattr(clipboard.sys, "platform", "darwin")
    monkeypatch.setattr(clipboard.shutil, "which", lambda _n: "/usr/local/bin/pngpaste")
    dest = tmp_path / "p.png"

    def fake_run(cmd, **kw):
        Path(cmd[1]).write_bytes(b"PNGDATA")
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(clipboard.subprocess, "run", fake_run)
    assert grab_clipboard_image(dest) is True
    assert dest.read_bytes() == b"PNGDATA"


def test_pngpaste_no_image(monkeypatch, tmp_path):
    monkeypatch.setattr(clipboard.sys, "platform", "darwin")
    monkeypatch.setattr(clipboard.shutil, "which", lambda _n: "/usr/local/bin/pngpaste")
    dest = tmp_path / "p.png"
    # pngpaste exits non-zero and writes nothing when the clipboard has no image.
    monkeypatch.setattr(
        clipboard.subprocess, "run",
        lambda *a, **k: type("R", (), {"returncode": 1})(),
    )
    assert grab_clipboard_image(dest) is False


# -- osascript fallback ------------------------------------------------------

def test_osascript_success(monkeypatch, tmp_path):
    monkeypatch.setattr(clipboard.sys, "platform", "darwin")
    monkeypatch.setattr(clipboard.shutil, "which", lambda _n: None)  # no pngpaste
    dest = tmp_path / "p.png"

    def fake_run(cmd, **kw):
        dest.write_bytes(b"PNG")
        return type("R", (), {"returncode": 0, "stdout": "ok\n"})()

    monkeypatch.setattr(clipboard.subprocess, "run", fake_run)
    assert grab_clipboard_image(dest) is True


def test_osascript_no_image(monkeypatch, tmp_path):
    monkeypatch.setattr(clipboard.sys, "platform", "darwin")
    monkeypatch.setattr(clipboard.shutil, "which", lambda _n: None)
    monkeypatch.setattr(
        clipboard.subprocess, "run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "no-image\n"})(),
    )
    assert grab_clipboard_image(tmp_path / "p.png") is False
