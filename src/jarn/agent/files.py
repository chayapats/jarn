"""File-type helpers for multimodal support.

DeepAgents' ``read_file`` (v0.5+) auto-detects images, PDFs, audio, and video and
passes them to the model as native content blocks — no extra wiring is needed for
*reading*. These helpers let the UI behave sensibly around such files (e.g. not
attempting a text diff on a binary write).
"""

from __future__ import annotations

from pathlib import Path

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
DOC_EXTS = {".pdf"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi"}

MULTIMODAL_EXTS = IMAGE_EXTS | DOC_EXTS | AUDIO_EXTS | VIDEO_EXTS


def is_multimodal_path(path: str | Path) -> bool:
    """True if the path looks like a non-text, model-renderable file."""
    return Path(path).suffix.lower() in MULTIMODAL_EXTS


def modality_of(path: str | Path) -> str:
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in DOC_EXTS:
        return "document"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in VIDEO_EXTS:
        return "video"
    return "text"
