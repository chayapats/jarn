"""REPL clipboard image paste (Ctrl+V)."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from jarn.repl.app import InlineApp


@pytest.mark.asyncio
async def test_paste_clipboard_image_attaches_path(tmp_path, base_config):
    """Ctrl+V saves the clipboard image and inserts an @path reference."""
    pasted = tmp_path / ".jarn" / "pastes" / "paste-1.png"
    pasted.parent.mkdir(parents=True, exist_ok=True)
    pasted.write_bytes(b"\x89PNG\r\n")

    app = InlineApp(base_config, tmp_path)
    with patch("jarn.tui.clipboard.save_clipboard_image", return_value=pasted):
        app._paste_clipboard_image()
        await asyncio.sleep(0.05)

    assert app.input.text.strip() == "@.jarn/pastes/paste-1.png"
    app.controller.close()


@pytest.mark.asyncio
async def test_paste_clipboard_image_shows_size_error(tmp_path, base_config):
    """Oversized clipboard images surface the size-cap message in the stream."""
    app = InlineApp(base_config, tmp_path)
    with (
        patch("jarn.tui.clipboard.save_clipboard_image", return_value=None),
        patch(
            "jarn.tui.clipboard.grab_error_message",
            return_value="clipboard image exceeds 10 MB -- save the file and use @path",
        ),
    ):
        app._paste_clipboard_image()
        await asyncio.sleep(0.05)

    assert "10 MB" in app._stream_text
    app.controller.close()


@pytest.mark.asyncio
async def test_placeholder_format(tmp_path, base_config):
    """Bracketed paste creates '[Pasted text #N +L lines]' token (Claude Code parity)."""
    app = InlineApp(base_config, tmp_path)

    # Find the _paste key-handler defined inside _build_keys()
    paste_handler = next(
        (b.handler for b in app._kb.bindings
         if getattr(b.handler, "__name__", "") == "_paste"),
        None,
    )
    assert paste_handler is not None, "_paste binding not found in app._kb"

    # Fake bracketed-paste event with 12 lines (triggers the collapse path)
    content = "\n".join(f"line {i}" for i in range(1, 13))  # 12 lines

    class _FakePasteEvent:
        data = content

    paste_handler(_FakePasteEvent())

    # The token written into the input buffer is the collapse placeholder
    token = app.input.text
    assert token.startswith("[Pasted text #"), (
        f"expected new format '[Pasted text #...]', got {token!r}"
    )
    assert "+12 lines" in token, f"line count in wrong format: {token!r}"
    # Old format was '[Pasted #N: L lines]'; new must not contain ': '
    assert ": " not in token, f"old-style colon format still present: {token!r}"

    # Round-trip: expansion must restore the original content
    expanded = app._expand_pastes(token)
    assert expanded == content, "expand_pastes did not restore original content"

    app.controller.close()
