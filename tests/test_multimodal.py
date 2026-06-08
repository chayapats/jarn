"""Multimodal file helpers + binary-aware diff."""

from __future__ import annotations

from jarn.agent.files import is_multimodal_path, modality_of
from jarn.tui.widgets.diff import diff_from_edit_args


def test_modality_detection():
    assert modality_of("a.png") == "image"
    assert modality_of("doc.pdf") == "document"
    assert modality_of("clip.mp4") == "video"
    assert modality_of("sound.mp3") == "audio"
    assert modality_of("main.py") == "text"


def test_is_multimodal():
    assert is_multimodal_path("photo.JPG")
    assert not is_multimodal_path("script.py")


def test_diff_skips_binary():
    diff = diff_from_edit_args({"file_path": "logo.png", "content": "<binary>"})
    assert "binary" in diff.plain


def test_diff_text_for_code():
    diff = diff_from_edit_args({"file_path": "a.py", "old_string": "x=1", "new_string": "x=2"})
    assert "x=1" in diff.plain and "x=2" in diff.plain
