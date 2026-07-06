"""File-type helpers for multimodal support.

DeepAgents' ``read_file`` (v0.5+) auto-detects images, PDFs, audio, and video and
passes them to the model as native content blocks — no extra wiring is needed for
*reading*. These helpers let the UI behave sensibly around such files (e.g. not
attempting a text diff on a binary write).
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

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


def image_content_block(path: str | Path) -> dict[str, Any] | None:
    """Encode ``path`` as a langchain-core v1 image content block.

    Returns ``{"type": "image", "base64": <b64>, "mime_type": <mime>}`` — the same
    shape DeepAgents' ``read_file`` emits for image reads (see
    ``deepagents.middleware.filesystem``), so it reaches every provider that
    already accepts read-file images. The MIME type is derived from the file
    extension via :func:`mimetypes.guess_type`. Returns ``None`` (best-effort) when
    the file can't be read, so a single bad path never aborts a turn.
    """
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    encoded = base64.standard_b64encode(raw).decode("ascii")
    mime = (
        mimetypes.guess_type("file" + Path(path).suffix)[0]
        or "application/octet-stream"
    )
    return {"type": "image", "base64": encoded, "mime_type": mime}
