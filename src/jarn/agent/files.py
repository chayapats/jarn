"""File-type helpers for multimodal support.

DeepAgents' ``read_file`` (v0.5+) auto-detects images, PDFs, audio, and video and
passes them to the model as native content blocks — no extra wiring is needed for
*reading*. These helpers let the UI behave sensibly around such files (e.g. not
attempting a text diff on a binary write) and own the core inline-image size/type
gate shared by every front-end (#54 / T-MEDIA-1).
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
DOC_EXTS = {".pdf"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".oga", ".flac"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi"}

MULTIMODAL_EXTS = IMAGE_EXTS | DOC_EXTS | AUDIO_EXTS | VIDEO_EXTS

#: Max size of an image inlined as a native content block (T-3-7 / #54).
#: Larger images must not be base64-inlined; documents use a separate staging path.
#: Aligned with the former TUI-only constant in :mod:`jarn.tui.completion`.
INLINE_IMAGE_MAX_BYTES: int = 5 * 1024 * 1024

#: Explicit image MIME allowlist for core ingest / inline encoding.
ALLOWED_IMAGE_MIMES: frozenset[str] = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/jpg",
        "image/gif",
        "image/webp",
        "image/bmp",
        "image/svg+xml",
        "image/x-icon",
        "image/vnd.microsoft.icon",
    }
)


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


def image_mime_allowed(mime: str) -> bool:
    """True when ``mime`` is an allowlisted image type (or ``image/*`` subtype)."""
    normalized = (mime or "").split(";", 1)[0].strip().lower()
    if not normalized:
        return False
    if normalized in ALLOWED_IMAGE_MIMES:
        return True
    # Permit well-formed image/* except clearly non-renderable odds.
    if normalized.startswith("image/") and normalized not in {
        "image/heic",
        "image/heif",
        "image/tiff",
    }:
        return True
    return False


def guess_image_mime(path: str | Path) -> str:
    """MIME type from ``path``'s extension, defaulting to ``application/octet-stream``."""
    return (
        mimetypes.guess_type("file" + Path(path).suffix)[0]
        or "application/octet-stream"
    )


def image_content_block(
    path: str | Path,
    *,
    max_bytes: int = INLINE_IMAGE_MAX_BYTES,
) -> dict[str, Any] | None:
    """Encode ``path`` as a langchain-core v1 image content block.

    Returns ``{"type": "image", "base64": <b64>, "mime_type": <mime>}`` — the same
    shape DeepAgents' ``read_file`` emits for image reads (see
    ``deepagents.middleware.filesystem``), so it reaches every provider that
    already accepts read-file images. The MIME type is derived from the file
    extension via :func:`mimetypes.guess_type`.

    Returns ``None`` (best-effort) when the file can't be read, exceeds
    ``max_bytes``, or has a disallowed MIME — so a single bad path never aborts a
    turn and the core size/type gate cannot be bypassed via raw ``images=`` paths.
    """
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError:
        return None
    if size > max_bytes:
        return None
    mime = guess_image_mime(p)
    if not image_mime_allowed(mime):
        return None
    try:
        raw = p.read_bytes()
    except OSError:
        return None
    return image_content_block_from_bytes(raw, mime, max_bytes=max_bytes)


def image_content_block_from_bytes(
    data: bytes,
    mime: str,
    *,
    max_bytes: int = INLINE_IMAGE_MAX_BYTES,
) -> dict[str, Any] | None:
    """Encode raw image ``data`` as a langchain-core v1 image content block.

    Applies the same size/type gate as :func:`image_content_block`. Returns
    ``None`` when oversize or the MIME is not allowlisted.
    """
    if len(data) > max_bytes:
        return None
    normalized = (mime or "").split(";", 1)[0].strip().lower()
    if not image_mime_allowed(normalized):
        return None
    encoded = base64.standard_b64encode(data).decode("ascii")
    return {"type": "image", "base64": encoded, "mime_type": normalized}
